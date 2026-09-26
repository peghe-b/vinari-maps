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
  5. Exact name first: a place or named feature (water, pass, ski area,
     resort, border crossing ...) whose name is exactly the whole query
     ranks above the streets and POIs that only carry that name and above
     every row of the reading that takes a word as a settlement: 'sarpi'
     is the village, not the Senaki — Poti — Sarpi motorway; 'თბილისის
     ზღვა' is the reservoir, not 'ზღვა' in Tbilisi. Only carrying the name
     means: a street named after a settlement or feature and nothing else,
     a route that lists it, a street or POI near it, any street or POI for
     a city or town. A district holds nothing by name alone (Rustavi's
     'შოთა რუსთაველის დასახლება' and Batumi's Rustaveli street), a street
     with a given name besides the surname keeps its rank against a far
     village of that surname, and so does a POI that only shares a far
     place's name (hotel 'ალმა'). With a position, an occupied place holds
     rows near the user only as a route or a neighbour. See
     ranking.exact_first in config/geocoder.json.
  6. No position and no settlement in the query: streets, and separately
     addresses, that do not fit like the best rows of the most important
     settlement among the best-fitting rows lose
     ranking.other_settlement_penalty (see _other_settlement_note). So
     'ჭავჭავაძის 37' is Tbilisi's when Tbilisi has a 37; when only Batumi
     has an exact 37 (Tbilisi a '37ა' or '20-37'), Batumi's comes first.
  7. When nothing matches every word, the search runs once more without the
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
import re
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
# A database built before a rule existed searches without it (exact_first
# None, other_settlement_penalty 0): it always searches with its own rules.
RANKING_DEFAULTS = {"occupied_factor": 1.0, "street_class_prior": {}, "lane_words": [], "lane_factor": 1.0,
                    "initial_bonus": 0.0, "type_word_missing": 1.0, "type_word_conflict": 1.0,
                    "anchor_street_min_text": 0.9, "anchor_same_place_km": 10.0, "city_reach_factor": 1.5,
                    "city_reach_min_km": 3.0, "exact_first": None, "other_settlement_penalty": 0.0,
                    "other_settlement_margin": 0.0}
MAIN_NAMES = ("name", "name_ka", "name_en", "name_ru")
# Route names list the places a road links: 'სენაკი — ფოთი — სარფი',
# 'ტირძნისი-დიცი-ერედვი-ხეითი' (hyphen, hyphen, non-breaking hyphen, en and
# em dash, horizontal bar; spaces around them or not).
ROUTE_DASHES = re.compile("\\s*[-\u2010\u2011\u2013\u2014\u2015]\\s*")
KM_PER_DEG = math.pi * 6371.0088 / 180.0


def distance_km(lat1, lon1, lat2, lon2):
    p1, p2 = math.radians(lat1), math.radians(lat2)
    a = (math.sin((p2 - p1) / 2) ** 2
         + math.cos(p1) * math.cos(p2) * math.sin(math.radians(lon2 - lon1) / 2) ** 2)
    return 2 * 6371.0088 * math.asin(min(1.0, math.sqrt(a)))


def box_distance_km(lat, lon, min_lat, min_lon, max_lat, max_lon):
    """From a point to a box (0 inside it)."""
    dy = max(min_lat - lat, 0.0, lat - max_lat) * KM_PER_DEG
    dx = max(min_lon - lon, 0.0, lon - max_lon) * KM_PER_DEG * math.cos(math.radians(lat))
    return math.hypot(dx, dy)


class Candidate:
    """One row that matched one reading of the query, until it is scored."""

    __slots__ = ("table", "row", "numbers", "split", "bonus", "text", "via",
                 "score", "text_final", "street_row", "held", "holders", "named", "fitness")

    def __init__(self, table, row, numbers, split, bonus, text, via):
        self.table = table
        self.row = row
        self.numbers = numbers      # house numbers of the query
        self.split = split          # found by a reading that took words as a settlement
        self.bonus = bonus          # city_bonus when it lies in that settlement
        self.text = text            # text_match score
        self.via = via              # the name text that scored it
        self.score = None
        self.text_final = None
        self.street_row = None
        self.held = None            # its own score when exact_first held it lower
        self.holders = None         # holders(), once worked out
        self.named = None           # named_after(), once worked out
        self.fitness = None         # fit(), once worked out


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
        texts = [row[c] for c in MAIN_NAMES if row[c]] if table != "addresses" else []
        if table != "addresses" and row["alt_names"]:
            texts.extend(row["alt_names"].split("|"))
        if table == "pois" and row["brand"]:
            texts.append(row["brand"])
            texts.extend(self.brands.get(" ".join(self.keys(row["brand"])), []))
        if table == "addresses":
            s = self.street(row["street_id"])
            if s is not None:
                texts.extend(s[c] for c in MAIN_NAMES if s[c])
                if s["alt_names"]:
                    texts.extend(s["alt_names"].split("|"))
            if row["place"]:
                texts.append(row["place"])
        return list(dict.fromkeys(texts))

    def main_texts(self, table, row, street_row=None):
        """The current names (not old_name, alt_name ... or a brand): the
        name columns; for an address its street's, or addr:place."""
        if table == "addresses":
            s = street_row if street_row is not None else self.street(row["street_id"])
            texts = {s[c] for c in MAIN_NAMES if s[c]} if s is not None else set()
            return texts | ({row["place"]} if row["place"] else set())
        return {row[c] for c in MAIN_NAMES if row[c]}

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
        """(score, a name word was left over, the name is a lane, the name
        text) for the best reading of the row's names (the first name on a
        tie); score 0 when a word is missing."""
        variants = []
        for text in self.name_texts(table, row):
            variants.append((self.keys(text), text))
            joined = self.compound(text)
            if joined:
                variants.append((joined, text))
        extra = self.keys(row["housenumber"]) if table == "addresses" else []
        if extra:
            variants = [(v + extra, text) for v, text in variants] or [(extra, None)]
        best = (0.0, False, False, None)
        r = self.rank
        for keys, source in variants:
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
                best = (value, left_over, lane, source)
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

        # Names: the reading that needs every word, and one per settlement
        # the query names ('rustaveli batumi': 'rustaveli' in Batumi).
        interpretations = [(q.required, None)]
        names_settlement = False
        for idx, hits in self.settlement_spans(q.required):
            rest = [t for i, t in enumerate(q.required) if i not in idx]
            legal = [r for r in hits if not r["occupied"]]
            if legal:
                names_settlement = True
                if rest:
                    interpretations.append((rest, legal))
            elif "places" in tables:
                add_occupied(hits)           # 'rustaveli sokhumi': explain first
        cands = self.candidates(tables, interpretations, near, ctx)
        exact = self.exact_rows(q, cands)
        for c in cands:
            self.fit_text(c, exact, q, near)
        centre = self.centre(cands) if near is None and not names_settlement else {}
        for c in cands:
            self.score(c, near, centre)
        if exact:
            self.hold_below_exact(cands, exact, q, near)
        for c in cands:
            row, extra = c.row, {"_order": c.held if c.held is not None else c.score}
            if c.table == "addresses":
                street_row = c.street_row
                extra["street"] = (street_row["name"] or street_row["name_en"]) if street_row is not None \
                    else row["place"]
                s_ka = (street_row["label_ka"] if street_row is not None and "label_ka" in street_row.keys()
                        else None) or extra["street"] or ""
                s_en = (street_row["label_en"] if street_row is not None and "label_en" in street_row.keys()
                        else None) or s_ka
                extra["label_ka"] = f"{s_ka} {row['housenumber']}".strip()
                extra["label_en"] = f"{s_en} {row['housenumber']}".strip()
            add(c.table, row, c.score, c.text_final, **extra)
        out = sorted(found.values(), key=lambda r: (0 if r.get("anchor") else 1, -r["score"],
                                                    -r.get("_order", r["score"]),
                                                    TABLES.index(r["table"]), r["id"]))
        for r in out:
            r.pop("_order", None)
        return out[:limit]

    def candidates(self, tables, interpretations, near, ctx):
        """Every row that matches a reading of the query, with its text score."""
        cand = self.rank["candidates"]
        out = []
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
                    text, _, _, via = self.text_match(table, row, use, ctx)
                    if text > 0:
                        out.append(Candidate(table, row, numbers, cities is not None, bonus, text, via))
        return out

    def fit_text(self, c, exact, q, near):
        """The text part of a candidate's score: its text match times the
        house number's fit (addresses), street_named_like_place and
        number_not_in_name."""
        table, row, r = c.table, c.row, self.rank
        text = c.text
        if table == "addresses":
            c.street_row = self.street(row["street_id"])
            text *= self.housenumber_fit(c.numbers, row["housenumber"])
        else:
            if table == "streets" and exact and (self.named_after(c, q) or self.holders(c, exact, q, near)):
                text *= r["street_named_like_place"]
            if c.numbers and not self.has_number(table, row, c.numbers):
                text *= r["number_not_in_name"]
        c.text_final = text

    def score(self, c, near, centre):
        """The final score of one candidate (see ranking._note)."""
        table, row, r = c.table, c.row, self.rank
        scale = self.near_km.get(row["kind"]) if table == "pois" else None
        score = self.final_score(table, c.text_final, row["importance"], row["lat"], row["lon"], near, c.bonus,
                                 scale)
        if table == "streets":
            score += self.class_prior.get(row["kind"], 0.0)
        elif table == "addresses" and c.street_row is not None:
            score += self.class_prior.get(c.street_row["kind"], 0.0)
        if table == "places" and row["occupied"]:
            score *= r["occupied_factor"]
        if table in centre and not self.fits_centre(c, *centre[table]):
            score -= r["other_settlement_penalty"]
        c.score = score

    # -- exact name first ----------------------------------------------------

    def words_are(self, keys, tokens, stem=False):
        """The name's words (street-type and noise words aside) are exactly
        the query's words, one to one, each as typed or its nominative; with
        stem, also a form with the same stem ('ერედვის' for 'ერედვი'), never
        a longer word the query only begins ('გალფი' is not 'გალი'), and
        noise words count as words ('Георгия' folds to the noise 'georgia')."""
        words = [k for k in keys if not self.fold.is_type_key(k) and (stem or not self.fold.is_noise_key(k))] \
            or list(keys)
        if len(words) != len(tokens):
            return False
        left = list(words)
        for t in tokens:
            for i, k in enumerate(left):
                if k in t.keys or (stem and self.same_word(t, k)):
                    del left[i]
                    break
            else:
                return False
        return True

    def exact_words(self, keys, tokens):
        return self.words_are(keys, tokens)

    def named_as(self, table, row, tokens, stem=False, texts=None):
        for text in texts or self.name_texts(table, row):
            for keys in (self.keys(text), self.compound(text)):
                if keys and self.words_are(keys, tokens, stem):
                    return True
        return False

    def exact_name(self, table, row, tokens):
        return self.named_as(table, row, tokens)

    def exact_rows(self, q, cands):
        """Places and named features that are the whole query (ranking.exact_first)."""
        ef = self.rank["exact_first"]
        if not ef:
            return []
        allowed = set(ef.get("type_groups", ()))
        for t in q.optional:   # 'ბოდორნის გზატკეცილი' asks for the road
            if not any(self.type_group.get(self.fold.stem(k)) in allowed for k in t.keys):
                return []
        features = set(ef["feature_kinds"])
        return [c for c in cands if not c.split
                and (c.table == "places" or (c.table == "pois" and c.row["kind"] in features))
                and self.exact_name(c.table, c.row, q.required)]

    def is_local(self, c, near):
        """Inside the box around the user that the local search reads."""
        if near is None:
            return False
        box = self.rank["candidates"]["local_box_deg"]
        k = box / max(0.2, math.cos(math.radians(near[0])))
        return abs(c.row["lat"] - near[0]) <= box and abs(c.row["lon"] - near[1]) <= k

    def holders(self, c, exact, q, near):
        """The exact rows that a street or POI only carries the name of
        (ranking.exact_first): (a) a street named after an exact settlement
        of near_kinds or an exact feature, and nothing else; (b) any street
        or POI, for an exact city or town; (c) a route that lists the name;
        (d) a street or POI within near_km of an exact settlement of
        near_kinds or an exact feature. With a position, an occupied exact
        place holds a row inside the local box only by (c) or (d)."""
        if c.holders is None:
            c.holders = self._holders(c, exact, q, near)
        return c.holders

    def _holders(self, c, exact, q, near):
        ef = self.rank["exact_first"]
        whole, near_kinds = set(ef["whole_kinds"]), set(ef["near_kinds"])
        local = self.is_local(c, near)
        route = None
        out = []
        for e in exact:
            is_place = e.table == "places"
            spare = is_place and e.row["occupied"] and local
            anchor = not is_place or e.row["kind"] in near_kinds    # a settlement or a feature
            if not spare:
                if is_place and e.row["kind"] in whole:                                     # (b)
                    out.append(e)
                    continue
                if anchor and self.named_after(c, q):                                        # (a)
                    out.append(e)
                    continue
            if route is None:                                                                # (c)
                route = self.is_route(c, q)
            if route:
                out.append(e)
                continue
            if anchor:                                                                       # (d)
                lat, lon = e.row["lat"], e.row["lon"]
                if c.table == "streets":
                    km = box_distance_km(lat, lon, c.row["min_lat"], c.row["min_lon"], c.row["max_lat"],
                                         c.row["max_lon"])
                else:
                    km = distance_km(lat, lon, c.row["lat"], c.row["lon"])
                if km <= ef["near_km"]:
                    out.append(e)
        return out

    def named_after(self, c, q):
        """A street (of the reading that needs every word) whose matched
        name is the query's words and nothing else, each the word or its
        stem ('ერედვის ქუჩა' for 'ერედვი')."""
        if c.named is None:
            c.named = (c.table == "streets" and not c.split and c.via is not None
                       and self.named_as(c.table, c.row, q.required, stem=True, texts=[c.via]))
        return c.named

    def is_route(self, c, q):
        for text in self.name_texts(c.table, c.row):
            parts = [p for p in ROUTE_DASHES.split(text) if p.strip()]
            if len(parts) >= 2 and any(self.exact_words(self.keys(p), q.required) for p in parts):
                return True
        return False

    def hold_below_exact(self, cands, exact, q, near):
        """Streets and POIs that only carry an exact row's name, and rows of
        a settlement reading, score at most the best of the exact rows that
        hold them minus margin. With a position, an occupied exact place
        holds no settlement-reading row inside the local box."""
        ef = self.rank["exact_first"]
        chosen = {id(e) for e in exact}
        for c in cands:
            if id(c) in chosen:
                continue
            if c.split:
                local = self.is_local(c, near)
                hold = [e for e in exact if not (local and e.table == "places" and e.row["occupied"])]
            elif c.table in ("streets", "pois"):
                hold = self.holders(c, exact, q, near)
            else:
                continue
            if hold:
                cap = max(e.score for e in hold) - ef["margin"]
                if c.score > cap:
                    c.held, c.score = c.score, cap

    # -- no position, no settlement --------------------------------------------

    def fit(self, c):
        """How well a street or address fits the query, for the centre:
        ((number tier, current name), text). Number tier (addresses): 2 when
        the house number is the query's number and nothing else, 1 when it
        holds it among others ('20-22', '166 კორპ. 8' for 22, 8), 0 when it
        only begins with it ('49ა') or lacks it. Current name: 1 when the
        best reading is a current name, not old_name, alt_name ..."""
        if c.fitness is None:
            c.fitness = self._fit(c)
        return c.fitness

    def _fit(self, c):
        tier = 0
        if c.table == "addresses":
            hn = [k for k in self.keys(c.row["housenumber"]) if not self.fold.is_noise_key(k)]
            left = list(c.numbers)
            for k in hn:
                t = next((t for t in left if k in t.keys), None)
                if t is None:
                    break
                left.remove(t)
            else:
                tier = 2 if hn else 0
            if not tier and self.housenumber_fit(c.numbers, c.row["housenumber"]) >= 1.0:
                tier = 1
        main = 1 if c.via is not None and c.via in self.main_texts(c.table, c.row, c.street_row) else 0
        return (tier, main), c.text_final

    def centre(self, cands):
        """Per table (streets, addresses): the most important settlement
        (lowest places.id: the capital first) among the best-fitting rows,
        those of the best fit tier with a text within
        other_settlement_margin of the best; and that tier. Per table, so an
        address that has the number in another city still beats the
        capital's street without it."""
        out = {}
        if not self.rank["other_settlement_penalty"]:
            return out
        margin = self.rank["other_settlement_margin"]
        for table in ("streets", "addresses"):
            fits = [(self.fit(c), c) for c in cands if c.table == table]
            if not fits:
                continue
            tier = max(f[0] for f, _ in fits)
            best = max(f[1] for f, _ in fits if f[0] == tier)
            ids = [c.row["city_id"] for f, c in fits
                   if f[0] == tier and f[1] >= best - margin - 1e-9 and c.row["city_id"] is not None]
            if ids:
                out[table] = (self.rows("places", [min(ids)])[0], tier)
        return out

    def fits_centre(self, c, centre, tier):
        """In the centre (its settlement, or within its city reach), and
        through a current name when the best-fitting rows are. No row fits
        better than those (the centre is chosen among them), so none that
        does is ever penalised."""
        return self.in_centre(c.row, centre) and self.fit(c)[0][1] >= tier[1]

    def in_centre(self, row, centre):
        return (row["city_id"] == centre["id"]
                or distance_km(centre["lat"], centre["lon"], row["lat"], row["lon"]) <= self.city_reach_km(centre))

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
