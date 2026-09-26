#!/usr/bin/env python3
"""The offline Georgia basemap: georgia.pmtiles for MapLibre, built by
Planetiler (OpenMapTiles schema) from the same safety-clipped extract as the
routing tiles.

Subcommands, as the workflow runs them:

  bounds          Print Planetiler's --bounds: the bounding box of Georgia's
                  frozen border (config/boundaries/georgia_28699.geojson)
                  plus a margin.
  verify-sources  Check the downloaded water, Natural Earth and lake files
                  against config/basemap.json (size pins or bands, optional
                  SHA-256 pins, zip integrity) and write sources.json.
  gate            Check a built georgia.pmtiles. Exit 0 only if every check
                  passes; the release happens only then.
  report          Write basemap_report.json next to a georgia.pmtiles that
                  passed the gate: the --asset-report that manifest.py
                  --extend turns into the "basemap" section of manifest.json.

What the gate checks:

1. The file. Its SHA-256 is recorded (the report refuses any other file),
   its size lies in the band of config/basemap.json, and it has not shrunk
   by more than drop_limit against the previous release (bytes and tiles at
   the top zoom), which would mean part of the country went missing.
2. The PMTiles header. Version 3, gzip-compressed vector tiles (MVT),
   clustered, zoom 0 to 14, bounds that cover Georgia, sections that lie in
   the file without overlapping, a root directory in the first 16 KiB.
3. The directory. Walked to the end: tile ids ascending, every tile inside
   the tile data, the header's three counts equal to what the walk found,
   every zoom from 0 to 14 present, enough tiles at zoom 14, no tile at any
   zoom outside the bounds, no tile above max_tile_bytes.
4. The metadata. All 16 OpenMapTiles layers, the OpenMapTiles and
   OpenStreetMap credits with their links (openmaptiles.org,
   openstreetmap.org/copyright), the ODbL notice in the description (ODbL
   4.2: the licence travels inside the file), the pinned Planetiler
   version, the OSM data time of the clipped extract, name:ka in the label
   layers, and no name:xx field for any language other than ka and en
   (proves --languages took effect).
5. Spot checks. Decoded zoom-14 tiles in central Tbilisi, Batumi and Kutaisi
   must hold enough named streets with Georgian name:ka, buildings, POIs
   and house numbers (Batumi also the sea), and zoom 8/10 tiles must carry
   the city labels თბილისი, ბათუმი and ქუთაისი.
6. No-go road scan. Every zoom-14 tile that touches the no-go area or lies
   partly outside Georgia is decoded, and every road line in it
   (transportation and transportation_name layers) must stay out of the
   no-go area and inside Georgia. This proves the map was drawn from the
   clipped extract: the unclipped extract has hundreds of roads there. Place
   labels, buildings and POIs inside the occupied areas stay on the map on
   purpose; the tiles there must exist and hold buildings, so an empty area
   cannot pass the scan.

Reading PMTiles and vector tiles needs only the standard library, so most
unit tests run anywhere. The no-go scan uses shapely and pyproj through
clip.py, like gate.py, and the OSM data time uses pyosmium.

Usage (as in the workflow):
  python scripts/basemap.py bounds
  python scripts/basemap.py verify-sources --dir build/basemap/sources --out build/basemap/sources.json
  python scripts/basemap.py gate --pmtiles build/basemap/georgia.pmtiles \\
      --clipped-pbf build/input/georgia-clipped.osm.pbf \\
      --results build/basemap/basemap_gate.json [--previous-manifest build/previous_manifest.json]
  python scripts/basemap.py report --pmtiles build/basemap/georgia.pmtiles \\
      --gate build/basemap/basemap_gate.json --sources build/basemap/sources.json \\
      --build-info build/basemap/build_info.json --clipped-pbf build/input/georgia-clipped.osm.pbf
"""

import argparse
import datetime as dt
import gzip
import hashlib
import json
import math
import os
import re
import struct
import sys
import time
import zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
DEFAULT_CONFIG = ROOT / "config" / "basemap.json"
CLIP_CONFIG = ROOT / "config" / "clip.json"
GEORGIA_BORDER = ROOT / "config" / "boundaries" / "georgia_28699.geojson"

SECTION = "basemap"

# PMTiles v3 (https://github.com/protomaps/PMTiles/blob/main/spec/v3/spec.md)
HEADER_BYTES = 127
ROOT_DIR_LIMIT = 16384          # the root directory must end within the first 16 KiB
MAX_DIR_DEPTH = 4
COMPRESSION = {0: "unknown", 1: "none", 2: "gzip", 3: "brotli", 4: "zstd"}
TILE_TYPE = {0: "unknown", 1: "mvt", 2: "png", 3: "jpeg", 4: "webp", 5: "avif", 6: "mlt"}

GEORGIAN = re.compile("[ა-ჿ]")      # Mkhedruli letters
# Planetiler's default attribution (OpenMapTilesSchema.ATTRIBUTION) links both credits.
ATTRIBUTION_NEEDS = ("OpenMapTiles", "OpenStreetMap", "openmaptiles.org", "openstreetmap.org/copyright")
DESCRIPTION_NEEDS = ("opendatacommons.org/licenses/odbl",)
ATTRIBUTION_TEXT = "© OpenMapTiles © OpenStreetMap contributors"
OPENMAPTILES_URL = "https://openmaptiles.org/"
COPYRIGHT_URL = "https://www.openstreetmap.org/copyright"
ATTRIBUTION_LINKS = [{"text": "© OpenMapTiles", "url": OPENMAPTILES_URL},
                     {"text": "© OpenStreetMap contributors", "url": COPYRIGHT_URL}]
ODBL_URL = "https://opendatacommons.org/licenses/odbl/1-0/"
# The licence of the schema version these tiles follow (3.16), not the moving master branch.
OPENMAPTILES_LICENCE_URL = "https://github.com/openmaptiles/openmaptiles/blob/v3.16/LICENSE.md"
CC_BY_4_URL = "https://creativecommons.org/licenses/by/4.0/"


def load_config(path=DEFAULT_CONFIG):
    """Read basemap.json and refuse settings that would be broken."""
    path = Path(path)
    config = json.loads(path.read_text(encoding="utf-8"))
    problems = []
    if not isinstance(config.get("version"), int) or config["version"] < 1:
        problems.append("'version' must be a positive integer")
    pt = config.get("planetiler", {})
    if not re.fullmatch(r"[0-9a-f]{64}", str(pt.get("jar_sha256", ""))):
        problems.append("planetiler.jar_sha256 must be 64 hex digits")
    if not str(pt.get("jar_url", "")).startswith("https://"):
        problems.append("planetiler.jar_url must be https")
    build = config.get("build", {})
    if not build.get("languages") or "ka" not in build["languages"]:
        problems.append("build.languages must include ka")
    if build.get("minzoom") != 0 or not isinstance(build.get("maxzoom"), int) or not 10 <= build["maxzoom"] <= 15:
        problems.append("build.minzoom must be 0 and build.maxzoom between 10 and 15")
    for source in config.get("sources", []):
        if not str(source.get("url", "")).startswith("https://"):
            problems.append(f"source {source.get('name')}: url must be https")
        if not re.fullmatch(r"[A-Za-z0-9._-]+", str(source.get("file", ""))):
            problems.append(f"source {source.get('name')}: odd file name")
        if source.get("sha256") is not None and not re.fullmatch(r"[0-9a-f]{64}", source["sha256"]):
            problems.append(f"source {source.get('name')}: sha256 must be null or 64 hex digits")
        if "bytes" not in source and not ("min_bytes" in source and "max_bytes" in source):
            problems.append(f"source {source.get('name')}: needs bytes or min_bytes/max_bytes")
    gate = config.get("gate", {})
    if not 0 < gate.get("min_bytes", 0) < gate.get("max_bytes", 0):
        problems.append("gate.min_bytes must be above 0 and below gate.max_bytes")
    if problems:
        raise ValueError("bad basemap config: " + "; ".join(problems))
    return config


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def file_entry(path):
    path = Path(path)
    return {"name": path.name, "bytes": path.stat().st_size, "sha256": sha256_file(path)}


