#!/usr/bin/env python3
"""Unit tests for clip.py on small made-up shapes. No downloads needed.

Run from the maps/ folder:
    python3 scripts/test_clip.py

Needs numpy and shapely. Without them every test is skipped (not failed),
so the file is safe to run on any machine. Tests that also need pyproj or
pyosmium skip on their own when those are missing.
"""

import json
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

try:
    import numpy  # noqa: F401
    import shapely  # noqa: F401
    from shapely.geometry import box
    import clip
    import fetch_boundaries
    HAVE_SHAPELY = True
except ImportError:
    HAVE_SHAPELY = False

try:
    import pyproj  # noqa: F401
    HAVE_PYPROJ = True
except ImportError:
    HAVE_PYPROJ = False

try:
    import osmium  # noqa: F401
    HAVE_OSMIUM = True
except ImportError:
    HAVE_OSMIUM = False

NEW_ID = 4_000_000_000

# A made-up world in plain degrees: the "country" is a 10 x 10 square, the
# no-go area a 2 x 2 square in its middle, the band 0.5 wider on each side.
TEST_CONFIG = {
    "soft_band": {"enabled": True,
                  "highway_classes": ["tertiary", "unclassified", "residential", "track"]},
    "tag_rules": {"four_wd_only_is_destination": True},
    "new_way_id_start": NEW_ID,
}


def make_clipper():
    zones = clip.Zones(georgia=box(0, 0, 10, 10), hard=box(4, 4, 6, 6),
                       band_outer=box(3.5, 3.5, 6.5, 6.5))
    return clip.WayClipper(zones, TEST_CONFIG)


def run(clipper, way_id, points, tags):
    """Feed one way; node ids are 1..n in order."""
    xs = [p[0] for p in points]
    ys = [p[1] for p in points]
    return clipper.process(way_id, list(range(1, len(points) + 1)), xs, ys, dict(tags))


@unittest.skipUnless(HAVE_SHAPELY, "numpy/shapely not installed")
class CarAccessTest(unittest.TestCase):
    """car_access must read tags the way Valhalla 3.6.3 graph.lua does."""

    def test_plain_road_is_open(self):
        self.assertEqual(clip.car_access({"highway": "residential"}), "yes")

    def test_access_no_blocks(self):
        self.assertEqual(clip.car_access({"highway": "residential", "access": "no"}), "no")

    def test_more_specific_tag_wins(self):
        self.assertEqual(clip.car_access({"highway": "track", "access": "no", "motorcar": "yes"}), "yes")
        self.assertEqual(clip.car_access({"highway": "track", "motor_vehicle": "no"}), "no")

    def test_permit_and_destination_are_destination_only(self):
        self.assertEqual(clip.car_access({"highway": "unclassified", "access": "permit"}), "destination")
        self.assertEqual(clip.car_access({"highway": "unclassified", "access": "destination"}), "destination")

    def test_unknown_values_are_ignored_like_graph_lua(self):
        self.assertEqual(clip.car_access({"highway": "unclassified", "access": "unknown"}), "yes")

    def test_semicolon_lists(self):
        self.assertEqual(clip.car_access({"highway": "service", "motor_vehicle": "no;yes"}), "yes")

    def test_footway_and_construction_are_closed(self):
        self.assertEqual(clip.car_access({"highway": "footway"}), "no")
        self.assertEqual(clip.car_access({"highway": "construction", "construction": "primary"}), "no")

    def test_make_destination_never_opens_a_road(self):
        tags = {"highway": "residential", "access": "no"}
        self.assertFalse(clip.make_destination_only(tags))
        self.assertNotIn("motorcar", tags)


