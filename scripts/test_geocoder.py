#!/usr/bin/env python3
"""Unit tests for the offline geocoder on small made-up data. No downloads.

Run from the maps/ folder:
    python3 scripts/test_geocoder.py

Needs only the Python standard library (sqlite3 with FTS5 and FTS4). The
test that reads an OSM file needs pyosmium and the cross-checks against
shapely need shapely; without them those tests are skipped, not failed
(the CI installs both and fails on any skip).
"""

import contextlib
import gzip
import io
import json
import math
import random
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import geocoder_build as gb  # noqa: E402
import geocoder_gate as gg  # noqa: E402
import geocoder_release as gr  # noqa: E402
from geocoder_fold import Fold, check_vectors, georgian_script  # noqa: E402
from geocoder_search import Searcher, label  # noqa: E402

try:
    import osmium  # noqa: F401
    HAVE_OSMIUM = True
except ImportError:
    HAVE_OSMIUM = False

try:
    import numpy  # noqa: F401
    import shapely  # noqa: F401
    from shapely.geometry import Point, Polygon
    HAVE_SHAPELY = True
except ImportError:
    HAVE_SHAPELY = False

FOLD = Fold.load()
CONFIG = gb.load_config()


@contextlib.contextmanager
def quiet():
    """Keep the scripts' own progress lines out of the test log."""
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        yield


def box(lon0, lat0, lon1, lat1):
    return [(lon0, lat0), (lon1, lat0), (lon1, lat1), (lon0, lat1), (lon0, lat0)]


def feature(name, rings):
    return {"type": "Feature", "properties": {"name": name},
            "geometry": {"type": "Polygon", "coordinates": [[list(p) for p in r] for r in rings]}}


# A made-up Georgia: a 1 x 1 degree square; the occupied area a small square
# in its north-east; no_go_hard 0.001 degrees (about 100 m) around it, the
# band 0.005 degrees.
GEORGIA = box(44.0, 41.5, 45.0, 42.5)
OCCUPIED = box(44.6, 42.2, 44.8, 42.4)
HARD = box(44.599, 42.199, 44.801, 42.401)
BAND = box(44.595, 42.195, 44.805, 42.405)


def write_zones(path):
    data = {"type": "FeatureCollection", "features": [
        feature("no_go_hard", [HARD]), feature("soft_band_outer", [BAND]),
        feature("occupied", [OCCUPIED]), feature("georgia", [GEORGIA])]}
    Path(path).write_text(json.dumps(data), encoding="utf-8")


def zones():
    return gb.Zones(gb.PolygonIndex([GEORGIA]), gb.PolygonIndex([HARD]), gb.PolygonIndex([BAND]),
                    gb.PolygonIndex([OCCUPIED]))


def synthetic_collector():
    """A tiny country fed straight into the Collector (no OSM file)."""
    c = gb.Collector(CONFIG)
    node = c.node
    # Places. The capital with its admin boundary, a village and a district
    # both called Vake, a town, occupied places, a place in Russia.
    node(1, {"place": "city", "name": "თბილისი", "name:en": "Tbilisi", "name:ru": "Тбилиси",
             "population": "1300000", "capital": "yes"}, 41.70, 44.30)
    node(2, {"place": "village", "name": "ვაკე", "name:en": "Vake", "population": "240"}, 41.95, 44.10)
    node(3, {"place": "neighbourhood", "name": "ვაკე", "name:en": "Vake"}, 41.72, 44.27)
    node(4, {"place": "town", "name": "ბათუმი", "name:en": "Batumi", "population": "20000"}, 41.60, 44.90)
    node(5, {"place": "town", "name": "Ленингор", "name:ka": "ახალგორი", "name:en": "Akhalgori"}, 42.30, 44.70)
    node(6, {"place": "village", "name": "Сухум", "name:ka": "სოხუმი", "old_name:ru": "Сухуми;Сохуми"}, 42.4005, 44.70)
    node(7, {"place": "village", "name": "Russia village", "name:en": "Russkoe"}, 42.70, 44.50)
    node(8, {"place": "village", "name": "ზოლი", "name:en": "Zoli"}, 42.197, 44.70)  # in the band
    node(9, {"place": "village", "name": "გუდაური", "name:en": "Gudauri", "population": "54"}, 42.10, 44.50)
    c.relation(100, {"type": "boundary", "boundary": "administrative", "admin_level": "4", "name": "თბილისი"},
               [("w", 1000, "outer"), ("w", 1001, "outer"), ("n", 1, "label")])
    c.way(1000, {}, [10, 11, 12], [(44.2, 41.6), (44.4, 41.6), (44.4, 41.8)])
    c.way(1001, {}, [12, 13, 10], [(44.4, 41.8), (44.2, 41.8), (44.2, 41.6)])
    # Streets. Rustaveli avenue in two ways sharing a node, a genitive-only
    # street named after a village, a street in the occupied area, the same
    # name again in Batumi.
    avenue = {"highway": "construction", "construction": "primary", "name": "შოთა რუსთაველის გამზირი",
              "name:en": "Shota Rustaveli Avenue", "name:ru": "проспект Шота Руставели"}
    c.way(2000, dict(avenue), [20, 21, 22], [(44.300, 41.700), (44.302, 41.702), (44.304, 41.704)])
    c.way(2001, dict(avenue, highway="primary"), [22, 23], [(44.304, 41.704), (44.306, 41.706)])
    c.way(2002, {"highway": "residential", "name": "გუდაურის ქუჩა", "name:en": "Gudauri Street"},
          [24, 25], [(44.31, 41.71), (44.312, 41.71)])
    c.way(2003, {"highway": "residential", "name": "проспект Мира"}, [26, 27], [(44.70, 42.30), (44.701, 42.30)])
    c.way(2004, {"highway": "secondary", "name": "შოთა რუსთაველის ქუჩა", "name:en": "Shota Rustaveli Street"},
          [28, 29], [(44.900, 41.600), (44.902, 41.601)])
    c.way(2005, {"highway": "footway", "name": "ბაღის ბილიკი"}, [30, 31], [(44.31, 41.72), (44.311, 41.72)])
    # Addresses: a shop and its building with the same address (merged,
    # the building wins), a short addr:street form, a street that exists
    # only in addresses, one in the occupied area.
    node(40, {"addr:housenumber": "12", "addr:street": "შოთა რუსთაველის გამზირი", "shop": "books"}, 41.7021, 44.3021)
    c.way(4000, {"building": "yes", "addr:housenumber": "12", "addr:street": "შოთა რუსთაველის გამზირი"},
          [41, 42, 43, 44, 41], [(44.3020, 41.7020), (44.3023, 41.7020), (44.3023, 41.7022),
                                 (44.3020, 41.7022), (44.3020, 41.7020)])
    node(45, {"addr:housenumber": "39ა", "addr:street": "რუსთაველის გამზირი"}, 41.7050, 44.3055)
    node(46, {"addr:housenumber": "5", "addr:street": "ახალი ქუჩა", "addr:street:en": "Akhali Street"}, 41.75, 44.35)
    node(47, {"addr:housenumber": "7", "addr:street": "ახალი ქუჩა"}, 41.7503, 44.3503)
    node(48, {"addr:housenumber": "1", "addr:street": "проспект Мира"}, 42.30, 44.70)
    # POIs.
    node(50, {"amenity": "fuel", "brand": "ვისოლი", "brand:en": "Wissol", "fuel:cng": "yes"}, 41.705, 44.31)
    c.way(5000, {"amenity": "fuel", "brand": "ვისოლი", "brand:en": "Wissol", "fuel:cng": "yes", "building": "roof"},
          [51, 52, 53, 51], [(44.3101, 41.7051), (44.3103, 41.7051), (44.3102, 41.7053), (44.3101, 41.7051)])
    node(54, {"amenity": "fuel", "brand": "გალფი", "brand:en": "Gulf"}, 41.62, 44.90)
    node(55, {"amenity": "parking"}, 41.701, 44.301)
    node(56, {"amenity": "hospital", "name": "Hospital"}, 42.35, 44.75)          # occupied: dropped
    node(57, {"barrier": "border_control", "name": "Border"}, 42.60, 44.50)     # Russia: dropped
    node(58, {"mountain_pass": "yes", "name": "ჯვრის უღელტეხილი", "name:en": "Jvari Pass"}, 42.0, 44.45)
    node(59, {"mountain_pass": "yes", "name": "ჯვრის უღელტეხილი"}, 41.55, 44.05)
    node(60, {"tourism": "hotel"}, 41.70, 44.30)                                # unnamed hotel: not kept
    c.way(2006, {"highway": "trunk", "name": "სამხედრო გზა"}, [61, 58, 62], [(44.44, 41.99), (44.45, 42.0), (44.46, 42.01)])
    c.relation(200, {"type": "multipolygon", "natural": "water", "water": "reservoir",
                     "name": "თბილისის წყალსაცავი", "alt_name:ka": "თბილისის ზღვა", "name:en": "Tbilisi reservoir"},
               [("w", 6000, "outer"), ("w", 6001, "outer")])
    c.way(6000, {}, [70, 71, 72], [(44.35, 41.74), (44.37, 41.74), (44.37, 41.76)])
    c.way(6001, {}, [70, 73, 72], [(44.35, 41.74), (44.35, 41.76), (44.37, 41.76)])  # reversed half
    # Review fixes (2026-09-26). An occupied city whose 12 km reach spills
    # over the line onto a legal street and address (addr:city naming it);
    # an occupied village without name:ka (left out); checkpoints at the
    # line; two airports, hotels and a street named after Batumi; a
    # person-named avenue, its lane and a small village of that surname; a
    # district and a street of one name; a park; an alias; a compound.
    node(10, {"place": "city", "name": "Цхинвал", "name:ka": "ცხინვალი", "name:en": "Tskhinval"}, 42.25, 44.65)
    node(11, {"place": "village", "name": "Аҷара"}, 42.33, 44.72)
    c.way(2010, {"highway": "residential", "name": "გორის ქუჩა"}, [80, 81], [(44.650, 42.170), (44.652, 42.170)])
    node(82, {"addr:housenumber": "3", "addr:street": "გორის ქუჩა", "addr:city": "ცხინვალი"}, 42.1702, 44.6512)
    node(83, {"barrier": "border_control", "name": "ზოლის საგუშაგო"}, 42.18, 44.70)   # 2 km from the line
    node(84, {"barrier": "border_control"}, 42.185, 44.66)                               # unnamed, at the line
    node(85, {"aeroway": "aerodrome", "name": "ბათუმის საერთაშორისო აეროპორტი",
              "name:en": "Batumi International Airport"}, 41.61, 44.88)
    node(86, {"aeroway": "aerodrome", "name": "თბილისის საერთაშორისო აეროპორტი",
              "name:en": "Tbilisi International Airport"}, 41.67, 44.35)
    node(87, {"tourism": "hotel", "name": "სასტუმრო ზღვა"}, 41.601, 44.901)
    node(88, {"tourism": "hotel", "name": "Hotel Tbilisi"}, 41.701, 44.301)
    c.way(2011, {"highway": "residential", "name": "ბათუმის ქუჩა", "name:en": "Batumi Street"},
          [89, 90], [(44.305, 41.705), (44.306, 41.705)])
    node(91, {"place": "village", "name": "წერეთელი", "name:en": "Tsereteli", "population": "900"}, 41.55, 44.50)
    c.way(2012, {"highway": "primary", "name": "აკაკი წერეთლის გამზირი", "name:en": "Akaki Tsereteli Avenue"},
          [92, 93], [(44.320, 41.720), (44.330, 41.722)])
    c.way(2013, {"highway": "residential", "name": "აკაკი წერეთლის შესახვევი"},
          [94, 95], [(44.302, 41.701), (44.303, 41.701)])
    node(96, {"place": "suburb", "name": "ისნის რაიონი", "name:en": "Isani District", "population": "130000"},
         41.69, 44.32)
    c.way(2014, {"highway": "residential", "name": "ისნის ქუჩა", "name:en": "Isani Street"},
          [97, 98], [(44.340, 41.690), (44.341, 41.690)])
    node(99, {"leisure": "park", "name": "ვაკის პარკი"}, 41.721, 44.271)
    node(100, {"place": "town", "name": "სტეფანწმინდა", "name:en": "Stepantsminda"}, 42.45, 44.20)
    c.way(2015, {"highway": "primary", "name": "ვაჟა-ფშაველას გამზირი", "name:en": "Vazha-Pshavela Avenue"},
          [101, 102], [(44.280, 41.725), (44.285, 41.726)])
    c.finish_relations()
    return c


def build_db(path, engine="fts5"):
    builder = gb.Builder(CONFIG, FOLD, zones(), synthetic_collector()).build()
    meta = gb.base_meta(gb.DEFAULT_CONFIG, gb.DEFAULT_FOLD_SPEC, FOLD, CONFIG,
                        {"sha256": "ab" * 32, "bytes": 1, "timestamp": "2026-09-24T20:21:02Z",
                         "tag": "osm-20260924T202102Z-3f2a1b0c", "clip_config_version": 2})
    gb.write_database(path, builder, meta, engine=engine)
    return builder


TBILISI = (41.70, 44.30)