# ---------------------------------------------------------------------------
# Tile math (Web Mercator, and the PMTiles Hilbert tile id)
# ---------------------------------------------------------------------------

def zxy_to_tileid(z, x, y):
    """PMTiles tile id: all tiles of lower zooms, then the Hilbert index."""
    if not 0 <= z <= 31:
        raise ValueError(f"zoom {z} out of range")
    n = 1 << z
    if not (0 <= x < n and 0 <= y < n):
        raise ValueError(f"tile {z}/{x}/{y} out of range")
    d = 0
    s = n >> 1
    while s > 0:
        rx = 1 if x & s else 0
        ry = 1 if y & s else 0
        d += s * s * ((3 * rx) ^ ry)
        if ry == 0:
            if rx == 1:
                x = n - 1 - x
                y = n - 1 - y
            x, y = y, x
        s >>= 1
    return ((1 << (2 * z)) - 1) // 3 + d


def zoom_start(z):
    """The first tile id of a zoom level."""
    return ((1 << (2 * z)) - 1) // 3


def tileid_to_zxy(tile_id):
    if tile_id < 0:
        raise ValueError("negative tile id")
    z = 0
    while zoom_start(z + 1) <= tile_id:
        z += 1
        if z > 31:
            raise ValueError("tile id too large")
    t = tile_id - zoom_start(z)
    n = 1 << z
    x = y = 0
    s = 1
    while s < n:
        rx = 1 & (t >> 1)
        ry = 1 & (t ^ rx)
        if ry == 0:
            if rx == 1:
                x = s - 1 - x
                y = s - 1 - y
            x, y = y, x
        x += s * rx
        y += s * ry
        t >>= 2
        s <<= 1
    return z, x, y


def lonlat_to_tile(lon, lat, z):
    n = 1 << z
    x = int((lon + 180.0) / 360.0 * n)
    lat_r = math.radians(max(min(lat, 85.0511287798), -85.0511287798))
    y = int((1.0 - math.asinh(math.tan(lat_r)) / math.pi) / 2.0 * n)
    return min(max(x, 0), n - 1), min(max(y, 0), n - 1)


def tile_lon(x, z):
    return x / (1 << z) * 360.0 - 180.0


def tile_lat(y, z):
    return math.degrees(math.atan(math.sinh(math.pi * (1.0 - 2.0 * y / (1 << z)))))


def tile_bounds(z, x, y, margin=0.0):
    """(west, south, east, north) of a tile, grown by margin (a share of the tile)."""
    return (tile_lon(x - margin, z), tile_lat(y + 1 + margin, z),
            tile_lon(x + 1 + margin, z), tile_lat(y - margin, z))


# ---------------------------------------------------------------------------
# PMTiles v3 reader
# ---------------------------------------------------------------------------

def read_varint(buf, pos):
    """Protobuf/PMTiles unsigned varint -> (value, next position)."""
    result = 0
    shift = 0
    while True:
        if pos >= len(buf):
            raise ValueError("truncated varint")
        b = buf[pos]
        pos += 1
        result |= (b & 0x7F) << shift
        if b < 0x80:
            return result, pos
        shift += 7
        if shift > 63:
            raise ValueError("varint longer than 64 bits")


def parse_header(raw):
    if len(raw) < HEADER_BYTES or raw[:7] != b"PMTiles":
        raise ValueError("not a PMTiles file (bad magic)")
    fields = struct.unpack_from("<11Q", raw, 8)
    names = ("root_offset", "root_length", "metadata_offset", "metadata_length",
             "leaf_offset", "leaf_length", "data_offset", "data_length",
             "addressed_tiles", "tile_entries", "tile_contents")
    h = dict(zip(names, fields))
    h["version"] = raw[7]
    (h["clustered"], h["internal_compression"], h["tile_compression"], h["tile_type"],
     h["min_zoom"], h["max_zoom"]) = struct.unpack_from("<6B", raw, 96)
    min_lon, min_lat, max_lon, max_lat = struct.unpack_from("<4i", raw, 102)
    h["bounds"] = [min_lon / 1e7, min_lat / 1e7, max_lon / 1e7, max_lat / 1e7]
    h["center_zoom"] = raw[118]
    c_lon, c_lat = struct.unpack_from("<2i", raw, 119)
    h["center"] = [c_lon / 1e7, c_lat / 1e7]
    return h


def decompress(data, kind):
    if kind == 1:
        return bytes(data)
    if kind == 2:
        return gzip.decompress(data)
    raise ValueError(f"unsupported compression {COMPRESSION.get(kind, kind)}")


def parse_directory(data):
    """Decompressed directory -> list of (tile_id, offset, length, run_length)."""
    pos = 0
    n, pos = read_varint(data, pos)
    ids, runs, lengths, offsets = [], [], [], []
    last = 0
    for _ in range(n):
        delta, pos = read_varint(data, pos)
        last += delta
        ids.append(last)
    for _ in range(n):
        v, pos = read_varint(data, pos)
        runs.append(v)
    for _ in range(n):
        v, pos = read_varint(data, pos)
        lengths.append(v)
    for i in range(n):
        v, pos = read_varint(data, pos)
        if v == 0 and i > 0:
            offsets.append(offsets[i - 1] + lengths[i - 1])
        else:
            offsets.append(v - 1)
    if pos != len(data):
        raise ValueError(f"directory has {len(data) - pos} trailing bytes")
    return list(zip(ids, offsets, lengths, runs))


def find_entry(entries, tile_id):
    lo, hi = 0, len(entries) - 1
    while lo <= hi:
        mid = (lo + hi) >> 1
        diff = tile_id - entries[mid][0]
        if diff > 0:
            lo = mid + 1
        elif diff < 0:
            hi = mid - 1
        else:
            return entries[mid]
    if hi >= 0:
        entry = entries[hi]
        if entry[3] == 0 or tile_id - entry[0] < entry[3]:
            return entry
    return None