@unittest.skipUnless(HAVE_SHAPELY, "numpy/shapely not installed")
class WayClipperTest(unittest.TestCase):

    def test_far_away_way_is_untouched(self):
        c = make_clipper()
        pieces, changed = run(c, 10, [(1, 1), (2, 1), (2, 2)], {"highway": "primary"})
        self.assertFalse(changed)
        self.assertEqual(pieces[0][1], [1, 2, 3])

    def test_way_through_the_zone_is_cut_in_two(self):
        c = make_clipper()
        pieces, changed = run(c, 11, [(1, 5), (3, 5), (5, 5), (7, 5), (9, 5)], {"highway": "primary"})
        self.assertTrue(changed)
        self.assertEqual([(p[0], p[1]) for p in pieces], [(11, [1, 2]), (NEW_ID, [4, 5])])
        self.assertIn(11, c.modified)

    def test_drop_mode_removes_the_whole_way(self):
        zones = clip.Zones(georgia=box(0, 0, 10, 10), hard=box(4, 4, 6, 6))
        c = clip.WayClipper(zones, dict(TEST_CONFIG, way_mode="drop"))
        pieces, _ = run(c, 18, [(1, 5), (3, 5), (5, 5), (7, 5), (9, 5)], {"highway": "primary"})
        self.assertEqual(pieces, [])
        self.assertIn(18, c.removed)

    def test_way_inside_the_zone_is_removed(self):
        c = make_clipper()
        pieces, changed = run(c, 12, [(4.5, 4.5), (5.5, 5.5)], {"highway": "residential"})
        self.assertEqual(pieces, [])
        self.assertIn(12, c.removed)
        self.assertEqual(c.stats["removed_no_go"], 1)

    def test_segment_crossing_without_a_node_inside_is_removed(self):
        # Both ends are outside the zone, but the straight line crosses it.
        c = make_clipper()
        pieces, _ = run(c, 13, [(3, 5), (7, 5)], {"highway": "trunk"})
        self.assertEqual(pieces, [])

    def test_road_leaving_the_country_stops_at_the_border(self):
        c = make_clipper()
        pieces, _ = run(c, 14, [(8, 1), (9.5, 1), (11, 1), (12, 1)], {"highway": "trunk"})
        self.assertEqual([p[1] for p in pieces], [[1, 2]])
        pieces, _ = run(c, 15, [(11, 1), (12, 1)], {"highway": "trunk"})
        self.assertEqual(pieces, [])
        self.assertEqual(c.stats["removed_outside_georgia"], 1)

    def test_node_without_location_counts_as_bad(self):
        c = make_clipper()
        pieces, _ = run(c, 16, [(1, 1), (float("nan"), float("nan")), (2, 2), (3, 2)],
                        {"highway": "residential"})
        self.assertEqual([p[1] for p in pieces], [[3, 4]])

    def test_non_roads_pass_through_even_inside(self):
        c = make_clipper()
        pieces, changed = run(c, 17, [(4.5, 4.5), (5.5, 4.5), (5.5, 5.5), (4.5, 4.5)],
                              {"building": "yes"})
        self.assertFalse(changed)
        self.assertEqual(len(pieces), 1)

    def test_minor_road_inside_the_band_becomes_destination_only(self):
        c = make_clipper()
        pieces, changed = run(c, 20, [(3.6, 3.6), (3.8, 3.6)], {"highway": "residential"})
        self.assertTrue(changed)
        self.assertEqual(pieces[0][2]["motorcar"], "destination")

    def test_main_road_inside_the_band_is_exempt(self):
        c = make_clipper()
        pieces, changed = run(c, 21, [(3.6, 3.6), (3.8, 3.6)], {"highway": "primary"})
        self.assertFalse(changed)
        self.assertNotIn("motorcar", pieces[0][2])

    def test_band_rule_keeps_existing_bans(self):
        c = make_clipper()
        pieces, changed = run(c, 22, [(3.6, 3.6), (3.8, 3.6)], {"highway": "track", "access": "no"})
        self.assertFalse(changed)

    def test_road_running_into_the_band_is_split_there(self):
        # (2.0) and (3.0) lie outside the band, (3.6) and (3.8) inside it.
        c = make_clipper()
        pieces, changed = run(c, 23, [(2.0, 3.6), (3.0, 3.6), (3.6, 3.6), (3.8, 3.6)],
                              {"highway": "residential"})
        self.assertTrue(changed)
        self.assertEqual([(p[0], p[1]) for p in pieces], [(23, [1, 2]), (NEW_ID, [2, 3, 4])])
        self.assertNotIn("motorcar", pieces[0][2])                  # outside: still free
        self.assertEqual(pieces[1][2]["motorcar"], "destination")   # inside: destination-only
        self.assertIn(23, c.modified)
        self.assertEqual(c.stats["band_split"], 1)

    def test_band_split_in_out_in(self):
        c = make_clipper()
        pieces, _ = run(c, 26, [(3.6, 3.0), (3.6, 3.6), (2.0, 3.6), (1.0, 3.6), (1.0, 3.7), (3.7, 3.7)],
                        {"highway": "unclassified"})
        flags = [p[2].get("motorcar") == "destination" for p in pieces]
        self.assertEqual([p[1] for p in pieces], [[1, 2, 3], [3, 4, 5], [5, 6]])
        self.assertEqual(flags, [True, False, True])

    def test_long_segment_crossing_the_band_counts(self):
        # Neither end is in the band, but the straight line crosses its corner.
        c = make_clipper()
        pieces, changed = run(c, 27, [(3.0, 4.2), (4.2, 3.0)], {"highway": "track"})
        self.assertTrue(changed)
        self.assertEqual(len(pieces), 1)
        self.assertEqual(pieces[0][2]["motorcar"], "destination")

    def test_band_does_not_split_a_road_it_cannot_change(self):
        c = make_clipper()
        pieces, changed = run(c, 28, [(2.0, 3.6), (3.6, 3.6)], {"highway": "residential", "access": "private"})
        self.assertFalse(changed)
        self.assertEqual(len(pieces), 1)

    def test_secondary_is_a_band_class_when_listed(self):
        zones = clip.Zones(georgia=box(0, 0, 10, 10), hard=box(4, 4, 6, 6), band_outer=box(3.5, 3.5, 6.5, 6.5))
        config = dict(TEST_CONFIG, soft_band={"enabled": True, "highway_classes": ["secondary"]})
        c = clip.WayClipper(zones, config)
        pieces, _ = run(c, 29, [(3.6, 3.6), (3.8, 3.6)], {"highway": "secondary"})
        self.assertEqual(pieces[0][2]["motorcar"], "destination")

    def test_segment_leaving_the_country_between_nodes_is_cut(self):
        # A U-shaped country: the notch 4..6 x 5..10 is abroad. Both nodes
        # are inside, the straight segment between them crosses the notch.
        country = box(0, 0, 10, 10).difference(box(4, 5, 6, 10))
        zones = clip.Zones(georgia=country, hard=box(20, 20, 21, 21))
        c = clip.WayClipper(zones, TEST_CONFIG)
        pieces, _ = run(c, 30, [(1, 8), (3, 8), (7, 8), (9, 8)], {"highway": "trunk"})
        self.assertEqual([p[1] for p in pieces], [[1, 2], [3, 4]])

    def test_ferry_leaving_the_country_is_dropped_whole(self):
        c = make_clipper()
        pieces, changed = run(c, 31, [(1, 1), (5, 1), (9, 1), (12, 1)], {"route": "ferry"})
        self.assertEqual(pieces, [])
        self.assertIn(31, c.removed)
        self.assertEqual(c.stats["removed_ferry"], 1)
        # a ferry wholly inside stays
        pieces, changed = run(c, 32, [(1, 1), (2, 1)], {"route": "ferry"})
        self.assertFalse(changed)

    def test_raw_road_probe(self):
        probe = clip.RawRoadProbe({"t1": (42.0, 44.0), "t2": (42.5, 44.5)})
        # a car-class road 30 m north of t1 (access=no still counts), a footway on t2
        dlat = 30 / 110574.0
        probe.feed(1, [43.99, 44.01], [42.0 + dlat, 42.0 + dlat], {"highway": "track", "access": "no"})
        probe.feed(2, [44.49, 44.51], [42.5, 42.5], {"highway": "footway"})
        report = probe.report()
        self.assertAlmostEqual(report["t1"]["raw_road_m"], 30.0, delta=0.5)
        self.assertEqual(report["t1"]["way_id"], 1)
        self.assertIsNone(report["t2"]["raw_road_m"])

    def test_cut_piece_in_the_band_is_tagged(self):
        # Runs from the band into the zone: the kept stub lies in the band.
        c = make_clipper()
        pieces, _ = run(c, 24, [(3.6, 5), (3.9, 5), (5, 5)], {"highway": "unclassified"})
        self.assertEqual(pieces[0][1], [1, 2])
        self.assertEqual(pieces[0][2]["motorcar"], "destination")

    def test_four_wd_only_becomes_destination(self):
        c = make_clipper()
        pieces, changed = run(c, 25, [(1, 8), (2, 8)], {"highway": "tertiary", "4wd_only": "yes"})
        self.assertTrue(changed)
        self.assertEqual(pieces[0][2]["motorcar"], "destination")
        self.assertEqual(c.stats["four_wd_destination"], 1)


