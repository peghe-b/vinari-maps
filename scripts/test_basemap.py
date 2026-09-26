#!/usr/bin/env python3
"""Unit tests for basemap.py on small made-up archives. No downloads needed.

Run from the maps/ folder:
    python3 scripts/test_basemap.py

The PMTiles reader, the vector tile decoder and most checks need only the
standard library. The no-go road scan needs numpy, shapely and pyproj; the
end-to-end test (gate, report, manifest.py --extend) also needs pyosmium.
Tests whose packages are missing are skipped, not failed.

The archives here are written by a small PMTiles v3 writer and vector tile
encoder in this file, following the specs, so the reader is checked against
an independent encoding. The tile id numbers were also checked against the
reference pmtiles package and against OpenFreeMap's planet archive (tile
14/10230/6100, central Tbilisi, is id 315972059 in both).
"""

import copy
import gzip
import json
import struct
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import basemap  # noqa: E402  (standard library only)

try:
    import numpy  # noqa: F401
    import shapely
    import pyproj  # noqa: F401
    HAVE_GEO = True
except ImportError:
    HAVE_GEO = False

try:
    import osmium
    HAVE_OSMIUM = True
except ImportError:
    HAVE_OSMIUM = False


# ---------------------------------------------------------------------------
# A tiny PMTiles v3 writer and MVT encoder, for the tests only
# ---------------------------------------------------------------------------

def varint(n):
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        if n:
            out.append(b | 0x80)
        else:
            out.append(b)
            return bytes(out)


def zigzag(n):
    return (n << 1) ^ (n >> 63)


def pb_varint(field, value):
    return varint(field << 3) + varint(value)


def pb_bytes(field, data):
    return varint((field << 3) | 2) + varint(len(data)) + data


def encode_value(v):
    if isinstance(v, bool):
        return pb_varint(7, int(v))
    if isinstance(v, str):
        return pb_bytes(1, v.encode("utf-8"))
    if isinstance(v, int):
        return pb_varint(5, v) if v >= 0 else pb_varint(6, zigzag(v))
    return varint((3 << 3) | 1) + struct.pack("<d", v)


def encode_geometry(gtype, parts):
    """parts: lists of (x, y); polygon rings are given without the closing point."""
    ints, cx, cy = [], 0, 0
    for part in parts:
        x, y = part[0]
        ints += [1 | (1 << 3), zigzag(x - cx), zigzag(y - cy)]
        cx, cy = x, y
        rest = part[1:]
        if gtype == basemap.POINT:
            continue
        ints.append(2 | (len(rest) << 3))
        for x, y in rest:
            ints += [zigzag(x - cx), zigzag(y - cy)]
            cx, cy = x, y
        if gtype == basemap.POLYGON:
            ints.append(7 | (1 << 3))
    return ints


def encode_layer(name, features, extent=4096):
    """features: (geometry type, properties, parts)."""
    keys, values, body = [], [], b""
    for gtype, props, parts in features:
        tags = []
        for k, v in props.items():
            if k not in keys:
                keys.append(k)
            tv = (type(v).__name__, v)
            if tv not in values:
                values.append(tv)
            tags += [keys.index(k), values.index(tv)]
        feature = (pb_bytes(2, b"".join(varint(t) for t in tags)) + pb_varint(3, gtype)
                   + pb_bytes(4, b"".join(varint(i) for i in encode_geometry(gtype, parts))))
        body += pb_bytes(2, feature)
    layer = (pb_varint(15, 2) + pb_bytes(1, name.encode("utf-8")) + body
             + b"".join(pb_bytes(3, k.encode("utf-8")) for k in keys)
             + b"".join(pb_bytes(4, encode_value(v)) for _, v in values) + pb_varint(5, extent))
    return pb_bytes(3, layer)


def encode_tile(layers):
    """layers: {name: [features]}"""
    return b"".join(encode_layer(name, feats) for name, feats in layers.items())


def serialize_directory(entries):
    buf = bytearray(varint(len(entries)))
    last = 0
    for e in entries:
        buf += varint(e[0] - last)
        last = e[0]
    for e in entries:
        buf += varint(e[3])
    for e in entries:
        buf += varint(e[2])
    for i, e in enumerate(entries):
        if i > 0 and e[1] == entries[i - 1][1] + entries[i - 1][2]:
            buf += varint(0)
        else:
            buf += varint(e[1] + 1)
    return gzip.compress(bytes(buf))