class PMTiles:
    """Read a PMTiles v3 archive through read_at(offset, length), so the same
    code reads a local file or (in checks by hand) HTTP ranges."""

    def __init__(self, read_at, size=None):
        self.read_at = read_at
        self.size = size
        self.header = parse_header(read_at(0, HEADER_BYTES))
        self._dirs = {}

    @classmethod
    def open(cls, path):
        f = open(path, "rb")
        size = os.fstat(f.fileno()).st_size

        def read_at(offset, length):
            f.seek(offset)
            data = f.read(length)
            if len(data) != length:
                raise ValueError(f"read {len(data)} of {length} bytes at {offset}: file truncated")
            return data

        archive = cls(read_at, size)
        archive._file = f
        return archive

    def close(self):
        f = getattr(self, "_file", None)
        if f:
            f.close()

    def directory(self, offset, length):
        key = (offset, length)
        if key not in self._dirs:
            raw = self.read_at(offset, length)
            self._dirs[key] = parse_directory(decompress(raw, self.header["internal_compression"]))
        return self._dirs[key]

    def metadata(self):
        h = self.header
        raw = self.read_at(h["metadata_offset"], h["metadata_length"])
        return json.loads(decompress(raw, h["internal_compression"]).decode("utf-8"))

    def entry(self, z, x, y):
        """The directory entry that holds a tile, or None."""
        h = self.header
        tile_id = zxy_to_tileid(z, x, y)
        offset, length = h["root_offset"], h["root_length"]
        for _ in range(MAX_DIR_DEPTH):
            found = find_entry(self.directory(offset, length), tile_id)
            if found is None:
                return None
            if found[3] > 0:
                return found
            offset, length = h["leaf_offset"] + found[1], found[2]
        raise ValueError("directory deeper than 4 levels")

    def raw_tile(self, offset, length):
        return self.read_at(self.header["data_offset"] + offset, length)

    def tile(self, z, x, y):
        """Decompressed tile bytes, or None when the archive has no such tile."""
        found = self.entry(z, x, y)
        if found is None:
            return None
        return decompress(self.raw_tile(found[1], found[2]), self.header["tile_compression"])

    def walk(self):
        """Yield every tile entry (tile_id, offset, length, run_length) in
        order, following leaf directories."""
        h = self.header

        def visit(offset, length, depth):
            if depth > MAX_DIR_DEPTH:
                raise ValueError("directory deeper than 4 levels")
            for entry in self.directory(offset, length):
                if entry[3] == 0:
                    if entry[1] + entry[2] > h["leaf_length"]:
                        raise ValueError(f"leaf directory at {entry[1]} runs past the leaf section")
                    yield from visit(h["leaf_offset"] + entry[1], entry[2], depth + 1)
                else:
                    yield entry

        yield from visit(h["root_offset"], h["root_length"], 1)


# ---------------------------------------------------------------------------
# Mapbox Vector Tile decoder (just enough for the checks)
# ---------------------------------------------------------------------------

def _fields(buf, start, end):
    """Yield (field, wire type, value) for one protobuf message: an int for
    varints, a (start, end) span for length-delimited fields, raw bytes for
    fixed32/fixed64."""
    pos = start
    while pos < end:
        key, pos = read_varint(buf, pos)
        field, wire = key >> 3, key & 7
        if wire == 0:
            value, pos = read_varint(buf, pos)
        elif wire == 2:
            length, pos = read_varint(buf, pos)
            value = (pos, pos + length)
            pos += length
        elif wire == 5:
            value = buf[pos:pos + 4]
            pos += 4
        elif wire == 1:
            value = buf[pos:pos + 8]
            pos += 8
        else:
            raise ValueError(f"unsupported protobuf wire type {wire}")
        if pos > end:
            raise ValueError("truncated protobuf message")
        yield field, wire, value


def _packed(buf, span):
    out = []
    pos, end = span
    while pos < end:
        v, pos = read_varint(buf, pos)
        out.append(v)
    return out


def _zigzag(v):
    return (v >> 1) ^ -(v & 1)


def _value(buf, span):
    for field, wire, v in _fields(buf, *span):
        if field == 1 and wire == 2:
            return bytes(buf[v[0]:v[1]]).decode("utf-8")
        if field == 2 and wire == 5:
            return struct.unpack("<f", v)[0]
        if field == 3 and wire == 1:
            return struct.unpack("<d", v)[0]
        if field == 4 and wire == 0:
            return v - (1 << 64) if v >= 1 << 63 else v
        if field == 5 and wire == 0:
            return v
        if field == 6 and wire == 0:
            return _zigzag(v)
        if field == 7 and wire == 0:
            return bool(v)
    return None


POINT, LINESTRING, POLYGON = 1, 2, 3


class Layer:
    def __init__(self, buf, span):
        self.buf = buf
        self.name = None
        self.version = 1
        self.extent = 4096
        self.keys = []
        self._value_spans = []
        self._feature_spans = []
        for field, wire, v in _fields(buf, *span):
            if field == 1 and wire == 2:
                self.name = bytes(buf[v[0]:v[1]]).decode("utf-8")
            elif field == 2 and wire == 2:
                self._feature_spans.append(v)
            elif field == 3 and wire == 2:
                self.keys.append(bytes(buf[v[0]:v[1]]).decode("utf-8"))
            elif field == 4 and wire == 2:
                self._value_spans.append(v)
            elif field == 5 and wire == 0:
                self.extent = v
            elif field == 15 and wire == 0:
                self.version = v
        self._values = None

    @property
    def values(self):
        if self._values is None:
            self._values = [_value(self.buf, s) for s in self._value_spans]
        return self._values

    def __len__(self):
        return len(self._feature_spans)

    def features(self):
        """Yield (geometry type, properties, geometry ints) for each feature."""
        keys, values = self.keys, self.values
        for span in self._feature_spans:
            tags, geom, gtype = [], [], 0
            for field, wire, v in _fields(self.buf, *span):
                if field == 2:
                    tags.extend(_packed(self.buf, v) if wire == 2 else [v])
                elif field == 3 and wire == 0:
                    gtype = v
                elif field == 4:
                    geom.extend(_packed(self.buf, v) if wire == 2 else [v])
            if len(tags) % 2:
                raise ValueError(f"layer {self.name}: odd number of tag indexes")
            props = {}
            for i in range(0, len(tags), 2):
                props[keys[tags[i]]] = values[tags[i + 1]]
            yield gtype, props, geom


def decode_tile(data, wanted=None):
    """Tile bytes (decompressed) -> {layer name: Layer}. With wanted, only
    those layers are kept."""
    layers = {}
    for field, wire, v in _fields(data, 0, len(data)):
        if field == 3 and wire == 2:
            layer = Layer(data, v)
            if wanted is None or layer.name in wanted:
                layers[layer.name] = layer
    return layers


def geometry_parts(ints):
    """MVT geometry commands -> list of parts, each a list of (x, y) in tile
    coordinates (a ring is closed by repeating its first point)."""
    parts = []
    current = None
    x = y = 0
    i, n = 0, len(ints)
    while i < n:
        command, count = ints[i] & 7, ints[i] >> 3
        i += 1
        if command in (1, 2):
            if i + 2 * count > n:
                raise ValueError("geometry runs past its end")
            for _ in range(count):
                x += _zigzag(ints[i])
                y += _zigzag(ints[i + 1])
                i += 2
                if command == 1:
                    current = [(x, y)]
                    parts.append(current)
                elif current is None:
                    raise ValueError("LineTo before MoveTo")
                else:
                    current.append((x, y))
        elif command == 7:
            if current:
                current.append(current[0])
        else:
            raise ValueError(f"unknown geometry command {command}")
    return parts


def outer_rings(ints):
    """Count the exterior rings of an MVT polygon geometry (positive area in
    tile coordinates). Planetiler merges the buildings of a tile into a few
    multipolygon features, so rings, not features, count buildings."""
    count = 0
    x = y = 0
    area = 0
    start = None
    prev = None
    i, n = 0, len(ints)
    while i < n:
        command, repeat = ints[i] & 7, ints[i] >> 3
        i += 1
        if command in (1, 2):
            for _ in range(repeat):
                x += _zigzag(ints[i])
                y += _zigzag(ints[i + 1])
                i += 2
                if command == 1:
                    start = prev = (x, y)
                    area = 0
                else:
                    area += prev[0] * y - x * prev[1]
                    prev = (x, y)
        elif command == 7 and start is not None:
            area += prev[0] * start[1] - start[0] * prev[1]
            if area > 0:
                count += 1
    return count


def to_lonlat(z, tx, ty, extent, part):
    n = float(1 << z)
    out = []
    for px, py in part:
        lon = (tx + px / extent) / n * 360.0 - 180.0
        lat = math.degrees(math.atan(math.sinh(math.pi * (1.0 - 2.0 * (ty + py / extent) / n))))
        out.append((lon, lat))
    return out


def georgian(value):
    return isinstance(value, str) and bool(GEORGIAN.search(value))


# ---------------------------------------------------------------------------
# bounds, verify-sources
# ---------------------------------------------------------------------------

