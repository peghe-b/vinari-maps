#!/usr/bin/env python3
"""The search-key fold of the offline geocoder (stdlib only).

One function turns Georgian, Latin (the national system, BGN, chat Latin
that follows the Georgian keyboard) and Russian into the same ASCII keys,
so 'რუსთაველის გამზირი', 'rustaveli', 'rusTaveli' and 'Руставели' all
search 'rustavel*'. The tables and rules live in config/geocoder_fold.json,
which the build copies into the database; the app must fold what the user
types with exactly the same rules (the file's 'vectors' are the shared test
cases for the Swift and Kotlin ports, and its 'method' is the order of the
steps).

Usage:
  python3 scripts/geocoder_fold.py "რუსთაველის გამზირი" rusTaveli
  python3 scripts/geocoder_fold.py --check      # run the vectors
"""

import functools
import json
import re
import sys
import unicodedata
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_SPEC = ROOT / "config" / "geocoder_fold.json"

TOKEN_RE = re.compile(r"[^\W_]+")
ROMAN_RE = re.compile(r"^(X{0,3})(IX|IV|V?I{0,3})$")
ROMAN_VALUES = {"I": 1, "V": 5, "X": 10}
LOWER_LATIN = set("abcdefghijklmnopqrstuvwxyz")


def roman_value(token):
    """'IV' -> 4 for tokens made only of capital I, V, X; otherwise None."""
    if not token or not ROMAN_RE.match(token):
        return None
    total, prev = 0, 0
    for ch in reversed(token):
        v = ROMAN_VALUES[ch]
        total = total - v if v < prev else total + v
        prev = max(prev, v)
    return total or None


def georgian_script(ch):
    """Mtavruli, Asomtavruli and Nuskhuri letters -> Mkhedruli."""
    cp = ord(ch)
    if 0x1C90 <= cp <= 0x1CBA or 0x1CBD <= cp <= 0x1CBF:
        return chr(cp - 0x1C90 + 0x10D0)
    if 0x10A0 <= cp <= 0x10C5:
        return chr(cp + 0x30)
    if 0x2D00 <= cp <= 0x2D25:
        return chr(cp - 0x2D00 + 0x10D0)
    if cp in (0x10C7, 0x2D27):
        return "ჷ"
    if cp in (0x10CD, 0x2D2D):
        return "ჽ"
    return ch


def is_title_case(token):
    """An ASCII capital followed only by ASCII lower case ('Shota')."""
    return (len(token) > 1 and token.isascii() and token.isalpha()
            and token[0].isupper() and token[1:].islower())


class QueryToken:
    """One word of a query: its readings (full keys) and the prefixes to search."""

    __slots__ = ("raw", "keys", "prefixes", "prefix_set", "is_number", "is_type")

    def __init__(self, raw, keys, prefixes, is_number, is_type):
        self.raw = raw
        self.keys = keys            # full folded keys, one per reading
        self.prefixes = prefixes    # stems of those keys (and extra forms), searched as prefixes
        self.prefix_set = frozenset(prefixes)
        self.is_number = is_number
        self.is_type = is_type

    def __repr__(self):
        return f"QueryToken({self.raw!r}, keys={self.keys}, prefixes={self.prefixes})"


class Query:
    __slots__ = ("text", "required", "optional", "tokens", "initials", "dropped")

    def __init__(self, text, required, optional, tokens, initials=(), dropped=()):
        self.text = text
        self.required = required    # every one must match
        self.optional = optional    # street-type words: count in ranking only
        self.tokens = tokens        # required and optional, in the order typed
        self.initials = list(initials)   # keys of one-letter initials ('ი.' -> 'i'): a ranking hint
        self.dropped = list(dropped)     # raw words left out (noise, unit words, postcodes)

    @property
    def empty(self):
        return not self.required

    def without(self, token):
        """The same query with one required token left out."""
        required = [t for t in self.required if t is not token]
        return Query(self.text, required, self.optional, [t for t in self.tokens if t is not token],
                     self.initials, self.dropped + [token.raw])


