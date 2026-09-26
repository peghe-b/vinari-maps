#!/usr/bin/env python3
"""Reference search over georgia_geocoder.sqlite (stdlib only).

This is the behaviour the app's search should copy (Swift and Kotlin), and
what the CI gate runs its known queries through. It reads the fold and the
ranking numbers from the database's own meta table, so a database always
searches with the rules it was built with.

  1. Fold the query (geocoder_fold.parse_query): every required word is a
     prefix search on its stem (and its extra forms: genitive, plural, the
     word without a postposition), all words must match (AND), chat-Latin
     readings are alternatives (OR). Street-type words are optional; noise
     words, apartment numbers and postcodes are dropped; a letter with a dot
     ('ი.') is an initial that only helps ranking.
  2. Category words ('parking', 'ბენზინი', 'АЗС', 'metani') list the nearest
     POIs of that kind. The other words of the query ('ბათუმის აეროპორტი',
     'parking vake') must name a settlement or district as a whole (a street
     only when no place fits); the POIs are then listed around it. When they
     name nothing, the POIs of that kind compete on their own names
     ('Kutaisi International Airport').
  3. Each table is searched through its full-text index in rank order (row
     id = importance rank), once over all of Georgia and once in a box
     around the user; a word that names a settlement ('rustaveli batumi')
     also restricts streets, addresses and POIs to that settlement.
     Numbers are house numbers: required for addresses, optional elsewhere.
  4. Every candidate is scored: text match (exact > same stem > prefix,
     times how much of the name the query covered), importance, nearness to
     the user, and for streets and addresses the road class; see
     config/geocoder.json 'ranking'.
  5. When nothing matches every word, the search runs once more without the
     rarest word, and marks those results partial.

Occupied places: a place inside no_go_hard is returned with occupied=1,
routable=false and a reason ('occupied', or 'occupation_line' for the 100 m
band just outside the drawn line), so the app can explain; the app must
never route to it. A query that names an occupied place beside other words
('rustaveli sokhumi', 'სოხუმის აეროპორტი') returns that place first, with
anchor='occupied'. Show label_ka / label_en, never name, for an occupied
place: name is what the de facto authorities call it.

Usage:
  python3 scripts/geocoder_search.py build/geocoder/georgia_geocoder.sqlite "rustaveli 12" [--near 41.69,44.80]
"""

import argparse
import itertools
import json
import math
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from geocoder_fold import Fold, Query  # noqa: E402

TABLES = ("places", "pois", "streets", "addresses")
MAX_FTS4_COMBINATIONS = 32
SETTLEMENT_KINDS = ("city", "town", "village", "hamlet")
DISTRICT_KINDS = ("suburb", "quarter", "neighbourhood")
ANCHOR_KINDS = SETTLEMENT_KINDS + DISTRICT_KINDS
SPECIFIC_FIRST = ("neighbourhood", "quarter", "suburb", "hamlet", "village", "town", "city")
REASONS = {"occupied": "occupied", "buffer": "occupation_line"}
RANKING_DEFAULTS = {"occupied_factor": 1.0, "street_class_prior": {}, "lane_words": [], "lane_factor": 1.0,
                    "initial_bonus": 0.0, "type_word_missing": 1.0, "type_word_conflict": 1.0,
                    "anchor_street_min_text": 0.9, "anchor_same_place_km": 10.0, "city_reach_factor": 1.5,
                    "city_reach_min_km": 3.0}


def distance_km(lat1, lon1, lat2, lon2):
    p1, p2 = math.radians(lat1), math.radians(lat2)
    a = (math.sin((p2 - p1) / 2) ** 2
         + math.cos(p1) * math.cos(p2) * math.sin(math.radians(lon2 - lon1) / 2) ** 2)
    return 2 * 6371.0088 * math.asin(min(1.0, math.sqrt(a)))


class Context:
    """What every text score of one query needs besides the words."""

    __slots__ = ("optional", "last_raw", "initials", "lane", "groups")

    def __init__(self, optional, last_raw, initials, lane, groups=frozenset()):
        self.optional = optional
        self.last_raw = last_raw
        self.initials = initials
        self.lane = lane            # the query itself names a lane / dead end
        self.groups = groups        # type_groups of the query's street-type words