@unittest.skipUnless(HAVE_SHAPELY, "numpy/shapely not installed")
class RelationTest(unittest.TestCase):

    def test_good_runs(self):
        self.assertEqual(clip.good_runs([True, True, False, True]), [(0, 2), (3, 4)])
        self.assertEqual(clip.good_runs([False, False]), [])

    def test_restriction_on_a_removed_way_is_dropped(self):
        members = [("w", 1, "from"), ("n", 5, "via"), ("w", 2, "to")]
        self.assertIsNone(clip.filter_relation({"type": "restriction"}, members, {2}, {}))

    def test_restriction_on_a_split_way_follows_the_via_node(self):
        members = [("w", 1, "from"), ("n", 5, "via"), ("w", 2, "to")]
        pieces = {2: [(2, [9, 8, 7]), (NEW_ID, [7, 6, 5, 4])]}
        kept = clip.filter_relation({"type": "restriction"}, members, set(), pieces)
        self.assertEqual(kept, [("w", 1, "from"), ("n", 5, "via"), ("w", NEW_ID, "to")])

    def test_restriction_whose_via_node_is_gone_is_dropped(self):
        members = [("w", 1, "from"), ("n", 5, "via"), ("w", 2, "to")]
        pieces = {2: [(2, [9, 8]), (NEW_ID, [6, 4])]}
        self.assertIsNone(clip.filter_relation({"type": "restriction"}, members, set(), pieces))

    def test_restriction_with_a_via_way_on_a_split_way_is_dropped(self):
        members = [("w", 1, "from"), ("w", 3, "via"), ("w", 2, "to")]
        pieces = {3: [(3, [1, 2]), (NEW_ID, [2, 3])]}
        self.assertIsNone(clip.filter_relation({"type": "restriction"}, members, set(), pieces))

    def test_other_relations_lose_only_missing_members(self):
        members = [("w", 1, ""), ("w", 2, ""), ("n", 9, "stop")]
        kept = clip.filter_relation({"type": "route"}, members, {2}, {})
        self.assertEqual(kept, [("w", 1, ""), ("n", 9, "stop")])

    def test_other_relations_list_every_piece_of_a_split_way(self):
        members = [("w", 1, "forward"), ("w", 2, "")]
        kept = clip.filter_relation({"type": "route"}, members, set(), {1: [(1, [1, 2]), (NEW_ID, [2, 3])]})
        self.assertEqual(kept, [("w", 1, "forward"), ("w", NEW_ID, "forward"), ("w", 2, "")])

    def test_relation_with_nothing_left_is_dropped(self):
        self.assertIsNone(clip.filter_relation({"type": "route"}, [("w", 2, "")], {2}, {}))