def border_bbox(path=GEORGIA_BORDER):
    feature = json.loads(Path(path).read_text(encoding="utf-8"))["features"][0]
    xs, ys = [], []

    def walk(c):
        if isinstance(c[0], (int, float)):
            xs.append(c[0])
            ys.append(c[1])
        else:
            for part in c:
                walk(part)

    walk(feature["geometry"]["coordinates"])
    return min(xs), min(ys), max(xs), max(ys)


def planetiler_bounds(config, border=GEORGIA_BORDER):
    """Georgia's bounding box plus the margin, rounded outward to 0.01 degree."""
    m = config["build"]["bounds_margin_deg"]
    w, s, e, n = border_bbox(border)
    return (math.floor((w - m) * 100) / 100, math.floor((s - m) * 100) / 100,
            math.ceil((e + m) * 100) / 100, math.ceil((n + m) * 100) / 100)


def verify_sources(config, directory):
    """Check each downloaded source file. Returns (entries, problems)."""
    entries, problems = [], []
    for source in config["sources"]:
        path = Path(directory) / source["file"]
        if not path.is_file():
            problems.append(f"{source['name']}: {path} is missing")
            continue
        size = path.stat().st_size
        if "bytes" in source and size != source["bytes"]:
            problems.append(f"{source['name']}: {size} bytes, the pin is {source['bytes']}; "
                            "the file changed upstream: review it and update config/basemap.json")
        if "min_bytes" in source and not source["min_bytes"] <= size <= source["max_bytes"]:
            problems.append(f"{source['name']}: {size} bytes, outside {source['min_bytes']}-{source['max_bytes']}")
        digest = sha256_file(path)
        if source.get("sha256") and digest != source["sha256"]:
            problems.append(f"{source['name']}: sha256 {digest}, the pin is {source['sha256']}")
        try:
            with zipfile.ZipFile(path) as z:
                bad = z.testzip()
                members = len(z.namelist())
            if bad:
                problems.append(f"{source['name']}: zip member {bad} is corrupt")
        except zipfile.BadZipFile as exc:
            problems.append(f"{source['name']}: not a valid zip ({exc})")
            members = 0
        modified = dt.datetime.fromtimestamp(path.stat().st_mtime, dt.timezone.utc).replace(microsecond=0)
        entries.append({"name": source["name"], "file": source["file"], "url": source["url"],
                        "bytes": size, "sha256": digest, "zip_members": members,
                        "last_modified": modified.isoformat(),
                        "pinned": "sha256" if source.get("sha256") else ("bytes" if "bytes" in source else "size band"),
                        "about": source.get("about"), "licence": source.get("licence"),
                        "attribution": source.get("attribution")})
    return entries, problems


# ---------------------------------------------------------------------------
# Gate checks. Each returns (problems, detail).
# ---------------------------------------------------------------------------

def osm_header_timestamp(pbf):
    """The replication time in the (clipped) PBF header; clip.py keeps it."""
    import osmium
    reader = osmium.io.Reader(str(pbf), osmium.osm.osm_entity_bits.NOTHING)
    try:
        value = reader.header().get("osmosis_replication_timestamp")
    finally:
        reader.close()
    if not value:
        raise ValueError(f"{pbf}: the PBF header has no osmosis_replication_timestamp")
    return value


def parse_time(value):
    return dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))


def check_size(size, config, previous=None, maxzoom_tiles=None):
    gate = config["gate"]
    problems, detail = [], {"bytes": size}
    if not gate["min_bytes"] <= size <= gate["max_bytes"]:
        problems.append(f"{size} bytes, outside the band {gate['min_bytes']}-{gate['max_bytes']}")
    if previous:
        before = next((f.get("bytes") for f in previous.get("files", [])
                       if f.get("name") == config["output"]), None)
        if before:
            detail["previous_bytes"] = before
            detail["previous_tag"] = previous.get("tag")
            if size < (1 - gate["drop_limit"]) * before:
                problems.append(f"{size} bytes against {before} in {previous.get('tag')}: more than "
                                f"{gate['drop_limit']:.0%} smaller; part of the country may be missing")
        per_zoom = ((previous.get(SECTION) or {}).get("tiles") or {}).get("per_zoom") or {}
        top = str(config["build"]["maxzoom"])
        if per_zoom.get(top) and maxzoom_tiles is not None:
            detail["previous_tiles_at_maxzoom"] = per_zoom[top]
            if maxzoom_tiles < (1 - gate["drop_limit"]) * per_zoom[top]:
                problems.append(f"{maxzoom_tiles} tiles at zoom {top} against {per_zoom[top]} in "
                                f"{previous.get('tag')}: more than {gate['drop_limit']:.0%} fewer")
    return problems, detail


def check_header(h, size, config, bounds):
    build = config["build"]
    problems = []
    if h["version"] != 3:
        problems.append(f"PMTiles version {h['version']}, expected 3")
    if h["tile_type"] != 1:
        problems.append(f"tile type {TILE_TYPE.get(h['tile_type'], h['tile_type'])}, expected mvt")
    if h["tile_compression"] != 2:
        problems.append(f"tile compression {COMPRESSION.get(h['tile_compression'])}, expected gzip")
    if h["internal_compression"] not in (1, 2):
        problems.append(f"directory compression {COMPRESSION.get(h['internal_compression'])}, "
                        "expected gzip or none")
    if h["clustered"] != 1:
        problems.append("tiles are not clustered")
    if (h["min_zoom"], h["max_zoom"]) != (build["minzoom"], build["maxzoom"]):
        problems.append(f"zoom {h['min_zoom']}-{h['max_zoom']}, expected {build['minzoom']}-{build['maxzoom']}")
    w, s, e, n = h["bounds"]
    bw, bs, be, bn = bounds
    if not (abs(w - bw) < 1e-4 and abs(s - bs) < 1e-4 and abs(e - be) < 1e-4 and abs(n - bn) < 1e-4):
        problems.append(f"bounds {h['bounds']}, expected {list(bounds)}")
    gw, gs, ge, gn = border_bbox()
    if not (w <= gw and s <= gs and e >= ge and n >= gn):
        problems.append(f"bounds {h['bounds']} do not cover Georgia {[gw, gs, ge, gn]}")
    cx, cy = h["center"]
    if not (w <= cx <= e and s <= cy <= n):
        problems.append(f"center {h['center']} outside the bounds")
    if h["root_offset"] != HEADER_BYTES:
        problems.append(f"root directory at {h['root_offset']}, expected {HEADER_BYTES}")
    if h["root_offset"] + h["root_length"] > ROOT_DIR_LIMIT:
        problems.append(f"root directory ends at {h['root_offset'] + h['root_length']}, past {ROOT_DIR_LIMIT}")
    sections = sorted([("root directory", h["root_offset"], h["root_length"]),
                       ("metadata", h["metadata_offset"], h["metadata_length"]),
                       ("leaf directories", h["leaf_offset"], h["leaf_length"]),
                       ("tile data", h["data_offset"], h["data_length"])], key=lambda t: t[1])
    end = HEADER_BYTES
    for name, offset, length in sections:
        if offset < end:
            problems.append(f"{name} at {offset} overlaps what comes before it (ends at {end})")
        end = max(end, offset + length)
    if end > size:
        problems.append(f"sections end at {end}, past the file end {size}")
    if h["data_length"] == 0 or h["metadata_length"] == 0:
        problems.append("no tile data or no metadata")
    if not h["addressed_tiles"] >= h["tile_entries"] >= h["tile_contents"] >= 1:
        problems.append(f"odd counts: {h['addressed_tiles']} addressed, {h['tile_entries']} entries, "
                        f"{h['tile_contents']} contents")
    detail = {k: h[k] for k in ("version", "min_zoom", "max_zoom", "bounds", "center", "center_zoom",
                                 "addressed_tiles", "tile_entries", "tile_contents")}
    detail["tile_type"] = TILE_TYPE.get(h["tile_type"])
    detail["tile_compression"] = COMPRESSION.get(h["tile_compression"])
    return problems, detail