class FoldTest(unittest.TestCase):

    def test_shared_vectors(self):
        self.assertEqual(check_vectors(FOLD), [])

    def test_old_georgian_scripts(self):
        mkhedruli = "თბილისი"
        mtavruli = "".join(chr(ord(ch) - 0x10D0 + 0x1C90) for ch in mkhedruli)
        asomtavruli = "".join(chr(ord(ch) - 0x30) for ch in mkhedruli)
        nuskhuri = "".join(chr(ord(ch) - 0x10D0 + 0x2D00) for ch in mkhedruli)
        for text in (mtavruli, asomtavruli, nuskhuri):
            self.assertEqual(FOLD.keys(text), ["tbilisi"], text)
        self.assertEqual(georgian_script("a"), "a")

    def test_capital_is_chat_only_after_lower_case(self):
        self.assertIn("rustaveli", FOLD.token_readings("Rustaveli"))   # auto-capitalised keyboard
        self.assertEqual(FOLD.token_readings("SOCAR"), ["socar"])      # all capitals: plain
        self.assertEqual(FOLD.token_readings("rusTaveli"), ["rustaveli"])
        self.assertEqual(sorted(FOLD.token_readings("Sota")), ["shota", "sota"])

    def test_house_number_letter_joins_number(self):
        q = FOLD.parse_query("რუსთაველის 39 ა")
        self.assertEqual([t.keys for t in q.required], [["rustavelis"], ["39a"]])
        self.assertTrue(q.required[1].is_number)

    def test_type_words_are_optional_unless_alone(self):
        q = FOLD.parse_query("ჭავჭავაძის გამზირი")
        self.assertEqual([t.prefixes for t in q.required], [["chavchavadz"]])
        self.assertEqual([t.keys for t in q.optional], [["gamziri"]])
        self.assertEqual(len(FOLD.parse_query("გამზირი").required), 1)

    def test_index_keys_skip_single_letters(self):
        self.assertEqual(FOLD.index_keys("35-ე ქ."), ["35"])
        self.assertEqual(FOLD.index_keys("Tbilisi tbilisi"), ["tbilisi"])

    def test_genitive_syncope(self):
        q = FOLD.parse_query("wereTeli")
        self.assertIn("ceretl", q.required[0].prefixes)   # finds წერეთლის
        self.assertTrue("ceretlis".startswith("ceretl"))

    def test_empty_and_symbols(self):
        self.assertTrue(FOLD.parse_query("  , . ").empty)
        self.assertEqual(FOLD.keys("№ 5"), ["5"])

    def test_house_letter_joins_on_both_sides_but_not_across_a_dot(self):
        self.assertEqual(FOLD.index_keys("20 ა"), ["20a"])        # the index too
        self.assertEqual(FOLD.index_keys("12-ა"), ["12a"])
        q = FOLD.parse_query("ჭავჭავაძის 37, ქ. თბილისი")
        self.assertEqual([t.keys[0] for t in q.required], ["chavchavadzis", "37", "tbilisi"])
        q = FOLD.parse_query("ჭავჭავაძის 37 ბ. 12")                 # ბ. = apartment: 37 stays alone, 12 goes
        self.assertEqual([t.keys[0] for t in q.required], ["chavchavadzis", "37"])

    def test_noise_units_and_postcodes_are_dropped(self):
        q = FOLD.parse_query("ქ. თბილისი, 0179, ჭავჭავაძის 37, ბინა 12, საქართველო")
        self.assertEqual([t.keys[0] for t in q.required], ["tbilisi", "chavchavadzis", "37"])
        self.assertEqual(sorted(q.dropped), sorted(["0179", "ბინა", "12", "საქართველო"]))
        self.assertEqual([t.keys[0] for t in FOLD.parse_query("пр. Чавчавадзе, дом 37").required],
                         ["chavchavadze", "37"])
        self.assertEqual([t.keys[0] for t in FOLD.parse_query("на Руставели").required], ["rustaveli"])
        # Alone, a noise word is still a query.
        self.assertEqual([t.keys[0] for t in FOLD.parse_query("საქართველო").required], ["sakartvelo"])
        # A postcode is dropped only beside another number, unless it starts with 0.
        self.assertEqual([t.keys[0] for t in FOLD.parse_query("rustaveli 1200").required], ["rustaveli", "1200"])
        self.assertEqual([t.keys[0] for t in FOLD.parse_query("tbilisi 0108").required], ["tbilisi"])

    def test_postpositions_give_the_nominative(self):
        for typed, nominative in (("ქუთაისში", "kutaisi"), ("გორში", "gori"), ("ვაკეში", "vake"),
                                  ("თბილისიდან", "tbilisi"), ("ბათუმამდე", "batumi"), ("ბათუმისკენ", "batumi"),
                                  ("რუსთაველზე", "rustaveli"), ("გალფთან", "galpi")):
            token = FOLD.parse_query(typed).required[0]
            self.assertIn(nominative, token.keys, typed)
            self.assertIn(FOLD.stem(nominative), token.prefixes, typed)
        self.assertTrue(FOLD.parse_query("რუსთაველის გამზირზე").optional)   # a type word with a postposition

    def test_initials_and_title_case(self):
        q = FOLD.parse_query("ი. აბაშიძის 5")
        self.assertEqual((q.initials, [t.keys[0] for t in q.required]), (["i"], ["abashidzis", "5"]))
        # One Title Case word may be chat Latin (Sota = შოთა); several are just capitalised.
        self.assertIn("shot", FOLD.parse_query("Sota rusTaveli").required[0].prefixes)
        self.assertNotIn("shot", FOLD.parse_query("Sota Rustaveli Street").required[0].prefixes)

    def test_compounds_and_roman_numerals(self):
        self.assertIn("vajapshavelas", FOLD.index_keys("ვაჟა-ფშაველას გამზირი"))
        self.assertEqual(FOLD.compound_keys("ვაჟა-ფშაველას გამზირი"), ["vajapshavelas", "gamziri"])
        self.assertIsNone(FOLD.compound_keys("Şıxlı-2"))
        self.assertEqual(FOLD.keys("vakhtang vi"), ["vahtang", "6"])
        self.assertEqual(FOLD.keys("vi"), ["vi"])                      # the first word is never a numeral

    def test_regex_port_hazards(self):
        # Unicode word classes: 'ტ' is a letter, so 5-ეტაჟიანი is no ordinal.
        self.assertEqual(FOLD.keys("5-ეტაჟიანი"), ["5", "etajiani"])
        # Step 0 turns й into и before any table: the table needs no й.
        self.assertNotIn("й", FOLD.spec["cyrillic"])
        self.assertEqual(FOLD.keys("Чайка"), ["chaika"])
        self.assertEqual(FOLD.keys("9-й"), ["9"])

    def test_romanised_labels(self):
        from geocoder_fold import romanise
        self.assertEqual(romanise("ცხინვალი", FOLD.spec), "Tskhinvali")
        self.assertEqual(romanise("ქვემო იკორთა", FOLD.spec), "Kvemo Ikorta")
        self.assertEqual(romanise("ტყვარჩელი", FOLD.spec), "Tqvarcheli")