def build_archive(path, tiles, metadata, bounds, minzoom=0, maxzoom=14, leaf_size=None, center=None):
    """tiles: {(z, x, y): uncompressed tile bytes}. Identical tiles are stored
    once; runs of consecutive ids with the same content share one entry."""
    by_id = sorted((basemap.zxy_to_tileid(*k), v) for k, v in tiles.items())
    data, where, entries = bytearray(), {}, []
    for tile_id, raw in by_id:
        blob = gzip.compress(raw, mtime=0)
        if blob not in where:
            where[blob] = (len(data), len(blob))
            data += blob
        offset, length = where[blob]
        last = entries[-1] if entries else None
        if last and last[1] == offset and last[0] + last[3] == tile_id:
            entries[-1] = (last[0], last[1], last[2], last[3] + 1)
        else:
            entries.append((tile_id, offset, length, 1))
    leaves = b""
    if leaf_size:
        root = []
        for i in range(0, len(entries), leaf_size):
            chunk = serialize_directory(entries[i:i + leaf_size])
            root.append((entries[i][0], len(leaves), len(chunk), 0))
            leaves += chunk
        root_dir = serialize_directory(root)
    else:
        root_dir = serialize_directory(entries)
    meta = gzip.compress(json.dumps(metadata).encode("utf-8"))
    root_off = basemap.HEADER_BYTES
    meta_off = root_off + len(root_dir)
    leaf_off = meta_off + len(meta)
    data_off = leaf_off + len(leaves)
    addressed = sum(e[3] for e in entries)
    w, s, e, n = bounds
    cx, cy = center or ((w + e) / 2, (s + n) / 2)
    header = (b"PMTiles" + bytes([3])
              + struct.pack("<11Q", root_off, len(root_dir), meta_off, len(meta), leaf_off, len(leaves),
                            data_off, len(data), addressed, len(entries), len(where))
              + struct.pack("<6B", 1, 2, 2, 1, minzoom, maxzoom)
              + struct.pack("<4i", *(round(v * 1e7) for v in bounds))
              + bytes([7]) + struct.pack("<2i", round(cx * 1e7), round(cy * 1e7)))
    assert len(header) == basemap.HEADER_BYTES
    Path(path).write_bytes(header + root_dir + meta + leaves + bytes(data))


def square(x, y, size, clockwise=True):
    ring = [(x, y), (x + size, y), (x + size, y + size), (x, y + size)]
    return ring if clockwise else ring[::-1]


# ---------------------------------------------------------------------------

class TileMathTest(unittest.TestCase):
    def test_known_ids(self):
        # PMTiles spec examples, and a real tile checked against the reference
        # package and OpenFreeMap's planet archive.
        self.assertEqual(basemap.zxy_to_tileid(0, 0, 0), 0)
        self.assertEqual([basemap.zxy_to_tileid(1, *xy) for xy in ((0, 0), (0, 1), (1, 1), (1, 0))], [1, 2, 3, 4])
        self.assertEqual(basemap.zxy_to_tileid(2, 0, 0), 5)
        self.assertEqual(basemap.zxy_to_tileid(14, 10230, 6100), 315972059)

    def test_round_trip(self):
        for z in range(8):
            ids = []
            for x in range(1 << z):
                for y in range(1 << z):
                    t = basemap.zxy_to_tileid(z, x, y)
                    self.assertEqual(basemap.tileid_to_zxy(t), (z, x, y))
                    ids.append(t)
            self.assertEqual(sorted(ids), list(range(basemap.zoom_start(z), basemap.zoom_start(z + 1))))
        self.assertEqual(basemap.tileid_to_zxy(315972059), (14, 10230, 6100))

    def test_lonlat(self):
        self.assertEqual(basemap.lonlat_to_tile(44.8015, 41.6934, 14), (10230, 6100))
        w, s, e, n = basemap.tile_bounds(14, 10230, 6100)
        self.assertTrue(w < 44.8015 < e and s < 41.6934 < n)


class DecoderTest(unittest.TestCase):
    def test_values_and_geometry(self):
        tile = encode_tile({"t": [
            (basemap.LINESTRING, {"name": "რუსთაველის გამზირი", "rank": 3, "neg": -7, "flag": True, "w": 1.5},
             [[(0, 0), (100, 50), (200, 50)], [(10, 10), (20, 20)]]),
            (basemap.POLYGON, {"class": "x"}, [square(0, 0, 10), square(20, 20, 10), square(22, 22, 2, False)]),
            (basemap.POINT, {}, [[(5, 6)]]),
        ]})
        layer = basemap.decode_tile(tile)["t"]
        self.assertEqual(len(layer), 3)
        feats = list(layer.features())
        gtype, props, geom = feats[0]
        self.assertEqual(props, {"name": "რუსთაველის გამზირი", "rank": 3, "neg": -7, "flag": True, "w": 1.5})
        self.assertEqual(basemap.geometry_parts(geom), [[(0, 0), (100, 50), (200, 50)], [(10, 10), (20, 20)]])
        self.assertTrue(basemap.georgian(props["name"]))
        self.assertFalse(basemap.georgian("Rustaveli Avenue"))
        _, _, poly = feats[1]
        self.assertEqual(basemap.outer_rings(poly), 2)          # the hole does not count
        self.assertEqual(basemap.geometry_parts(poly)[0][-1], (0, 0))  # rings are closed
        self.assertEqual(basemap.geometry_parts(feats[2][2]), [[(5, 6)]])

    def test_wanted_layers_and_lonlat(self):
        tile = encode_tile({"a": [(basemap.POINT, {}, [[(0, 0)]])], "b": [(basemap.POINT, {}, [[(4096, 4096)]])]})
        self.assertEqual(set(basemap.decode_tile(tile, {"b"})), {"b"})
        (lon, lat), = basemap.to_lonlat(14, 10230, 6100, 4096, [(4096, 4096)])
        w, s, e, n = basemap.tile_bounds(14, 10230, 6100)
        self.assertAlmostEqual(lon, e)
        self.assertAlmostEqual(lat, s)

    def test_bad_data_fails(self):
        with self.assertRaises(ValueError):
            basemap.geometry_parts([2 | (1 << 3), 0, 0])       # LineTo before MoveTo
        with self.assertRaises(ValueError):
            basemap.read_varint(b"\xff\xff", 0)                # truncated