def scan_directory(pm, config):
    """Walk every entry once. Returns (problems, detail, z_tiles) where
    z_tiles maps each tile id at the no-go scan zoom to (offset, length)."""
    h = pm.header
    gate = config["gate"]
    maxzoom = config["build"]["maxzoom"]
    scan_zoom = gate["no_go_scan"]["zoom"]
    problems = []
    if h["addressed_tiles"] > gate["max_addressed_tiles"]:
        return ([f"{h['addressed_tiles']} addressed tiles, more than {gate['max_addressed_tiles']}: "
                 "not a Georgia-sized map (world bounds?)"], {}, {})
    per_zoom = {z: 0 for z in range(maxzoom + 1)}
    addressed = entries = 0
    offsets = set()
    largest = (0, None)
    last_end = -1
    outside = []
    z_tiles = {}
    w, s, e, n = h["bounds"]
    ranges = {}
    for z in range(maxzoom + 1):
        x0, y0 = lonlat_to_tile(w, n, z)
        x1, y1 = lonlat_to_tile(e, s, z)
        ranges[z] = (x0 - 1, x1 + 1, y0 - 1, y1 + 1)
    for tile_id, offset, length, run in pm.walk():
        entries += 1
        addressed += run
        if tile_id <= last_end:
            problems.append(f"tile id {tile_id} is not after the previous entry")
            break
        last_end = tile_id + run - 1
        if offset + length > h["data_length"] or length == 0:
            problems.append(f"tile {tile_id} (offset {offset}, length {length}) is outside the tile data")
            break
        offsets.add(offset)
        if length > largest[0]:
            largest = (length, tileid_to_zxy(tile_id))
        for t in range(tile_id, tile_id + run):
            z, x, y = tileid_to_zxy(t)
            if z > maxzoom:
                problems.append(f"tile {z}/{x}/{y} is above zoom {maxzoom}")
                break
            per_zoom[z] += 1
            x0, x1, y0, y1 = ranges[z]
            if not (x0 <= x <= x1 and y0 <= y <= y1) and len(outside) < 5:
                outside.append(f"{z}/{x}/{y}")
            if z == scan_zoom:
                z_tiles[t] = (offset, length)
    if addressed != h["addressed_tiles"]:
        problems.append(f"the walk found {addressed} addressed tiles, the header says {h['addressed_tiles']}")
    if entries != h["tile_entries"]:
        problems.append(f"the walk found {entries} tile entries, the header says {h['tile_entries']}")
    if len(offsets) != h["tile_contents"]:
        problems.append(f"the walk found {len(offsets)} distinct tiles, the header says {h['tile_contents']}")
    missing = [z for z, count in per_zoom.items() if count == 0]
    if missing:
        problems.append(f"no tiles at zoom {missing}")
    if per_zoom[maxzoom] < gate["min_tiles_at_maxzoom"]:
        problems.append(f"{per_zoom[maxzoom]} tiles at zoom {maxzoom}, fewer than {gate['min_tiles_at_maxzoom']}")
    if outside:
        problems.append(f"tiles outside the bounds, e.g. {outside}")
    if largest[0] > gate["max_tile_bytes"]:
        z, x, y = largest[1]
        problems.append(f"tile {z}/{x}/{y} is {largest[0]} bytes, more than {gate['max_tile_bytes']}")
    detail = {"addressed": addressed, "entries": entries, "contents": len(offsets),
              "per_zoom": {str(z): c for z, c in per_zoom.items()},
              "largest_bytes": largest[0],
              "largest_tile": "/".join(map(str, largest[1])) if largest[1] else None}
    return problems, detail, z_tiles


def check_metadata(meta, config, osm_timestamp=None):
    gate = config["gate"]
    problems = []
    attribution = str(meta.get("attribution", ""))
    for word in ATTRIBUTION_NEEDS:
        if word not in attribution:
            problems.append(f"attribution lacks '{word}': {attribution!r}")
    description = str(meta.get("description", ""))
    for word in DESCRIPTION_NEEDS:
        if word not in description:
            problems.append(f"description lacks the ODbL notice '{word}' (build.archive_description): {description!r}")
    version = meta.get("planetiler:version")
    if version != config["planetiler"]["version"]:
        problems.append(f"built by Planetiler {version!r}, expected {config['planetiler']['version']}")
    if str(meta.get("version")) != config["planetiler"]["openmaptiles_schema"]:
        problems.append(f"schema version {meta.get('version')!r}, expected "
                        f"OpenMapTiles {config['planetiler']['openmaptiles_schema']}")
    if meta.get("format") not in (None, "pbf", "mvt"):
        problems.append(f"format {meta.get('format')!r}, expected pbf")
    stamp = meta.get("planetiler:osm:osmosisreplicationtime")
    if osm_timestamp is not None:
        try:
            same = stamp is not None and parse_time(stamp) == parse_time(osm_timestamp)
        except ValueError:
            same = False
        if not same:
            problems.append(f"OSM data time {stamp!r}, the clipped extract says {osm_timestamp!r}: "
                            "not built from this extract")
    layers = {layer.get("id"): layer for layer in meta.get("vector_layers") or []}
    missing = [name for name in gate["required_layers"] if name not in layers]
    if missing:
        problems.append(f"layers missing: {missing}")
    for name in gate["layers_with_name_ka"]:
        fields = (layers.get(name) or {}).get("fields") or {}
        if name in layers and "name:ka" not in fields:
            problems.append(f"layer {name} has no name:ka field (was --languages=ka,en passed?)")
    allowed = set(gate["allowed_name_fields"])
    extra = sorted({f for layer in layers.values() for f in (layer.get("fields") or {})
                    if (f.startswith("name:") or f.startswith("name_")) and f not in allowed})
    if extra:
        problems.append(f"name fields for other languages: {extra[:10]} (only ka and en are wanted)")
    detail = {"attribution": attribution, "planetiler_version": version,
              "planetiler_githash": meta.get("planetiler:githash"),
              "schema_version": meta.get("version"), "osm_timestamp": stamp,
              "layers": sorted(layers)}
    return problems, detail


def spot_counts(tile_bytes):
    """Count what the spot checks look at in one decoded tile."""
    layers = decode_tile(tile_bytes, {"transportation_name", "building", "poi", "housenumber", "water"})
    c = {"named_streets": 0, "streets_with_ka": 0, "buildings": 0, "pois": 0, "pois_with_ka": 0,
         "housenumbers": 0, "water_classes": [], "examples": []}
    if "transportation_name" in layers:
        for gtype, props, _ in layers["transportation_name"].features():
            if gtype == LINESTRING and props.get("name"):
                c["named_streets"] += 1
                if georgian(props.get("name:ka")):
                    c["streets_with_ka"] += 1
                    if len(c["examples"]) < 3:
                        c["examples"].append(props["name:ka"])
    if "building" in layers:
        c["buildings"] = sum(outer_rings(g) for t, _, g in layers["building"].features() if t == POLYGON)
    if "poi" in layers:
        for _, props, _ in layers["poi"].features():
            c["pois"] += 1
            if georgian(props.get("name:ka")):
                c["pois_with_ka"] += 1
    if "housenumber" in layers:
        c["housenumbers"] = len(layers["housenumber"])
    if "water" in layers:
        c["water_classes"] = sorted({str(p.get("class")) for _, p, _ in layers["water"].features()})
    return c


