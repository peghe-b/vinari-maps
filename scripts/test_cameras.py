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
        self.assertEqual(gb.SCHEMA_VERSION, 3)

    def test_gate_accepts_good_rows(self):
        # Three of the made-up cameras stand far from every road on purpose.
        gate = dict(GATE, cameras=dict(GATE["cameras"], min_road_share=0.75))
        problems, detail = gg.check_cameras(self.con, CONFIG, gate)
        self.assertEqual(problems, [])
        self.assertEqual((detail["rows"], detail["on_road"], detail["with_maxspeed"]), (12, 9, 11))
        problems, _ = gg.check_cameras(self.con, CONFIG, GATE)
        self.assertEqual(problems, ["cameras: only 9 of 12 lie on a car road (at least 90%)"])

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
