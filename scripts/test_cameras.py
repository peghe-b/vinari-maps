#!/usr/bin/env python3
"""Unit tests for the speed cameras (scripts/geocoder_cameras.py) on small
made-up data. No downloads.

Run from the maps/ folder:
    python3 scripts/test_cameras.py

Needs only the Python standard library. The test that reads an OSM file needs
pyosmium and the zone cross-check needs shapely; without them those tests are
skipped, not failed (the CI installs both and fails on any skip).
"""

import contextlib
import io
import json
import sqlite3
import sys
import tempfile
import unittest
from collections import Counter
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import geocoder_build as gb  # noqa: E402
import geocoder_cameras as gc  # noqa: E402
import geocoder_gate as gg  # noqa: E402
import geocoder_hazards as gh  # noqa: E402
from geocoder_fold import Fold  # noqa: E402

try:
    import osmium  # noqa: F401
    HAVE_OSMIUM = True
except ImportError:
    HAVE_OSMIUM = False

try:
    import numpy  # noqa: F401
    import shapely  # noqa: F401
    HAVE_SHAPELY = True
except ImportError:
    HAVE_SHAPELY = False

FOLD = Fold.load()
CONFIG = gb.load_config()
CFG = CONFIG["cameras"]
GATE = json.loads((HERE.parent / "config" / "geocoder_gate.json").read_text(encoding="utf-8"))


@contextlib.contextmanager
def quiet():
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        yield


def box(lon0, lat0, lon1, lat1):
    return [(lon0, lat0), (lon1, lat0), (lon1, lat1), (lon0, lat1), (lon0, lat0)]


def feature(name, rings):
    return {"type": "Feature", "properties": {"name": name},
            "geometry": {"type": "Polygon", "coordinates": [[list(p) for p in r] for r in rings]}}


# The made-up Georgia of test_geocoder.py: a 1 x 1 degree square, the
# occupied area a small square in its north-east, no_go_hard about 100 m
# around it, the band about 500 m.
GEORGIA = box(44.0, 41.5, 45.0, 42.5)
OCCUPIED = box(44.6, 42.2, 44.8, 42.4)
HARD = box(44.599, 42.199, 44.801, 42.401)
BAND = box(44.595, 42.195, 44.805, 42.405)


def zones():
    return gb.Zones(gb.PolygonIndex([GEORGIA]), gb.PolygonIndex([HARD]), gb.PolygonIndex([BAND]),
                    gb.PolygonIndex([OCCUPIED]))


def write_zones(path):
    data = {"type": "FeatureCollection", "features": [
        feature("no_go_hard", [HARD]), feature("soft_band_outer", [BAND]),
        feature("occupied", [OCCUPIED]), feature("georgia", [GEORGIA])]}
    Path(path).write_text(json.dumps(data), encoding="utf-8")


NODES = {
    # W1: a two-way primary running east along 41.80, limit 80
    101: (44.10, 41.80), 102: (44.11, 41.80), 103: (44.12, 41.80), 104: (44.13, 41.80), 105: (44.14, 41.80),
    # W2: a motorway running north (one-way without a tag)
    201: (44.30, 41.70), 202: (44.30, 41.71), 203: (44.30, 41.72),
    # W3 eastbound and W4 westbound: a divided road, the carriageways 33 m apart
    301: (44.10, 41.90), 302: (44.12, 41.90), 401: (44.12, 41.9003), 402: (44.10, 41.9003),
    # W5: a single one-way road drawn north, oneway=-1 (traffic goes south)
    501: (44.50, 41.60), 502: (44.50, 41.62),
    # W6 and W7 cross at 601 (a tertiary running east, a secondary running north)
    600: (44.20, 41.65), 601: (44.21, 41.65), 602: (44.22, 41.65), 700: (44.21, 41.64), 702: (44.21, 41.66),
}
WAYS = {
    1: ([101, 102, 103, 104, 105], {"highway": "primary", "maxspeed": "80", "ref": "შ 1", "name:ka": "გზა"}),
    2: ([201, 202, 203], {"highway": "motorway", "ref": "S1"}),
    3: ([301, 302], {"highway": "trunk", "oneway": "yes"}),
    4: ([401, 402], {"highway": "trunk", "oneway": "yes"}),
    5: ([501, 502], {"highway": "secondary", "oneway": "-1"}),
    6: ([600, 601, 602], {"highway": "tertiary"}),
    7: ([700, 601, 702], {"highway": "secondary"}),
    8: ([101, 103], {"highway": "footway"}),
}
CAM = {"highway": "speed_camera"}
CAMERAS = {
    102: dict(CAM, direction="90", maxspeed="60"),       # looks east: checks westbound traffic
    103: dict(CAM, direction="forward"),                 # no limit of its own: the road's 80
    104: dict(CAM, direction="0", maxspeed="60"),        # looks across the road: both directions
    150: dict(CAM, direction="90"),                      # C1 drawn again 2.5 m away: merged
    202: dict(CAM, direction="0", maxspeed="110"),       # looks north on a northbound motorway
    160: dict(CAM, maxspeed="70"),                       # beside W3, W4 runs the other way 44 m off
    170: dict(CAM, maxspeed="40"),                       # beside W5, nothing parallel
    180: dict(CAM, maxspeed="50 km/h"),                  # far from any road
    181: dict(CAM, maxspeed="60"),                       # in the occupied area
    182: dict(CAM, maxspeed="60"),                       # abroad
    183: dict(CAM, maxspeed="60"),                       # in the band
    184: dict(CAM, enforcement="traffic_signals"),       # a red-light camera
    190: dict(CAM, maxspeed="50"),                       # a device of section r910
    601: dict(CAM, direction="forward", maxspeed="60"),  # on a junction: belongs to the secondary
}
CAMERA_AT = {150: (44.11003, 41.80), 160: (44.11, 41.8999), 170: (44.5001, 41.61), 180: (44.9, 41.55),
             181: (44.7, 42.3), 182: (45.5, 41.8), 183: (44.597, 42.3), 184: (44.8, 41.6), 190: (44.1001, 41.8)}