def write_test_config(folder, hard=100, occupied_version=1):
    """A config folder with a small square no-go area near Gori."""
    folder = Path(folder)
    (folder / "b").mkdir()

    def feature(osm_id, version, ring):
        return {"type": "FeatureCollection", "features": [{
            "type": "Feature",
            "properties": {"osm_type": "relation", "osm_id": osm_id, "osm_version": version},
            "geometry": {"type": "MultiPolygon", "coordinates": [[ring]]}}]}

    square = [[44.0, 42.2], [44.02, 42.2], [44.02, 42.22], [44.0, 42.22], [44.0, 42.2]]
    country = [[43.0, 41.5], [45.0, 41.5], [45.0, 43.0], [43.0, 43.0], [43.0, 41.5]]
    (folder / "b" / "zone.geojson").write_text(json.dumps(feature(1, 1, square)))
    (folder / "b" / "country.geojson").write_text(json.dumps(feature(2, 1, country)))
    config = {
        "version": 1,
        "metric_crs": "+proj=aeqd +lat_0=42.3 +lon_0=43.4 +ellps=WGS84 +units=m +no_defs",
        "country": {"name": "test", "osm_type": "relation", "osm_id": 2, "osm_version": 1,
                    "file": "b/country.geojson"},
        "no_go": [{"name": "zone", "osm_type": "relation", "osm_id": 1,
                   "osm_version": occupied_version, "file": "b/zone.geojson"}],
        "hard_buffer_m": hard,
        "soft_band": {"enabled": True, "outer_m": 500, "highway_classes": ["residential"]},
        "tag_rules": {"four_wd_only_is_destination": True},
        "new_way_id_start": NEW_ID,
    }
    path = folder / "clip.json"
    path.write_text(json.dumps(config))
    return path