class Searcher:
    def __init__(self, db, fold=None):
        if isinstance(db, sqlite3.Connection):
            self.con = db
        else:
            self.con = sqlite3.connect(f"file:{Path(db).resolve()}?mode=ro", uri=True)
        self.con.row_factory = sqlite3.Row
        self.meta = {k: v for k, v in self.con.execute("SELECT key, value FROM meta")}
        self.fts = self.meta.get("fts", "fts5")
        self.col = "rowid" if self.fts == "fts5" else "docid"
        self.fold = fold or Fold(json.loads(self.meta["fold_json"]))
        self.config = json.loads(self.meta["config_json"])
        self.rank = dict(RANKING_DEFAULTS, **self.config["ranking"])
        not_prefix = {}
        for name, cat in self.config.get("categories", {}).items():
            if not name.startswith("_"):
                not_prefix[name] = frozenset(k for w in cat.get("not_prefix", []) for k in self.fold.keys(w))
        self.categories = [(tuple(r["phrase"].split()), r["category"], r["kind"], r["attr"],
                            not_prefix.get(r["category"], frozenset()))
                           for r in self.con.execute("SELECT * FROM categories")]
        self.categories.sort(key=lambda c: -len(c[0]))
        brands = {}
        for canon, words in self.config.get("brands", {}).items():
            if canon.startswith("_"):
                continue
            for w in [canon] + words:
                brands[" ".join(self.fold.keys(w))] = [canon] + words
        self.brands = brands
        self.near_km = {r["kind"]: r["near_km"] for r in self.config["pois"]["rules"] if "near_km" in r}
        self.lane_stems = {self.fold.stem(k) for w in self.rank["lane_words"] for k in self.fold.keys(w)
                           if len(k) > 1}
        self.class_prior = self.rank["street_class_prior"]
        self.type_group = {}
        for group, words in self.rank.get("type_groups", {}).items():
            for w in words:
                for k in self.fold.keys(w):
                    if len(k) > 1:
                        self.type_group.setdefault(self.fold.stem(k), group)
        self._keys = {}
        self._compound = {}

    # -- small helpers ------------------------------------------------------

    def keys(self, text):
        got = self._keys.get(text)
        if got is None:
            got = self._keys[text] = self.fold.keys(text)
        return got

    def compound(self, text):
        if text not in self._compound:
            self._compound[text] = self.fold.compound_keys(text) if "-" in text or "‑" in text or "–" in text \
                or "‐" in text else None
        return self._compound[text]

    def match_expressions(self, tokens):
        """FTS MATCH strings: one for FTS5 (OR inside, AND between words);
        for FTS4 one per combination of readings (plain readings first), to
        stay clear of the two FTS4 query syntaxes that disagree on OR."""
        if self.fts == "fts5":
            return [" AND ".join("(" + " OR ".join(f"{p}*" for p in t.prefixes) + ")" for t in tokens)]
        choices = [t.prefixes for t in tokens]
        combos = []
        for combo in itertools.product(*choices):
            combos.append(" ".join(f"{p}*" for p in combo))
            if len(combos) >= MAX_FTS4_COMBINATIONS:
                break
        return combos

    def fetch_ids(self, table, tokens, limit, where="", params=()):
        ids = []
        for expr in self.match_expressions(tokens):
            if where:
                sql = (f"SELECT {table}_fts.{self.col} FROM {table}_fts JOIN {table} t "
                       f"ON t.id = {table}_fts.{self.col} WHERE {table}_fts MATCH ? AND {where} "
                       f"ORDER BY {table}_fts.{self.col} LIMIT ?")
            else:
                sql = f"SELECT {self.col} FROM {table}_fts WHERE {table}_fts MATCH ? ORDER BY {self.col} LIMIT ?"
            ids.extend(r[0] for r in self.con.execute(sql, (expr, *params, limit)))
        return list(dict.fromkeys(ids))

    def rows(self, table, ids):
        if not ids:
            return []
        out = []
        for chunk in range(0, len(ids), 500):
            part = ids[chunk:chunk + 500]
            marks = ",".join("?" * len(part))
            out.extend(self.con.execute(f"SELECT * FROM {table} WHERE id IN ({marks})", part))
        return out

    def street(self, street_id):
        if street_id is None:
            return None
        return self.con.execute("SELECT * FROM streets WHERE id = ?", (street_id,)).fetchone()

    def name_texts(self, table, row):
        texts = [row[c] for c in ("name", "name_ka", "name_en", "name_ru") if row[c]] if table != "addresses" else []
        if table != "addresses" and row["alt_names"]:
            texts.extend(row["alt_names"].split("|"))
        if table == "pois" and row["brand"]:
            texts.append(row["brand"])
            texts.extend(self.brands.get(" ".join(self.keys(row["brand"])), []))
        if table == "addresses":
            s = self.street(row["street_id"])
            if s is not None:
                texts.extend(s[c] for c in ("name", "name_ka", "name_en", "name_ru") if s[c])
                if s["alt_names"]:
                    texts.extend(s["alt_names"].split("|"))
            if row["place"]:
                texts.append(row["place"])
        return list(dict.fromkeys(texts))

    def same_word(self, token, key):
        """A name word that is the token itself or its stem (no prefix)."""
        return key in token.keys or bool(self.fold.name_bases(key) & token.prefix_set)

    # -- scoring ------------------------------------------------------------

    def word_score(self, token, key, last):
        m = self.rank["match"]
        if key in token.keys:
            return m["exact"]
        if self.fold.name_bases(key) & token.prefix_set:
            return m["stem"]
        if any(key.startswith(p) for p in token.prefixes):
            return m["prefix_last"] if last else m["prefix"]
        return 0.0

    def text_match(self, table, row, tokens, ctx):
        """(score, a name word was left over, the name is a lane) for the
        best reading of the row's names; score 0 when a word is missing."""
        variants = []
        for text in self.name_texts(table, row):
            variants.append(self.keys(text))
            joined = self.compound(text)
            if joined:
                variants.append(joined)
        extra = self.keys(row["housenumber"]) if table == "addresses" else []
        if extra:
            variants = [v + extra for v in variants] or [extra]
        best = (0.0, False, False)
        r = self.rank
        for keys in variants:
            if not keys:
                continue
            total, covered = 0.0, set()
            for t in tokens:
                last = t.raw == ctx.last_raw
                score, where = 0.0, None
                for i, k in enumerate(keys):
                    s = self.word_score(t, k, last)
                    if s > score:
                        score, where = s, i
                if score == 0.0:
                    total = -1.0
                    break
                total += score
                covered.add(where)
            if total < 0:
                continue
            opt = 0
            for t in ctx.optional:
                if any(self.word_score(t, k, False) > 0 for k in keys):
                    opt += 1
            # How much of the name the query said; street-type and noise
            # words do not count, and a street's given name may be left out.
            words = [i for i, k in enumerate(keys)
                     if not self.fold.is_type_key(k) and not self.fold.is_noise_key(k)] or list(range(len(keys)))
            names = [i for i in words if not keys[i][:1].isdigit()]
            spare = 1 if table in ("streets", "addresses") and len(names) >= 2 else 0
            left_over = bool(set(names) - covered)
            share = min(1.0, len(covered & set(words)) / max(1, len(words) - spare))
            cw = r["coverage_weight"]
            value = (total / len(tokens)) * (1 - cw + cw * share) + r["optional_bonus"] * opt
            if ctx.optional and not opt:
                # The query names a street type this name lacks: fine for one
                # of the same group (street / avenue), worse for another group
                # ('isnis kucha' against ისნის რაიონი).
                groups = {self.type_group.get(self.fold.stem(k), "?") for k in keys if self.fold.is_type_key(k)}
                if not groups:
                    value *= r["type_word_missing"]
                elif not groups & ctx.groups:
                    value *= r["type_word_conflict"]
            if ctx.initials and any(i not in covered and keys[i].startswith(x)
                                    for i in names for x in ctx.initials):
                value += r["initial_bonus"]
            lane = (table in ("streets", "addresses") and not ctx.lane
                    and any(self.fold.stem(k) in self.lane_stems for k in keys))
            if lane:
                value *= r["lane_factor"]
            if value > best[0]:
                best = (value, left_over, lane)
        return best

    def text_score(self, table, row, tokens, optional, last_raw, initials=()):
        """The text part of the score (kept for callers of the first version)."""
        ctx = Context(optional, last_raw, list(initials), self.query_names_lane(tokens + list(optional)),
                      self.query_groups(optional))
        return self.text_match(table, row, tokens, ctx)[0]

    def query_groups(self, tokens):
        return frozenset(g for t in tokens for k in t.keys
                         for g in [self.type_group.get(self.fold.stem(k))] if g)

    def query_names_lane(self, tokens):
        return any(self.fold.stem(k) in self.lane_stems for t in tokens for k in t.keys)

    def final_score(self, table, text, importance, lat, lon, near, bonus=0.0, scale_km=None):
        r = self.rank
        weight = r["w_text"] + r["w_importance"]
        score = r["w_text"] * (text + bonus) + r["w_importance"] * importance
        if near is not None:
            km = distance_km(near[0], near[1], lat, lon)
            score += r["w_near"] / (1.0 + km / (scale_km or r["near_scale_km"][table]))
            weight += r["w_near"]
        return score / weight

    # -- categories and anchors -----------------------------------------------

    def match_category(self, tokens):
        """(category, kind, attr, other tokens) for a category phrase in the
        query, or None. Exact words win over the same stem ('hotels',
        'აეროპორტამდე'), which win over a last word still being typed
        ('parki' for parking; never a word in the category's not_prefix)."""
        last = len(tokens) - 1
        for mode in ("exact", "stem", "prefix"):
            for phrase, category, kind, attr, not_prefix in self.categories:
                n = len(phrase)
                for start in range(0, len(tokens) - n + 1):
                    ok = True
                    for j in range(n):
                        t = tokens[start + j]
                        word = phrase[j]
                        if word in t.keys:
                            continue
                        if mode != "exact" and len(word) >= 5 and self.fold.stem(word) in t.prefix_set:
                            continue
                        if (mode == "prefix" and start + j == last and j == n - 1
                                and any(len(k) >= 3 and word.startswith(k) and k not in not_prefix
                                        for k in t.keys)):
                            continue
                        ok = False
                        break
                    if ok:
                        rest = tokens[:start] + tokens[start + n:]
                        return category, kind, attr, rest
        return None

    def nearest_pois(self, kind, attr, center, limit):
        cond, params = "kind = ?", [kind]
        if attr:
            cond += " AND (';' || attrs || ';') LIKE ?"
            params.append(f"%;{attr};%")
        if center is None:
            return list(self.con.execute(f"SELECT * FROM pois WHERE {cond} ORDER BY id LIMIT ?", (*params, limit)))
        lat, lon = center
        rows = []
        for span in (0.02, 0.08, 0.3, 1.0, 5.0):
            k = span / max(0.2, math.cos(math.radians(lat)))
            rows = list(self.con.execute(
                f"SELECT * FROM pois WHERE {cond} AND lat BETWEEN ? AND ? AND lon BETWEEN ? AND ?",
                (*params, lat - span, lat + span, lon - k, lon + k)))
            if len(rows) >= limit:
                break
        rows.sort(key=lambda r: distance_km(lat, lon, r["lat"], r["lon"]))
        return rows[:limit]

    def places_named(self, tokens, kinds, limit=50):
        """Places of these kinds with a name made of exactly these words
        (type words aside), each word itself or its stem: 'ბათუმის' is
        ბათუმი, 'ვაკეში' is ვაკე, but 'ბათუმის ქუჩა' is no place."""
        hits = []
        for row in self.rows("places", self.fetch_ids("places", tokens, limit)):
            if row["kind"] not in kinds:
                continue
            for text in self.name_texts("places", row):
                for keys in (self.keys(text), self.compound(text)):
                    if not keys:
                        continue
                    words = [k for k in keys if not self.fold.is_type_key(k)] or keys
                    if len(words) == len(tokens) and all(self.same_word(t, k) for k, t in zip(words, tokens)):
                        hits.append(row)
                        break
                else:
                    continue
                break
        return hits

    def anchor(self, tokens, near):
        """What the words beside a category word name: ('place', row) for a
        legal settlement or district (the best by importance and nearness),
        ('occupied', rows) when they name only occupied places, ('street',
        row) when no place but a street fits them wholly, else None."""
        hits = self.places_named(tokens, ANCHOR_KINDS)
        if hits:
            legal = [r for r in hits if not r["occupied"]]
            if not legal:
                return "occupied", hits
            ctx = Context([], tokens[-1].raw, [], False)
            scored = []
            for r in legal:   # the exact name before a stem match ('ვაკეში': ვაკე before ვაკის რაიონი)
                text = self.text_match("places", r, tokens, ctx)[0]
                scored.append((text, self.final_score("places", text, r["importance"], r["lat"], r["lon"], near), r))
            best = max(scored, key=lambda x: (x[1], -x[2]["id"]))
            # The same name close by: the most specific kind (Tbilisi's suburbs are whole districts).
            same = [x for x in scored if x[0] >= best[0] - 1e-9
                    and distance_km(best[2]["lat"], best[2]["lon"], x[2]["lat"], x[2]["lon"])
                    <= self.rank["anchor_same_place_km"]]
            pick = min(same, key=lambda x: (SPECIFIC_FIRST.index(x[2]["kind"]), -x[1], x[2]["id"]))
            return "place", pick[2]
        sub = Query(" ".join(t.raw for t in tokens), list(tokens), [], list(tokens))
        streets = self._search(sub, near, 1, tables=("streets",), categories=False)
        if streets and streets[0]["text"] >= self.rank["anchor_street_min_text"]:
            return "street", streets[0]
        return None

    def city_reach_km(self, place):
        """How far from a settlement's point a row still counts as near it
        when the query names that settlement."""
        reach = self.config["places"]["kinds"].get(place["kind"], {}).get("reach_km", 0) or 0
        return max(reach * self.rank["city_reach_factor"], self.rank["city_reach_min_km"])

    def settlement_spans(self, tokens):
        """Words that name a settlement: [(indices, [place rows])]."""
        spans = []
        for n in (2, 1):
            for start in range(0, len(tokens) - n + 1):
                span = tokens[start:start + n]
                if any(t.is_number for t in span) or n == len(tokens):
                    continue
                hits = self.places_named(span, SETTLEMENT_KINDS)
                if hits:
                    spans.append((list(range(start, start + n)), hits))
        return spans

    # -- the search ---------------------------------------------------------

    def search(self, text, near=None, limit=10):
        q = self.fold.parse_query(text)
        if q.empty:
            return []
        out = self._search(q, near, limit)
        if out or len(q.required) < 2:
            return out
        drop = self.rarest(q.required)
        out = self._search(q.without(drop), near, limit)
        for r in out:
            r["partial"] = True
            r["ignored"] = [drop.raw]
        return out

    def rarest(self, tokens):
        """The word with the fewest matches in the whole index (none at all
        first); the later one when two tie."""
        best = None
        for pos, t in enumerate(tokens):
            n = 0
            for table in TABLES:
                for expr in self.match_expressions([t]):
                    n += self.con.execute(
                        f"SELECT count(*) FROM (SELECT {self.col} FROM {table}_fts WHERE {table}_fts MATCH ? LIMIT 500)",
                        (expr,)).fetchone()[0]
            if best is None or (n, -pos) < best[0]:
                best = ((n, -pos), t)
        return best[1]

    def _search(self, q, near, limit, tables=TABLES, categories=True):
        cand = self.rank["candidates"]
        found = {}
        ordered = q.tokens or q.required
        ctx = Context(q.optional, ordered[-1].raw, q.initials, self.query_names_lane(ordered),
                      self.query_groups(q.optional))

        def add(table, row, score, text, **extra):
            key = (table, row["id"])
            if key not in found or found[key]["score"] < score or extra.get("anchor"):
                if key in found and found[key].get("anchor") and not extra.get("anchor"):
                    return
                found[key] = result(table, row, score, text=round(text, 4), **extra)

        def add_occupied(rows):
            for row in rows:
                add("places", row, self.final_score("places", 1.0, row["importance"], row["lat"], row["lon"], near),
                    1.0, anchor="occupied")

        # Categories: nearest of a kind.
        cat = self.match_category(ordered) if categories else None
        if cat is not None:
            category, kind, attr, rest = cat
            center, by_name = near, None
            rest = [t for t in rest if not t.is_type]
            if rest:
                anchor = self.anchor(rest, near)
                if anchor is None:
                    by_name = rest           # 'Kutaisi International Airport'
                elif anchor[0] == "occupied":
                    add_occupied(anchor[1])  # explain; the POIs stay around the user
                else:
                    center = (anchor[1]["lat"], anchor[1]["lon"])
            for row in self.nearest_pois(kind, attr, center, cand["category"]):
                text = 1.0 if by_name is None else self.text_match("pois", row, by_name, ctx)[0]
                score = self.final_score("pois", text, row["importance"], row["lat"], row["lon"], center,
                                         scale_km=self.near_km.get(row["kind"]))
                add("pois", row, score, text, category=category)

        # Names.
        interpretations = [(q.required, None)]
        for idx, hits in self.settlement_spans(q.required):
            rest = [t for i, t in enumerate(q.required) if i not in idx]
            legal = [r for r in hits if not r["occupied"]]
            if legal:
                if rest:
                    interpretations.append((rest, legal))
            elif "places" in tables:
                add_occupied(hits)           # 'rustaveli sokhumi': explain first
        full_places = []   # places that match the whole query, word for word
        for table in tables:
            for tokens, cities in interpretations:
                if table == "places" and cities:
                    continue
                city_ids = [c["id"] for c in cities] if cities else None
                words = [t for t in tokens if not t.is_number]
                numbers = [t for t in tokens if t.is_number]
                if table == "addresses":
                    if not numbers or not words:
                        continue
                    use = tokens
                else:
                    use = words or tokens
                if cities:
                    # 'rustaveli batumi': rows of that settlement, or close to it.
                    marks = ",".join("?" * len(city_ids))
                    ids = self.fetch_ids(table, use, cand["global"], f"t.city_id IN ({marks})", city_ids)
                    for c in cities:
                        box = self.city_reach_km(c) / 111.0
                        k = box / max(0.2, math.cos(math.radians(c["lat"])))
                        ids += self.fetch_ids(table, use, cand["local"],
                                              "t.lat BETWEEN ? AND ? AND t.lon BETWEEN ? AND ?",
                                              (c["lat"] - box, c["lat"] + box, c["lon"] - k, c["lon"] + k))
                else:
                    ids = self.fetch_ids(table, use, cand["global"])
                    if near is not None:
                        box = cand["local_box_deg"]
                        k = box / max(0.2, math.cos(math.radians(near[0])))
                        ids += self.fetch_ids(table, use, cand["local"],
                                              "t.lat BETWEEN ? AND ? AND t.lon BETWEEN ? AND ?",
                                              (near[0] - box, near[0] + box, near[1] - k, near[1] + k))
                for row in self.rows(table, list(dict.fromkeys(ids))):
                    bonus = 0.0
                    if cities:
                        if row["city_id"] in city_ids:
                            bonus = self.rank["city_bonus"]
                        elif not any(distance_km(c["lat"], c["lon"], row["lat"], row["lon"]) <= self.city_reach_km(c)
                                     for c in cities):
                            continue   # elsewhere: only the reading with every word may offer it
                    text, left_over, _ = self.text_match(table, row, use, ctx)
                    if text <= 0:
                        continue
                    if table == "places" and text >= 0.999 and not left_over:
                        full_places.append(row)
                    street_row = self.street(row["street_id"]) if table == "addresses" else None
                    if table == "addresses":
                        text *= self.housenumber_fit(numbers, row["housenumber"])
                    else:
                        if (table == "streets" and full_places and not q.optional
                                and (not left_over or any(p["kind"] in ("city", "town") for p in full_places))):
                            text *= self.rank["street_named_like_place"]
                        if numbers and not self.has_number(table, row, numbers):
                            text *= self.rank["number_not_in_name"]
                    scale = self.near_km.get(row["kind"]) if table == "pois" else None
                    score = self.final_score(table, text, row["importance"], row["lat"], row["lon"],
                                             near, bonus, scale)
                    if table == "streets":
                        score += self.class_prior.get(row["kind"], 0.0)
                    elif table == "addresses" and street_row is not None:
                        score += self.class_prior.get(street_row["kind"], 0.0)
                    if table == "places" and row["occupied"]:
                        score *= self.rank["occupied_factor"]
                    if table == "addresses":
                        extra = {"street": (street_row["name"] or street_row["name_en"]) if street_row is not None
                                 else row["place"]}
                        s_ka = (street_row["label_ka"] if street_row is not None and "label_ka" in street_row.keys()
                                else None) or extra["street"] or ""
                        s_en = (street_row["label_en"] if street_row is not None and "label_en" in street_row.keys()
                                else None) or s_ka
                        extra["label_ka"] = f"{s_ka} {row['housenumber']}".strip()
                        extra["label_en"] = f"{s_en} {row['housenumber']}".strip()
                        add(table, row, score, text, **extra)
                    else:
                        add(table, row, score, text)
        out = sorted(found.values(), key=lambda r: (0 if r.get("anchor") else 1, -r["score"],
                                                    TABLES.index(r["table"]), r["id"]))
        return out[:limit]

    def has_number(self, table, row, numbers):
        keys = {k for text in self.name_texts(table, row) for k in self.keys(text)}
        return any(k in keys for t in numbers for k in t.keys)

    def housenumber_fit(self, numbers, housenumber):
        keys = self.keys(housenumber)
        best = 0.5
        for t in numbers:
            for k in keys:
                if k in t.keys:
                    return 1.0
                if any(k.startswith(p) for p in t.prefixes):
                    best = max(best, 0.85 if k[len(t.keys[0]):len(t.keys[0]) + 1].isalpha() else 0.7)
        return best