class Fold:
    def __init__(self, spec):
        self.spec = spec
        self.version = int(spec["version"])
        self.apostrophes = set(spec["apostrophes"])
        self.number_prefix = [(re.compile(spec["number_prefix"][0]), spec["number_prefix"][1])] \
            if spec.get("number_prefix") else []
        self.ordinals = [(re.compile(p), r) for p, r in spec["ordinals"]]
        self.house_letter = [(re.compile(spec["house_letter"][0]), spec["house_letter"][1])] \
            if spec.get("house_letter") else []
        self.hyphens = set(spec.get("compound_hyphens", ()))
        self.roman_max = int(spec.get("roman_numeral_max", 39))
        self.chat = dict(spec["chat_capitals"])
        self.georgian = dict(spec["georgian"])
        self.cyrillic = dict(spec["cyrillic"])
        self.latin_special = dict(spec["latin_special"])
        self.rules = {a: b for a, b in spec["skeleton"]}
        self.rule_lengths = sorted({len(a) for a in self.rules}, reverse=True)
        stem = spec["stem"]
        self.min_key = int(stem["min_key"])
        self.min_stem = int(stem["min_stem"])
        self.short_is_key = int(stem.get("short_is_key", 0))
        self.vowels = set(stem["vowels"])
        syncope = stem.get("syncope", {})
        endings = syncope.get("endings", {})
        if isinstance(endings, list):   # fold v1: one min_key for every ending
            endings = {e: int(syncope.get("min_key", 99)) for e in endings}
        self.syncope = {e: int(n) for e, n in endings.items()}
        plural = stem.get("plural", {})
        self.plural_endings = tuple(plural.get("endings", ()))
        self.plural_min = int(plural.get("min_key", 99))
        posts = stem.get("postpositions", {}).get("endings", {})
        if isinstance(posts, list):
            posts = {e: "" for e in posts}
        self.postpositions = tuple(sorted(posts.items(), key=lambda item: len(item[0])))
        self.type_words = self._word_keys(spec["type_words"])
        self.type_stems = {self.stem(k) for k in self.type_words}
        self.noise_words = self._word_keys(spec.get("noise_words", {}))
        units = spec.get("unit_words", {})
        self.unit_letters = {w for lang, words in units.items() if not lang.startswith("_")
                             for w in words if len(w) == 1}
        self.unit_words = self._word_keys(units)
        postcode = spec.get("postcode") or {}
        self.postcode = (int(postcode["digits"]), int(postcode["min"]), int(postcode["max"])) if postcode else None

    def _word_keys(self, table):
        out = set()
        for lang, words in table.items():
            if lang.startswith("_"):
                continue
            for word in words:
                out.update(k for k in self.keys(word) if len(k) > 1)
        return out

    @classmethod
    def load(cls, path=DEFAULT_SPEC):
        return cls(json.loads(Path(path).read_text(encoding="utf-8")))

    # -- text to tokens -----------------------------------------------------

    def _clean(self, text):
        out = []
        for ch in text:
            ch = georgian_script(ch)
            if ch in self.apostrophes:
                continue
            special = self.latin_special.get(ch.lower())
            out.append(special if special is not None else ch)
        text = unicodedata.normalize("NFD", "".join(out))
        text = "".join(ch for ch in text if unicodedata.category(ch) != "Mn")
        text = unicodedata.normalize("NFC", text)
        for pattern, repl in self.number_prefix + self.ordinals + self.house_letter:
            text = pattern.sub(repl, text)
        return text

    def token_info(self, text):
        """[token, joined to the next token by a hyphen, followed by '.']
        for every token, with the case kept (the chat rule needs it)."""
        clean = self._clean(text or "")
        matches = list(TOKEN_RE.finditer(clean))
        out = []
        for i, m in enumerate(matches):
            token, end = m.group(), m.end()
            nxt = matches[i + 1] if i + 1 < len(matches) else None
            joined = (nxt is not None and nxt.start() == end + 1 and clean[end] in self.hyphens
                      and not token[-1].isdigit() and not nxt.group()[0].isdigit())
            dot = end < len(clean) and clean[end] == "."
            value = None
            if token.isascii() and token.isupper():
                value = roman_value(token)
            elif i > 0 and len(token) >= 2 and token.isascii() and token.islower() and set(token) <= set("ivx"):
                value = roman_value(token.upper())
            if value is not None and value <= self.roman_max:
                token = str(value)
            out.append([token, joined, dot])
        return out

    def raw_tokens(self, text):
        """Tokens with their case kept."""
        return [t for t, _, _ in self.token_info(text)]

    # -- one token to keys --------------------------------------------------

    def _intermediate(self, token, chat_at=()):
        """Latin intermediate string; chat_at holds the positions read as
        Georgian keyboard capitals."""
        out = []
        for i, ch in enumerate(token):
            if i in chat_at:
                out.append(self.georgian[self.chat[ch]])
                continue
            low = ch.lower()
            if low in self.georgian:
                out.append(self.georgian[low])
            elif low in self.cyrillic:
                out.append(self.cyrillic[low])
            elif low.isascii() and low.isalnum():
                out.append(low)
            elif ch.isdigit():
                out.append(str(unicodedata.digit(ch, 0)))
            # anything else (other scripts, symbols) is dropped
        return "".join(out)

    def _skeleton(self, s):
        out, i, n = [], 0, len(s)
        while i < n:
            for length in self.rule_lengths:
                part = s[i:i + length]
                if len(part) == length and part in self.rules:
                    out.append(self.rules[part])
                    i += length
                    break
            else:
                out.append(s[i])
                i += 1
        collapsed = []
        for ch in "".join(out):
            if collapsed and ch == collapsed[-1] and not ch.isdigit():
                continue
            collapsed.append(ch)
        return "".join(collapsed)

    def token_key(self, token):
        """The index key of one token (no chat reading)."""
        return self._skeleton(self._intermediate(token))

    def token_readings(self, token, lead=True):
        """Every key a typed token may mean (chat Latin gives up to three).
        lead=False: no reading of a leading capital as a Georgian letter."""
        plain = self.token_key(token)
        if not any(ch in LOWER_LATIN for ch in token):
            return [plain] if plain else []
        mid = {i for i in range(1, len(token))
               if token[i] in self.chat and token[i - 1] in LOWER_LATIN}
        lead = lead and len(token) > 1 and token[0] in self.chat and token[1] in LOWER_LATIN
        readings = [plain]
        if mid:
            readings.append(self._skeleton(self._intermediate(token, mid)))
        if lead:
            readings.append(self._skeleton(self._intermediate(token, mid | {0})))
        return [r for i, r in enumerate(readings) if r and r not in readings[:i]]

    # -- names and queries --------------------------------------------------

    def keys(self, text):
        """All keys of a text, in order (duplicates kept)."""
        return [k for k in (self.token_key(t) for t in self.raw_tokens(text)) if k]

    def _compounds(self, info):
        """Runs of hyphen-joined letter tokens: [(first index, last index)]."""
        runs, start = [], None
        for i, (_, joined, _) in enumerate(info):
            if joined and start is None:
                start = i
            if not joined and start is not None:
                runs.append((start, i))
                start = None
        return runs

    def compound_keys(self, text):
        """Keys of the name with every hyphenated compound joined into one
        key ('ვაჟა-ფშაველას გამზირი' -> [vajapshavelas, gamziri]), or None
        when the text has no compound."""
        info = self.token_info(text)
        runs = self._compounds(info)
        if not runs:
            return None
        out, i = [], 0
        for a, b in runs:
            out.extend(self.token_key(t) for t, _, _ in info[i:a])
            out.append(self.token_key("".join(t for t, _, _ in info[a:b + 1])))
            i = b + 1
        out.extend(self.token_key(t) for t, _, _ in info[i:])
        return [k for k in out if k]

    def index_keys(self, text):
        """What goes into the search index for one name: every key except
        single letters, once each, then the joined key of each compound."""
        seen, out = set(), []
        info = self.token_info(text)
        keys = [self.token_key(t) for t, _, _ in info]
        keys += [self.token_key("".join(t for t, _, _ in info[a:b + 1])) for a, b in self._compounds(info)]
        for key in keys:
            if key and (len(key) > 1 or key.isdigit()) and key not in seen:
                seen.add(key)
                out.append(key)
        return out

    @functools.lru_cache(maxsize=200000)
    def stem(self, key):
        if not key or key[0].isdigit() or len(key) < self.min_key:
            return key
        n = len(key)
        if key.endswith("is") and (n - 2 >= self.min_stem or n == self.short_is_key):
            return key[:-2]
        if key[-1] in self.vowels and n - 1 >= self.min_stem:
            return key[:-1]
        if key.endswith("s") and n - 1 >= self.min_stem + 1:
            return key[:-1]
        return key

    def plural_base(self, key):
        """gmirebis -> gmir, gmirta -> gmir; None when no plural ending fits."""
        if len(key) < self.plural_min or key[0].isdigit():
            return None
        for ending in self.plural_endings:
            if (key.endswith(ending) and len(key) - len(ending) >= self.min_stem
                    and key[-len(ending) - 1] not in self.vowels):
                return key[:-len(ending)]
        return None

    def _post_split(self, text):
        """(rest, ending, nominative rule) for the shortest postposition
        that ends text with at least min_stem letters before it, or None."""
        if not text or text[:1].isdigit():
            return None
        for ending, rule in self.postpositions:
            if text.endswith(ending) and len(text) - len(ending) >= self.min_stem:
                return text[:-len(ending)], ending, rule
        return None

    def postposition_base(self, key):
        """ბათუმში (batumshi) -> batum: a key without the shortest
        postposition that fits, before stemming; None when none fits."""
        split = self._post_split(key)
        return split[0] if split else None

    def postposition_forms(self, token, chat_at=()):
        """(base key, nominative key or None) of a typed token that ends in a
        postposition, cut on the intermediate Latin before the skeleton, so a
        letter the skeleton would merge stays (ქუთაისში: kutais + shi, not
        kutaishi -> kutai); None when no postposition fits."""
        split = self._post_split(self._intermediate(token, chat_at))
        if split is None:
            return None
        rest, _, rule = split
        base = self._skeleton(rest)
        nominative = None
        if rule == "+i":            # -ში, -ზე, -თან: the nominative -ი is dropped after a consonant
            nominative = base + "i" if base[-1:] not in self.vowels else base
        elif rule.count(">") == 1:  # 'a>i' (ბათუმამდე), 'is>i' (ბათუმისკენ)
            old, new = rule.split(">")
            nominative = base[:-len(old)] + new if base.endswith(old) else base
        elif rule == "=":           # -დან after the instrumental: თბილისიდან -> tbilisi
            nominative = base
        return base, nominative

    def extra_prefixes(self, key):
        """The query-side forms searched besides stem(key) (postpositions
        are added by parse_query, which has the typed token)."""
        out = []
        for ending, min_key in self.syncope.items():
            if len(key) >= min_key and key.endswith(ending):
                out.append(key[:-3] + key[-2])
        plural = self.plural_base(key)
        if plural:
            out.append(plural)
        return out

    @functools.lru_cache(maxsize=200000)
    def name_bases(self, key):
        """Forms of a NAME key that count as the same stem in ranking:
        its stem and, for a plural, the key without the plural ending."""
        out = {self.stem(key)}
        plural = self.plural_base(key)
        if plural:
            out.add(plural)
        return frozenset(out)

    @functools.lru_cache(maxsize=200000)
    def is_type_key(self, key):
        if key in self.type_words or self.stem(key) in self.type_stems:
            return True
        base = self.postposition_base(key)
        return base is not None and (base in self.type_words or self.stem(base) in self.type_stems)

    @functools.lru_cache(maxsize=200000)
    def is_noise_key(self, key):
        if key in self.noise_words:
            return True
        base = self.postposition_base(key)
        return base is not None and base in self.noise_words

    def is_postcode(self, key):
        if self.postcode is None:
            return False
        digits, low, high = self.postcode
        return len(key) == digits and key.isdigit() and low <= int(key) <= high

    def parse_query(self, text):
        info = self.token_info(text)
        lead = sum(1 for t, _, _ in info if is_title_case(t)) <= 1
        words, initials, dropped = [], [], []   # words: [raw, readings]
        skip_number = False
        for i, (raw, _, dot) in enumerate(info):
            readings = self.token_readings(raw, lead)
            if not readings:
                continue
            key = readings[0]
            number = key[:1].isdigit()
            if skip_number:
                skip_number = False
                if number:
                    dropped.append(raw)
                    continue
            single = len(raw) == 1 and not raw.isdigit()
            if (key in self.unit_words and not single) or (single and dot and raw.lower() in self.unit_letters):
                skip_number = True
                dropped.append(raw)
                continue
            if single:
                later_word = any(not t[0].isdigit() and len(t) > 1 for t, _, _ in info[i + 1:])
                if dot and later_word and not key[:1].isdigit():
                    initials.append(key)
                continue
            words.append([raw, readings])
        # Noise words go when anything else is left.
        if any(not self.is_noise_key(r[0]) for _, r in words):
            kept = []
            for raw, readings in words:
                if not readings[0][:1].isdigit() and any(self.is_noise_key(r) for r in readings):
                    dropped.append(raw)
                else:
                    kept.append([raw, readings])
            words = kept
        # Postcodes: a leading 0, or another number beside them.
        numbers = [w for w in words if w[1][0][:1].isdigit()]
        postcodes = [w for w in numbers if self.is_postcode(w[1][0])
                     and (w[1][0].startswith("0") or len(numbers) > 1)]
        if postcodes and len(postcodes) < len(words):
            if len(postcodes) == len(numbers):    # keep one number if all look like postcodes
                postcodes = [w for w in postcodes if w[1][0].startswith("0")] or postcodes[1:]
            dropped.extend(w[0] for w in postcodes)
            words = [w for w in words if not any(w is p for p in postcodes)]
        tokens = []
        for raw, readings in words:
            is_number = readings[0][:1].isdigit()
            is_type = not is_number and any(self.is_type_key(r) for r in readings)
            prefixes, extra_keys = [], []
            for r in readings:
                prefixes.append(self.stem(r))
                if not is_number:
                    prefixes.extend(self.extra_prefixes(r))
            if not is_number:
                forms = self.postposition_forms(raw)
                if forms:
                    base, nominative = forms
                    prefixes.extend([base, self.stem(base)])
                    if nominative:
                        extra_keys.append(nominative)
                        prefixes.append(self.stem(nominative))
            prefixes = list(dict.fromkeys(p for p in prefixes if p))
            keys = list(dict.fromkeys(readings + extra_keys))
            tokens.append(QueryToken(raw, keys, prefixes, is_number, is_type))
        required = [t for t in tokens if not t.is_type]
        optional = [t for t in tokens if t.is_type]
        if not required:
            required, optional = optional, []
        return Query(text, required, optional, tokens, initials, dropped)