RELATIONS = [
    # a speed check on W3 whose device is an untagged node beside it
    (900, {"type": "enforcement", "enforcement": "maxspeed", "maxspeed": "70"},
     [("n", 301, "from"), ("n", 901, "device"), ("n", 302, "to")]),
    # an average-speed section along W1
    (910, {"type": "enforcement", "enforcement": "average_speed", "maxspeed": "50"},
     [("n", 101, "from"), ("n", 190, "device"), ("n", 105, "to")]),
    # a section that ends in the occupied area: dropped whole
    (920, {"type": "enforcement", "enforcement": "average_speed", "maxspeed": "90"},
     [("n", 201, "from"), ("n", 921, "to")]),
    # not a camera at all
    (930, {"type": "enforcement", "enforcement": "check"}, [("n", 102, "device")]),
]
UNTAGGED_OFF_WAY = {901: (44.11, 41.89995), 921: (44.7, 42.35)}


def collector():
    """A geocoder Collector fed like read_pbf feeds it: relations, then the
    tagged nodes, then the ways, then the untagged member nodes by id."""
    c = gb.Collector(CONFIG)
    for rel_id, tags, members in RELATIONS:
        if c.wants_relation(tags):
            c.relation(rel_id, tags, members)
    for node_id, tags in sorted(CAMERAS.items()):
        lon, lat = CAMERA_AT.get(node_id) or NODES[node_id]
        c.node(node_id, tags, lat, lon)
    for way_id, (refs, tags) in sorted(WAYS.items()):
        c.way(way_id, tags, refs, [NODES[r] for r in refs])
    for node_id in c.cams.missing_members():
        c.cams.coords[node_id] = UNTAGGED_OFF_WAY[node_id]
    return c


def build(stats=None):
    return {r["osm"]: r for r in gc.build_cameras(collector().cams, zones(), CFG, stats)}


class ParseTest(unittest.TestCase):
    def test_maxspeed(self):
        zones_kmh = CFG["maxspeed_zones"]
        for value, kmh in (("60", 60), (" 90 ", 90), ("50 km/h", 50), ("50kmh", 50), ("40 mph", 64),
                           ("GE:urban", 60), ("GE:rural", 90), ("GE:motorway", 110), ("none", None),
                           ("signals", None), ("60;90", None), ("500", None), ("0", None), ("nan", None),
                           ("inf", None), ("", None), (None, None)):
            self.assertEqual(gc.parse_maxspeed(value, zones_kmh), kmh, value)

    def test_direction(self):
        for value, parsed in (("forward", ("along", 1)), ("backward", ("along", -1)), ("90", ("degrees", 90.0)),
                              ("-20", ("degrees", 340.0)), ("370", ("degrees", 10.0)), ("NE", ("degrees", 45.0)),
                              ("wsw", ("degrees", 247.5)), ("25;205", ("both", None)), ("both", ("both", None)),
                              ("up", None), ("", None), (None, None)):
            self.assertEqual(gc.parse_direction(value), parsed, value)

    def test_oneway(self):
        for tags, sign in (({"highway": "primary"}, 0), ({"highway": "primary", "oneway": "yes"}, 1),
                           ({"highway": "primary", "oneway": "-1"}, -1), ({"highway": "motorway"}, 1),
                           ({"highway": "motorway", "oneway": "no"}, 0),
                           ({"highway": "tertiary", "junction": "roundabout"}, 1),
                           ({"highway": "primary", "oneway": "reversible"}, 0)):
            self.assertEqual(gc.oneway_sign(tags), sign, tags)

    def test_angles(self):
        self.assertAlmostEqual(gc.bearing(41.8, 44.1, 41.8, 44.2), 90.0, delta=0.1)
        self.assertAlmostEqual(gc.bearing(41.8, 44.1, 41.7, 44.1), 180.0, delta=0.01)
        self.assertEqual(gc.angle_diff(350, 10), 20)
        self.assertEqual(gc.onto_line(260, 90), 270)
        self.assertEqual(gc.onto_line(100, 90), 90)


class CamerasTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.stats = Counter()
        cls.rows = build(cls.stats)

    def test_occupied_abroad_and_band(self):
        self.assertNotIn("n181", self.rows)
        self.assertNotIn("n182", self.rows)
        self.assertNotIn("r920", self.rows)             # its end lies in the occupied area
        self.assertEqual(self.stats["cameras_dropped_occupied"], 2)
        self.assertEqual(self.stats["cameras_dropped_outside"], 1)
        self.assertEqual(self.rows["n183"]["band"], 1)
        self.assertEqual(self.rows["n102"]["band"], 0)

    def test_degrees_are_where_the_camera_looks(self):
        r = self.rows["n102"]
        self.assertEqual((r["heading"], r["heading_src"]), (270, "direction"))
        self.assertEqual((r["maxspeed"], r["maxspeed_src"]), (60, "camera"))
        self.assertEqual((r["road_bearing"], r["road_dist_m"], r["road_class"], r["road_ref"], r["road_name"]),
                         (90, 0.0, "primary", "შ 1", "გზა"))

    def test_forward_follows_the_way_and_the_road_gives_the_limit(self):
        r = self.rows["n103"]
        self.assertEqual((r["heading"], r["heading_src"]), (90, "direction"))
        self.assertEqual((r["maxspeed"], r["maxspeed_src"]), (80, "road"))

    def test_direction_across_the_road_means_both(self):
        r = self.rows["n104"]
        self.assertIsNone(r["heading"])
        self.assertIsNone(r["heading_src"])
        self.assertEqual(self.stats["direction_across_road"], 1)

    def test_a_camera_drawn_twice_is_kept_once(self):
        self.assertNotIn("n150", self.rows)             # the copy without a limit goes
        self.assertEqual(self.stats["cameras_merged"], 1)

    def test_one_way_road_wins_over_the_tag(self):
        r = self.rows["n202"]
        self.assertEqual((r["heading"], r["heading_src"], r["maxspeed"]), (0, "oneway", 110))
        self.assertEqual(self.stats["direction_against_oneway"], 1)

    def test_divided_road_beside_the_camera_leaves_both_directions(self):
        r = self.rows["n160"]
        self.assertIsNone(r["heading"])
        self.assertEqual(r["road_bearing"], 90)
        self.assertLess(r["road_dist_m"], 15)

    def test_single_one_way_road_beside_the_camera(self):
        r = self.rows["n170"]
        self.assertEqual((r["heading"], r["heading_src"], r["road_class"]), (180, "oneway", "secondary"))

    def test_camera_far_from_roads_keeps_no_road(self):
        r = self.rows["n180"]
        self.assertEqual(r["maxspeed"], 50)
        self.assertIsNone(r["road_bearing"])
        self.assertIsNone(r["road_dist_m"])
        self.assertIsNone(r["heading"])

    def test_kinds(self):
        self.assertEqual(self.rows["n184"]["kind"], "red_light")
        self.assertEqual({r["kind"] for k, r in self.rows.items() if k not in ("n184", "r910")}, {"fixed"})

    def test_relation_with_an_untagged_device(self):
        r = self.rows["r900"]
        self.assertEqual((r["lat"], r["lon"]), (41.89995, 44.11))
        self.assertEqual((r["kind"], r["maxspeed"], r["maxspeed_src"]), ("fixed", 70, "relation"))
        self.assertEqual((r["heading"], r["heading_src"]), (90, "relation"))   # from -> to, eastbound
        self.assertNotIn("r930", self.rows)

    def test_average_speed_section(self):
        r = self.rows["r910"]
        self.assertEqual((r["kind"], r["lat"], r["lon"], r["end_lat"], r["end_lon"]),
                         ("average_speed", 41.8, 44.1, 41.8, 44.14))
        self.assertEqual((r["heading"], r["heading_src"], r["maxspeed"]), (90, "relation", 50))
        self.assertNotIn("n190", self.rows)             # its device is the section, not a camera of its own
        self.assertIsNone(self.rows["n102"]["end_lat"])

    def test_junction_node_belongs_to_the_bigger_road(self):
        r = self.rows["n601"]
        self.assertEqual((r["road_class"], r["road_bearing"], r["heading"]), ("secondary", 0, 0))

    def test_rows_are_sorted_and_counted(self):
        rows = gc.build_cameras(collector().cams, zones(), CFG)
        self.assertEqual([r["osm"] for r in rows],
                         ["n102", "n103", "n104", "n160", "n170", "n180", "n183", "n184", "n202", "n601",
                          "r900", "r910"])
        self.assertTrue(all(set(r) == set(gc.COLUMNS) for r in rows))

    def test_roads_far_from_every_camera_are_not_kept(self):
        c = collector()
        c.cams.way(99, {"highway": "primary"}, [1, 2], [(44.95, 42.45), (44.96, 42.45)])
        self.assertNotIn(99, c.cams.roads)
        self.assertNotIn(8, c.cams.roads)               # a footway is no road for a camera
        self.assertIn(3, c.cams.roads)                  # holds no camera node but runs past one

    def test_facing_can_be_switched_to_travel(self):
        cfg = dict(CFG, direction_degrees="travel")
        rows = {r["osm"]: r for r in gc.build_cameras(collector().cams, zones(), cfg)}
        self.assertEqual(rows["n102"]["heading"], 90)

    def test_config_is_checked(self):
        bad = json.loads(json.dumps(CONFIG))
        bad["cameras"]["relations"]["maxspeed"] = "speed"
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "g.json"
            path.write_text(json.dumps(bad), encoding="utf-8")
            with self.assertRaises(ValueError):
                gb.load_config(path)