@unittest.skipUnless(HAVE_SHAPELY and HAVE_PYPROJ, "shapely or pyproj not installed")
class ZonesTest(unittest.TestCase):

    def north_of_zone(self, metres):
        """A point this many metres north of the square's north edge."""
        lon, lat, _ = pyproj.Geod(ellps="WGS84").fwd(44.01, 42.22, 0, metres)
        return lon, lat

    def test_buffers_are_real_metres(self):
        with tempfile.TemporaryDirectory() as tmp:
            zones = clip.build_zones(clip.load_config(write_test_config(tmp)))
        for metres, in_hard, in_band in ((95, True, True), (105, False, True),
                                         (495, False, True), (505, False, False)):
            x, y = self.north_of_zone(metres)
            self.assertEqual(bool(shapely.intersects_xy(zones.hard, x, y)), in_hard, metres)
            self.assertEqual(bool(shapely.intersects_xy(zones.band_outer, x, y)), in_band, metres)

    def test_unsafe_buffer_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(ValueError):
                clip.load_config(write_test_config(tmp, hard=400))

    def test_frozen_file_must_match_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = clip.load_config(write_test_config(tmp, occupied_version=2))
            with self.assertRaises(ValueError):
                clip.build_zones(config)

    def test_hand_made_polygon_is_checked_by_hash(self):
        ring = [[44.1, 42.3], [44.11, 42.3], [44.11, 42.31], [44.1, 42.31], [44.1, 42.3]]
        derived = [{"osm_type": "relation", "osm_id": 7, "osm_version": 3}]
        with tempfile.TemporaryDirectory() as tmp:
            path = write_test_config(tmp)
            config = json.loads(path.read_text())
            (Path(tmp) / "b" / "hand.geojson").write_text(json.dumps({"type": "FeatureCollection", "features": [{
                "type": "Feature", "properties": {"hand_made": True, "derived_from": derived},
                "geometry": {"type": "MultiPolygon", "coordinates": [[ring]]}}]}))
            entry = {"name": "hand", "hand_made": True, "file": "b/hand.geojson",
                     "geometry_sha256": fetch_boundaries.geometry_sha256([[ring]]),
                     "derived_from": [dict(derived[0], file="b/src.geojson")]}
            config["no_go"].append(entry)
            path.write_text(json.dumps(config))
            zones = clip.build_zones(clip.load_config(path))
            self.assertTrue(shapely.intersects_xy(zones.hard, 44.105, 42.305))
            entry["geometry_sha256"] = "0" * 64
            path.write_text(json.dumps(config))
            with self.assertRaises(ValueError):
                clip.build_zones(clip.load_config(path))

    def test_geometry_hash_ignores_start_point_and_direction(self):
        ring = [[0, 0], [1, 0], [1, 1], [0, 1], [0, 0]]
        turned = [[1, 1], [1, 0], [0, 0], [0, 1], [1, 1]]
        self.assertEqual(fetch_boundaries.geometry_sha256([[ring]]), fetch_boundaries.geometry_sha256([[turned]]))
        moved = [[0, 0], [1, 0], [1, 1.0001], [0, 1], [0, 0]]
        self.assertNotEqual(fetch_boundaries.geometry_sha256([[ring]]), fetch_boundaries.geometry_sha256([[moved]]))