def check_vectors(fold):
    """Run the shared test vectors; returns a list of failures."""
    vectors = fold.spec["vectors"]
    failures = []
    for group in vectors["same"]:
        name_key = fold.index_keys(group[0])[0]
        for typed in group[1:]:
            readings = fold.token_readings(fold.raw_tokens(typed)[0])
            if name_key not in readings:
                failures.append(f"same: {typed!r} reads {readings}, not {name_key!r} ({group[0]!r})")
    for a, b in vectors["different"]:
        if fold.keys(a)[0] == fold.keys(b)[0]:
            failures.append(f"different: {a!r} and {b!r} both give {fold.keys(a)[0]!r}")
    for case in vectors["keys"]:
        got = fold.keys(case["text"])
        if got != case["keys"]:
            failures.append(f"keys: {case['text']!r} gave {got}, expected {case['keys']}")
    for case in vectors.get("index_keys", []):
        got = fold.index_keys(case["text"])
        if got != case["keys"]:
            failures.append(f"index_keys: {case['text']!r} gave {got}, expected {case['keys']}")
    for case in vectors["query"]:
        q = fold.parse_query(case["text"])
        got_req = [t.prefixes for t in q.required]
        got_opt = [t.keys[0] for t in q.optional]
        if got_req != case["required"] or got_opt != case["optional"]:
            failures.append(f"query: {case['text']!r} gave required {got_req} optional {got_opt}, "
                            f"expected {case['required']} / {case['optional']}")
        if "initials" in case and q.initials != case["initials"]:
            failures.append(f"query: {case['text']!r} gave initials {q.initials}, expected {case['initials']}")
    tables = fold.spec["tables"]
    for letter, latin in tables["national_2002"].items():
        if letter.startswith("_"):
            continue
        if fold.token_key(letter) != fold.token_key(latin.replace("'", "")):
            failures.append(f"national: {letter} folds to {fold.token_key(letter)!r}, "
                            f"its Latin {latin!r} to {fold.token_key(latin.replace(chr(39), ''))!r}")
    for latin, letter in tables["keyboard_chat"].items():
        if latin.startswith("_"):
            continue
        typed = "a" + latin + "a"  # a chat capital counts after a lower-case letter
        if fold.token_key("ა" + letter + "ა") not in fold.token_readings(typed):
            failures.append(f"chat: {typed!r} does not read as {'ა' + letter + 'ა'!r}")
    return failures