def result(table, row, score, **extra):
    out = {"table": table, "id": row["id"], "kind": row["kind"] if table != "addresses" else "address",
           "lat": row["lat"], "lon": row["lon"], "score": round(score, 4)}
    names = row.keys()
    for col in ("name", "name_ka", "name_en", "name_ru", "label_ka", "label_en", "occupied", "zone", "band",
                "city_id", "street_id", "housenumber", "brand", "attrs", "osm"):
        if col in names:
            out[col] = row[col]
    if table == "places":
        out["routable"] = not row["occupied"]
        out["reason"] = REASONS.get(row["zone"], "occupied") if row["occupied"] else None
    else:
        out["routable"] = True       # nothing but places is kept inside no_go_hard
        out["reason"] = None
    out.update(extra)
    return out


def label(hit):
    """What the app shows: label_ka, else label_en; never 'name' for an
    occupied place."""
    text = hit.get("label_ka") or hit.get("label_en")
    if not text and not hit.get("occupied"):
        text = hit.get("name") or hit.get("name_en") or hit.get("brand")
    return text or ""


def main(argv=None):
    p = argparse.ArgumentParser(description="Search georgia_geocoder.sqlite like the app does.")
    p.add_argument("db")
    p.add_argument("query", nargs="+")
    p.add_argument("--near", help="lat,lon of the user")
    p.add_argument("--limit", type=int, default=8)
    args = p.parse_args(argv)
    near = tuple(float(v) for v in args.near.split(",")) if args.near else None
    s = Searcher(args.db)
    for text in args.query:
        print(f"== {text}")
        for r in s.search(text, near=near, limit=args.limit):
            flag = f" NOT ROUTABLE ({r['reason']})" if not r["routable"] else ""
            part = f" partial, ignored {r['ignored']}" if r.get("partial") else ""
            print(f"  {r['score']:.3f} {r['table']:<9} {r['kind']:<14} {label(r)} "
                  f"({r['lat']:.5f}, {r['lon']:.5f}){flag}{part}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