def check_spot(pm, spot):
    z = spot["zoom"]
    x, y = lonlat_to_tile(spot["lon"], spot["lat"], z)
    data = pm.tile(z, x, y)
    if data is None:
        return [f"no tile {z}/{x}/{y}"], {"tile": f"{z}/{x}/{y}"}
    c = spot_counts(data)
    problems = []
    share = c["streets_with_ka"] / c["named_streets"] if c["named_streets"] else 0.0
    if c["named_streets"] < spot["min_named_streets"]:
        problems.append(f"{c['named_streets']} named streets, fewer than {spot['min_named_streets']}")
    if share < spot["min_street_ka_share"]:
        problems.append(f"{share:.0%} of named streets have a Georgian name:ka, "
                        f"less than {spot['min_street_ka_share']:.0%}")
    for key, need in (("buildings", "min_buildings"), ("pois", "min_pois"),
                      ("pois_with_ka", "min_pois_with_ka"), ("housenumbers", "min_housenumbers")):
        if c[key] < spot.get(need, 0):
            problems.append(f"{c[key]} {key.replace('_', ' ')}, fewer than {spot[need]}")
    if spot.get("water_class") and spot["water_class"] not in c["water_classes"]:
        problems.append(f"no water of class {spot['water_class']} (found {c['water_classes']})")
    detail = {"tile": f"{z}/{x}/{y}", "street_ka_share": round(share, 3), **c}
    return problems, detail


def check_city_label(pm, label):
    """The city's Georgian label must be in its tile or a neighbour."""
    z = label["zoom"]
    cx, cy = lonlat_to_tile(label["lon"], label["lat"], z)
    for dx in (0, -1, 1):
        for dy in (0, -1, 1):
            if not (0 <= cx + dx < 1 << z and 0 <= cy + dy < 1 << z):
                continue
            data = pm.tile(z, cx + dx, cy + dy)
            if data is None:
                continue
            layer = decode_tile(data, {"place"}).get("place")
            if layer is None:
                continue
            for _, props, _ in layer.features():
                if props.get("name:ka") == label["name_ka"]:
                    return [], {"tile": f"{z}/{cx + dx}/{cy + dy}", "class": props.get("class"),
                                "name": props.get("name"), "name:en": props.get("name:en")}
    return [f"no place label with name:ka {label['name_ka']} at zoom {z} near "
            f"{label['lat']},{label['lon']}"], {"tile": f"{z}/{cx}/{cy}"}


class NoGoAreas:
    """The areas the road scan checks against, in lon/lat.

    hard     no road line may touch it (the no-go area, a few metres smaller)
    country  every road line must lie inside it (Georgia, a few metres larger)
    legal    tiles wholly inside it cannot break either rule and are skipped
    no_go    the no-go area as clip.py uses it; tiles touching it count
             towards the scan's coverage
    """

    def __init__(self, hard, country, legal, no_go):
        import shapely
        self.hard, self.country, self.legal, self.no_go = hard, country, legal, no_go
        for geom in (hard, country, legal, no_go):
            shapely.prepare(geom)

    @classmethod
    def from_clip_config(cls, clip_config, hard_margin_m, country_margin_m):
        sys.path.insert(0, str(HERE))
        from clip import build_zones  # noqa: E402  (same folder)
        checks = build_zones(clip_config, hard_margin_m=hard_margin_m, country_margin_m=country_margin_m)
        exact = build_zones(clip_config)
        legal = exact.georgia.difference(exact.hard)
        return cls(hard=checks.hard, country=checks.georgia, legal=legal, no_go=exact.hard)


def road_class(props, scan):
    cls = props.get("class")
    if not isinstance(cls, str):
        return False
    if cls.endswith("_construction"):
        return True
    if cls not in scan["road_classes"]:
        return False
    return not (cls == "path" and props.get("subclass") in scan["allowed_path_subclasses"])


def scan_no_go(pm, z_tiles, areas, scan, zoom):
    """Decode every tile at `zoom` that is not wholly legal and check its road
    lines. Returns (problems, detail)."""
    import numpy as np
    import shapely

    ids = sorted(z_tiles)
    boxes = [shapely.box(*tile_bounds(*tileid_to_zxy(t), margin=0.125)) for t in ids]
    skip = shapely.contains(areas.legal, boxes) if boxes else np.array([], dtype=bool)
    in_no_go = shapely.intersects(areas.no_go, boxes) if boxes else np.array([], dtype=bool)
    counts = {"tiles_at_zoom": len(ids), "tiles_checked": 0, "tiles_in_no_go": int(in_no_go.sum()),
              "road_lines": 0, "other_lines": 0, "buildings_in_no_go": 0, "places_in_no_go": 0,
              "no_go_lines": 0, "outside_lines": 0}
    examples = {"no_go_lines": [], "outside_lines": [], "places_in_no_go": []}
    # One tile's content can be stored once and addressed many times (open
    # sea); the lines are kept in tile coordinates and placed per tile.
    cache = {}
    for t, skipped, touches_no_go in zip(ids, skip, in_no_go):
        if skipped:
            continue
        counts["tiles_checked"] += 1
        z, x, y = tileid_to_zxy(t)
        offset, length = z_tiles[t]
        key = (offset, length, bool(touches_no_go))
        if key not in cache:
            if len(cache) > 256:
                cache.clear()
            data = decompress(pm.raw_tile(offset, length), pm.header["tile_compression"])
            layers = decode_tile(data, {"transportation", "transportation_name", "building", "place"})
            lines, other = [], 0
            for name in ("transportation", "transportation_name"):
                layer = layers.get(name)
                if layer is None:
                    continue
                for gtype, props, geom in layer.features():
                    if gtype != LINESTRING:
                        continue
                    if not road_class(props, scan):
                        other += 1
                        continue
                    for part in geometry_parts(geom):
                        if len(part) >= 2:
                            lines.append((part, layer.extent, name, props))
            places, buildings = [], 0
            if touches_no_go and "place" in layers:
                places = [p.get("name:ka") or p.get("name") for _, p, _ in layers["place"].features()]
            if touches_no_go and "building" in layers:
                buildings = sum(outer_rings(g) for gt, _, g in layers["building"].features() if gt == POLYGON)
            cache[key] = (lines, other, places, buildings)
        lines, other, places, buildings = cache[key]
        counts["other_lines"] += other
        if touches_no_go:
            counts["buildings_in_no_go"] += buildings
            counts["places_in_no_go"] += len(places)
            for p in places:
                if len(examples["places_in_no_go"]) < 10 and p and p not in examples["places_in_no_go"]:
                    examples["places_in_no_go"].append(p)
        if not lines:
            continue
        geoms = [shapely.linestrings(to_lonlat(z, x, y, extent, part)) for part, extent, _, _ in lines]
        counts["road_lines"] += len(geoms)
        hits = shapely.intersects(areas.hard, geoms)
        outside = ~shapely.covers(areas.country, geoms)
        for flag, bucket in ((hits, "no_go_lines"), (outside, "outside_lines")):
            for i in np.nonzero(flag)[0]:
                counts[bucket] += 1
                if len(examples[bucket]) < 10:
                    part, _, layer_name, props = lines[i]
                    lon, lat = to_lonlat(z, x, y, lines[i][1], part[:1])[0]
                    examples[bucket].append({"tile": f"{z}/{x}/{y}", "layer": layer_name,
                                             "class": props.get("class"), "name": props.get("name"),
                                             "first_point": [round(lat, 6), round(lon, 6)]})
    problems = []
    if counts["no_go_lines"]:
        problems.append(f"{counts['no_go_lines']} road line(s) inside the no-go area: the map was not drawn "
                        f"from the clipped extract, e.g. {examples['no_go_lines'][:3]}")
    if counts["outside_lines"]:
        problems.append(f"{counts['outside_lines']} road line(s) outside Georgia, e.g. {examples['outside_lines'][:3]}")
    if counts["tiles_in_no_go"] < scan["min_tiles_in_no_go"]:
        problems.append(f"only {counts['tiles_in_no_go']} zoom-{zoom} tiles touch the no-go area, "
                        f"fewer than {scan['min_tiles_in_no_go']}: the occupied areas are missing from the map")
    if counts["buildings_in_no_go"] < scan["min_buildings_in_no_go"]:
        problems.append(f"only {counts['buildings_in_no_go']} buildings in tiles that touch the no-go area, "
                        f"fewer than {scan['min_buildings_in_no_go']}: the scan saw an empty area")
    return problems, {**counts, "examples": examples}