class DatabaseTest(unittest.TestCase):
    """The table inside georgia_geocoder.sqlite, and the gate's checks of it."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "g.sqlite"
        builder = gb.Builder(CONFIG, FOLD, zones(), collector()).build()
        with quiet():
            counts, _ = gb.write_database(self.db, builder, {"schema_version": gb.SCHEMA_VERSION})
        self.counts = counts
        self.con = sqlite3.connect(str(self.db))

    def tearDown(self):
        self.con.close()
        self.tmp.cleanup()

    def test_table_and_counts(self):
        self.assertEqual(self.counts["cameras"], 12)
        cols = [r[1] for r in self.con.execute("PRAGMA table_info(cameras)")]
        self.assertEqual(cols, ["id", *gc.COLUMNS])
        self.assertEqual(self.con.execute("SELECT heading, maxspeed FROM cameras WHERE osm = 'n102'").fetchone(),
                         (270, 60))
        counts, _ = gg.db_counts(self.con)
        self.assertEqual(counts["cameras"], 12)
        self.assertEqual(gb.SCHEMA_VERSION, 4)

    def test_gate_accepts_good_rows(self):
        # Three of the made-up cameras stand far from every road on purpose.
        gate = dict(GATE, cameras=dict(GATE["cameras"], min_road_share=0.75, min_sections=0))
        problems, detail = gg.check_cameras(self.con, CONFIG, gate)
        self.assertEqual(problems, [])
        self.assertEqual((detail["rows"], detail["on_road"], detail["with_maxspeed"]), (12, 9, 11))
        problems, _ = gg.check_cameras(self.con, CONFIG, dict(GATE, cameras=dict(GATE["cameras"], min_sections=0)))
        self.assertEqual(problems, ["cameras: only 9 of 12 lie on a car road (at least 90%)"])
        problems, _ = gg.check_cameras(self.con, CONFIG, gate | {"cameras": dict(gate["cameras"], min_sections=1)})
        self.assertTrue(any("ministry's sections" in p for p in problems), problems)

    def test_gate_refuses_bad_rows(self):
        for sql, word in (("UPDATE cameras SET heading = 400 WHERE osm = 'n102'", "heading"),
                          ("UPDATE cameras SET maxspeed = 999 WHERE osm = 'n102'", "maxspeed"),
                          ("UPDATE cameras SET kind = 'mobile' WHERE osm = 'n102'", "kind"),
                          ("UPDATE cameras SET road_dist_m = 90 WHERE osm = 'n160'", "road"),
                          ("UPDATE cameras SET end_lat = 41.8, end_lon = 44.2 WHERE osm = 'n102'", "section end"),
                          ("UPDATE cameras SET maxspeed = NULL, maxspeed_src = NULL", "speed limit"),
                          ("UPDATE cameras SET osm = 'n102'", "twice")):
            with self.subTest(word=word):
                self.con.execute("SAVEPOINT s")
                self.con.execute(sql)
                problems, _ = gg.check_cameras(self.con, CONFIG, GATE)
                self.assertTrue(any(word in p for p in problems), (word, problems))
                self.con.execute("ROLLBACK TO s")

    def test_gate_count_band(self):
        counts, kinds = gg.db_counts(self.con)
        problems = gg.check_counts(counts, kinds, {}, {"counts": {"cameras": GATE["counts"]["cameras"]},
                                                       "kinds": {}, "indexed_share": {}})
        self.assertTrue(any(p.startswith("cameras: 12 rows") for p in problems), problems)
        self.assertEqual(GATE["counts"]["cameras"], [120, 600])

    @unittest.skipUnless(HAVE_SHAPELY, "shapely not installed")
    def test_gate_finds_a_camera_in_the_occupied_area(self):
        path = Path(self.tmp.name) / "zones.geojson"
        write_zones(path)
        z = gg.ShapelyZones(path)
        problems, detail = gg.check_zones(self.con, z)
        self.assertEqual(problems, [])
        self.assertEqual(detail["cameras"]["rows"], 12)
        self.assertEqual(detail["camera section ends"]["rows"], 1)
        self.con.execute("UPDATE cameras SET end_lat = 42.3, end_lon = 44.7 WHERE osm = 'r910'")
        self.con.execute("UPDATE cameras SET lat = 42.3, lon = 44.7 WHERE osm = 'n102'")
        problems, _ = gg.check_zones(self.con, z)
        self.assertTrue(any(p.startswith("cameras: 1 rows inside no_go_hard") for p in problems), problems)
        self.assertTrue(any(p.startswith("camera section ends: 1 rows inside") for p in problems), problems)


# ---------------------------------------------------------------------------
# The ministry's sections (cameras.sections, config/average_speed_sections.json)
# ---------------------------------------------------------------------------

SEC_NODES = {
    # W11 eastbound and W12 westbound: a divided road ref ს 9 along 41.95, the carriageways 33 m apart
    1101: (44.30, 41.95), 1102: (44.40, 41.95), 1201: (44.40, 41.9503), 1202: (44.30, 41.9503),
    # W13: a two-way ს 9 piece that runs into the occupied area
    1301: (44.59, 42.30), 1302: (44.70, 42.30),
    # W14: a ramp of ს 9 leaving W11 to the north-east
    1401: (44.35, 41.95), 1402: (44.352, 41.953),
}
SEC_WAYS = {
    11: ([1101, 1102], {"highway": "motorway", "ref": "ს 9", "oneway": "yes", "maxspeed": "90"}),
    12: ([1201, 1202], {"highway": "motorway", "ref": "ს 9", "oneway": "yes"}),
    13: ([1301, 1302], {"highway": "trunk", "ref": "ს 9"}),
    14: ([1401, 1402], {"highway": "motorway_link", "ref": "ს 9", "oneway": "yes"}),
}


def sec(sid, road, a, b, kmh):
    return {"id": sid, "source": "mia-1", "road": road, "maxspeed": kmh,
            "from": {"lat": a[0], "lon": a[1], "what": "start", "evidence": "test"},
            "to": {"lat": b[0], "lon": b[1], "what": "end", "evidence": "test"}}


SECTIONS = [
    # the km post stands on the westbound carriageway; the section runs east: onto W11
    sec("mia-1-1", {"ref": "ს 9"}, (41.9503, 44.31), (41.9503, 44.38), 90),
    # the same stretch westbound: onto W12
    sec("mia-1-2", {"ref": "ს 9"}, (41.95, 44.38), (41.95, 44.31), 90),
    # W11 says 90, the release 70: no limit
    sec("mia-1-3", {"ref": "ს 9"}, (41.95, 44.33), (41.95, 44.36), 70),
    # no road with this ref: not placed
    sec("mia-1-4", {"ref": "ს 99"}, (41.95, 44.31), (41.95, 44.38), 90),
    # by name on the two-way W1 (name:ka გზა, maxspeed 80)
    sec("mia-1-5", {"name": "გზა"}, (41.80, 44.12), (41.80, 44.14), 80),
    # ends in the occupied area: dropped
    sec("mia-1-6", {"ref": "ს 9"}, (42.30, 44.592), (42.30, 44.65), 60),
]


def section_collector(sections=SECTIONS):
    c = gb.Collector(dict(CONFIG, cameras=dict(CFG, _sections=sections)))
    for way_id, (refs, tags) in sorted((WAYS | SEC_WAYS).items()):
        nodes = NODES | SEC_NODES
        c.way(way_id, tags, refs, [nodes[r] for r in refs])
    return c


class SectionsTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.stats = Counter()
        cfg = dict(CFG, _sections=SECTIONS)
        cls.rows = {r["osm"]: r for r in gc.build_cameras(section_collector().cams, zones(), cfg, cls.stats)}

    def test_each_end_goes_onto_the_carriageway_of_its_way(self):
        east, west = self.rows["mia-1-1"], self.rows["mia-1-2"]
        self.assertEqual((east["lat"], east["lon"], east["end_lat"], east["end_lon"]), (41.95, 44.31, 41.95, 44.38))
        self.assertEqual((east["heading"], east["heading_src"], east["road_dist_m"]), (90, "section", 0.0))
        self.assertEqual((west["lat"], west["end_lat"], west["heading"]), (41.9503, 41.9503, 270))
        self.assertEqual((east["kind"], east["road_class"], east["road_ref"]), ("average_speed", "motorway", "ს 9"))

    def test_limit_from_the_release_unless_the_road_says_otherwise(self):
        self.assertEqual((self.rows["mia-1-1"]["maxspeed"], self.rows["mia-1-1"]["maxspeed_src"]), (90, "mia"))
        self.assertEqual((self.rows["mia-1-2"]["maxspeed"], self.rows["mia-1-2"]["maxspeed_src"]), (90, "mia"))
        self.assertEqual((self.rows["mia-1-3"]["maxspeed"], self.rows["mia-1-3"]["maxspeed_src"]), (None, None))
        self.assertEqual(self.stats["sections_maxspeed_conflict"], 1)

    def test_by_name_on_a_two_way_road(self):
        r = self.rows["mia-1-5"]
        self.assertEqual((r["lat"], r["lon"], r["end_lon"], r["heading"], r["maxspeed"]), (41.8, 44.12, 44.14, 90, 80))

    def test_unplaced_and_occupied_are_dropped(self):
        self.assertNotIn("mia-1-4", self.rows)
        self.assertNotIn("mia-1-6", self.rows)
        self.assertEqual(self.stats["sections_unplaced"], 1)
        self.assertEqual(self.stats["sections_unplaced mia-1-4"], 1)
        self.assertEqual(self.stats["sections_dropped_zone"], 1)
        self.assertEqual(self.stats["sections_placed"], 4)

    def test_a_ramp_of_the_same_road_is_not_taken(self):
        # a post 3 m from the ramp's start, 11 m from the main line: the ramp leaves at 27 degrees
        s = sec("mia-2-1", {"ref": "ს 9"}, (41.9501, 44.3501), (41.95, 44.39), 90)
        rows = {r["osm"]: r for r in gc.build_cameras(section_collector([s]).cams, zones(), dict(CFG, _sections=[s]))}
        self.assertEqual((rows["mia-2-1"]["lat"], rows["mia-2-1"]["road_class"]), (41.95, "motorway"))

    def test_rows_sort_after_osm(self):
        rows = gc.build_cameras(section_collector().cams, zones(), dict(CFG, _sections=SECTIONS))
        osm = [r["osm"] for r in rows]
        self.assertEqual(osm[-4:], ["mia-1-1", "mia-1-2", "mia-1-3", "mia-1-5"])
        self.assertEqual(gc.osm_key("mia-12210-7"), (2, 12210, 7, ""))
        self.assertLess(gc.osm_key("mia-12493-1a"), gc.osm_key("mia-12493-1b"))

    def test_the_file_is_checked(self):
        good = sec("mia-3-1", {"ref": "ს 9"}, (41.95, 44.31), (41.95, 44.38), 90)
        cases = {
            "id": dict(good, id="mia-x"),
            "source": dict(good, source="nowhere"),
            "road": dict(good, road={"ref": "ს 9", "name": "x"}),
            "lat and lon": dict(good, to={"lat": 50, "lon": 44.3, "what": "x", "evidence": "y"}),
            "evidence": dict(good, to={"lat": 41.95, "lon": 44.38, "what": "x"}),
            "maxspeed": dict(good, maxspeed=150),
            "long": dict(good, to={"lat": 41.95, "lon": 44.3101, "what": "x", "evidence": "y"}),
        }
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "s.json"
            for word, s in cases.items():
                with self.subTest(word=word):
                    path.write_text(json.dumps({"sources": {"mia-1": {}}, "sections": [s]}), encoding="utf-8")
                    problems = []
                    gc.load_sections(path, problems)
                    self.assertTrue(any(word in p for p in problems), (word, problems))
            path.write_text(json.dumps({"sources": {"mia-1": {}}, "sections": [dict(good, id="mia-1-1", source="mia-1")]}),
                            encoding="utf-8")
            problems = []
            self.assertEqual(len(gc.load_sections(path, problems)), 1)
            self.assertEqual(problems, [])

    def test_the_curated_file(self):
        """Every section of config/average_speed_sections.json: a known source,
        an official police.ge page, ends 0.2-40 km apart, both directions of a
        stretch where the release has both."""
        data = json.loads((HERE.parent / "config" / CONFIG["cameras"]["sections"]["file"]).read_text(encoding="utf-8"))
        self.assertGreaterEqual(len(CFG["_sections"]), GATE["cameras"]["min_sections"])
        for name, src in data["sources"].items():
            self.assertTrue(src["url"].startswith("https://mia.police.ge/") or src["url"].startswith("https://police.ge/"),
                            name)
            self.assertRegex(src["date"], r"^20[12][0-9]-[01][0-9]-[0-3][0-9]$")
        for s in CFG["_sections"]:
            self.assertIn(s["source"], data["sources"])
            self.assertEqual(s["id"].split("-")[1], s["source"].split("-")[1])
        self.assertTrue(all(n.get("why") for n in data["not_placed"]))


# ---------------------------------------------------------------------------
# Hazards (scripts/geocoder_hazards.py)
# ---------------------------------------------------------------------------

HZ_NODES = {
    # W21: a residential street running north; 2101 a bump on it, 2102 a bump on its junction with the primary W29
    2100: (44.40, 41.60), 2101: (44.40, 41.601), 2102: (44.40, 41.602), 2103: (44.40, 41.603),
    2901: (44.399, 41.602), 2902: (44.401, 41.602),
    # W22: a footway with a hump on it
    2201: (44.41, 41.60), 2202: (44.41, 41.601), 2203: (44.41, 41.602),
    # W23 (a tertiary running east) and W24 (a live railway running north) cross at 2302
    2301: (44.42, 41.61), 2302: (44.421, 41.61), 2303: (44.422, 41.61), 2401: (44.421, 41.609), 2402: (44.421, 41.611),
    # W25 (a secondary running east) and W26 (an abandoned railway) cross at 2502
    2501: (44.43, 41.62), 2502: (44.431, 41.62), 2503: (44.432, 41.62), 2601: (44.431, 41.619), 2602: (44.431, 41.621),
    # W27: a road with a level crossing tag but no railway in the extract
    2701: (44.44, 41.63), 2702: (44.441, 41.63), 2703: (44.442, 41.63),
    # W28: in the occupied area; W30: in the band
    2801: (44.70, 42.30), 2802: (44.70, 42.301), 2803: (44.70, 42.302),
    3001: (44.597, 42.30), 3002: (44.597, 42.301), 3003: (44.597, 42.302),
}
HZ_WAYS = {
    21: ([2100, 2101, 2102, 2103], {"highway": "residential"}),
    29: ([2901, 2102, 2902], {"highway": "primary"}),
    22: ([2201, 2202, 2203], {"highway": "footway"}),
    23: ([2301, 2302, 2303], {"highway": "tertiary"}),
    24: ([2401, 2302, 2402], {"railway": "rail"}),
    25: ([2501, 2502, 2503], {"highway": "secondary"}),
    26: ([2601, 2502, 2602], {"railway": "abandoned"}),
    27: ([2701, 2702, 2703], {"highway": "unclassified"}),
    28: ([2801, 2802, 2803], {"highway": "residential"}),
    30: ([3001, 3002, 3003], {"highway": "residential"}),
}
HZ_TAGS = {
    2101: {"traffic_calming": "bump"}, 2102: {"traffic_calming": "hump"}, 2202: {"traffic_calming": "hump"},
    2302: {"railway": "level_crossing"}, 2502: {"railway": "level_crossing"}, 2702: {"railway": "level_crossing"},
    2802: {"traffic_calming": "bump"}, 3002: {"traffic_calming": "bump"},
    2203: {"traffic_calming": "table"},            # not a kind the app speaks
}


def hazard_collector():
    c = gb.Collector(CONFIG)
    for node_id, tags in sorted(HZ_TAGS.items()):
        lon, lat = HZ_NODES[node_id]
        c.node(node_id, tags, lat, lon)
    for way_id, (refs, tags) in sorted(HZ_WAYS.items()):
        c.way(way_id, tags, refs, [HZ_NODES[r] for r in refs])
    return c


class HazardsTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.stats = Counter()
        cls.rows = {r["osm"]: r for r in gh.build_hazards(hazard_collector().hazards, zones(),
                                                            CONFIG["hazards"], cls.stats)}

    def test_kept_rows(self):
        self.assertEqual(sorted(self.rows), ["n2101", "n2102", "n2302", "n3002"])
        self.assertEqual(self.rows["n2101"], {"osm": "n2101", "kind": "bump", "lat": 41.601, "lon": 44.4,
                                              "road_bearing": 0, "road_class": "residential", "band": 0})

    def test_a_junction_node_takes_the_bigger_road(self):
        self.assertEqual((self.rows["n2102"]["kind"], self.rows["n2102"]["road_class"],
                          self.rows["n2102"]["road_bearing"]), ("bump", "primary", 90))

    def test_level_crossing_needs_a_live_railway(self):
        self.assertEqual((self.rows["n2302"]["kind"], self.rows["n2302"]["road_bearing"]), ("rail", 90))
        self.assertNotIn("n2502", self.rows)
        self.assertNotIn("n2702", self.rows)
        self.assertEqual(self.stats["hazards_rail_without_live_railway"], 2)

    def test_off_road_occupied_and_band(self):
        self.assertNotIn("n2202", self.rows)
        self.assertEqual(self.stats["hazards_off_road_bump"], 1)
        self.assertNotIn("n2802", self.rows)
        self.assertEqual(self.stats["hazards_dropped_occupied"], 1)
        self.assertEqual(self.rows["n3002"]["band"], 1)
        self.assertEqual((self.stats["hazards_bump"], self.stats["hazards_rail"], self.stats["hazards"]), (3, 1, 4))

    def test_config_is_checked(self):
        self.assertEqual(gh.check_config(CONFIG["hazards"], gc.CAR_CLASSES), [])
        for change, word in (({"kinds": {"bump": [{"traffic_calming": ["bump"]}]}}, "exactly bump and rail"),
                             ({"road_classes": ["footway"]}, "car road classes"),
                             ({"live_railways": []}, "live_railways")):
            with self.subTest(word=word):
                problems = gh.check_config(dict(CONFIG["hazards"], **change), gc.CAR_CLASSES)
                self.assertTrue(any(word in p for p in problems), problems)

    def test_table_and_gate(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "g.sqlite"
            builder = gb.Builder(CONFIG, FOLD, zones(), hazard_collector()).build()
            with quiet():
                counts, _ = gb.write_database(db, builder, {"schema_version": gb.SCHEMA_VERSION})
            self.assertEqual(counts["hazards"], 4)
            con = sqlite3.connect(str(db))
            try:
                self.assertEqual([r[1] for r in con.execute("PRAGMA table_info(hazards)")], ["id", *gh.COLUMNS])
                problems, detail = gg.check_hazards(con, CONFIG)
                self.assertEqual(problems, [])
                self.assertEqual(detail["kinds"], {"bump": 3, "rail": 1})
                counts, kinds = gg.db_counts(con)
                self.assertEqual((counts["hazards"], kinds["hazards.bump"], kinds["hazards.rail"]), (4, 3, 1))
                for sql, word in (("UPDATE hazards SET kind = 'pothole' WHERE osm = 'n2101'", "kind"),
                                  ("UPDATE hazards SET road_bearing = 200 WHERE osm = 'n2101'", "road"),
                                  ("UPDATE hazards SET road_class = 'footway' WHERE osm = 'n2101'", "road"),
                                  ("UPDATE hazards SET osm = 'n2101'", "twice")):
                    with self.subTest(word=word):
                        con.execute("SAVEPOINT s")
                        con.execute(sql)
                        problems, _ = gg.check_hazards(con, CONFIG)
                        self.assertTrue(any(word in p for p in problems), (word, problems))
                        con.execute("ROLLBACK TO s")
            finally:
                con.close()


OSM_XML = """<?xml version="1.0" encoding="UTF-8"?>
<osm version="0.6" generator="test">
  <node id="1" version="1" lat="41.80" lon="44.10"/>
  <node id="2" version="1" lat="41.80" lon="44.12"/>
  <node id="3" version="1" lat="41.80" lon="44.14"/>
  <node id="4" version="1" lat="41.80005" lon="44.11"/>
  <node id="5" version="1" lat="41.80" lon="44.13"><tag k="highway" v="speed_camera"/><tag k="maxspeed" v="70"/></node>
  <way id="10" version="1"><nd ref="1"/><nd ref="2"/><nd ref="3"/><tag k="highway" v="primary"/><tag k="oneway" v="yes"/></way>
  <relation id="20" version="1">
    <member type="node" ref="1" role="from"/><member type="node" ref="4" role="device"/><member type="node" ref="2" role="to"/>
    <tag k="type" v="enforcement"/><tag k="enforcement" v="maxspeed"/><tag k="maxspeed" v="60"/>
  </relation>