@unittest.skipUnless(HAVE_SHAPELY and HAVE_PYPROJ, "shapely or pyproj not installed")
class RealConfigTest(unittest.TestCase):
    """Loads the real config/clip.json and frozen polygons (small files)."""

    @classmethod
    def setUpClass(cls):
        cls.config = clip.load_config(clip.DEFAULT_CONFIG)
        cls.zones = clip.build_zones(cls.config)
        cls.routes = json.loads((HERE.parent / "config" / "gate_routes.json").read_text(encoding="utf-8"))

    def inside(self, geom, lat, lon):
        return bool(shapely.intersects_xy(geom, lon, lat))

    def test_places_named_in_the_law_are_no_go(self):
        for p in self.routes["inside_no_go"]:
            self.assertTrue(self.inside(self.zones.hard, p["lat"], p["lon"]), p["name"])

    def test_main_towns_are_legal(self):
        for key in ("tbilisi", "gori", "khashuri", "zugdidi", "batumi", "stepantsminda", "sarpi"):
            p = self.routes["places"][key]
            self.assertTrue(self.inside(self.zones.georgia, p["lat"], p["lon"]), key)
            self.assertFalse(self.inside(self.zones.hard, p["lat"], p["lon"]), key)

    def test_s1_near_khurvaleti_is_outside_the_hard_zone(self):
        self.assertFalse(self.inside(self.zones.hard, 42.0199286, 44.3096653))

    def test_targets_past_the_border_crossings_are_outside_georgia(self):
        for key in ("upper_lars_ru", "mamison_ru", "sadakhlo_am", "red_bridge_az", "sarpi_tr"):
            p = self.routes["places"][key]
            self.assertFalse(self.inside(self.zones.georgia, p["lat"], p["lon"]), key)

    def test_gate_test_data_is_consistent(self):
        import gate
        checker = gate.Checker(self.config)
        self.assertEqual(gate.check_counts(self.routes), [])
        self.assertEqual(gate.check_test_data(self.routes, checker), [])

    def test_release_tag_format(self):
        import manifest
        self.assertEqual(manifest.release_tag("2026-09-24T20:21:02Z", "3F2A1B0C99"),
                         "osm-20260924T202102Z-3f2a1b0c")
        with self.assertRaises(ValueError):
            manifest.release_tag("2026-09-24T20:21:02Z\nBASH_ENV=x")