# ---------------------------------------------------------------------------
# Subcommands
# ---------------------------------------------------------------------------

def run_gate(args):
    config = load_config(args.config)
    gate = config["gate"]
    results = []
    summary = {"output": config["output"], "pmtiles_sha256": None, "basemap_config_version": config["version"],
               "osm_timestamp": None, "clipped_pbf": None, "tiles": None}

    def record(name, problems, detail=None):
        results.append({"name": name, "passed": not problems, "problems": problems, "detail": detail or {}})
        mark = "PASS" if not problems else "FAIL"
        print(f"{mark}  {name}" + ("" if not problems else "\n      " + "\n      ".join(problems)))

    def guarded(name, func):
        started = time.time()
        try:
            problems, detail = func()
        except Exception as exc:  # a crash is a failure, never a pass
            problems, detail = [f"error: {type(exc).__name__}: {exc}"], {}
        detail = dict(detail or {})
        detail["seconds"] = round(time.time() - started, 1)
        record(name, problems, detail)
        return detail

    path = Path(args.pmtiles)
    if path.name != config["output"]:
        record("file name", [f"{path.name}, expected {config['output']}"])
    summary["pmtiles_sha256"] = sha256_file(path)
    size = path.stat().st_size
    print(f"pmtiles under test: {size} bytes, sha256 {summary['pmtiles_sha256']}")

    osm_timestamp = None
    if args.clipped_pbf:
        summary["clipped_pbf"] = file_entry(args.clipped_pbf)
        try:
            osm_timestamp = osm_header_timestamp(args.clipped_pbf)
            summary["osm_timestamp"] = osm_timestamp
        except Exception as exc:
            record("clipped extract", [f"cannot read its OSM data time: {exc}"])
    else:
        record("clipped extract", ["--clipped-pbf is required: the map must be tied to the clipped extract"])

    pm = PMTiles.open(path)
    try:
        bounds = planetiler_bounds(config)
        guarded("pmtiles header", lambda: check_header(pm.header, size, config, bounds))
        z_tiles = {}
        tiles_detail = {}

        def directory():
            nonlocal z_tiles, tiles_detail
            problems, tiles_detail, z_tiles = scan_directory(pm, config)
            return problems, tiles_detail

        guarded("pmtiles directory", directory)
        summary["tiles"] = tiles_detail
        previous = None
        if args.previous_manifest and Path(args.previous_manifest).exists():
            previous = json.loads(Path(args.previous_manifest).read_text(encoding="utf-8"))
        top = (tiles_detail.get("per_zoom") or {}).get(str(config["build"]["maxzoom"]))
        guarded("size", lambda: check_size(size, config, previous, top))
        guarded("metadata", lambda: check_metadata(pm.metadata(), config, osm_timestamp))
        for spot in gate["spot_checks"]:
            guarded(f"spot: {spot['name']}", lambda s=spot: check_spot(pm, s))
        for label in gate["city_labels"]:
            guarded(f"city label: {label['name']}", lambda lb=label: check_city_label(pm, lb))
        scan = gate["no_go_scan"]
        if args.skip_no_go_scan:
            print("WARN  no-go road scan skipped (--skip-no-go-scan); never use this in CI")
        else:
            def no_go():
                sys.path.insert(0, str(HERE))
                from clip import load_config as load_clip_config  # noqa: E402
                clip_config = load_clip_config(args.clip_config)
                summary["clip_config_version"] = clip_config["version"]
                areas = NoGoAreas.from_clip_config(clip_config, scan["hard_margin_m"], scan["country_margin_m"])
                if not z_tiles:
                    return [f"no zoom-{scan['zoom']} tiles to scan"], {}
                return scan_no_go(pm, z_tiles, areas, scan, scan["zoom"])

            guarded("no-go road scan", no_go)
    finally:
        pm.close()

    failed = [r for r in results if not r["passed"]]
    summary.update({"passed": not failed, "checks": len(results), "failed": len(failed), "results": results})
    if args.results:
        Path(args.results).write_text(json.dumps(summary, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"basemap gate: {len(results) - len(failed)}/{len(results)} checks passed")
    return 0 if not failed else 1


def report_problems(gate, pmtiles_sha256, config, clipped):
    """Why this gate result may not be published, or an empty list."""
    problems = []
    if not gate.get("passed") or gate.get("failed"):
        problems.append("the basemap gate did not pass")
    names = {r.get("name") for r in gate.get("results", [])}
    for needed in REQUIRED_CHECKS:
        if needed not in names:
            problems.append(f"the gate ran without its '{needed}' check")
    if gate.get("pmtiles_sha256") != pmtiles_sha256:
        problems.append(f"the gate tested a pmtiles with sha256 {gate.get('pmtiles_sha256')}, "
                        f"but the file to publish is {pmtiles_sha256}")
    if gate.get("basemap_config_version") != config["version"]:
        problems.append(f"the gate ran with basemap config v{gate.get('basemap_config_version')}, "
                        f"config is v{config['version']}")
    if (gate.get("clipped_pbf") or {}).get("sha256") != clipped["sha256"]:
        problems.append("the gate checked another clipped extract")
    return problems


REQUIRED_CHECKS = ("pmtiles header", "pmtiles directory", "size", "metadata", "no-go road scan")

LICENCE_NOTE = (
    "georgia.pmtiles contains information from OpenStreetMap (© OpenStreetMap contributors), made "
    "available under the Open Database License (ODbL 1.0), and is itself published as a derivative "
    "database under the ODbL 1.0 (the method is this repository at the source commit). Its layers "
    "follow the OpenMapTiles schema (CC-BY 4.0, https://creativecommons.org/licenses/by/4.0/), so every "
    "map drawn from it must visibly show '© OpenMapTiles © OpenStreetMap contributors', linked to "
    "https://openmaptiles.org/ and https://www.openstreetmap.org/copyright, in the map corner at all "
    "times, not only on an About screen or behind an (i) button. Low zoom levels also use Natural Earth "
    "(public domain), and the sea and lake labels OpenStreetMap water polygons and osm-lakelines (both "
    "ODbL, © OpenStreetMap contributors). If an app store or the app puts it behind technical protection "
    "(DRM, encryption at rest), the unrestricted copy in this GitHub Release must stay available (ODbL 4.7 "
    "parallel distribution); never merge other map data into it (ODbL 4.4).")

TOOL_LICENCE = (
    "planetiler.jar: Apache-2.0; it also bundles LGPL GeoTools (gt-shapefile, gt-epsg-hsql, which also carries "
    "the EPSG dataset terms), EDL JTS, ICU and others per its NOTICE.md. planetiler-openmaptiles: BSD-3-Clause "
    "code, CC-BY-4.0 schema. Runs in CI only and is never distributed; georgia.pmtiles is data made from "
    "ODbL and public-domain inputs, not a derivative of this code.")


def credit_sources(sources):
    """Everything the basemap is made from, for the manifest's credits."""
    out = [{"name": "OpenStreetMap (the safety-clipped extract)", "licence": "ODbL-1.0",
            "attribution": "© OpenStreetMap contributors", "url": COPYRIGHT_URL, "licence_url": ODBL_URL},
           {"name": "OpenMapTiles schema 3.16 (layers and their fields)", "licence": "CC-BY-4.0",
            "attribution": "© OpenMapTiles", "url": OPENMAPTILES_URL, "licence_url": CC_BY_4_URL}]
    for src in sources:
        licence = str(src.get("licence") or "")
        out.append({"name": src.get("about") or src.get("name"), "licence": licence,
                    "attribution": src.get("attribution"), "url": src.get("url"),
                    **({"licence_url": ODBL_URL} if licence.startswith("ODbL") else {})})
    return out


def build_report(config, config_path, gate, pmtiles, clipped, clip_config, sources, build_info, commit):
    """The asset report manifest.py reads (--asset-report): section, passed,
    files, then the body that becomes manifest.json's "basemap" section."""
    by_name = {r["name"]: r for r in gate["results"]}
    meta = by_name["metadata"]["detail"]
    header = by_name["pmtiles header"]["detail"]
    pt = config["planetiler"]
    return {
        "section": SECTION,
        "passed": True,
        "files": [pmtiles],
        "binds_to": {"osm_timestamp": gate.get("osm_timestamp"), "clipped_pbf_sha256": clipped["sha256"],
                     "clip_config_version": clip_config["version"], "source_commit": commit},
        "format": "PMTiles v3, gzip-compressed Mapbox Vector Tiles",
        "schema": f"OpenMapTiles {pt['openmaptiles_schema']}",
        "attribution": ATTRIBUTION_TEXT,
        "attribution_links": ATTRIBUTION_LINKS,
        "attribution_html": meta.get("attribution"),
        "attribution_rule": "always visible in the map corner, turn-by-turn mode included; never only behind (i)",
        "licence": "ODbL-1.0",
        "licence_url": ODBL_URL,
        "schema_licence": "CC-BY-4.0 (OpenMapTiles)",
        "schema_licence_url": OPENMAPTILES_LICENCE_URL,
        "schema_licence_uri": CC_BY_4_URL,
        "licence_note": LICENCE_NOTE,
        "credit_sources": credit_sources(sources),
        "languages": config["build"]["languages"],
        "label_rule": "text-field coalesce(name:ka, name); never uppercase Georgian (Mtavruli)",
        "minzoom": header.get("min_zoom"),
        "maxzoom": header.get("max_zoom"),
        "bounds": header.get("bounds"),
        "center": header.get("center"),
        "layers": meta.get("layers"),
        "tiles": gate.get("tiles"),
        "input": {"clipped_pbf": clipped, "osm_timestamp": gate.get("osm_timestamp"),
                  "clip_config_version": clip_config["version"],
                  "note": ("the same safety-clipped extract as valhalla_tiles.tar: no road inside the "
                           "occupied areas (plus 100 m) or outside Georgia; place labels, buildings and "
                           "POIs there stay on the map")},
        "tool": {"name": "Planetiler", "version": pt["version"], "licence": TOOL_LICENCE,
                 "jar_url": pt["jar_url"], "jar_sha256": pt["jar_sha256"],
                 "planetiler_commit": pt["planetiler_commit"], "githash": meta.get("planetiler_githash"),
                 "profile": "planetiler-openmaptiles", "profile_commit": pt["openmaptiles_commit"],
                 "profile_licence": "BSD-3-Clause (code), CC-BY-4.0 (schema)",
                 "java": build_info.get("java"), "arguments": build_info.get("arguments")},
        "sources": sources,
        "config_version": config["version"],
        "config_sha256": sha256_file(config_path),
        "gate": {"passed": gate["passed"], "checks": gate["checks"], "failed": gate["failed"],
                 "pmtiles_sha256": gate["pmtiles_sha256"],
                 "results": [{"name": r["name"], "passed": r["passed"]} for r in gate["results"]]},
    }


def run_report(args):
    config = load_config(args.config)
    gate = json.loads(Path(args.gate).read_text(encoding="utf-8"))
    pmtiles = file_entry(args.pmtiles)
    clipped = file_entry(args.clipped_pbf)
    problems = report_problems(gate, pmtiles["sha256"], config, clipped)
    out = Path(args.out) if args.out else Path(args.pmtiles).parent / "basemap_report.json"
    if out.resolve().parent != Path(args.pmtiles).resolve().parent:
        problems.append("the report must lie next to georgia.pmtiles (manifest.py looks for it there)")
    if not args.commit:
        problems.append("--commit (or $GITHUB_SHA) is required")
    if problems:
        for problem in problems:
            print(f"ERROR: {problem}", file=sys.stderr)
        print("ERROR: no basemap report written", file=sys.stderr)
        return 1
    sources = json.loads(Path(args.sources).read_text(encoding="utf-8"))
    build_info = json.loads(Path(args.build_info).read_text(encoding="utf-8")) if args.build_info else {}
    sys.path.insert(0, str(HERE))
    from clip import load_config as load_clip_config  # noqa: E402
    clip_config = load_clip_config(args.clip_config)
    report = build_report(config, args.config, gate, pmtiles, clipped, clip_config, sources, build_info,
                          args.commit)
    out.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"basemap report: {pmtiles['name']} {pmtiles['bytes']} bytes, sha256 {pmtiles['sha256'][:12]}...")
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description="Georgia basemap (georgia.pmtiles): bounds, sources, gate, "
                                                 "manifest report.")
    parser.add_argument("--config", default=str(DEFAULT_CONFIG))
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("bounds", help="print Planetiler's --bounds")

    p = sub.add_parser("verify-sources", help="check the downloaded source files, write sources.json")
    p.add_argument("--dir", required=True)
    p.add_argument("--out", required=True)

    p = sub.add_parser("gate", help="check a built georgia.pmtiles")
    p.add_argument("--pmtiles", required=True)
    p.add_argument("--clipped-pbf", help="the clipped extract the map was built from")
    p.add_argument("--clip-config", default=str(CLIP_CONFIG))
    p.add_argument("--previous-manifest", help="manifest.json of the latest release, if any")
    p.add_argument("--results", help="write the full results as JSON here")
    p.add_argument("--skip-no-go-scan", action="store_true", help="only for local experiments")

    p = sub.add_parser("report", help="write the manifest report for a gated georgia.pmtiles")
    p.add_argument("--pmtiles", required=True)
    p.add_argument("--gate", required=True)
    p.add_argument("--sources", required=True)
    p.add_argument("--build-info")
    p.add_argument("--clipped-pbf", required=True)
    p.add_argument("--clip-config", default=str(CLIP_CONFIG))
    p.add_argument("--commit", default=os.environ.get("GITHUB_SHA"))
    p.add_argument("--out", help="default: basemap_report.json next to the pmtiles")

    args = parser.parse_args(argv)
    if args.command == "bounds":
        print(",".join(f"{v:.2f}" for v in planetiler_bounds(load_config(args.config))))
        return 0
    if args.command == "verify-sources":
        entries, problems = verify_sources(load_config(args.config), args.dir)
        for e in entries:
            mark = "bad" if any(p.startswith(e["name"] + ":") for p in problems) else "ok "
            print(f"{mark} {e['name']}: {e['bytes']} bytes, sha256 {e['sha256'][:12]}..., pinned by {e['pinned']}")
        if problems:
            for problem in problems:
                print(f"::error::{problem}")
            return 1
        Path(args.out).write_text(json.dumps(entries, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        return 0
    if args.command == "gate":
        return run_gate(args)
    return run_report(args)


if __name__ == "__main__":
    sys.exit(main())