</osm>
"""


@unittest.skipUnless(HAVE_OSMIUM, "pyosmium not installed")
class ReadFileTest(unittest.TestCase):
    """The pyosmium reading path: an untagged device node on no way is found
    by id in a pass of its own."""

    def test_read_osm_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "in.osm"
            src.write_text(OSM_XML, encoding="utf-8")
            c = gb.Collector(CONFIG)
            gb.read_pbf(src, c)
        self.assertEqual(sorted(c.cams.cameras), [5])
        self.assertEqual(c.cams.coords[4], (44.11, 41.80005))
        self.assertIn(10, c.cams.roads)
        rows = {r["osm"]: r for r in gc.build_cameras(c.cams, zones(), CFG)}
        self.assertEqual(sorted(rows), ["n5", "r20"])
        self.assertEqual((rows["r20"]["maxspeed"], rows["r20"]["heading"]), (60, 90))
        self.assertEqual((rows["n5"]["heading"], rows["n5"]["heading_src"]), (90, "oneway"))


if __name__ == "__main__":
    missing = [name for name, ok in (("pyosmium", HAVE_OSMIUM), ("shapely", HAVE_SHAPELY)) if not ok]
    if missing:
        print(f"{' and '.join(missing)} not installed: the tests that need them are skipped")
    unittest.main(verbosity=2)