class ArchiveTest(unittest.TestCase):
    META = {"name": "x", "vector_layers": []}
    BOUNDS = (44.0, 41.0, 46.0, 43.0)

    def tiles(self):
        tiles = {}
        for z in range(0, 5):
            for x in range(1 << z):
                tiles[(z, x, 0)] = encode_tile({"l": [(basemap.POINT, {"z": z, "x": x}, [[(1, 1)]])]})
        sea = encode_tile({"water": [(basemap.POLYGON, {"class": "ocean"}, [square(0, 0, 4096)])]})
        for y in range(1 << 4):
            tiles[(4, 3, y)] = sea
        return tiles

    def check_archive(self, leaf_size):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "t.pmtiles"
            tiles = self.tiles()
            build_archive(path, tiles, self.META, self.BOUNDS, maxzoom=4, leaf_size=leaf_size)
            pm = basemap.PMTiles.open(path)
            try:
                self.assertEqual(pm.metadata(), self.META)
                for (z, x, y), raw in tiles.items():
                    self.assertEqual(pm.tile(z, x, y), raw, f"{z}/{x}/{y}")
                self.assertIsNone(pm.tile(4, 2, 5))
                walked = list(pm.walk())
                h = pm.header
                self.assertEqual(sum(e[3] for e in walked), h["addressed_tiles"])
                self.assertEqual(len(walked), h["tile_entries"])
                self.assertEqual(len({e[1] for e in walked}), h["tile_contents"])
                self.assertLess(h["tile_contents"], h["addressed_tiles"])   # the sea is stored once
            finally:
                pm.close()

    def test_flat_directory(self):
        self.check_archive(None)

    def test_leaf_directories(self):
        self.check_archive(3)

    def test_not_pmtiles(self):
        with self.assertRaises(ValueError):
            basemap.parse_header(b"\0" * 127)


def small_config(**gate):
    config = basemap.load_config()
    config = copy.deepcopy(config)
    config["build"]["maxzoom"] = 4
    config["gate"].update({"min_tiles_at_maxzoom": 1, "min_bytes": 1, "max_bytes": 10 ** 9,
                           "no_go_scan": dict(config["gate"]["no_go_scan"], zoom=4)})
    config["gate"].update(gate)
    return config