OSM_XML = """<?xml version='1.0' encoding='UTF-8'?>
<osm version="0.6" generator="test">
  {nodes}
  <way id="100" version="1"><nd ref="1"/><nd ref="2"/><nd ref="3"/><nd ref="4"/><nd ref="5"/>
    <tag k="highway" v="primary"/></way>
  <way id="101" version="1"><nd ref="6"/><nd ref="7"/><tag k="highway" v="residential"/></way>
  <way id="102" version="1"><nd ref="8"/><nd ref="9"/><tag k="highway" v="residential"/></way>
  <way id="103" version="1"><nd ref="6"/><nd ref="7"/><nd ref="3"/><nd ref="6"/><tag k="building" v="yes"/></way>
  <relation id="500" version="1"><member type="way" ref="100" role="from"/>
    <member type="node" ref="2" role="via"/><member type="way" ref="102" role="to"/>
    <tag k="type" v="restriction"/><tag k="restriction" v="no_left_turn"/></relation>
  <relation id="501" version="1"><member type="way" ref="101" role=""/>
    <member type="way" ref="102" role=""/><member type="way" ref="100" role=""/>
    <tag k="type" v="route"/><tag k="route" v="road"/></relation>
  <relation id="502" version="1"><member type="way" ref="100" role="from"/>
    <member type="node" ref="3" role="via"/><member type="way" ref="102" role="to"/>
    <tag k="type" v="restriction"/><tag k="restriction" v="no_u_turn"/></relation>
</osm>
"""
NODES = {1: (1, 5), 2: (3, 5), 3: (5, 5), 4: (7, 5), 5: (9, 5), 6: (4.5, 4.5), 7: (5.5, 5.5),
         8: (1, 1), 9: (2, 1)}


@unittest.skipUnless(HAVE_SHAPELY and HAVE_OSMIUM, "shapely or pyosmium not installed")
class FileRoundTripTest(unittest.TestCase):
    """The whole read-clip-write path on a tiny OSM file."""

    def test_round_trip(self):
        nodes = "\n  ".join(f'<node id="{i}" version="1" lat="{y}" lon="{x}"/>'
                            for i, (x, y) in NODES.items())
        with tempfile.TemporaryDirectory() as tmp:
            src, dst = Path(tmp) / "in.osm", Path(tmp) / "out.osm"
            src.write_text(OSM_XML.format(nodes=nodes))
            clipper = make_clipper()
            stats = clip.run_clip(src, dst, clipper)

            ways, relations, node_ids, order = {}, {}, set(), []
            for obj in osmium.FileProcessor(str(dst)):
                if obj.is_node():
                    node_ids.add(obj.id)
                elif obj.is_way():
                    ways[obj.id] = ([n.ref for n in obj.nodes], dict(obj.tags))
                    order.append(obj.id)
                elif obj.is_relation():
                    relations[obj.id] = [(m.type, m.ref) for m in obj.members]

        self.assertEqual(node_ids, set(NODES))                 # nodes pass through
        self.assertEqual(ways[100][0], [1, 2])                  # cut, first piece keeps its id
        self.assertEqual(ways[NEW_ID][0], [4, 5])               # second piece, fresh id
        self.assertNotIn(101, ways)                             # inside the zone: gone
        self.assertIn(102, ways)
        self.assertIn(103, ways)                                # a building is not a road
        self.assertEqual(order, sorted(order))                  # fresh ids come last
        self.assertEqual(relations[500], [("w", 100), ("n", 2), ("w", 102)])  # via node 2 is on piece 100
        self.assertNotIn(502, relations)                        # via node 3 was cut away
        self.assertEqual(relations[501], [("w", 102), ("w", 100), ("w", NEW_ID)])  # lost 101, lists both pieces
        self.assertEqual(stats["relations_dropped"], 1)


if __name__ == "__main__":
    if not HAVE_SHAPELY:
        print("numpy/shapely not installed: all clip tests are skipped")
    unittest.main(verbosity=2)