class GeometryTest(unittest.TestCase):

    STAR = [(math.cos(a) * (1.0 if i % 2 == 0 else 0.45) + 44.0, math.sin(a) * (1.0 if i % 2 == 0 else 0.45) + 42.0)
            for i, a in enumerate(i * math.pi / 7 for i in range(14))]
    HOLE = [(43.9, 41.9), (44.1, 41.9), (44.1, 42.1), (43.9, 42.1)]
    OTHER = box(46.0, 40.0, 46.5, 40.5)

    @staticmethod
    def brute(rings, x, y):
        inside = False
        for ring in rings:
            pts = gb.clean_ring(ring)
            for (x1, y1), (x2, y2) in zip(pts, pts[1:] + pts[:1]):
                if (y1 > y) != (y2 > y) and x < x1 + (y - y1) * (x2 - x1) / (y2 - y1):
                    inside = not inside
        return inside

    def test_polygon_index_matches_brute_force(self):
        rings = [self.STAR, self.HOLE, self.OTHER]
        poly = gb.PolygonIndex(rings)
        rnd = random.Random(7)
        for _ in range(4000):
            x, y = rnd.uniform(42.8, 46.8), rnd.uniform(39.8, 43.2)
            self.assertEqual(poly.contains(x, y), self.brute(rings, x, y), (x, y))
        self.assertFalse(poly.contains(44.0, 42.0))   # in the hole
        self.assertTrue(poly.contains(46.2, 40.2))    # second polygon

    @unittest.skipUnless(HAVE_SHAPELY, "shapely not installed")
    def test_polygon_index_matches_shapely(self):
        shp = Polygon(self.STAR, [self.HOLE])
        poly = gb.PolygonIndex([self.STAR, self.HOLE])
        rnd = random.Random(11)
        for _ in range(4000):
            x, y = rnd.uniform(42.8, 45.2), rnd.uniform(40.8, 43.2)
            self.assertEqual(poly.contains(x, y), shp.contains(Point(x, y)), (x, y))

    def test_representative_point_lies_inside(self):
        c_shape = [(0, 0), (3, 0), (3, 1), (1, 1), (1, 2), (3, 2), (3, 3), (0, 3), (0, 0)]
        x, y = gb.representative_point([c_shape])
        self.assertTrue(gb.PolygonIndex([c_shape]).contains(x, y))

    def test_assemble_rings_joins_reversed_and_unordered_ways(self):
        ways = [([1, 2], [(0, 0), (1, 0)]), ([3, 2], [(1, 1), (1, 0)]), ([3, 4, 1], [(1, 1), (0, 1), (0, 0)]),
                ([8, 9], [(5, 5), (6, 6)])]
        rings, left = gb.assemble_rings(ways)
        self.assertEqual(len(rings), 1)
        self.assertEqual(left, 1)
        self.assertAlmostEqual(abs(gb.ring_area_centroid(rings[0])[0]), 1.0)

    def test_zones(self):
        z = zones()
        self.assertEqual(z.zone(42.3, 44.7), "occupied")
        self.assertEqual(z.zone(42.1995, 44.7), "buffer")
        self.assertEqual(z.zone(42.197, 44.7), "band")
        self.assertIsNone(z.zone(41.7, 44.3))
        self.assertEqual(z.zone(42.7, 44.5), "outside")

    def test_zones_from_geojson(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "z.geojson"
            write_zones(path)
            self.assertEqual(gb.Zones.from_geojson(path).zone(42.3, 44.7), "occupied")

    def test_cluster_points(self):
        groups = gb.cluster_points([(41.7, 44.3), (41.7005, 44.3), (41.8, 44.3)], 100)
        self.assertEqual(sorted(len(g) for g in groups), [1, 2])


class BuilderTest(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.db = Path(cls.tmp.name) / "geo.sqlite"
        cls.builder = build_db(cls.db)
        cls.con = sqlite3.connect(cls.db)
        cls.searcher = Searcher(str(cls.db))

    @classmethod
    def tearDownClass(cls):
        cls.con.close()
        cls.searcher.con.close()
        cls.tmp.cleanup()

    def q(self, sql, *args):
        return self.con.execute(sql, args).fetchall()

    def test_occupied_places_are_kept_and_flagged(self):
        rows = dict(self.q("SELECT name, occupied FROM places"))
        self.assertEqual(rows["Ленингор"], 1)
        self.assertEqual(rows["Сухум"], 1)          # in the 100 m buffer
        self.assertEqual(rows["თბილისი"], 0)
        self.assertEqual(self.q("SELECT zone FROM places WHERE name = 'Сухум'")[0][0], "buffer")
        self.assertEqual(self.q("SELECT zone FROM places WHERE name = 'ზოლი'")[0][0], "band")

    def test_nothing_else_inside_the_occupied_area_or_abroad(self):
        self.assertEqual(self.q("SELECT count(*) FROM streets WHERE name = 'проспект Мира'")[0][0], 0)
        self.assertEqual(self.q("SELECT count(*) FROM addresses WHERE housenumber = '1'")[0][0], 0)
        self.assertEqual(self.q("SELECT count(*) FROM pois WHERE kind IN ('hospital', 'border_control')")[0][0], 0)
        self.assertEqual(self.q("SELECT count(*) FROM places WHERE name = 'Russia village'")[0][0], 0)
        z = zones()
        for table in ("streets", "addresses", "pois"):
            for lat, lon in self.q(f"SELECT lat, lon FROM {table}"):
                self.assertNotIn(z.zone(lat, lon), ("occupied", "buffer", "outside"), table)

    def test_row_ids_rank_importance(self):
        first = self.q("SELECT name FROM places ORDER BY id LIMIT 1")[0][0]
        self.assertEqual(first, "თბილისი")
        imps = [r[0] for r in self.q("SELECT importance FROM streets ORDER BY id")]
        self.assertEqual(imps, sorted(imps, reverse=True))

    def test_district_gets_its_city_and_streets_their_settlement(self):
        city_id = self.q("SELECT id FROM places WHERE name = 'თბილისი'")[0][0]
        self.assertEqual(self.q("SELECT parent_id FROM places WHERE kind = 'neighbourhood'")[0][0], city_id)
        avenue = self.q("SELECT city_id, kind, length_m FROM streets WHERE name = 'შოთა რუსთაველის გამზირი'")
        self.assertEqual(len(avenue), 1)                       # two ways, one street
        self.assertEqual(avenue[0][0], city_id)
        self.assertEqual(avenue[0][1], "primary")              # construction=primary counts as primary
        self.assertGreater(avenue[0][2], 700)
        self.assertEqual(self.q("SELECT count(*) FROM streets WHERE name = 'ბაღის ბილიკი'")[0][0], 0)

    def test_addresses_merge_link_and_make_virtual_streets(self):
        rows = self.q("SELECT osm, street_id FROM addresses WHERE housenumber = '12'")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][0], "w4000")                  # the building won
        avenue = self.q("SELECT id FROM streets WHERE name = 'შოთა რუსთაველის გამზირი'")[0][0]
        self.assertEqual(rows[0][1], avenue)
        short = self.q("SELECT street_id FROM addresses WHERE housenumber = '39ა'")[0][0]
        self.assertEqual(short, avenue)                        # 'რუსთაველის გამზირი' found the avenue
        virtual = self.q("SELECT id, name_en FROM streets WHERE kind = 'virtual'")
        self.assertEqual(len(virtual), 1)
        self.assertEqual(virtual[0][1], "Akhali Street")
        self.assertEqual(len(self.q("SELECT id FROM addresses WHERE street_id = ?", virtual[0][0])), 2)

    def test_pois_merge_attrs_and_road_passes(self):
        fuel = self.q("SELECT brand, attrs FROM pois WHERE kind = 'fuel' AND brand = 'ვისოლი'")
        self.assertEqual(len(fuel), 1)                          # node and area merged
        self.assertIn("cng", fuel[0][1].split(";"))
        self.assertEqual(self.q("SELECT count(*) FROM pois WHERE kind = 'parking'")[0][0], 1)
        self.assertEqual(self.q("SELECT count(*) FROM pois WHERE kind = 'hotel' AND name IS NULL")[0][0], 0)
        passes = dict(self.q("SELECT osm, importance FROM pois WHERE kind = 'mountain_pass'"))
        self.assertGreater(passes["n58"], passes["n59"])       # on a road
        water = self.q("SELECT lat, lon FROM pois WHERE kind = 'water'")
        self.assertEqual(len(water), 1)
        self.assertTrue(41.74 < water[0][0] < 41.76 and 44.35 < water[0][1] < 44.37)

    def test_meta_carries_licence_and_rules(self):
        meta = dict(self.q("SELECT key, value FROM meta"))
        self.assertEqual(meta["licence"], "ODbL-1.0")
        self.assertIn("OpenStreetMap contributors", meta["attribution"])
        self.assertEqual(json.loads(meta["fold_json"])["version"], FOLD.version)
        self.assertEqual(meta["fts"], "fts5")
        self.assertEqual(gg.check_meta(self.con, gb.DEFAULT_FOLD_SPEC, gb.DEFAULT_CONFIG, "ab" * 32)[0], [])
        self.assertTrue(gg.check_meta(self.con, gb.DEFAULT_FOLD_SPEC, gb.DEFAULT_CONFIG, "cd" * 32)[0])

    def top(self, text, near=None):
        hits = self.searcher.search(text, near=near, limit=5)
        self.assertTrue(hits, f"nothing for {text!r}")
        return hits[0]

    def test_search_places_in_every_script(self):
        for text in ("თბილისი", "tbilisi", "Тбилиси", "TBILISI"):
            self.assertEqual(self.top(text)["name"], "თბილისი", text)
        top = self.top("ვაკე", near=TBILISI)
        self.assertEqual(top["kind"], "neighbourhood")          # the district beats the far village

    def test_search_occupied_places_are_flagged(self):
        top = self.top("akhalgori")
        self.assertEqual((top["table"], top["occupied"]), ("places", 1))
        top = self.top("Сухуми")
        self.assertEqual((top["table"], top["occupied"]), ("places", 1))
        for hit in self.searcher.search("проспект Мира", limit=10):
            self.assertEqual(hit["table"], "places")

    def test_search_streets_and_addresses(self):
        for text in ("rustaveli", "რუსთაველის გამზირი", "руставели", "rusTavelis gamziri"):
            top = self.top(text, near=TBILISI)
            self.assertEqual((top["table"], top["name"]), ("streets", "შოთა რუსთაველის გამზირი"), text)
        top = self.top("rustaveli 12", near=TBILISI)
        self.assertEqual((top["table"], top["housenumber"]), ("addresses", "12"))
        top = self.top("რუსთაველის 39 ა", near=TBILISI)
        self.assertEqual((top["table"], top["housenumber"]), ("addresses", "39ა"))
        top = self.top("batumi rustaveli")
        self.assertEqual((top["table"], top["name"]), ("streets", "შოთა რუსთაველის ქუჩა"))
        top = self.top("akhali 5")
        self.assertEqual((top["table"], top["housenumber"]), ("addresses", "5"))

    def test_place_beats_street_named_after_it(self):
        self.assertEqual(self.top("gudauri")["table"], "places")

    def test_search_pois_brands_and_categories(self):
        self.assertEqual(self.top("wissol", near=TBILISI)["kind"], "fuel")
        self.assertEqual(self.top("виссол", near=TBILISI)["kind"], "fuel")
        top = self.top("metani", near=TBILISI)
        self.assertIn("cng", top["attrs"].split(";"))
        self.assertEqual(self.top("parking", near=TBILISI)["kind"], "parking")
        self.assertEqual(self.top("ბენზინი", near=(41.6, 44.9))["brand"], "გალფი")
        self.assertEqual(self.top("თბილისის ზღვა")["kind"], "water")
        self.assertEqual(self.top("jvari pass")["osm"], "n58")

    def test_legal_rows_never_belong_to_an_occupied_settlement(self):
        occupied_city = self.q("SELECT id FROM places WHERE name_ka = 'ცხინვალი'")[0][0]
        street = self.q("SELECT city_id FROM streets WHERE name = 'გორის ქუჩა'")[0][0]
        address = self.q("SELECT city_id FROM addresses WHERE housenumber = '3'")[0][0]
        self.assertNotEqual(street, occupied_city)        # 9 km away, inside the city's 12 km reach
        self.assertNotEqual(address, occupied_city)       # although addr:city names it
        self.assertEqual(gg.check_occupied_links(self.con), [])
        # The old grid (every settlement) would have taken it.
        self.assertEqual(self.builder.settlement_of(42.17, 44.65, occupied=True),
                         next(i for i, r in enumerate(self.builder.places) if r["names"].main["name_ka"] == "ცხინვალი"))

    def test_occupied_places_are_labelled_from_name_ka(self):
        rows = {r[0]: r[1:] for r in self.q("SELECT name, label_ka, label_en FROM places WHERE occupied = 1")}
        self.assertEqual(rows["Ленингор"], ("ახალგორი", "Akhalgori"))
        self.assertEqual(rows["Цхинвал"], ("ცხინვალი", "Tskhinvali"))       # never name:en 'Tskhinval'
        self.assertNotIn("Аҷара", rows)                                       # no name:ka: left out
        self.assertEqual(self.builder.stats["places_occupied_without_name_ka_hidden"], 1)
        self.assertEqual(gg.check_labels(self.con, CONFIG, FOLD.spec)[0], [])
        legal = dict((r[0], r[1:]) for r in self.q("SELECT name, label_ka, label_en FROM places WHERE occupied = 0"))
        self.assertEqual(legal["თბილისი"], ("თბილისი", "Tbilisi"))
        hit = self.top("ცხინვალი")
        self.assertEqual((hit["occupied"], hit["routable"], hit["reason"]), (1, False, "occupied"))
        self.assertEqual(label(hit), "ცხინვალი")
        hit = self.top("Сухуми")
        self.assertEqual((hit["routable"], hit["reason"]), (False, "occupation_line"))   # the 100 m buffer

    def test_checkpoints_at_the_line_are_no_border(self):
        kinds = dict(self.q("SELECT kind, count(*) FROM pois WHERE kind IN ('border_control', 'line_checkpoint') "
                            "GROUP BY kind"))
        self.assertEqual(kinds, {"line_checkpoint": 1})
        self.assertEqual(self.builder.stats["pois_dropped_line_checkpoint_unnamed"], 1)
        for hit in self.searcher.search("border", near=(42.18, 44.70), limit=10):
            self.assertNotEqual(hit["kind"], "line_checkpoint")

    def test_aliases(self):
        top = self.top("kazbegi")
        self.assertEqual((top["table"], top["label_ka"]), ("places", "სტეფანწმინდა"))
        self.assertEqual(self.top("Казбеги")["label_ka"], "სტეფანწმინდა")

    def test_category_with_a_city_in_the_genitive(self):
        near = TBILISI
        self.assertEqual(self.top("ბათუმის აეროპორტი", near)["name"], "ბათუმის საერთაშორისო აეროპორტი")
        self.assertEqual(self.top("Batumi International Airport", near)["name"], "ბათუმის საერთაშორისო აეროპორტი")
        self.assertEqual(self.top("Tbilisi International Airport", (41.6, 44.9))["name"],
                         "თბილისის საერთაშორისო აეროპორტი")
        self.assertEqual(self.top("ბათუმის სასტუმრო", near)["name"], "სასტუმრო ზღვა")
        self.assertEqual(self.top("აეროპორტი", near)["name"], "თბილისის საერთაშორისო აეროპორტი")

    def test_occupied_place_named_beside_other_words(self):
        for text in ("rustaveli ცხინვალი", "ბენზინი ცხინვალი", "hospital tskhinvali", "ცხინვალის აეროპორტი"):
            hits = self.searcher.search(text, near=TBILISI, limit=5)
            self.assertEqual((hits[0]["table"], hits[0]["occupied"], hits[0].get("anchor")),
                             ("places", 1, "occupied"), text)
            self.assertFalse(hits[0]["routable"])
            for hit in hits[1:]:
                self.assertTrue(hit["routable"], text)

    def test_pasted_addresses(self):
        near = TBILISI
        top = self.top("12 Shota Rustaveli Ave, Tbilisi 0108, Georgia", near)
        self.assertEqual((top["table"], top["housenumber"]), ("addresses", "12"))
        top = self.top("შოთა რუსთაველის გამზ. N12, ბინა 5, საქართველო", near)
        self.assertEqual((top["table"], top["housenumber"]), ("addresses", "12"))
        for text in ("Batumi, Georgia", "ბათუმი, საქართველო", "ბათუმში"):
            top = self.top(text, near)
            self.assertEqual((top["table"], top["name"]), ("places", "ბათუმი"), text)

    def test_surnames_types_and_initials(self):
        top = self.top("tsereteli", TBILISI)                 # the avenue, not the lane or the village
        self.assertEqual(top["name"], "აკაკი წერეთლის გამზირი")
        self.assertEqual(self.top("gudauri")["table"], "places")   # a street named only after it still yields
        top = self.top("isnis kucha", TBILISI)                # the street the query names, not the district
        self.assertEqual((top["table"], top["name"]), ("streets", "ისნის ქუჩა"))
        self.assertEqual(self.top("ვაჟაფშაველას", TBILISI)["name"], "ვაჟა-ფშაველას გამზირი")

    def test_park_is_no_parking(self):
        for text in ("პარკი", "ვაკის პარკი", "парк"):
            for hit in self.searcher.search(text, near=TBILISI, limit=3):
                self.assertNotEqual(hit.get("category"), "parking", text)
        self.assertEqual(self.top("ვაკის პარკი", TBILISI)["name"], "ვაკის პარკი")
        self.assertEqual(self.top("parkin", TBILISI)["kind"], "parking")   # still being typed

    def test_a_word_that_matches_nothing_is_left_out(self):
        hits = self.searcher.search("rustaveli qwzxv", near=TBILISI, limit=3)
        self.assertTrue(hits)
        self.assertEqual((hits[0]["name"], hits[0]["partial"], hits[0]["ignored"]),
                         ("შოთა რუსთაველის გამზირი", True, ["qwzxv"]))
        self.assertNotIn("partial", self.top("rustaveli", TBILISI))

    def test_fts4_expressions_stay_bounded(self):
        q = FOLD.parse_query("Rustaveli Tsereteli Zugdidi Sokhumi")
        self.assertTrue(all(len(t.prefixes) <= 2 for t in q.required))   # no leading-capital chat readings
        s4 = Searcher.__new__(Searcher)
        s4.fts = "fts4"
        exprs = s4.match_expressions(FOLD.parse_query("ქუთაისში ბათუმში თბილისიდან ვაკეში").required)
        self.assertLessEqual(len(exprs), 32)
        self.assertEqual(exprs[0], "kutaish* batumsh* tbilisidan* vakesh*")   # the plain readings first

    def test_fts4_build_gives_the_same_answers(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "geo4.sqlite"
            build_db(db, engine="fts4")
            s4 = Searcher(str(db))
            try:
                for text, near in (("rustaveli", TBILISI), ("rustaveli 12", TBILISI), ("Сухуми", None),
                                   ("wissol", TBILISI), ("Sota rusTaveli", TBILISI)):
                    a = self.searcher.search(text, near=near, limit=3)
                    b = s4.search(text, near=near, limit=3)
                    self.assertEqual([(h["table"], h["id"]) for h in a], [(h["table"], h["id"]) for h in b], text)
            finally:
                s4.con.close()


def rules_collector():
    """A second made-up country for the ranking rules of config v3, shaped
    after the five known queries the first full-Georgia run failed
    ('gudauri', 'sarpi', 'ერედვი', 'ჭავჭავაძის 37', 'თბილისის ზღვა'), but
    with other coordinates and every number made up."""
    c = gb.Collector(CONFIG)
    node, way = c.node, c.way

    def line(way_id, tags, *points):
        way(way_id, tags, list(range(way_id * 10, way_id * 10 + len(points))), [(lon, lat) for lat, lon in points])

    def area(way_id, tags, lat0, lon0, lat1, lon1):
        refs = [way_id * 10 + i for i in range(4)]
        way(way_id, tags, refs + refs[:1], box(lon0, lat0, lon1, lat1))

    # Settlements: the capital, a regional capital, a town, villages, a
    # hamlet, a legal locality and an occupied locality (inside the zone).
    node(1, {"place": "city", "name": "თბილისი", "name:en": "Tbilisi", "population": "1300000",
             "capital": "yes", "wikidata": "Q994"}, 41.70, 44.30)
    node(2, {"place": "city", "name": "ქუთაისი", "name:en": "Kutaisi", "population": "130000",
             "capital": "4", "wikidata": "Q1024"}, 42.00, 44.10)
    node(3, {"place": "town", "name": "ფოთი", "name:en": "Poti", "population": "41000"}, 41.95, 44.45)
    node(4, {"place": "village", "name": "სარფი", "name:en": "Sarpi", "population": "785",
             "wikidata": "Q2302798"}, 41.55, 44.95)
    node(5, {"place": "village", "name": "გუდაური", "name:en": "Gudauri", "population": "54",
             "wikidata": "Q1553013"}, 42.10, 44.30)
    node(6, {"place": "locality", "name": "Ирыхъæу", "name:ka": "ერედვი", "name:en": "Eredvi",
             "population": "0", "wikidata": "Q3650323"}, 42.25, 44.65)
    node(7, {"place": "hamlet", "name": "ხეითი", "name:en": "Kheiti"}, 42.13, 44.52)
    node(8, {"place": "locality", "name": "ბოდორნა", "name:en": "Bodorna"}, 41.90, 44.60)
    node(9, {"place": "village", "name": "წერეთელი", "name:en": "Tsereteli", "population": "900"}, 41.55, 44.50)
    # 'sarpi': a road named after the village, a motorway that lists it among
    # the towns it links (far away, near Poti), the crossing at the state
    # border (name:en only) and the foreign post across it.
    line(100, {"highway": "trunk", "name": "სარფი"}, (41.549, 44.96), (41.5495, 44.97))
    route = {"highway": "motorway", "name": "სენაკი — ფოთი — სარფი", "name:en": "Senaki — Poti — Sarpi",
             "name:ru": "Сенаки — Поти — Сарпи"}
    line(101, route, (41.95, 44.40), (41.95, 44.45))
    line(102, dict(route), (41.95, 44.45), (41.95, 44.50))
    node(103, {"barrier": "border_control", "name:en": "Sarpi Border Control"}, 41.551, 44.995)
    node(104, {"barrier": "border_control", "name": "Sarp Hudut Kapısı"}, 41.551, 45.005)   # abroad
    # 'gudauri': an access road 5 km away under construction, the ski area, a resort.
    line(110, {"highway": "construction", "construction": "secondary", "name": "გუდაურთან მისასვლელი",
               "name:en": "Gudauri Access Road"}, (42.06, 44.33), (42.05, 44.36))
    area(111, {"landuse": "winter_sports", "name": "Gudauri Ski Resort"}, 42.115, 44.31, 42.125, 44.33)
    node(112, {"leisure": "resort", "name": "Gudauriski"}, 42.095, 44.31)
    node(113, {"tourism": "resort", "name": "ბახმარო რეზორტი", "name:en": "Bakhmaro Resort"}, 41.85, 44.20)
    # 'ერედვი': streets inside the zone (dropped), a legal road listing it
    # 14 km away (hyphens without spaces), a street in the capital named after it.
    line(120, {"highway": "residential", "name": "ერედვის ქუჩა"}, (42.25, 44.651), (42.251, 44.652))
    line(121, {"highway": "tertiary", "name": "ტირძნისი-დიცი-ერედვი-ხეითი"}, (42.10, 44.62), (42.12, 44.63))
    line(122, {"highway": "residential", "name": "ერედვის ქუჩა", "name:en": "Eredvi Street"},
         (41.71, 44.31), (41.711, 44.312))
    # A legal locality and a road near it that has its name among other words.
    line(125, {"highway": "primary", "name": "ახალი ბოდორნის გზატკეცილი"}, (41.92, 44.60), (41.93, 44.66))
    # A person-named avenue 24 km from a village of that surname keeps its rank.
    line(126, {"highway": "primary", "name": "აკაკი წერეთლის გამზირი", "name:en": "Akaki Tsereteli Avenue"},
         (41.720, 44.320), (41.722, 44.330))
    # 'თბილისის ზღვა': the reservoir (alt name), a primary street in the
    # capital whose old name has 'ზღვა', a street named after the reservoir;
    # a landuse=reservoir and a water=lake without natural=water.
    c.relation(130, {"type": "multipolygon", "natural": "water", "water": "reservoir",
                     "name": "თბილისის წყალსაცავი", "alt_name:ka": "თბილისის ზღვა", "name:en": "Tbilisi reservoir",
                     "wikidata": "Q1899389"}, [("w", 131, "outer"), ("w", 132, "outer")])
    way(131, {}, [1310, 1311, 1312], [(44.35, 41.74), (44.37, 41.74), (44.37, 41.76)])
    way(132, {}, [1312, 1313, 1310], [(44.37, 41.76), (44.35, 41.76), (44.35, 41.74)])
    line(133, {"highway": "primary", "name": "ლეხ კაჩინსკის ქუჩა", "name:en": "Lech Kaczyński Street",
               "old_name": "შავი ზღვის ქუჩა", "old_name:en": "Shavi Zghva Street;Black Sea Street"},
         (41.690, 44.28), (41.690, 44.31))
    line(134, {"highway": "residential", "name": "თბილისის ზღვის ქუჩა", "name:en": "Tbilisi Zghvi Street"},
         (41.765, 44.36), (41.766, 44.362))
    area(135, {"landuse": "reservoir", "name": "ჟინვალის წყალსაცავი"}, 42.20, 44.10, 42.22, 44.13)
    area(136, {"water": "lake", "name": "ლისის ტბა"}, 41.74, 44.20, 41.745, 44.205)
    area(137, {"natural": "water", "water": "river", "name": "მტკვარი"}, 41.68, 44.28, 41.685, 44.32)
    # 'ჭავჭავაძის 37': a short tertiary avenue in the capital and a long
    # primary one in Kutaisi, each with a number 37.
    line(140, {"highway": "tertiary", "name": "ილია ჭავჭავაძის გამზირი"}, (41.705, 44.270), (41.706, 44.275))
    node(141, {"addr:housenumber": "37", "addr:street": "ილია ჭავჭავაძის გამზირი"}, 41.7052, 44.2705)
    line(142, {"highway": "primary", "name": "ილია ჭავჭავაძის გამზირი"}, (42.000, 44.080), (42.000, 44.140))
    node(143, {"addr:housenumber": "37", "addr:street": "ილია ჭავჭავაძის გამზირი"}, 42.0002, 44.1001)
    # 'ვაჟა-ფშაველას 70': the capital has the avenue, only Kutaisi has number 70.
    line(144, {"highway": "primary", "name": "ვაჟა-ფშაველას გამზირი"}, (41.725, 44.25), (41.726, 44.27))
    line(145, {"highway": "residential", "name": "ვაჟა-ფშაველას ქუჩა"}, (42.005, 44.09), (42.006, 44.092))
    node(146, {"addr:housenumber": "70", "addr:street": "ვაჟა-ფშაველას ქუჩა"}, 42.0052, 44.0905)
    # Only another city has the exact number: the capital has 49ა, 20-22
    # and '166 კორპ. 8' on its street of that name, Kutaisi 49, 22 and 8.
    line(147, {"highway": "residential", "name": "ოთარ ჭილაძის ქუჩა"}, (41.715, 44.280), (41.716, 44.285))
    for i, (number, lon) in enumerate((("49ა", 44.2805), ("20-22", 44.2815), ("166 კორპ. 8", 44.2825))):
        node(1470 + i, {"addr:housenumber": number, "addr:street": "ოთარ ჭილაძის ქუჩა"}, 41.7152, lon)
    line(148, {"highway": "residential", "name": "ოთარ ჭილაძის ქუჩა"}, (42.010, 44.120), (42.011, 44.125))
    for i, (number, lon) in enumerate((("49", 44.1205), ("22", 44.1215), ("8", 44.1225))):
        node(1480 + i, {"addr:housenumber": number, "addr:street": "ოთარ ჭილაძის ქუჩა"}, 42.0102, lon)
    # Old names: the capital's secondary street was Lermontov street and
    # only Kutaisi has one now; the capital's former Pushkin street is
    # secondary, its Pushkin street now residential.
    line(149, {"highway": "secondary", "name": "გიგა ლორთქიფანიძის ქუჩა", "old_name": "მიხეილ ლერმონტოვის ქუჩა"},
         (41.720, 44.290), (41.721, 44.295))
    line(150, {"highway": "tertiary", "name": "მიხეილ ლერმონტოვის ქუჩა"}, (42.020, 44.100), (42.021, 44.105))
    line(151, {"highway": "secondary", "name": "ნიკო ნიკოლაძის ქუჩა", "old_name": "ალექსანდრე პუშკინის ქუჩა"},
         (41.725, 44.290), (41.726, 44.295))
    line(152, {"highway": "residential", "name": "ალექსანდრე პუშკინის ქუჩა"}, (41.730, 44.290), (41.731, 44.293))
    line(153, {"highway": "tertiary", "name": "ალექსანდრე პუშკინის ქუჩა"}, (42.025, 44.100), (42.026, 44.105))
    # A district of a far town named after a person, and Kutaisi's street
    # of that person.
    node(10, {"place": "suburb", "name": "შოთა რუსთაველის დასახლება"}, 41.56, 44.80)
    line(154, {"highway": "secondary", "name": "შოთა რუსთაველის ქუჩა", "name:en": "Shota Rustaveli Street"},
         (42.010, 44.100), (42.012, 44.110))
    # A hotel in the capital that shares the name of a far hamlet, one
    # with the name of a town, and a street that only begins with a far
    # village's name.
    node(11, {"place": "hamlet", "name": "ალმა", "name:en": "Alma"}, 42.45, 44.05)
    node(155, {"tourism": "hotel", "name": "ალმა"}, 41.702, 44.305)
    node(156, {"tourism": "hotel", "name": "ფოთი"}, 41.703, 44.306)
    node(12, {"place": "village", "name": "გული", "population": "200"}, 42.40, 44.20)
    line(157, {"highway": "residential", "name": "გულნარას ქუჩა"}, (41.705, 44.300), (41.706, 44.302))
    # An occupied village whose name a square of the capital has.
    node(13, {"place": "village", "name": "Тависуплеба", "name:ka": "თავისუფლება", "population": "300"},
         42.35, 44.75)
    line(158, {"highway": "primary", "name": "თავისუფლების მოედანი", "name:en": "Freedom Square"},
         (41.695, 44.300), (41.696, 44.302))
    # Newly indexed kinds inside the occupied area and its 100 m buffer
    # (all dropped), and a named crossing 1 km outside the line (no border).
    area(170, {"landuse": "reservoir", "name": "ზონის წყალსაცავი"}, 42.30, 44.70, 42.31, 44.71)
    area(171, {"water": "lake", "name": "ზონის ტბა"}, 42.32, 44.70, 42.33, 44.71)
    area(172, {"natural": "water", "water": "reservoir", "name": "ზონის ზღვა"}, 42.34, 44.70, 42.35, 44.71)
    node(173, {"leisure": "resort", "name": "ზონის კურორტი"}, 42.30, 44.75)
    area(174, {"tourism": "resort", "name": "ზონის სასტუმრო კომპლექსი"}, 42.36, 44.72, 42.37, 44.73)
    node(175, {"barrier": "border_control", "name": "ზონის საზღვარი"}, 42.30, 44.65)
    node(176, {"leisure": "resort", "name": "ბუფერის კურორტი"}, 42.1995, 44.70)
    node(177, {"barrier": "border_control", "name": "ხაზის პუნქტი"}, 42.19, 44.70)
    # Occupied and legal places of one name: of like importance, and an
    # occupied village far bigger than the legal hamlet.
    node(14, {"place": "village", "name": "Ахалсопели", "name:ka": "ახალსოფელი", "population": "300"}, 42.28, 44.72)
    node(15, {"place": "village", "name": "ახალსოფელი", "population": "300"}, 41.85, 44.20)
    node(16, {"place": "village", "name": "Колхида", "name:ka": "კოლხიდა", "population": "2500", "wikidata": "Q1"},
         42.25, 44.72)
    node(17, {"place": "hamlet", "name": "კოლხიდა"}, 41.80, 44.60)
    c.finish_relations()
    return c


class RankingRulesTest(unittest.TestCase):
    """Config v3: an exact place or feature before a street that merely
    has its name; the capital first for a street or address without a
    position or settlement; water, resorts, localities and border
    crossings are indexed."""

    KUTAISI = (42.00, 44.10)
    FAR = (42.45, 44.95)          # far from the capital and from ერედვი's roads

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.db = Path(cls.tmp.name) / "rules.sqlite"
        cls.builder = gb.Builder(CONFIG, FOLD, zones(), rules_collector()).build()
        gb.write_database(cls.db, cls.builder, gb.base_meta(gb.DEFAULT_CONFIG, gb.DEFAULT_FOLD_SPEC, FOLD, CONFIG))
        cls.con = sqlite3.connect(cls.db)
        cls.searcher = Searcher(str(cls.db))

    @classmethod
    def tearDownClass(cls):
        cls.con.close()
        cls.searcher.con.close()
        cls.tmp.cleanup()

    def hits(self, text, near=None, limit=10):
        hits = self.searcher.search(text, near=near, limit=limit)
        self.assertTrue(hits, f"nothing for {text!r}")
        return hits

    def test_exact_village_beats_a_route_that_lists_it(self):
        hits = self.hits("sarpi")
        self.assertEqual((hits[0]["table"], hits[0]["label_ka"]), ("places", "სარფი"))
        streets = [h for h in hits if h["table"] == "streets"]
        self.assertEqual({h["name"] for h in streets}, {"სარფი", "სენაკი — ფოთი — სარფი"})   # still offered, below
        self.assertTrue(all(h["score"] < hits[0]["score"] for h in hits[1:]))
        self.assertEqual(self.hits("სარფი")[0]["label_ka"], "სარფი")

    def test_exact_village_beats_a_street_near_it(self):
        for near in (None, TBILISI):
            hits = self.hits("gudauri", near)
            self.assertEqual((hits[0]["table"], hits[0]["kind"]), ("places", "village"), near)
            road = next(h for h in hits if h["table"] == "streets")
            self.assertEqual(road["name"], "გუდაურთან მისასვლელი")

    def test_exact_locality_and_hamlet_beat_streets(self):
        self.assertEqual(self.q("SELECT kind FROM places WHERE name = 'ბოდორნა'"), [("locality",)])
        self.assertEqual(self.q("SELECT kind FROM places WHERE name = 'ხეითი'"), [("hamlet",)])
        top = self.hits("ბოდორნა")[0]
        self.assertEqual((top["table"], top["kind"]), ("places", "locality"))
        top = self.hits("kheiti")[0]                     # the road 'ტირძნისი-დიცი-ერედვი-ხეითი' lists it
        self.assertEqual((top["table"], top["kind"]), ("places", "hamlet"))
        # A street-type word asks for the street.
        self.assertEqual(self.hits("ბოდორნის გზატკეცილი")[0]["table"], "streets")

    def test_occupied_place_stays_and_beats_streets_named_after_it(self):
        rows = self.q("SELECT kind, occupied, zone, label_ka FROM places WHERE name_ka = 'ერედვი'")
        self.assertEqual(rows, [("locality", 1, "occupied", "ერედვი")])
        streets = [r[0] for r in self.q("SELECT lat FROM streets WHERE name = 'ერედვის ქუჩა'")]
        self.assertEqual(len(streets), 1)                                   # the one in the zone is gone
        self.assertLess(streets[0], 42.0)
        for text, near in (("ერედვი", None), ("eredvi", None), ("ერედვი", self.FAR), ("Эредви", (42.10, 44.62))):
            hits = self.hits(text, near)
            self.assertEqual((hits[0]["table"], hits[0].get("occupied"), hits[0]["routable"]), ("places", 1, False),
                             (text, near))
            self.assertEqual(label(hits[0]), "ერედვი")
            for hit in hits[1:]:
                self.assertTrue(hit["routable"], text)

    def test_named_water_beats_a_street_found_through_the_city(self):
        hits = self.hits("თბილისის ზღვა")
        self.assertEqual((hits[0]["table"], hits[0]["kind"]), ("pois", "water"))
        self.assertEqual(hits[0]["name"], "თბილისის წყალსაცავი")
        names = [h["name"] for h in hits]
        self.assertIn("ლეხ კაჩინსკის ქუჩა", names)                    # 'ზღვა' in Tbilisi, still offered
        street = hits[names.index("ლეხ კაჩინსკის ქუჩა")]
        self.assertGreater(names.index("ლეხ კაჩინსკის ქუჩა"), 0)      # below the reservoir
        self.assertLess(street["score"], hits[0]["score"])
        self.assertEqual(self.hits("თბილისის ზღვა", TBILISI)[0]["kind"], "water")
        # Without the whole name, the reading 'ზღვა in Tbilisi' still works.
        self.assertEqual(self.hits("შავი ზღვის ქუჩა თბილისი")[0]["name"], "ლეხ კაჩინსკის ქუჩა")

    def test_water_bodies_are_indexed(self):
        water = dict(self.q("SELECT name, kind FROM pois WHERE kind = 'water'"))
        self.assertEqual(set(water), {"თბილისის წყალსაცავი", "ჟინვალის წყალსაცავი", "ლისის ტბა"})   # no river
        self.assertEqual(self.hits("ჟინვალის წყალსაცავი")[0]["kind"], "water")
        self.assertEqual(self.hits("zhinvali")[0]["kind"], "water")

    def test_resorts_and_border_crossings_are_indexed(self):
        resorts = {r[0] for r in self.q("SELECT name FROM pois WHERE kind = 'resort'")}
        self.assertEqual(resorts, {"Gudauriski", "ბახმარო რეზორტი"})
        crossing = self.q("SELECT name_en, label_ka FROM pois WHERE kind = 'border_control'")
        self.assertEqual(crossing, [("Sarpi Border Control", "Sarpi Border Control")])   # the foreign post is gone
        self.assertEqual(self.hits("gudauriski")[0]["kind"], "resort")
        self.assertEqual(self.hits("Bakhmaro Resort")[0]["kind"], "resort")
        self.assertEqual(self.hits("sarpi border control")[0]["kind"], "border_control")
        self.assertEqual(self.hits("საბაჟო სარფი")[0]["kind"], "border_control")       # category + village
        self.assertIn("border_control", [h["kind"] for h in self.hits("sarpi")])

    def test_person_named_avenue_far_from_a_village_keeps_its_rank(self):
        for near in (None, TBILISI):
            self.assertEqual(self.hits("tsereteli", near)[0]["name"], "აკაკი წერეთლის გამზირი", near)

    def test_address_without_city_or_position_prefers_the_capital(self):
        top = self.hits("ჭავჭავაძის 37")[0]
        self.assertEqual((top["table"], top["housenumber"]), ("addresses", "37"))
        self.assertLess(top["lat"], 41.8)                                    # Tbilisi, not Kutaisi
        top = self.hits("chavchavadze")[0]
        self.assertEqual((top["table"], top["kind"]), ("streets", "tertiary"))   # the capital's avenue
        # A position or a named settlement decides instead.
        self.assertGreater(self.hits("ჭავჭავაძის 37", (42.0, 44.1))[0]["lat"], 41.9)
        self.assertGreater(self.hits("ჭავჭავაძის 37, ქუთაისი")[0]["lat"], 41.9)
        self.assertGreater(self.hits("ქუთაისი ჭავჭავაძის 37")[0]["lat"], 41.9)
        # Per table: an address that has the number beats the capital's street without it.
        top = self.hits("ვაჟა-ფშაველას 70")[0]
        self.assertEqual((top["table"], top["housenumber"]), ("addresses", "70"))
        self.assertEqual(self.hits("ვაჟა-ფშაველას")[0]["kind"], "primary")      # the capital's avenue

    def test_occupied_place_does_not_hide_a_street_near_the_user(self):
        # 'თავისუფლება' in Tbilisi is Freedom Square, not the occupied village.
        for text in ("ერედვი", "თავისუფლება"):
            hits = self.hits(text, TBILISI)
            self.assertEqual((hits[0]["table"], hits[0]["routable"]), ("streets", True), text)
            occupied = [h for h in hits if h.get("occupied")]
            self.assertEqual(len(occupied), 1, text)                       # still offered, flagged
            self.assertFalse(occupied[0]["routable"])
            # Without a position, or far from the street, it explains first.
            for near in (None, self.FAR):
                top = self.hits(text, near)[0]
                self.assertEqual((top["table"], top.get("occupied")), ("places", 1), (text, near))
        # A road that lists it stays below it even beside the user.
        hits = self.hits("ერედვი", (42.10, 44.62))
        self.assertEqual(hits[0].get("occupied"), 1)
        self.assertEqual(hits[1]["name"], "ტირძნისი-დიცი-ერედვი-ხეითი")

    def test_district_named_after_a_person_holds_no_street(self):
        # A district of a far town holds no street near the user (it only
        # takes street_named_like_place, as a village would).
        for text in ("შოთა რუსთაველის", "shota rustavelis"):
            hits = self.hits(text, self.KUTAISI)
            self.assertEqual((hits[0]["table"], hits[0]["name"]), ("streets", "შოთა რუსთაველის ქუჩა"), text)
            self.assertIn("suburb", [h["kind"] for h in hits])             # the far district, below
            self.assertIn("შოთა რუსთაველის ქუჩა", [h["name"] for h in self.hits(text)])

    def test_poi_named_like_a_far_place_keeps_its_rank(self):
        hits = self.hits("ალმა", TBILISI)
        self.assertEqual((hits[0]["table"], hits[0]["kind"]), ("pois", "hotel"))
        self.assertIn("hamlet", [h["kind"] for h in hits])
        # A city or town still comes before every POI named after it.
        hits = self.hits("ფოთი", TBILISI)
        self.assertEqual((hits[0]["table"], hits[0]["kind"]), ("places", "town"))
        self.assertEqual((hits[1]["table"], hits[1]["kind"]), ("pois", "hotel"))

    def test_named_after_needs_the_word_or_its_stem(self):
        s = self.searcher

        def named(text, name):
            row = s.con.execute("SELECT * FROM streets WHERE name = ?", (name,)).fetchone()
            return s.named_as("streets", row, s.fold.parse_query(text).required, stem=True)

        # The noise word 'georgia' (Russian 'Георгия') is a word of a name here.
        self.assertFalse(s.words_are(s.keys("площадь Георгия Саакадзе"), s.fold.parse_query("саакадзе").required,
                                     stem=True))

        self.assertTrue(named("ერედვი", "ერედვის ქუჩა"))
        self.assertTrue(named("eredvi", "ერედვის ქუჩა"))
        self.assertTrue(named("sarpi", "სარფი"))
        self.assertFalse(named("გული", "გულნარას ქუჩა"))             # only begins with it
        self.assertFalse(named("tsereteli", "აკაკი წერეთლის გამზირი"))  # a given name besides it

    def test_address_with_the_exact_number_only_in_another_city(self):
        # The capital has 49ა, 20-22 and '166 კორპ. 8'; only Kutaisi has 49, 22 and 8.
        for number in ("49", "22", "8"):
            top = self.hits(f"ჭილაძის {number}")[0]
            self.assertEqual((top["table"], top["housenumber"]), ("addresses", number), number)
            self.assertGreater(top["lat"], 41.9, number)
        top = self.hits("ჭილაძის 49ა")[0]
        self.assertEqual((top["housenumber"], top["lat"] < 41.8), ("49ა", True))
        top = self.hits("ჭილაძის 20-22")[0]
        self.assertEqual((top["housenumber"], top["lat"] < 41.8), ("20-22", True))
        # When the capital has the number too, the capital's (the 37 test above).

    def test_current_name_beats_an_old_name(self):
        # Only Kutaisi has a Lermontov street now; the capital's was renamed.
        for text in ("მიხეილ ლერმონტოვის", "lermontovis"):
            top = self.hits(text)[0]
            self.assertEqual((top["name"], top["lat"] > 41.9), ("მიხეილ ლერმონტოვის ქუჩა", True), text)
        # A position decides instead: the old name still finds the street.
        self.assertEqual(self.hits("lermontovis", TBILISI)[0]["name"], "გიგა ლორთქიფანიძის ქუჩა")
        # The capital has a Pushkin street now and a secondary one that was.
        for text in ("პუშკინის", "ალექსანდრე პუშკინის"):
            hits = self.hits(text)
            self.assertEqual((hits[0]["name"], hits[0]["kind"]), ("ალექსანდრე პუშკინის ქუჩა", "residential"), text)
            self.assertIn("ნიკო ნიკოლაძის ქუჩა", [h["name"] for h in hits])

    def test_new_kinds_inside_the_occupied_area_are_dropped(self):
        names = {r[0] for r in self.q("SELECT name FROM pois")}
        for name in ("ზონის წყალსაცავი", "ზონის ტბა", "ზონის ზღვა", "ზონის კურორტი", "ზონის სასტუმრო კომპლექსი",
                     "ზონის საზღვარი", "ბუფერის კურორტი"):
            self.assertNotIn(name, names)
        self.assertEqual(self.builder.stats["pois_dropped_occupied"], 6)
        self.assertEqual(self.builder.stats["pois_dropped_buffer"], 1)
        z = zones()
        for table in ("streets", "addresses", "pois"):
            for lat, lon in self.q(f"SELECT lat, lon FROM {table}"):
                self.assertNotIn(z.zone(lat, lon), ("occupied", "buffer", "outside"), table)
        # A crossing at the line is a line_checkpoint: no border, and no exact feature.
        self.assertEqual(self.q("SELECT kind FROM pois WHERE name = 'ხაზის პუნქტი'"), [("line_checkpoint",)])
        self.assertNotIn("line_checkpoint", CONFIG["ranking"]["exact_first"]["feature_kinds"])
        self.assertEqual([h for h in self.searcher.search("ზონის ტბა", limit=10) if "ზონის" in (h["name"] or "")], [])

    def test_occupied_and_legal_places_of_one_name(self):
        hits = self.hits("ახალსოფელი")                                     # like importance: the legal one
        self.assertEqual((hits[0]["occupied"], hits[1]["occupied"]), (0, 1))
        hits = self.hits("კოლხიდა")                                        # a far bigger occupied village
        self.assertEqual((hits[0]["occupied"], hits[0]["routable"], hits[1]["kind"]), (1, False, "hamlet"))

    def q(self, sql, *args):
        return self.con.execute(sql, args).fetchall()


def poi_collector():
    """A third made-up country for the POI kinds of config v4, shaped after
    what the second simulator drive missed ('rustavelis teatri' found only
    the university next door, 'eastpoint', 'ამირანი კინო', the Public
    Service Hall), with the traps the category anchor would fall into (a
    suburb called რუსთაველი, a street of a man called Amiran). Every name
    and number is made up."""
    c = gb.Collector(CONFIG)
    node, way = c.node, c.way

    def line(way_id, tags, *points):
        way(way_id, tags, list(range(way_id * 10, way_id * 10 + len(points))), [(lon, lat) for lat, lon in points])

    def area(way_id, tags, lat0, lon0, lat1, lon1):
        refs = [way_id * 10 + i for i in range(4)]
        way(way_id, tags, refs + refs[:1], box(lon0, lat0, lon1, lat1))

    node(1, {"place": "city", "name": "თბილისი", "name:en": "Tbilisi", "population": "1300000", "capital": "yes"},
         41.70, 44.30)
    node(2, {"place": "suburb", "name": "რუსთაველი", "name:en": "Rustaveli"}, 41.74, 44.36)
    node(3, {"place": "town", "name": "ბათუმი", "name:en": "Batumi", "population": "170000"}, 41.60, 44.90)
    line(100, {"highway": "primary", "name": "შოთა რუსთაველის გამზირი", "name:en": "Shota Rustaveli Avenue"},
         (41.700, 44.300), (41.706, 44.306))
    line(101, {"highway": "residential", "name": "ამირან ფანცულაიას ქუჩა"}, (41.75, 44.40), (41.751, 44.401))
    line(102, {"highway": "residential", "name": "სკოლის ქუჩა", "name:en": "School Street"},
         (41.72, 44.33), (41.721, 44.331))
    # Culture on and near the avenue: the theatre, the opera nearer to the
    # avenue's middle, the university next door; a cinema whose name lacks
    # its kind; a museum in a castle, a gallery, a library.
    c.relation(200, {"type": "multipolygon", "amenity": "theatre", "name": "რუსთაველის თეატრი",
                     "name:en": "Rustaveli National Theatre", "wikidata": "Q1860439"}, [("w", 201, "outer")])
    way(201, {}, [2010, 2011, 2012, 2013, 2010], box(44.3012, 41.7008, 44.3016, 41.7011))
    area(202, {"amenity": "theatre", "name": "ოპერისა და ბალეტის თეატრი", "name:en": "Opera and Ballet Theatre",
               "wikidata": "Q2596393"}, 41.7030, 44.3030, 41.7033, 44.3033)
    node(203, {"amenity": "university", "name": "შოთა რუსთაველის თეატრისა და კინოს უნივერსიტეტი",
               "name:en": "Shota Rustaveli Theatre and Film University"}, 41.7010, 44.3005)
    node(204, {"amenity": "cinema", "name": "ამირანი", "name:en": "Amirani", "brand": "კავეა"}, 41.708, 44.285)
    node(205, {"amenity": "cinema", "name": "აპოლო", "name:en": "Apollo"}, 41.702, 44.302)
    node(206, {"tourism": "museum", "historic": "castle", "name": "საქართველოს ეროვნული მუზეუმი",
               "name:en": "Georgian National Museum", "wikidata": "Q1386417"}, 41.6960, 44.2990)
    node(207, {"tourism": "gallery", "name": "ეროვნული გალერეა"}, 41.6990, 44.2980)
    node(208, {"amenity": "library", "name": "ეროვნული ბიბლიოთეკა"}, 41.6980, 44.2970)
    node(209, {"tourism": "zoo", "name": "თბილისის ზოოპარკი", "name:en": "Tbilisi Zoo"}, 41.713, 44.277)
    node(210, {"leisure": "water_park", "name": "ბათუმის აკვაპარკი"}, 41.62, 44.88)
    node(211, {"tourism": "viewpoint", "name": "მთაწმინდის გადასახედი"}, 41.694, 44.290)
    node(212, {"tourism": "aquarium", "name": "ბათუმის აკვარიუმი"}, 41.61, 44.89)
    node(213, {"tourism": "theme_park", "name": "მთაწმინდის პარკი"}, 41.695, 44.289)
    # Public services and offices.
    area(220, {"office": "government", "government": "public_service", "name": "იუსტიციის სახლი",
               "name:en": "Public Service Hall", "operator": "სსიპ იუსტიციის სახლი"}, 41.6988, 44.3060, 41.6992, 44.3066)
    node(221, {"office": "government", "name": "ვარკეთილის იუსტიციის სახლი", "operator": "სსიპ იუსტიციის სახლი"},
         41.709, 44.36)
    node(222, {"office": "government", "name": "საქართველოს იუსტიციის სამინისტრო"}, 41.677, 44.326)
    node(223, {"amenity": "townhall", "name": "თბილისის მერია"}, 41.704, 44.296)
    node(224, {"amenity": "post_office", "name": "საქართველოს ფოსტა", "brand": "საქართველოს ფოსტა"}, 41.698, 44.297)
    node(225, {"amenity": "school", "name": "N51 საჯარო სკოლა"}, 41.721, 44.332)
    node(226, {"amenity": "school"}, 41.722, 44.334)                                       # unnamed: left out
    node(227, {"amenity": "kindergarten", "name": "ფიფქია"}, 41.715, 44.320)
    # Money and medicine: unnamed pharmacies and ATMs stay; two ATMs of one
    # bank 30 m apart are one row.
    node(230, {"amenity": "pharmacy", "brand": "ავერსი", "brand:en": "Aversi"}, 41.7005, 44.3040)
    node(231, {"amenity": "pharmacy"}, 41.7001, 44.3002)
    node(232, {"healthcare": "pharmacy", "name": "ფარმა"}, 41.71, 44.31)
    node(233, {"amenity": "bank", "name": "თიბისი ბანკი", "atm": "yes"}, 41.7015, 44.3020)
    node(234, {"amenity": "bank", "name": "ლიბერთი", "name:en": "Liberty"}, 41.7025, 44.3025)
    node(235, {"amenity": "atm", "brand": "საქართველოს ბანკი", "brand:en": "Bank of Georgia"}, 41.7003, 44.3003)
    node(236, {"amenity": "atm", "brand": "საქართველოს ბანკი", "brand:en": "Bank of Georgia"}, 41.70057, 44.3003)
    node(237, {"amenity": "atm"}, 41.7040, 44.3050)
    # Food, drink and shops; a hotel with a restaurant stays a hotel.
    node(240, {"amenity": "restaurant", "name": "ბარბარესთანი", "name:en": "Barbarestan"}, 41.716, 44.303)
    node(241, {"tourism": "hotel", "amenity": "restaurant", "name": "სასტუმრო ვილა"}, 41.717, 44.304)
    node(242, {"amenity": "fast_food", "brand": "მაკდონალდსი", "brand:en": "McDonald's"}, 41.7009, 44.3012)
    node(243, {"amenity": "cafe", "name": "კოფი ლაბი", "name:en": "Coffee Lab"}, 41.706, 44.296)
    node(244, {"amenity": "cafe"}, 41.7061, 44.2961)                                      # unnamed: left out
    node(245, {"amenity": "pub", "name": "დაბლინი", "name:en": "Dublin"}, 41.704, 44.292)
    node(246, {"amenity": "nightclub", "name": "ბასიანი", "name:en": "Bassiani"}, 41.723, 44.787)
    node(247, {"shop": "electronics", "brand": "ზუმერი", "brand:en": "Zoommer", "name": "ზუმერი"}, 41.7035, 44.3045)
    node(248, {"shop": "clothes", "name": "ზარა", "name:en": "Zara"}, 41.7045, 44.3055)
    node(249, {"shop": "books", "name": "ბიბლუსი", "brand": "ბიბლუსი"}, 41.7050, 44.3060)
    node(250, {"shop": "kiosk", "name": "ჯიხური"}, 41.7052, 44.3062)                     # no shop kind: left out
    node(251, {"shop": "convenience", "brand": "ორი ნაბიჯი", "brand:en": "Ori Nabiji"}, 41.7055, 44.3065)
    node(252, {"shop": "supermarket", "name": "კარფური", "name:en": "Carrefour"}, 41.72, 44.31)
    node(253, {"shop": "car_parts"}, 41.73, 44.32)                                        # unnamed: kept
    node(254, {"shop": "car", "name": "თეგეტა მოტორსი"}, 41.74, 44.33)
    area(255, {"shop": "mall", "name": "ისთ ფოინთი", "name:en": "East Point"}, 41.689, 44.398, 41.691, 44.401)
    node(256, {"leisure": "fitness_centre", "name": "ფიტნეს ჰაუსი"}, 41.7038, 44.3048)
    node(257, {"leisure": "swimming_pool", "access": "private", "name": "კერძო აუზი"}, 41.71, 44.32)   # private
    node(258, {"leisure": "swimming_pool", "name": "ლაგუნა ვერე", "sport": "swimming"}, 41.712, 44.29)
    # Inside the occupied area and its 100 m buffer: nothing but places; in the band: flagged.
    node(270, {"amenity": "theatre", "name": "ზონის თეატრი"}, 42.30, 44.70)
    node(271, {"amenity": "cafe", "name": "ზონის კაფე"}, 42.31, 44.71)
    node(272, {"amenity": "pharmacy"}, 42.32, 44.72)
    node(273, {"amenity": "atm", "brand": "Сбербанк"}, 42.1995, 44.70)
    node(274, {"shop": "convenience", "name": "ზონის მარკეტი"}, 42.33, 44.73)
    node(275, {"amenity": "cafe", "name": "ზოლის კაფე"}, 42.197, 44.70)
    c.finish_relations()
    return c


class PoiCoverageTest(unittest.TestCase):
    """Config v4: theatres, museums, cinemas, food, money, medicine,
    schools, public services and shops are searchable; generic unnamed
    POIs stay out (or are kept unnamed where the driver kinds do the same);
    nothing but places inside the occupied area."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.db = Path(cls.tmp.name) / "pois.sqlite"
        cls.builder = gb.Builder(CONFIG, FOLD, zones(), poi_collector()).build()
        gb.write_database(cls.db, cls.builder, gb.base_meta(gb.DEFAULT_CONFIG, gb.DEFAULT_FOLD_SPEC, FOLD, CONFIG))
        cls.con = sqlite3.connect(cls.db)
        cls.searcher = Searcher(str(cls.db))

    @classmethod
    def tearDownClass(cls):
        cls.con.close()
        cls.searcher.con.close()
        cls.tmp.cleanup()

    def q(self, sql, *args):
        return self.con.execute(sql, args).fetchall()

    def top(self, text, near=None):
        hits = self.searcher.search(text, near=near, limit=5)
        self.assertTrue(hits, f"nothing for {text!r}")
        return hits[0]

    def kind_of(self, osm):
        rows = self.q("SELECT kind FROM pois WHERE osm = ?", osm)
        return rows[0][0] if rows else None

    def test_each_kind_from_its_tags(self):
        expected = {
            "r200": "theatre", "w202": "theatre", "n204": "cinema", "n206": "museum", "n207": "gallery",
            "n208": "library", "n209": "zoo", "n210": "theme_park", "n211": "viewpoint", "n212": "attraction",
            "n213": "theme_park", "w220": "public_service", "n222": "government", "n223": "government",
            "n224": "post_office", "n225": "school", "n227": "kindergarten", "n230": "pharmacy", "n232": "pharmacy",
            "n233": "bank", "n234": "bank", "n240": "restaurant", "n241": "hotel", "n242": "fast_food",
            "n243": "cafe", "n245": "bar", "n246": "nightclub", "n247": "shop", "n248": "shop", "n249": "shop",
            "n251": "supermarket", "n252": "supermarket", "n253": "car_parts", "n254": "car_dealer", "w255": "mall",
            "n256": "sports_centre", "n258": "sports_centre"}
        for osm, kind in expected.items():
            self.assertEqual(self.kind_of(osm), kind, osm)
        # A museum in a castle is a museum, a hotel with a restaurant a hotel,
        # an aquarium still an attraction; the Public Service Hall by its operator too.
        self.assertEqual(self.kind_of("n221"), "public_service")
        kinds = {r[0] for r in self.q("SELECT kind FROM pois")}
        self.assertLessEqual(kinds, {r["kind"] for r in CONFIG["pois"]["rules"]} | {"line_checkpoint"})

    def test_unnamed_pois_stay_out_or_are_kept_like_driver_kinds(self):
        for osm in ("n226", "n244", "n250", "n257"):         # unnamed school and cafe, a kiosk, a private pool
            self.assertIsNone(self.kind_of(osm), osm)
        kept = {r[0]: r[1:] for r in self.q("SELECT osm, label_ka, name FROM pois WHERE osm IN ('n231', 'n237', 'n253')")}
        self.assertEqual(kept, {"n231": (None, None), "n237": (None, None), "n253": (None, None)})
        keys = self.builder.search_keys()["pois"]
        for i, p in enumerate(self.builder.pois):             # found by category only, never by name
            if p["osm"] in ("n231", "n237", "n253"):
                self.assertEqual(keys[i], [], p["osm"])
        atms = self.q("SELECT osm, label_ka FROM pois WHERE kind = 'atm' AND brand IS NOT NULL")
        self.assertEqual(len(atms), 1)                       # two ATMs of one bank, 30 m apart
        self.assertEqual(atms[0][1], "საქართველოს ბანკი")    # labelled by its brand
        # Its search name 'საქართველოს ბანკი ბანკომატი' is no name: brand weight only.
        imp = self.q("SELECT importance FROM pois WHERE kind = 'atm' AND brand IS NOT NULL")[0][0]
        rule = next(r for r in CONFIG["pois"]["rules"] if r["kind"] == "atm")
        self.assertAlmostEqual(imp, rule["prior"] + CONFIG["pois"]["brand_weight"], places=4)

    def test_attrs(self):
        attrs = dict(self.q("SELECT osm, attrs FROM pois WHERE attrs IS NOT NULL"))
        self.assertEqual(attrs["n233"], "atm")
        self.assertEqual(attrs["n256"], "fitness")
        self.assertEqual(attrs["n258"], "pool")             # leisure=swimming_pool or sport=swimming
        self.assertEqual(attrs["n251"], "convenience")
        self.assertEqual(attrs["n247"], "electronics")
        self.assertEqual(attrs["n248"], "clothes")
        self.assertEqual(attrs["n249"], "books")
        self.assertEqual(attrs["n223"], "townhall")
        self.assertEqual(attrs["n210"], "water_park")
        self.assertNotIn("n252", attrs)                     # a supermarket is no convenience store

    def test_nothing_new_inside_the_occupied_area(self):
        names = {r[0] for r in self.q("SELECT name FROM pois WHERE name IS NOT NULL")}
        for name in ("ზონის თეატრი", "ზონის კაფე", "ზონის მარკეტი"):
            self.assertNotIn(name, names)
        self.assertEqual(self.builder.stats["pois_dropped_occupied"], 4)
        self.assertEqual(self.builder.stats["pois_dropped_buffer"], 1)
        z = zones()
        for lat, lon in self.q("SELECT lat, lon FROM pois"):
            self.assertNotIn(z.zone(lat, lon), ("occupied", "buffer", "outside"))
        self.assertEqual(self.q("SELECT band FROM pois WHERE name = 'ზოლის კაფე'"), [(1,)])
        self.assertEqual(gg.check_occupied_links(self.con), [])

    def test_theatre_beats_the_university_next_door(self):
        for near in (TBILISI, None, (41.60, 44.90)):
            for text in ("რუსთაველის თეატრი", "rustavelis teatri", "Rustaveli Theatre", "რუსთაველის თეატრში"):
                top = self.top(text, near)
                self.assertEqual((top["kind"], top["osm"]), ("theatre", "r200"), (text, near))
        # No category for the bare word: 'რუსთაველის' would anchor it to the suburb.
        self.assertIsNone(self.searcher.match_category(FOLD.parse_query("რუსთაველის თეატრი").required))
        self.assertIn(self.top("თეატრი", TBILISI)["kind"], ("theatre",))

    def test_kind_words_find_a_name_said_with_its_kind(self):
        for text in ("ამირანი კინო", "amirani cinema", "kino amirani", "ამირანი კინოთეატრი", "Амирани кинотеатр"):
            top = self.top(text, TBILISI)
            self.assertEqual((top["kind"], top["osm"]), ("cinema", "n204"), text)
            self.assertNotIn("partial", top, text)
        self.assertEqual(self.top("ამირანი კინო")["osm"], "n204")          # also far from 'ამირან ფანცულაიას ქუჩა'
        self.assertEqual(self.top("ლიბერთი ბანკი", TBILISI)["osm"], "n234")
        self.assertEqual(self.top("East Point mall", TBILISI)["osm"], "w255")
        self.assertEqual(self.top("ბარბარესთანი რესტორანი", TBILISI)["osm"], "n240")
        # Only a search name: the label stays the POI's own name.
        row = self.q("SELECT label_ka, label_en, alt_names FROM pois WHERE osm = 'n204'")[0]
        self.assertEqual(row[:2], ("ამირანი", "Amirani"))
        self.assertIn("ამირანი კინო", row[2].split("|"))
        # A name that holds the word gets no second one ('თეატრი' is in 'რუსთაველის თეატრი').
        alt = self.q("SELECT alt_names FROM pois WHERE osm = 'r200'")[0][0] or ""
        self.assertFalse([a for a in alt.split("|") if a.count("თეატრი") > 1 or a.endswith(" театр")], alt)
        # 'კინო' is held by 'კინოთეატრი': a cinema called so gets no 'კინო'.
        c = gb.Collector(CONFIG)
        c.node(1, {"amenity": "cinema", "name": "კინოთეატრი რუსთაველი"}, 41.70, 44.30)
        b = gb.Builder(CONFIG, FOLD, zones(), c).build()
        self.assertFalse([a for a in b.pois[0]["names"].alt if a.endswith(" კინო") or a.endswith(" кинотеатр")])

    def test_joined_names(self):
        for text in ("eastpoint", "EastPoint", "east point"):
            top = self.top(text, TBILISI)
            self.assertEqual((top["kind"], top["osm"]), ("mall", "w255"), text)
        alt = self.q("SELECT alt_names FROM pois WHERE osm = 'w255'")[0][0].split("|")
        self.assertIn("EastPoint", alt)
        self.assertNotIn("ისთფოინთი", alt)                                   # Latin names only

    def test_public_service_hall(self):
        top = self.top("იუსტიციის სახლი", TBILISI)
        self.assertEqual((top["kind"], top["osm"]), ("public_service", "w220"))
        hits = self.searcher.search("იუსტიციის სახლი", near=(41.709, 44.36), limit=5)
        self.assertEqual(hits[0]["osm"], "n221")                              # the one near you
        self.assertEqual(self.top("public service hall", TBILISI)["osm"], "w220")

    def test_categories_of_the_new_kinds(self):
        near = TBILISI
        cases = [("აფთიაქი", "pharmacy", None), ("аптека", "pharmacy", None), ("ბანკომატი", "atm", None),
                 ("atm", "atm", None), ("ბანკი", "bank", None), ("ფოსტა", "post_office", None),
                 ("რესტორანი", "restaurant", None), ("კაფე", "cafe", None), ("bar", "bar", None),
                 ("სწრაფი კვება", "fast_food", None), ("სკოლა", "school", None), ("საბავშვო ბაღი", "kindergarten", None),
                 ("ფიტნესი", "sports_centre", "fitness"), ("საცურაო აუზი", "sports_centre", "pool"),
                 ("აკვაპარკი", "theme_park", "water_park"), ("ზოოპარკი", "zoo", None), ("გადასახედი", "viewpoint", None),
                 ("მაღაზია", "shop", None), ("ელექტრონიკა", "shop", "electronics"), ("წიგნის მაღაზია", "shop", "books"),
                 ("ავტონაწილები", "car_parts", None), ("ავტოსალონი", "car_dealer", None),
                 ("მარკეტი", "supermarket", None)]
        for text, kind, attr in cases:
            top = self.top(text, near)
            self.assertEqual((top["kind"], top.get("category") is not None), (kind, True), text)
            if attr:
                self.assertIn(attr, (top.get("attrs") or "").split(";"), text)
        # The unnamed pharmacy 20 m from you is listed for the category.
        kinds = [h["osm"] for h in self.searcher.search("აფთიაქი", near=(41.7001, 44.3002), limit=5)]
        self.assertIn("n231", kinds)
        # A place named beside the word: 'ბათუმის აკვაპარკი' is Batumi's.
        self.assertEqual(self.top("ბათუმის აკვაპარკი", near)["osm"], "n210")
        self.assertEqual(self.top("ავერსი აფთიაქი", near)["osm"], "n230")
        # Brand words (config brands): Russian and Latin forms of a Georgian brand.
        self.assertEqual(self.top("Аверси", near)["osm"], "n230")
        self.assertEqual(self.top("bog", near)["kind"], "atm")
        self.assertEqual(self.top("Макдональдс", near)["kind"], "fast_food")

    def test_new_words_take_no_old_reading(self):
        s = self.searcher

        def reads(text):
            got = s.match_category(FOLD.parse_query(text).tokens)
            return got[0] if got else None

        # What a word still being typed read before v4, it reads now.
        for text, category in (("par", "parking"), ("პარ", "parking"), ("ელე", "charging"), ("ელექტრო", "charging"),
                               ("საწ", "fuel"), ("ზაპ", "fuel"), ("პოლ", "police"), ("ავტ", "lpg"),
                               ("parkin", "parking"), ("პარკი", None), ("park", None)):
            self.assertEqual(reads(text), category, text)
        # Names that hold a new category word only as a start or a type are no category.
        for text in ("Kazbegi View", "Lapuri Pass", "V. Barnov Str.", "ზემო ბარი", "ბარში", "Aqua", "ტექნიკური უნივერსიტეტი",
                     "ღვინის მუზეუმი", "სპორტის სასახლე", "რუსთაველის თეატრი", "ამირანი კინო", "ეროვნული მუზეუმი",
                     "იუსტიციის სახლი", "მერი შერვაშიძის", "Stori Bridge", "Road to farm", "სუპერი"):
            self.assertIsNone(reads(text), text)

    def test_category_config_is_sound(self):
        seen = {}
        noise = {k for w in FOLD.spec["noise_words"].values() if isinstance(w, list) for x in w for k in FOLD.keys(x)}
        for name, cat in CONFIG["categories"].items():
            if name.startswith("_"):
                continue
            for word in cat["words"]:
                keys = FOLD.keys(word)
                self.assertTrue(keys, word)
                # A noise word is dropped from the query, so the phrase could never match.
                self.assertFalse(set(keys) & noise, f"{name}: {word!r} holds a noise word")
                phrase = " ".join(keys)
                target = (cat["kind"], cat.get("attr"))
                self.assertEqual(seen.setdefault(phrase, target), target, f"{word!r} means two things")
            for word in cat.get("not_prefix", []):
                self.assertEqual(len(FOLD.keys(word)), 1, word)
        for rule in CONFIG["pois"]["rules"]:
            for word in rule.get("kind_words", []):
                self.assertTrue(FOLD.parse_query(word).required, word)
        # A brand word belongs to one brand only: the app builds this index
        # from an unordered dictionary, so a shared word would pick either.
        owner = {}
        for canon, words in CONFIG["brands"].items():
            if canon.startswith("_"):
                continue
            for word in [canon] + words:
                key = " ".join(FOLD.keys(word))
                self.assertEqual(owner.setdefault(key, canon), canon, f"{word!r} is in {owner[key]!r} and {canon!r}")

    def test_gate_names_only_known_kinds(self):
        gate = json.loads(gg.DEFAULT_GATE.read_text(encoding="utf-8"))
        poi_kinds = {r["kind"] for r in CONFIG["pois"]["rules"]} | {"line_checkpoint"}
        place_kinds = set(CONFIG["places"]["kinds"])
        for key in gate["kinds"]:
            table, kind = key.split(".")
            self.assertIn(kind, poi_kinds if table == "pois" else place_kinds, key)
        for case in gate["queries"]:
            if "kind" in case:
                tables = case["table"] if isinstance(case["table"], list) else [case["table"]]
                known = set().union(*[poi_kinds if t == "pois" else place_kinds for t in tables])
                self.assertIn(case["kind"], known, case["q"])
            if "attr" in case:
                rule = next(r for r in CONFIG["pois"]["rules"] if r["kind"] == case["kind"])
                self.assertIn(case["attr"], rule.get("attrs", {}), case["q"])
            if isinstance(case.get("near"), str):
                self.assertIn(case["near"], gate["near"], case["q"])


class GateTest(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.db = Path(cls.tmp.name) / "geo.sqlite"
        build_db(cls.db)
        cls.zones_path = Path(cls.tmp.name) / "zones.geojson"
        write_zones(cls.zones_path)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    GATE = {"near": {"tbilisi": list(TBILISI)}, "queries": [
        {"q": "tbilisi", "table": "places", "kind": "city", "at": [41.70, 44.30], "km": 1},
        {"q": "akhalgori", "table": "places", "occupied": 1, "at": [42.30, 44.70], "km": 1},
        {"q": "rustaveli 12", "near": "tbilisi", "table": "addresses", "at": [41.7021, 44.3021], "km": 0.1},
        {"q": "metani", "near": "tbilisi", "table": "pois", "kind": "fuel", "attr": "cng", "at": "near", "km": 2}]}

    def test_known_queries_pass(self):
        s = Searcher(str(self.db))
        problems, results, _ = gg.run_queries(s, self.GATE)
        self.assertEqual(problems, [])
        self.assertEqual(len(results), 4)
        s.con.close()

    def test_a_wrong_expectation_fails(self):
        s = Searcher(str(self.db))
        gate = dict(self.GATE, queries=[{"q": "tbilisi", "table": "streets", "at": [42.3, 44.7], "km": 1}])
        problems, _, _ = gg.run_queries(s, gate)
        self.assertEqual(len(problems), 2)                     # wrong table and too far
        s.con.close()

    def test_top_n_and_name(self):
        s = Searcher(str(self.db))
        gate = dict(self.GATE, queries=[
            {"q": "vake", "table": "places", "kind": "village", "top": 3},
            {"q": "rustaveli", "near": "tbilisi", "table": "streets", "name": "შოთა რუსთაველის გამზირი"},
            {"q": "rustaveli", "near": "tbilisi", "table": "streets", "name": "ისნის ქუჩა"}])
        problems, results, _ = gg.run_queries(s, gate)
        self.assertEqual([r["passed"] for r in results], [True, True, False])
        s.con.close()

    def test_occupied_links_and_labels_are_checked(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "x.sqlite"
            db.write_bytes(self.db.read_bytes())
            con = sqlite3.connect(db)
            occupied = con.execute("SELECT id FROM places WHERE name_ka = 'ცხინვალი'").fetchone()[0]
            con.execute("UPDATE streets SET city_id = ? WHERE name = 'გორის ქუჩა'", (occupied,))
            con.execute("UPDATE places SET label_ka = name WHERE name = 'Ленингор'")
            self.assertTrue(gg.check_occupied_links(con))
            self.assertTrue(gg.check_labels(con, CONFIG, FOLD.spec)[0])
            con.close()

    def test_counts_and_previous_release(self):
        counts = {"places": 10, "streets": 5}
        gate = {"counts": {"places": [5, 20], "streets": [6, 9]}, "kinds": {"pois.fuel": [1, 3]},
                "indexed_share": {"places": 0.99}}
        problems = gg.check_counts(counts, {"pois.fuel": 2}, {"places": 9}, gate)
        self.assertEqual(len(problems), 2)                     # streets too few, places not all indexed
        with tempfile.TemporaryDirectory() as tmp:
            prev = Path(tmp) / "m.json"
            prev.write_text(json.dumps({"tag": "t", "geocoder": {"counts": {"places": 20}}}))
            self.assertTrue(gg.check_previous(counts, prev, 0.2)[0])
            self.assertFalse(gg.check_previous({"places": 19}, prev, 0.2)[0])

    @unittest.skipUnless(HAVE_SHAPELY, "numpy/shapely not installed")
    def test_zone_check_with_shapely(self):
        con = sqlite3.connect(self.db)
        z = gg.ShapelyZones(self.zones_path)
        problems, detail = gg.check_zones(con, z)
        self.assertEqual(problems, [])
        self.assertGreaterEqual(detail["places"]["occupied"], 2)
        con.execute("UPDATE places SET occupied = 0 WHERE name = 'Ленингор'")
        self.assertTrue(gg.check_zones(con, z)[0])
        con.close()

    @unittest.skipUnless(HAVE_SHAPELY, "numpy/shapely not installed")
    def test_whole_gate_cli(self):
        gate_cfg = Path(self.tmp.name) / "gate.json"
        queries = self.GATE["queries"] * 8                     # at least 30 are required
        gate_cfg.write_text(json.dumps({
            "counts": {"places": [1, 100]}, "kinds": {}, "indexed_share": {"places": 0.99},
            "max_drop_vs_previous": 0.2, "size_bytes": [1000, 10 ** 8], "max_query_ms": 5000,
            "near": self.GATE["near"], "queries": queries}))
        results = Path(self.tmp.name) / "results.json"
        with quiet():
            code = gg.main(["--db", str(self.db), "--zones", str(self.zones_path), "--results", str(results),
                            "--gate-config", str(gate_cfg), "--expect-source-sha256", "ab" * 32])
        out = json.loads(results.read_text())
        self.assertEqual(code, 0, [r for r in out["results"] if not r["passed"]])
        self.assertEqual(out["db_sha256"], gb.sha256_file(self.db))


class ReleaseTest(unittest.TestCase):

    TAG = "osm-20260924T202102Z-3f2a1b0c"
    COMMIT = "3f2a1b0c" + "0" * 32

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        tmp = self.tmp = Path(self._tmp.name)
        self.db = tmp / "georgia_geocoder.sqlite"
        build_db(self.db)
        sha = gb.sha256_file(self.db)
        self.gate = {"passed": True, "checks": 3, "failed": 0, "db_sha256": sha, "counts": {"places": 1},
                     "kinds": {}, "results": [{"name": "x", "passed": True}], "queries": []}
        self.manifest = {"schema": 2, "tag": self.TAG, "files": [{"name": "valhalla_tiles.tar", "bytes": 1,
                                                                   "sha256": "00" * 32}],
                         "osm": {"timestamp": "2026-09-24T20:21:02Z", "clipped_pbf": {"sha256": "ab" * 32}},
                         "clip": {"config_version": 2}, "source_commit": self.COMMIT}
        self.write(gate=self.gate, manifest=self.manifest)
        (tmp / "report.json").write_text(json.dumps({"db": {"sha256": sha}, "source": {"sha256": "ab" * 32}}))

    def tearDown(self):
        self._tmp.cleanup()

    def write(self, gate=None, manifest=None):
        if gate is not None:
            (self.tmp / "gate.json").write_text(json.dumps(gate))
        if manifest is not None:
            (self.tmp / "manifest.json").write_text(json.dumps(manifest))

    def run_release(self, out):
        args = ["--db", str(self.db), "--gate", str(self.tmp / "gate.json"),
                "--build-report", str(self.tmp / "report.json"), "--manifest", str(self.tmp / "manifest.json"),
                "--commit", self.COMMIT, "--out-dir", str(self.tmp / out)]
        with quiet():
            return gr.main(args)

    def test_pack_and_report(self):
        self.assertEqual(self.run_release("a"), 0)
        self.assertEqual(self.run_release("b"), 0)
        a = (self.tmp / "a" / gr.PACKED_NAME).read_bytes()
        self.assertEqual(a, (self.tmp / "b" / gr.PACKED_NAME).read_bytes())   # reproducible
        self.assertEqual(gzip.decompress(a), self.db.read_bytes())
        report = json.loads((self.tmp / "a" / gr.REPORT_NAME).read_text())
        self.assertEqual((report["section"], report["passed"]), ("geocoder", True))
        self.assertEqual(report["files"][0]["sha256"], gb.sha256_file(self.tmp / "a" / gr.PACKED_NAME))
        self.assertEqual(report["sqlite"]["sha256"], gb.sha256_file(self.db))
        self.assertEqual(report["licence"], "ODbL-1.0")
        self.assertIn("ODbL 4.7", report["licence_note"])            # parallel distribution, not "never encrypt"
        self.assertNotIn("Do not encrypt", report["licence_note"])
        self.assertEqual(report["credit_sources"][0]["licence"], "ODbL-1.0")
        self.assertEqual(report["binds_to"], {"osm_timestamp": "2026-09-24T20:21:02Z",
                                              "clipped_pbf_sha256": "ab" * 32, "clip_config_version": 2,
                                              "source_commit": self.COMMIT})

    def test_refuses_another_extract_or_a_failed_gate(self):
        self.write(manifest=dict(self.manifest, osm={"timestamp": "2026-09-24T20:21:02Z",
                                                     "clipped_pbf": {"sha256": "cd" * 32}}))
        self.assertEqual(self.run_release("c"), 1)
        self.write(gate=dict(self.gate, passed=False), manifest=self.manifest)
        self.assertEqual(self.run_release("d"), 1)
        self.write(gate=self.gate, manifest=dict(self.manifest, tag="osm-20260101T000000Z"))
        self.assertEqual(self.run_release("e"), 1)

    @unittest.skipUnless(HAVE_SHAPELY, "numpy/shapely not installed (manifest.py needs them)")
    def test_manifest_extend_takes_the_report(self):
        import manifest as mf
        self.assertEqual(self.run_release("f"), 0)
        out = self.tmp / "final.json"
        args = ["--extend", str(self.tmp / "manifest.json"), "--asset-report",
                str(self.tmp / "f" / gr.REPORT_NAME), "--out", str(out)]
        with quiet():
            self.assertEqual(mf.main(args), 0)
        final = json.loads(out.read_text())
        self.assertEqual([f["name"] for f in final["files"]], ["valhalla_tiles.tar", gr.PACKED_NAME])
        self.assertEqual(final["geocoder"]["files"], [gr.PACKED_NAME])
        self.assertTrue(final["geocoder"]["gate"]["passed"])
        self.assertEqual([c["part"] for c in final["credits"]], ["routing", "geocoder"])
        self.assertEqual(final["credits"][1]["files"], [gr.PACKED_NAME])
        self.assertNotIn("map_attribution", final)                   # no basemap in this release
        # A report bound to another commit is refused.
        self.write(manifest=dict(self.manifest, source_commit="f" * 40))
        with quiet():
            self.assertEqual(mf.main(args), 1)
        # So is a geocoder report without binds_to, or with only part of it.
        self.write(manifest=self.manifest)
        report_path = self.tmp / "f" / gr.REPORT_NAME
        good = json.loads(report_path.read_text())
        for binds in (None, {"source_commit": self.COMMIT}):
            bad = dict(good)
            if binds is None:
                del bad["binds_to"]
            else:
                bad["binds_to"] = binds
            report_path.write_text(json.dumps(bad))
            with quiet():
                self.assertEqual(mf.main(args), 1, binds)


OSM_XML = """<?xml version='1.0' encoding='UTF-8'?>
<osm version="0.6" generator="test">
  <node id="1" version="1" lat="41.70" lon="44.30"><tag k="place" v="city"/><tag k="name" v="თბილისი"/>
    <tag k="population" v="1300000"/></node>
  <node id="2" version="1" lat="41.74" lon="44.35"/>
  <node id="3" version="1" lat="41.74" lon="44.37"/>
  <node id="4" version="1" lat="41.76" lon="44.37"/>
  <node id="5" version="1" lat="41.76" lon="44.35"/>
  <node id="6" version="1" lat="41.701" lon="44.301"/>
  <node id="7" version="1" lat="41.702" lon="44.305"><tag k="mountain_pass" v="yes"/><tag k="name" v="Pass"/></node>
  <node id="8" version="1" lat="42.30" lon="44.70"><tag k="place" v="town"/><tag k="name" v="Ленингор"/>
    <tag k="name:ka" v="ახალგორი"/></node>
  <node id="12" version="1" lat="42.35" lon="44.75"><tag k="place" v="village"/><tag k="name" v="Аџьрҩара"/></node>
  <node id="9" version="1" lat="41.7015" lon="44.3015"/>
  <node id="10" version="1" lat="41.7015" lon="44.3018"/>
  <node id="11" version="1" lat="41.7018" lon="44.3018"/>
  <way id="100" version="1"><nd ref="2"/><nd ref="3"/><nd ref="4"/></way>
  <way id="101" version="1"><nd ref="4"/><nd ref="5"/><nd ref="2"/></way>
  <way id="102" version="1"><nd ref="6"/><nd ref="7"/><tag k="highway" v="primary"/>
    <tag k="name" v="შოთა რუსთაველის გამზირი"/></way>
  <way id="103" version="1"><nd ref="9"/><nd ref="10"/><nd ref="11"/><nd ref="9"/><tag k="building" v="yes"/>
    <tag k="addr:housenumber" v="12"/><tag k="addr:street" v="შოთა რუსთაველის გამზირი"/></way>
  <relation id="200" version="1"><member type="way" ref="100" role="outer"/><member type="way" ref="101" role="outer"/>
    <tag k="type" v="multipolygon"/><tag k="natural" v="water"/><tag k="name" v="თბილისის წყალსაცავი"/></relation>
</osm>
"""


@unittest.skipUnless(HAVE_OSMIUM, "pyosmium not installed")
class ReadFileTest(unittest.TestCase):
    """The pyosmium reading path on a tiny OSM file."""

    def test_read_osm_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "in.osm"
            src.write_text(OSM_XML, encoding="utf-8")
            c = gb.Collector(CONFIG)
            gb.read_pbf(src, c)
        self.assertEqual(sorted(p["osm"] for p in c.places), ["n1", "n12", "n8"])
        water = [p for p in c.pois if p["rule"]["kind"] == "water"]
        self.assertEqual([p["osm"] for p in water], ["r200"])
        self.assertTrue(41.74 < water[0]["lat"] < 41.76)
        passes = [p for p in c.pois if p["rule"]["kind"] == "mountain_pass"]
        self.assertTrue(passes[0]["on_road"])
        self.assertEqual([w["id"] for w in c.street_ways], [102])
        self.assertEqual([a["osm"] for a in c.addresses], ["w103"])
        self.assertTrue(c.addresses[0]["building"])
        builder = gb.Builder(CONFIG, FOLD, zones(), c).build()
        self.assertEqual({r["names"].display: r["occupied"] for r in builder.places},
                         {"თბილისი": 0, "Ленингор": 1})          # no name:ka: left out (owner decision pending)
        self.assertEqual(builder.stats["places_occupied_without_name_ka_hidden"], 1)
        self.assertEqual([r["label"] for r in builder.places if r["occupied"]], [("ახალგორი", "Akhalgori")])


if __name__ == "__main__":
    missing = [name for name, ok in (("pyosmium", HAVE_OSMIUM), ("shapely", HAVE_SHAPELY)) if not ok]
    if missing:
        print(f"{' and '.join(missing)} not installed: the tests that need them are skipped")
    try:
        sqlite3.connect(":memory:").execute("CREATE VIRTUAL TABLE t USING fts5(x)")
    except sqlite3.OperationalError:
        print("this Python's SQLite has no FTS5: the geocoder cannot be built here")
        sys.exit(1)
    unittest.main(verbosity=2)