class DirectoryAndHeaderTest(unittest.TestCase):
    def test_counts_and_zooms(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "t.pmtiles"
            archive = ArchiveTest()
            bounds = (-180.0, -85.0, 180.0, 85.0)
            build_archive(path, archive.tiles(), {}, bounds, maxzoom=4, leaf_size=4)
            pm = basemap.PMTiles.open(path)
            try:
                problems, detail, z_tiles = basemap.scan_directory(pm, small_config())
                self.assertEqual(problems, [])
                self.assertEqual(detail["per_zoom"]["4"], 16 + 16 - 1)   # row y=0 plus column x=3
                self.assertEqual(len(z_tiles), 31)
                problems, _, _ = basemap.scan_directory(pm, small_config(min_tiles_at_maxzoom=100))
                self.assertTrue(any("fewer than 100" in p for p in problems))
                problems, _, _ = basemap.scan_directory(pm, small_config(max_tile_bytes=10))
                self.assertTrue(any("more than 10" in p for p in problems))
            finally:
                pm.close()

    def test_tiles_outside_bounds_and_missing_zoom(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "t.pmtiles"
            tiles = {(z, 0, 0): encode_tile({"l": []}) for z in (0, 1, 2, 4)}
            build_archive(path, tiles, {}, (44.0, 41.0, 46.0, 43.0), maxzoom=4)
            pm = basemap.PMTiles.open(path)
            try:
                problems, _, _ = basemap.scan_directory(pm, small_config())
            finally:
                pm.close()
            text = " ".join(problems)
            self.assertIn("no tiles at zoom [3]", text)
            self.assertIn("outside the bounds", text)

    def test_header(self):
        config = basemap.load_config()
        bounds = basemap.planetiler_bounds(config)
        self.assertEqual(bounds, (39.78, 40.95, 46.84, 43.69))
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "t.pmtiles"
            build_archive(path, {(0, 0, 0): b""}, {"a": 1}, bounds)
            pm = basemap.PMTiles.open(path)
            h = dict(pm.header)
            pm.close()
            size = path.stat().st_size
            self.assertEqual(basemap.check_header(h, size, config, bounds)[0], [])
            bad = dict(h, tile_type=2, tile_compression=1, max_zoom=15, clustered=0,
                       bounds=[-180.0, -85.0, 180.0, 85.0])
            text = " ".join(basemap.check_header(bad, size, config, bounds)[0])
            for word in ("tile type png", "tile compression none", "zoom 0-15", "not clustered", "bounds"):
                self.assertIn(word, text)
            self.assertTrue(basemap.check_header(h, size - 1, config, bounds)[0])   # truncated file


GOOD_FIELDS = {"name": "String", "name_en": "String", "name_de": "String", "name:latin": "String",
               "name:nonlatin": "String", "name_int": "String", "name:ka": "String", "name:en": "String",
               "class": "String"}


def good_metadata(config, stamp="2026-09-24T20:21:02Z"):
    return {
        "attribution": '<a href="https://www.openmaptiles.org/">&copy; OpenMapTiles</a> '
                       '<a href="https://www.openstreetmap.org/copyright">&copy; OpenStreetMap contributors</a>',
        "description": config["build"]["archive_description"],
        "planetiler:version": config["planetiler"]["version"], "planetiler:githash": "abc",
        "version": config["planetiler"]["openmaptiles_schema"], "format": "pbf",
        "planetiler:osm:osmosisreplicationtime": stamp,
        "vector_layers": [{"id": name, "fields": dict(GOOD_FIELDS)} for name in config["gate"]["required_layers"]],
    }


class MetadataTest(unittest.TestCase):
    def setUp(self):
        self.config = basemap.load_config()

    def test_good(self):
        problems, detail = basemap.check_metadata(good_metadata(self.config), self.config, "2026-09-24T20:21:02Z")
        self.assertEqual(problems, [])
        self.assertEqual(len(detail["layers"]), 16)

    def test_bad(self):
        meta = good_metadata(self.config, stamp="2026-09-17T20:21:02Z")
        meta["planetiler:version"] = "0.9.0"
        meta["attribution"] = "OpenStreetMap"
        meta["vector_layers"] = [l for l in meta["vector_layers"] if l["id"] != "housenumber"]
        meta["vector_layers"][0]["fields"]["name:ru"] = "String"
        for layer in meta["vector_layers"]:
            if layer["id"] == "place":
                del layer["fields"]["name:ka"]
        text = " ".join(basemap.check_metadata(meta, self.config, "2026-09-24T20:21:02Z")[0])
        for word in ("OpenMapTiles", "0.9.0", "not built from this extract", "housenumber",
                     "place has no name:ka", "name:ru"):
            self.assertIn(word, text)

    def test_credits_need_links_and_the_odbl_notice(self):
        meta = good_metadata(self.config)
        meta["attribution"] = "© OpenMapTiles © OpenStreetMap contributors"   # the words, no links
        meta["description"] = "OpenMapTiles for Georgia"                       # no licence inside the file
        text = " ".join(basemap.check_metadata(meta, self.config, "2026-09-24T20:21:02Z")[0])
        for word in ("openmaptiles.org", "openstreetmap.org/copyright", "opendatacommons.org/licenses/odbl"):
            self.assertIn(word, text)
        # The configured description itself carries the notice the gate needs.
        self.assertIn("opendatacommons.org/licenses/odbl", self.config["build"]["archive_description"])

    def test_report_credits(self):
        self.assertTrue(basemap.OPENMAPTILES_LICENCE_URL.endswith("/v3.16/LICENSE.md"))
        sources = [{"name": s["name"], "about": s["about"], "licence": s["licence"],
                    "attribution": s["attribution"], "url": s["url"]} for s in self.config["sources"]]
        credit = basemap.credit_sources(sources)
        licences = [c["licence"] for c in credit]
        self.assertEqual(licences[:2], ["ODbL-1.0", "CC-BY-4.0"])
        self.assertIn("public domain", licences)
        self.assertEqual(sum(1 for c in credit if c["licence"].startswith("ODbL")), 3)  # OSM, water, lakes
        self.assertIn("ODbL 4.7", basemap.LICENCE_NOTE)
        self.assertIn("https://openmaptiles.org/", basemap.LICENCE_NOTE)
        self.assertIn("LGPL", basemap.TOOL_LICENCE)

    def test_time_formats(self):
        # Java prints the instant; pyosmium gives Geofabrik's header string.
        problems, _ = basemap.check_metadata(good_metadata(self.config, "2026-09-24T20:21:00Z"),
                                             self.config, "2026-09-24T20:21:00Z")
        self.assertEqual(problems, [])


def city_tile(z, name_ka):
    return encode_tile({"place": [(basemap.POINT, {"class": "city", "name": name_ka, "name:ka": name_ka,
                                                   "name:en": "x"}, [[(2048, 2048)]])]})


def rich_tile(streets=150, ka_share=0.9, buildings=1200, pois=1500, pois_ka=500, housenumbers=120, sea=True):
    lines = []
    for i in range(streets):
        props = {"class": "minor", "name": f"street {i}"}
        if i < streets * ka_share:
            props["name:ka"] = f"ქუჩა {i}"
        lines.append((basemap.LINESTRING, props, [[(i, 0), (i, 100)]]))
    rings = [square((i % 60) * 60, (i // 60) * 60, 40) for i in range(buildings)]
    layers = {
        "transportation_name": lines,
        "building": [(basemap.POLYGON, {"render_height": 10}, rings[:len(rings) // 2]),
                     (basemap.POLYGON, {"render_height": 20}, rings[len(rings) // 2:])],
        "poi": [(basemap.POINT, dict({"class": "shop"}, **({"name:ka": "მაღაზია"} if i < pois_ka else {})),
                 [[(i % 4096, 7)]]) for i in range(pois)],
        "housenumber": [(basemap.POINT, {"housenumber": str(i)}, [[(i, 9)]]) for i in range(housenumbers)],
    }
    if sea:
        layers["water"] = [(basemap.POLYGON, {"class": "ocean"}, [square(0, 0, 4096)])]
    return encode_tile(layers)


class SpotCheckTest(unittest.TestCase):
    def archive(self, tmp, tiles):
        path = Path(tmp) / "t.pmtiles"
        build_archive(path, tiles, {}, basemap.planetiler_bounds(basemap.load_config()))
        return basemap.PMTiles.open(path)

    def test_spots_and_labels(self):
        config = basemap.load_config()
        tiles = {}
        for spot in config["gate"]["spot_checks"]:
            x, y = basemap.lonlat_to_tile(spot["lon"], spot["lat"], spot["zoom"])
            tiles[(spot["zoom"], x, y)] = rich_tile()
        for label in config["gate"]["city_labels"]:
            x, y = basemap.lonlat_to_tile(label["lon"], label["lat"], label["zoom"])
            tiles[(label["zoom"], x + 1, y)] = city_tile(label["zoom"], label["name_ka"])   # a neighbour
        with tempfile.TemporaryDirectory() as tmp:
            pm = self.archive(tmp, tiles)
            try:
                for spot in config["gate"]["spot_checks"]:
                    problems, detail = basemap.check_spot(pm, spot)
                    self.assertEqual(problems, [], spot["name"])
                    self.assertEqual(detail["buildings"], 1200)
                for label in config["gate"]["city_labels"]:
                    self.assertEqual(basemap.check_city_label(pm, label)[0], [], label["name"])
                missing = dict(config["gate"]["city_labels"][0], name_ka="არსად")
                self.assertTrue(basemap.check_city_label(pm, missing)[0])
            finally:
                pm.close()

    def test_poor_tile_fails(self):
        spot = basemap.load_config()["gate"]["spot_checks"][1]     # Batumi, wants the sea
        x, y = basemap.lonlat_to_tile(spot["lon"], spot["lat"], 14)
        with tempfile.TemporaryDirectory() as tmp:
            pm = self.archive(tmp, {(14, x, y): rich_tile(streets=40, ka_share=0.2, buildings=10, sea=False)})
            try:
                text = " ".join(basemap.check_spot(pm, spot)[0])
            finally:
                pm.close()
        for word in ("Georgian name:ka", "buildings", "ocean"):
            self.assertIn(word, text)

    def test_missing_tile_fails(self):
        spot = basemap.load_config()["gate"]["spot_checks"][0]
        with tempfile.TemporaryDirectory() as tmp:
            pm = self.archive(tmp, {(0, 0, 0): b""})
            try:
                self.assertIn("no tile", basemap.check_spot(pm, spot)[0][0])
            finally:
                pm.close()


class RoadClassTest(unittest.TestCase):
    def test_classes(self):
        scan = basemap.load_config()["gate"]["no_go_scan"]
        yes = [{"class": c} for c in ("motorway", "minor", "track", "path", "ferry", "service")]
        yes.append({"class": "primary_construction"})
        no = [{"class": c} for c in ("rail", "transit", "aerialway", "pier", "bridge")]
        no.append({"class": "path", "subclass": "platform"})
        no.append({})
        for props in yes:
            self.assertTrue(basemap.road_class(props, scan), props)
        for props in no:
            self.assertFalse(basemap.road_class(props, scan), props)


@unittest.skipUnless(HAVE_GEO, "numpy, shapely or pyproj not installed")
class NoGoScanTest(unittest.TestCase):
    """A made-up world near 0,0: the country is a 2 x 2 degree square, the
    no-go area a small square inside it."""

    Z = 14

    def setUp(self):
        from shapely.geometry import box
        hard = box(0.10, 0.10, 0.30, 0.30)
        country = box(-1, -1, 1, 1)
        self.areas = basemap.NoGoAreas(hard=hard, country=country, legal=country.difference(hard), no_go=hard)
        self.scan = dict(basemap.load_config()["gate"]["no_go_scan"], min_tiles_in_no_go=1,
                         min_buildings_in_no_go=1)

    def run_scan(self, tiles):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "t.pmtiles"
            build_archive(path, tiles, {}, (-2, -2, 2, 2))
            pm = basemap.PMTiles.open(path)
            try:
                z_tiles = {}
                for tile_id, offset, length, run in pm.walk():
                    for t in range(tile_id, tile_id + run):
                        z_tiles[t] = (offset, length)
                return basemap.scan_no_go(pm, z_tiles, self.areas, self.scan, self.Z)
            finally:
                pm.close()

    def tile_at(self, lon, lat):
        return (self.Z,) + basemap.lonlat_to_tile(lon, lat, self.Z)

    def test_clipped_map_passes(self):
        inside = encode_tile({
            "transportation": [(basemap.LINESTRING, {"class": "rail", "subclass": "rail"}, [[(0, 0), (4096, 4096)]]),
                               (basemap.LINESTRING, {"class": "path", "subclass": "platform"}, [[(9, 9), (99, 9)]]),
                               (basemap.POLYGON, {"class": "path", "subclass": "pedestrian"}, [square(5, 5, 50)])],
            "building": [(basemap.POLYGON, {}, [square(100, 100, 20), square(200, 200, 20)])],
            "place": [(basemap.POINT, {"class": "city", "name:ka": "ცხინვალი"}, [[(10, 10)]])],
        })
        legal_road = encode_tile({"transportation": [(basemap.LINESTRING, {"class": "primary"},
                                                      [[(0, 0), (4096, 4096)]])]})
        problems, detail = self.run_scan({self.tile_at(0.2, 0.2): inside, self.tile_at(-0.5, -0.5): legal_road})
        self.assertEqual(problems, [])
        self.assertEqual(detail["tiles_checked"], 1)            # the legal tile is skipped
        self.assertEqual(detail["buildings_in_no_go"], 2)
        self.assertEqual(detail["examples"]["places_in_no_go"], ["ცხინვალი"])

    def test_roads_in_no_go_and_abroad_fail(self):
        road = encode_tile({"transportation_name": [(basemap.LINESTRING, {"class": "minor", "name": "x"},
                                                     [[(0, 0), (4096, 4096)]])],
                            "building": [(basemap.POLYGON, {}, [square(100, 100, 20)])]})
        problems, detail = self.run_scan({self.tile_at(0.2, 0.2): road, self.tile_at(1.5, 0.5): road})
        self.assertEqual(detail["no_go_lines"], 1)
        self.assertEqual(detail["outside_lines"], 1)            # the no-go tile's road is inside Georgia
        self.assertEqual(len(problems), 2)

    def test_empty_no_go_area_fails(self):
        self.scan["min_buildings_in_no_go"] = 10
        empty = encode_tile({"water": [(basemap.POLYGON, {"class": "lake"}, [square(0, 0, 50)])]})
        problems, _ = self.run_scan({self.tile_at(0.2, 0.2): empty})
        self.assertTrue(any("empty area" in p for p in problems))


class SourcesTest(unittest.TestCase):
    def test_verify(self):
        with tempfile.TemporaryDirectory() as tmp:
            good = Path(tmp) / "a.zip"
            with zipfile.ZipFile(good, "w") as z:
                z.writestr("a.txt", "hello")
            (Path(tmp) / "b.zip").write_bytes(b"not a zip")
            size = good.stat().st_size
            config = {"sources": [
                {"name": "a", "file": "a.zip", "url": "https://x/a.zip", "bytes": size, "sha256": None},
                {"name": "b", "file": "b.zip", "url": "https://x/b.zip", "min_bytes": 1, "max_bytes": 5},
                {"name": "c", "file": "c.zip", "url": "https://x/c.zip", "bytes": 1},
            ]}
            entries, problems = basemap.verify_sources(config, tmp)
            self.assertEqual([e["name"] for e in entries], ["a", "b"])
            self.assertEqual(entries[0]["pinned"], "bytes")
            text = " ".join(problems)
            self.assertIn("b: 9 bytes, outside 1-5", text)
            self.assertIn("b: not a valid zip", text)
            self.assertIn("c:", text)
            config["sources"][0]["sha256"] = "0" * 64
            self.assertTrue(any("the pin is" in p for p in basemap.verify_sources(config, tmp)[1]))


class ConfigTest(unittest.TestCase):
    def test_real_config(self):
        config = basemap.load_config()
        self.assertEqual(config["build"]["languages"], ["ka", "en"])
        self.assertEqual(config["build"]["maxzoom"], 14)
        self.assertFalse(config["build"].get("exclude_layers"))          # full quality
        self.assertEqual(len(config["gate"]["required_layers"]), 16)
        for label in config["gate"]["city_labels"]:
            self.assertTrue(basemap.georgian(label["name_ka"]))

    @unittest.skipUnless(HAVE_GEO, "numpy, shapely or pyproj not installed")
    def test_points_are_legal_georgia(self):
        import clip
        zones = clip.build_zones(clip.load_config())
        config = basemap.load_config()
        for p in config["gate"]["spot_checks"] + config["gate"]["city_labels"]:
            self.assertTrue(shapely.intersects_xy(zones.georgia, p["lon"], p["lat"]), p["name"])
            self.assertFalse(shapely.intersects_xy(zones.hard, p["lon"], p["lat"]), p["name"])

    def test_bad_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = json.loads(basemap.DEFAULT_CONFIG.read_text(encoding="utf-8"))
            config["planetiler"]["jar_sha256"] = "latest"
            config["build"]["languages"] = ["en"]
            path = Path(tmp) / "c.json"
            path.write_text(json.dumps(config), encoding="utf-8")
            with self.assertRaises(ValueError):
                basemap.load_config(path)


@unittest.skipUnless(HAVE_GEO and HAVE_OSMIUM, "numpy, shapely, pyproj or pyosmium not installed")
class EndToEndTest(unittest.TestCase):
    """gate -> report -> manifest.py --extend on a made-up Georgia map."""

    STAMP = "2026-09-24T20:21:02Z"

    def make_pbf(self, path, stamp):
        header = osmium.io.Header()
        header.set("osmosis_replication_timestamp", stamp)
        Path(path).unlink(missing_ok=True)
        writer = osmium.SimpleWriter(str(path), header=header)
        try:
            writer.add_node(osmium.osm.mutable.Node(id=1, location=(44.8, 41.7)))
        finally:
            writer.close()

    def make_map(self, tmp, config, no_go_tile):
        tiles = {}
        # One tile per zoom at Tbilisi, so every zoom exists.
        for z in range(15):
            tiles[(z,) + basemap.lonlat_to_tile(44.9, 41.75, z)] = encode_tile(
                {"landcover": [(basemap.POLYGON, {"class": "grass"}, [square(0, 0, 4096)])]})
        for spot in config["gate"]["spot_checks"]:
            tiles[(14,) + basemap.lonlat_to_tile(spot["lon"], spot["lat"], 14)] = rich_tile()
        for label in config["gate"]["city_labels"]:
            tiles[(label["zoom"],) + basemap.lonlat_to_tile(label["lon"], label["lat"], label["zoom"])] = \
                city_tile(label["zoom"], label["name_ka"])
        tiles[(14,) + basemap.lonlat_to_tile(43.9701, 42.2257, 14)] = no_go_tile        # Tskhinvali
        path = Path(tmp) / "map" / "georgia.pmtiles"
        path.parent.mkdir()
        build_archive(path, tiles, good_metadata(config, self.STAMP), basemap.planetiler_bounds(config))
        return path

    def test_gate_report_extend(self):
        sys.path.insert(0, str(HERE))
        import clip
        import manifest
        base_config = basemap.load_config()
        config = copy.deepcopy(base_config)
        config["gate"].update({"min_bytes": 1, "max_bytes": 10 ** 9, "min_tiles_at_maxzoom": 1})
        config["gate"]["no_go_scan"].update({"min_tiles_in_no_go": 1, "min_buildings_in_no_go": 1})
        clean = encode_tile({"building": [(basemap.POLYGON, {}, [square(100, 100, 20)])],
                             "transportation": [(basemap.LINESTRING, {"class": "rail"}, [[(0, 0), (4096, 0)]])]})
        dirty = encode_tile({"building": [(basemap.POLYGON, {}, [square(100, 100, 20)])],
                             "transportation": [(basemap.LINESTRING, {"class": "secondary"},
                                                 [[(0, 0), (4096, 4096)]])]})
        with tempfile.TemporaryDirectory() as tmp:
            cfg = Path(tmp) / "basemap.json"
            cfg.write_text(json.dumps(config, ensure_ascii=False), encoding="utf-8")
            pbf = Path(tmp) / "georgia-clipped.osm.pbf"
            self.make_pbf(pbf, self.STAMP)
            sources = Path(tmp) / "sources.json"
            sources.write_text(json.dumps([{"name": s["name"], "about": s["about"], "licence": s["licence"],
                                            "attribution": s["attribution"], "url": s["url"]}
                                           for s in config["sources"]]), encoding="utf-8")
            pmtiles = self.make_map(tmp, config, clean)
            results = Path(tmp) / "gate.json"
            argv = ["--config", str(cfg), "gate", "--pmtiles", str(pmtiles), "--clipped-pbf", str(pbf),
                    "--results", str(results)]
            self.assertEqual(basemap.main(argv), 0, results.read_text(encoding="utf-8"))
            report_argv = ["--config", str(cfg), "report", "--pmtiles", str(pmtiles), "--gate", str(results),
                           "--sources", str(sources), "--clipped-pbf", str(pbf), "--commit", "a" * 40]
            self.assertEqual(basemap.main(report_argv), 0)
            report_path = pmtiles.parent / "basemap_report.json"
            report = json.loads(report_path.read_text(encoding="utf-8"))
            self.assertEqual(report["files"][0]["name"], "georgia.pmtiles")

            clipped = basemap.file_entry(pbf)
            base = {"tag": "osm-20260924T202102Z-aaaaaaaa", "files": [{"name": "valhalla_tiles.tar"}],
                    "osm": {"timestamp": self.STAMP, "clipped_pbf": clipped},
                    "clip": {"config_version": clip.load_config()["version"]}, "source_commit": "a" * 40}
            base_path = Path(tmp) / "manifest.json"
            base_path.write_text(json.dumps(base), encoding="utf-8")
            out = Path(tmp) / "final.json"
            ext = ["--extend", str(base_path), "--asset-report", str(report_path), "--out", str(out)]
            self.assertEqual(manifest.main(ext), 0)
            final = json.loads(out.read_text(encoding="utf-8"))
            self.assertEqual([f["name"] for f in final["files"]], ["valhalla_tiles.tar", "georgia.pmtiles"])
            self.assertEqual(final["basemap"]["files"], ["georgia.pmtiles"])
            self.assertEqual(final["basemap"]["attribution"], basemap.ATTRIBUTION_TEXT)
            # The map credit and one credit per part, with every source of the map.
            self.assertIn("OpenMapTiles", final["map_attribution"]["text"])
            self.assertIn("OpenStreetMap", final["map_attribution"]["text"])
            self.assertEqual([link["url"] for link in final["map_attribution"]["links"]],
                             ["https://openmaptiles.org/", "https://www.openstreetmap.org/copyright"])
            credit = {c["part"]: c for c in final["credits"]}
            self.assertEqual(credit["basemap"]["files"], ["georgia.pmtiles"])
            self.assertEqual(credit["basemap"]["schema_licence_uri"], "https://creativecommons.org/licenses/by/4.0/")
            names = " ".join(s["name"] for s in credit["basemap"]["sources"])
            for word in ("OpenStreetMap", "OpenMapTiles", "Natural Earth", "water polygons", "osm-lakelines"):
                self.assertIn(word, names)
            self.assertEqual(credit["routing"]["files"], ["valhalla_tiles.tar"])

            # binds_to must be there, and complete, for OpenStreetMap data.
            good = report_path.read_text(encoding="utf-8")
            for binds in (None, {"source_commit": "a" * 40}):
                bad = json.loads(good)
                if binds is None:
                    del bad["binds_to"]
                else:
                    bad["binds_to"] = binds
                report_path.write_text(json.dumps(bad), encoding="utf-8")
                self.assertEqual(manifest.main(ext), 1, binds)
            report_path.write_text(good, encoding="utf-8")
            self.assertEqual(manifest.main(ext), 0)

            # Another clipped extract, or the same section twice, is refused.
            other = dict(base, osm={"timestamp": self.STAMP, "clipped_pbf": dict(clipped, sha256="0" * 64)})
            base_path.write_text(json.dumps(other), encoding="utf-8")
            self.assertEqual(manifest.main(ext), 1)
            base_path.write_text(json.dumps(final), encoding="utf-8")
            self.assertEqual(manifest.main(ext), 1)

            # A map drawn from the unclipped extract fails the gate, and no report follows.
            report_path.unlink()
            dirty_dir = Path(tmp) / "d"
            dirty_dir.mkdir()
            pmtiles = self.make_map(dirty_dir, config, dirty)
            argv[argv.index("--pmtiles") + 1] = str(pmtiles)
            self.assertEqual(basemap.main(argv), 1)
            gate = json.loads(results.read_text(encoding="utf-8"))
            scan = next(r for r in gate["results"] if r["name"] == "no-go road scan")
            self.assertFalse(scan["passed"])
            report_argv[report_argv.index("--pmtiles") + 1] = str(pmtiles)
            self.assertEqual(basemap.main(report_argv), 1)
            self.assertFalse((pmtiles.parent / "basemap_report.json").exists())

            # A map whose OSM time differs from the clipped extract fails too.
            self.make_pbf(pbf, "2026-09-17T20:21:02Z")
            clean_dir = Path(tmp) / "c"
            clean_dir.mkdir()
            pmtiles = self.make_map(clean_dir, config, clean)
            argv[argv.index("--pmtiles") + 1] = str(pmtiles)
            self.assertEqual(basemap.main(argv), 1)


if __name__ == "__main__":
    if not HAVE_GEO:
        print("numpy/shapely/pyproj not installed: the no-go scan and end-to-end tests are skipped")
    unittest.main(verbosity=2)