_ROMAN_TABLES = {}


def romanise(text, spec):
    """Georgian letters in the national 2002 system without apostrophes,
    each word capitalised ('ცხინვალი' -> 'Tskhinvali'); other characters
    stay. For labels, never for search."""
    table = _ROMAN_TABLES.get(id(spec))
    if table is None:
        table = {k: v.replace("'", "") for k, v in spec["tables"]["national_2002"].items() if not k.startswith("_")}
        table = _ROMAN_TABLES[id(spec)] = (table, spec["georgian"])
    table, extra = table
    out = []
    for ch in text:
        ch = georgian_script(ch)
        out.append(table.get(ch, extra.get(ch, ch)))
    words = re.split(r"(\s+|-)", "".join(out))
    return "".join(w[:1].upper() + w[1:] if w.strip() and w != "-" else w for w in words)


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    fold = Fold.load()
    if argv[:1] == ["--check"]:
        failures = check_vectors(fold)
        for f in failures:
            print("FAIL", f)
        print(f"fold v{fold.version}: {'all vectors pass' if not failures else f'{len(failures)} failures'}")
        return 1 if failures else 0
    for text in argv:
        q = fold.parse_query(text)
        print(f"{text!r}: index keys {fold.index_keys(text)}; search "
              f"{[t.prefixes for t in q.required]} optional {[t.keys for t in q.optional]}"
              f"{f' initials {q.initials}' if q.initials else ''}{f' dropped {q.dropped}' if q.dropped else ''}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
