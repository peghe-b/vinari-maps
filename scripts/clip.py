#!/usr/bin/env python3
"""Safety clip: remove every road the navigator must never use.

Georgian law (Law on Occupied Territories Art 4, Criminal Code Art 322-1)
makes it a crime for foreign citizens and stateless persons to enter
Abkhazia or the Tskhinvali region from most directions, and anyone near the
line risks detention by the de facto authorities. A navigator that routes a
driver across the occupation line, even by accident, can put that driver in
prison or in danger. So the roads are removed from the map data itself,
before the routing tiles are built. Nothing a phone sends later can bring
them back.

What this script does to the OpenStreetMap extract:

1. Builds three areas from the frozen polygons in config/boundaries/:
   - the no-go area: both occupied regions plus the whole Perevi village
     (a hand-made polygon), pushed outward by hard_buffer_m (100 m) in a
     metric projection;
   - the soft band: from that edge out to soft_band.outer_m (500 m);
   - Georgia's recognised border (relation 28699).
2. For every road-like way (anything with highway=*, or a ferry or car
   train route) it keeps only the stretches whose nodes are inside Georgia
   and outside the no-go area, and whose straight segments neither touch
   the no-go area nor leave Georgia between two nodes. A way that is partly
   bad is cut at its last good node: the first piece keeps the OSM id,
   later pieces get fresh ids. A way with nothing left is dropped. A ferry
   or car train with any bad stretch is dropped whole: a boat that leaves
   the country has no legal use in these tiles.
3. Minor roads (the soft_band classes) are split where they enter or leave
   the soft band, and every piece inside the band becomes destination-only
   (motorcar=destination), unless cars are already banned or restricted
   there. Roads tagged 4wd_only=yes become destination-only too.
4. Turn restrictions that mention a removed way are dropped. A restriction
   on a cut or split way is kept when its via node lies on exactly one
   surviving piece of that way (the member is pointed at that piece), and
   dropped otherwise. Other relations lose the members that no longer exist
   and list every surviving piece of a split way in its place.

Nodes, boundaries, buildings and everything else pass through unchanged,
so Valhalla's admin and timezone lookups keep working.

It also measures, in the raw input, how far each must-fail target of
config/gate_routes.json lies from the nearest car road. The gate uses this
to reject a strict test that could never have found a road anyway.

Usage (as in the CI workflow):
  python scripts/clip.py --input build/georgia-latest.osm.pbf \
      --output build/georgia-clipped.osm.pbf \
      --zones build/zones.geojson --report build/clip_report.json

Libraries: shapely (BSD-3), pyproj (MIT) and, for reading and writing the
PBF, pyosmium (BSD-2). They are imported only where needed so that
test_clip.py can check the logic without the big ones installed.
"""

import argparse
import json
import math
import sys
import time
from pathlib import Path

import numpy as np
import shapely
from shapely.geometry import LineString, Point, mapping, shape
from shapely.ops import transform, unary_union

sys.path.insert(0, str(Path(__file__).resolve().parent))
from fetch_boundaries import geometry_sha256  # noqa: E402  (same folder, stdlib only)

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG = ROOT / "config" / "clip.json"
DEFAULT_ROUTES = ROOT / "config" / "gate_routes.json"

# The S1/E60 motorway runs about 410 m from the South Ossetia line near
# Khurvaleti. A hard buffer near that size would cut Georgia's main road.
MAX_HARD_BUFFER_M = 300

FERRY_ROUTES = ("ferry", "shuttle_train")

# The raw-road probe looks this far around each must-fail target.
PROBE_RADIUS_M = 1000

ODBL_NOTICE = "© OpenStreetMap contributors, ODbL 1.0: https://opendatacommons.org/licenses/odbl/1-0/"


# ---------------------------------------------------------------------------
# Configuration and areas
# ---------------------------------------------------------------------------

def load_config(path=DEFAULT_CONFIG):
    """Read clip.json and refuse settings that would be unsafe or broken."""
    path = Path(path)
    config = json.loads(path.read_text(encoding="utf-8"))
    problems = []
    if not isinstance(config.get("version"), int) or config["version"] < 1:
        problems.append("'version' must be a positive integer")
    hard = config.get("hard_buffer_m")
    if not isinstance(hard, (int, float)) or not 0 < hard <= MAX_HARD_BUFFER_M:
        problems.append(f"hard_buffer_m must be above 0 and at most {MAX_HARD_BUFFER_M}")
    band = config.get("soft_band", {})
    if band.get("enabled") and not band.get("outer_m", 0) > (hard or 0):
        problems.append("soft_band.outer_m must be larger than hard_buffer_m")
    if not config.get("no_go"):
        problems.append("the no_go list is empty")
    if config.get("new_way_id_start", 0) < 2_000_000_000:
        problems.append("new_way_id_start is too low and could clash with real OSM ids")
    if config.get("way_mode", "cut") not in ("cut", "drop"):
        problems.append("way_mode must be 'cut' or 'drop'")
    if problems:
        raise ValueError("bad clip config: " + "; ".join(problems))
    config["_dir"] = str(path.parent)
    return config


def source_key(entry):
    return (entry.get("osm_type"), entry.get("osm_id"), entry.get("osm_version"))


def load_boundary(config, entry):
    """Load one frozen GeoJSON polygon and check it is the object the
    config names, so a stale, swapped or edited file cannot slip through."""
    path = Path(config["_dir"]) / entry["file"]
    feature = json.loads(path.read_text(encoding="utf-8"))["features"][0]
    props = feature["properties"]
    if entry.get("hand_made"):
        if props.get("hand_made") is not True:
            raise ValueError(f"{path.name}: clip.json calls it hand-made but the file does not")
        if [source_key(s) for s in props.get("derived_from", [])] != \
                [source_key(s) for s in entry.get("derived_from", [])]:
            raise ValueError(f"{path.name}: its derived_from list differs from clip.json")
        actual = geometry_sha256(feature["geometry"]["coordinates"])
        if actual != entry.get("geometry_sha256"):
            raise ValueError(f"{path.name}: geometry hash is {actual} but clip.json says "
                             f"{entry.get('geometry_sha256')}; a hand-made polygon changed")
    else:
        for key in ("osm_type", "osm_id", "osm_version"):
            if props.get(key) != entry.get(key):
                raise ValueError(f"{path.name}: {key} is {props.get(key)!r} but clip.json "
                                 f"says {entry.get(key)!r}; refresh the file or fix the config")
    geom = shape(feature["geometry"])
    if geom.is_empty or not geom.is_valid:
        raise ValueError(f"{path.name}: polygon is empty or invalid")
    return geom


class Zones:
    """The areas the clip works with, all in lon/lat (EPSG:4326).

    georgia     recognised border; ways must stay inside it
    occupied    the no-go areas exactly as drawn (for reports and the app)
    hard        occupied pushed out by hard_buffer_m; nothing may touch it
    band_outer  occupied pushed out by soft_band.outer_m
    """

    def __init__(self, georgia, hard, band_outer=None, occupied=None):
        self.georgia = georgia
        self.hard = hard
        self.band_outer = band_outer
        self.occupied = occupied
        for geom in (georgia, hard, band_outer):
            if geom is not None:
                shapely.prepare(geom)  # makes repeated tests fast


def build_zones(config, hard_margin_m=0.0, country_margin_m=0.0):
    """Build the Zones from the frozen polygons.

    The buffers are computed in the metric projection named in the config,
    so 100 m means 100 m on the ground. The two margins exist for gate.py:
    it checks results against a zone 1 m smaller (to absorb coordinate
    rounding) and a country a few metres larger.
    """
    import pyproj  # only needed here

    to_m = pyproj.Transformer.from_crs("EPSG:4326", config["metric_crs"], always_xy=True).transform
    to_ll = pyproj.Transformer.from_crs(config["metric_crs"], "EPSG:4326", always_xy=True).transform

    occupied = unary_union([load_boundary(config, e) for e in config["no_go"]])
    occupied_m = transform(to_m, occupied)

    def grow(geom_m, metres):
        if metres == 0:
            return transform(to_ll, geom_m)
        return transform(to_ll, geom_m.buffer(metres, quad_segs=16))

    hard = grow(occupied_m, config["hard_buffer_m"] - hard_margin_m)
    band = config.get("soft_band", {})
    band_outer = grow(occupied_m, band["outer_m"]) if band.get("enabled") else None

    georgia = load_boundary(config, config["country"])
    if country_margin_m:
        georgia = grow(transform(to_m, georgia), country_margin_m)
    return Zones(georgia=georgia, hard=hard, band_outer=band_outer, occupied=occupied)


def write_zones(zones, config, path):
    """Save the areas as GeoJSON. gate.py reads this file, and it is
    published with the tiles so the app can run the same checks."""
    features = []
    for name, geom, about in (
        ("no_go_hard", zones.hard, f"occupied areas pushed out by {config['hard_buffer_m']} m; "
                                    "no road exists inside"),
        ("soft_band_outer", zones.band_outer, "outer edge of the warning band"),
        ("occupied", zones.occupied, "occupied areas as drawn in OSM, plus the whole Perevi village"),
        ("georgia", zones.georgia, "Georgia's recognised border (OSM relation 28699)"),
    ):
        if geom is not None:
            features.append({"type": "Feature",
                             "properties": {"name": name, "about": about,
                                            "clip_config_version": config["version"]},
                             "geometry": mapping(geom)})
    collection = {"type": "FeatureCollection",
                  "attribution": ODBL_NOTICE,
                  "features": features}
    Path(path).write_text(json.dumps(collection, separators=(",", ":"), ensure_ascii=False),
                          encoding="utf-8")


# ---------------------------------------------------------------------------
# Car access, mirroring Valhalla 3.6.3 lua/graph.lua
# ---------------------------------------------------------------------------

# highway values whose default car access is "true" in graph.lua
CAR_HIGHWAYS = {
    "motorway", "motorway_link", "trunk", "trunk_link", "primary", "primary_link",
    "secondary", "secondary_link", "tertiary", "tertiary_link", "unclassified",
    "residential", "residential_link", "living_street", "service", "road", "track",
}

# graph.lua 'access' table
LUA_ACCESS = {
    "yes": "true", "private": "true", "no": "false", "permissive": "true",
    "agricultural": "false", "use_sidepath": "true", "delivery": "true",
    "designated": "true", "dismount": "true", "discouraged": "false",
    "forestry": "false", "destination": "true", "customers": "true",
    "official": "true", "public": "true", "restricted": "true", "allowed": "true",
    "emergency": "false", "psv": "false", "permit": "true", "residents": "true",
}

# graph.lua 'motor_vehicle' table (also used for vehicle= and motorcar=)
LUA_MOTOR_VEHICLE = {
    "yes": "true", "private": "true", "no": "false", "permissive": "true",
    "agricultural": "false", "delivery": "true", "designated": "true",
    "discouraged": "false", "forestry": "false", "destination": "true",
    "customers": "true", "official": "true", "public": "true",
    "restricted": "true", "allowed": "true", "permit": "true", "residents": "true",
}

# graph.lua 'private' table: any of these makes a road destination-only
LUA_PRIVATE = {v: "true" for v in ("private", "destination", "customers", "delivery",
                                   "permit", "residents")}


def lua_any_in(table, value):
    """Python copy of graph.lua any_in(): handles 'a;b' lists the same way."""
    if value is None:
        return None
    if value in table:
        return table[value]
    found = None
    for part in value.split(";"):
        if not part:
            continue
        found = table.get(part) or found
        if found == "true":
            break
    return found


def car_access(tags):
    """How Valhalla 3.6.3 would treat a car on this way:
    'no', 'destination' (allowed, destination-only) or 'yes'."""
    highway = tags.get("highway")
    if highway == "construction":
        return "no"  # graph.lua switches all access off for construction
    allowed = highway in CAR_HIGHWAYS or tags.get("route") in FERRY_ROUTES
    if (tags.get("impassable") == "yes"
            or lua_any_in(LUA_ACCESS, tags.get("access")) == "false"
            or tags.get("smoothness") == "impassable"):
        allowed = False
    # The most specific tag present wins: motorcar, then motor_vehicle, then vehicle.
    override = (lua_any_in(LUA_MOTOR_VEHICLE, tags.get("motorcar"))
                or lua_any_in(LUA_MOTOR_VEHICLE, tags.get("motor_vehicle"))
                or lua_any_in(LUA_MOTOR_VEHICLE, tags.get("vehicle")))
    if override is not None:
        allowed = override == "true"
    if not allowed:
        return "no"
    for key in ("access", "motor_vehicle", "motorcar", "vehicle"):
        if lua_any_in(LUA_PRIVATE, tags.get(key)) == "true":
            return "destination"
    return "yes"


def make_destination_only(tags):
    """Add motorcar=destination if cars may use the way freely today.
    Never opens a road: banned or already restricted ways are left alone.
    Returns True if the tags changed."""
    if car_access(tags) != "yes":
        return False
    tags["motorcar"] = "destination"
    return True


def is_road_like(tags):
    """Ways Valhalla can turn into drivable edges (see filter_tags_generic
    in graph.lua): highways, ferries and car trains. Everything else
    (buildings, land use, boundaries) passes through untouched."""
    return "highway" in tags or tags.get("route") in FERRY_ROUTES


# ---------------------------------------------------------------------------
# The clip itself, independent of any file format (unit-tested)
# ---------------------------------------------------------------------------

def good_runs(seg_ok):
    """Turn a per-segment True/False list into (first_node, last_node)
    index pairs, one per unbroken run of good segments."""
    runs, start = [], None
    for i, ok in enumerate(seg_ok):
        if ok and start is None:
            start = i
        elif not ok and start is not None:
            runs.append((start, i))  # segments start..i-1 -> nodes start..i
            start = None
    if start is not None:
        runs.append((start, len(seg_ok)))
    return runs


def split_by_flags(first_node, flags):
    """Split a run of segments where a per-segment flag changes.
    Returns (first_node, last_node, flag) pieces; neighbours share a node."""
    pieces, start = [], first_node
    for i in range(1, len(flags)):
        if flags[i] != flags[i - 1]:
            pieces.append((start, first_node + i, bool(flags[i - 1])))
            start = first_node + i
    pieces.append((start, first_node + len(flags), bool(flags[-1])))
    return pieces


def segment_lines(xs, ys, idx):
    """One two-point line per segment index (segment i joins node i and i+1)."""
    return shapely.linestrings(np.stack([np.column_stack([xs[idx], ys[idx]]),
                                         np.column_stack([xs[idx + 1], ys[idx + 1]])], axis=1))


class RawRoadProbe:
    """How far each target lies from the nearest car-class road in the raw
    input. Access tags are ignored on purpose: a stub tagged access=no is
    still a road that one OSM edit could open, and the clip must remove it
    anyway, so a strict test on it proves something.

    Distances use a flat projection around each target, which is accurate
    to well under 1 percent within PROBE_RADIUS_M."""

    def __init__(self, targets, radius_m=PROBE_RADIUS_M):
        self.ids = list(targets)
        self.lat = np.array([targets[k][0] for k in self.ids], dtype=float)
        self.lon = np.array([targets[k][1] for k in self.ids], dtype=float)
        self.m_lat = 110_574.0
        self.m_lon = 111_320.0 * np.cos(np.radians(self.lat))
        self.pad_lat = radius_m / self.m_lat
        self.pad_lon = radius_m / self.m_lon
        self.best = {k: (math.inf, None) for k in self.ids}

    def feed(self, way_id, xs, ys, tags):
        if not self.ids:
            return
        if tags.get("highway") not in CAR_HIGHWAYS and tags.get("route") not in FERRY_ROUTES:
            return
        xs = np.asarray(xs, dtype=float)
        ys = np.asarray(ys, dtype=float)
        finite = np.isfinite(xs) & np.isfinite(ys)
        if finite.sum() < 1:
            return
        x, y = xs[finite], ys[finite]
        near = ((self.lon + self.pad_lon >= x.min()) & (self.lon - self.pad_lon <= x.max())
                & (self.lat + self.pad_lat >= y.min()) & (self.lat - self.pad_lat <= y.max()))
        for i in np.nonzero(near)[0]:
            px = (x - self.lon[i]) * self.m_lon[i]
            py = (y - self.lat[i]) * self.m_lat
            geom = LineString(zip(px, py)) if len(px) > 1 else Point(px[0], py[0])
            d = geom.distance(Point(0.0, 0.0))
            key = self.ids[i]
            if d < self.best[key][0]:
                self.best[key] = (d, way_id)

    def report(self):
        return {k: {"raw_road_m": (round(d, 1) if math.isfinite(d) else None), "way_id": w}
                for k, (d, w) in self.best.items()}


class WayClipper:
    """Decides, way by way, what survives. Keeps the bookkeeping that the
    relation step and the report need."""

    def __init__(self, zones, config, probe=None):
        self.zones = zones
        band = config.get("soft_band", {})
        self.band_enabled = bool(band.get("enabled")) and zones.band_outer is not None
        self.band_classes = set(band.get("highway_classes", []))
        self.four_wd_rule = bool(config.get("tag_rules", {}).get("four_wd_only_is_destination"))
        self.cut_ways = config.get("way_mode", "cut") == "cut"
        self.next_id = int(config["new_way_id_start"])
        self.probe = probe
        self.removed = set()    # way ids that are gone completely
        self.pieces = {}        # cut or split way id -> [(piece id, node ids), ...]
        self.stats = {"road_ways": 0, "kept_whole": 0, "cut": 0, "band_split": 0, "removed": 0,
                      "extra_pieces": 0, "band_destination": 0, "four_wd_destination": 0,
                      "removed_no_go": 0, "removed_outside_georgia": 0, "removed_ferry": 0}
        self.samples = {"removed_no_go": [], "removed_outside_georgia": [], "cut": [],
                        "band_split": [], "made_destination": []}

    @property
    def modified(self):
        """Ids of ways that were cut or split (the first piece kept the id)."""
        return self.pieces.keys()

    def _sample(self, key, way_id):
        if len(self.samples[key]) < 40:
            self.samples[key].append(way_id)

    def classify(self, xs, ys):
        """Per node and per segment: may this stay?"""
        xs = np.asarray(xs, dtype=float)
        ys = np.asarray(ys, dtype=float)
        finite = np.isfinite(xs) & np.isfinite(ys)  # a node without location counts as bad
        in_country = finite & shapely.intersects_xy(self.zones.georgia, xs, ys)
        in_hard = finite & shapely.intersects_xy(self.zones.hard, xs, ys)
        node_ok = in_country & ~in_hard
        seg_ok = node_ok[:-1] & node_ok[1:]
        # A straight segment can cut through a corner of the no-go area, or
        # across a bend of the border, even when both of its ends are fine,
        # so test the segments too. One test on the whole way first skips
        # this for almost every way.
        if seg_ok.any():
            clear = False
            if finite.all():
                line = shapely.linestrings(np.column_stack([xs, ys]))
                clear = (not shapely.intersects(self.zones.hard, line)
                         and bool(shapely.covers(self.zones.georgia, line)))
            if not clear:
                idx = np.nonzero(seg_ok)[0]
                segs = segment_lines(xs, ys, idx)
                bad = (shapely.intersects(self.zones.hard, segs)
                       | ~shapely.covers(self.zones.georgia, segs))
                seg_ok[idx[bad]] = False
        return node_ok, in_country, in_hard, seg_ok

    def band_flags(self, xs, ys, a, b):
        """Per segment of nodes a..b: True if it lies (partly) in the soft
        band. A segment counts when either end is inside band_outer or the
        straight line between them crosses it (long segments)."""
        x, y = xs[a:b + 1], ys[a:b + 1]
        flags = np.zeros(b - a, dtype=bool)
        if not shapely.intersects(self.zones.band_outer, shapely.linestrings(np.column_stack([x, y]))):
            return flags
        node_in = shapely.intersects_xy(self.zones.band_outer, x, y)
        flags = node_in[:-1] | node_in[1:]
        rest = np.nonzero(~flags)[0]
        if len(rest):
            flags[rest[shapely.intersects(self.zones.band_outer, segment_lines(x, y, rest))]] = True
        return flags

    def process(self, way_id, node_ids, xs, ys, tags):
        """Decide the fate of one way.

        Returns (pieces, changed): pieces is a list of (id, node_ids, tags)
        to write, empty if the way is removed; changed is False when the
        way can be copied exactly as it was.
        """
        if not is_road_like(tags):
            return [(way_id, node_ids, tags)], False
        self.stats["road_ways"] += 1
        if len(node_ids) < 2:
            return [(way_id, node_ids, tags)], False  # broken way; Valhalla skips it anyway

        xs = np.asarray(xs, dtype=float)
        ys = np.asarray(ys, dtype=float)
        if self.probe is not None:
            self.probe.feed(way_id, xs, ys, tags)
        node_ok, in_country, in_hard, seg_ok = self.classify(xs, ys)
        ferry = tags.get("route") in FERRY_ROUTES
        whole = bool(seg_ok.all())

        if whole:
            runs = [(0, len(node_ids) - 1)]
        else:
            # "drop" mode (config way_mode) removes the whole way instead of
            # cutting it; stricter, but it also deletes the legal part. A
            # ferry is always dropped whole.
            runs = good_runs(seg_ok.tolist()) if (self.cut_ways and not ferry) else []
            if not runs:
                if in_hard.any():
                    reason = "removed_no_go"
                elif not in_country.all():
                    reason = "removed_outside_georgia"
                else:
                    reason = "removed_no_go"  # every bad segment crosses the no-go area or the border
                self.removed.add(way_id)
                self.stats["removed"] += 1
                self.stats[reason] += 1
                if ferry:
                    self.stats["removed_ferry"] += 1
                self._sample(reason, way_id)
                return [], True

        base = dict(tags)
        four = (self.four_wd_rule and base.get("4wd_only") == "yes"
                and make_destination_only(base))
        if four:
            self.stats["four_wd_destination"] += 1
        band = (self.band_enabled and base.get("highway") in self.band_classes
                and car_access(base) == "yes")

        parts = []
        for a, b in runs:
            flags = self.band_flags(xs, ys, a, b) if band else None
            if flags is None or not flags.any():
                parts.append((a, b, False))
            elif flags.all():
                parts.append((a, b, True))
            else:
                parts.extend(split_by_flags(a, flags))

        if whole and len(parts) == 1:
            self.stats["kept_whole"] += 1
            in_band = parts[0][2] and make_destination_only(base)
            if in_band:
                self.stats["band_destination"] += 1
            if four or in_band:
                self._sample("made_destination", way_id)
            return [(way_id, node_ids, base)], bool(four or in_band)

        if whole:
            self.stats["band_split"] += 1
            self._sample("band_split", way_id)
        else:
            self.stats["cut"] += 1
            self._sample("cut", way_id)
        pieces = []
        for n, (a, b, in_band) in enumerate(parts):
            piece_id = way_id if n == 0 else self._new_id()
            piece_tags = dict(base)
            if in_band and make_destination_only(piece_tags):
                self.stats["band_destination"] += 1
                self._sample("made_destination", piece_id)
            pieces.append((piece_id, list(node_ids[a:b + 1]), piece_tags))
        self.stats["extra_pieces"] += len(pieces) - 1
        self.pieces[way_id] = [(pid, nodes) for pid, nodes, _ in pieces]
        return pieces, True

    def _new_id(self):
        new_id = self.next_id
        self.next_id += 1
        if new_id >= 2 ** 32:
            raise OverflowError("ran out of fresh way ids below 2^32")
        return new_id


def filter_relation(tags, members, removed, pieces):
    """Decide what happens to one relation.

    members is a list of (type, ref, role) with type 'n', 'w' or 'r';
    removed is the set of way ids that are gone; pieces maps each cut or
    split way id to its surviving [(piece id, node ids), ...].
    Returns None to drop the relation, or the (possibly changed) member list.
    """
    if tags.get("type", "").startswith("restriction"):
        if any(m[0] == "w" and m[1] in removed for m in members):
            return None
        if not any(m[0] == "w" and m[1] in pieces for m in members):
            return list(members)
        # Only a single via node can tell which piece a member now is.
        via = [m[1] for m in members if m[2] == "via"]
        if len(via) != 1 or any(m[0] != "n" for m in members if m[2] == "via"):
            return None
        out = []
        for m in members:
            if m[0] == "w" and m[1] in pieces:
                hits = [pid for pid, nodes in pieces[m[1]] if via[0] in nodes]
                if len(hits) != 1:
                    return None  # the via node is gone, or ambiguous (a loop)
                out.append(("w", hits[0], m[2]))
            else:
                out.append(m)
        return out
    kept = []
    for m in members:
        if m[0] == "w" and m[1] in removed:
            continue
        if m[0] == "w" and m[1] in pieces:
            kept.extend(("w", pid, m[2]) for pid, _ in pieces[m[1]])
        else:
            kept.append(m)
    return kept or None


# ---------------------------------------------------------------------------
# PBF reading and writing (pyosmium)
# ---------------------------------------------------------------------------

def run_clip(input_path, output_path, clipper):
    """Stream the extract once: nodes pass through, ways go through the
    clipper, relations are filtered. New pieces are held back until the
    first relation so that way ids stay in ascending order in the file."""
    import osmium
    from osmium.osm import mutable

    stats = {"relations_dropped": 0, "relations_changed": 0, "relation_members_removed": 0}

    class Handler(osmium.SimpleHandler):
        def __init__(self, writer):
            super().__init__()
            self.writer = writer
            self.pending = []  # fresh-id pieces, written after the last real way

        def flush(self):
            for piece in sorted(self.pending, key=lambda w: w.id):
                self.writer.add_way(piece)
            self.pending = []

        def node(self, n):
            self.writer.add_node(n)

        def way(self, w):
            tags = {t.k: t.v for t in w.tags}
            if not is_road_like(tags):
                self.writer.add_way(w)  # buildings, land use...: no location work
                return
            node_ids, xs, ys = [], [], []
            for nd in w.nodes:
                node_ids.append(nd.ref)
                if nd.location.valid():
                    xs.append(nd.location.lon)
                    ys.append(nd.location.lat)
                else:
                    xs.append(float("nan"))
                    ys.append(float("nan"))
            pieces, changed = clipper.process(w.id, node_ids, xs, ys, tags)
            if not changed:
                self.writer.add_way(w)  # untouched: copy as is
                return
            for piece_id, piece_nodes, piece_tags in pieces:
                piece = mutable.Way(id=piece_id, version=w.version, visible=True,
                                    changeset=w.changeset, timestamp=w.timestamp,
                                    uid=w.uid, user=w.user, nodes=piece_nodes, tags=piece_tags)
                if piece_id == w.id:
                    self.writer.add_way(piece)
                else:
                    self.pending.append(piece)

        def relation(self, r):
            if self.pending:
                self.flush()
            members = [(m.type, m.ref, m.role) for m in r.members]
            tags = {t.k: t.v for t in r.tags}
            kept = filter_relation(tags, members, clipper.removed, clipper.pieces)
            if kept is None:
                stats["relations_dropped"] += 1
                return
            if kept != members:
                stats["relations_changed"] += 1
                stats["relation_members_removed"] += sum(
                    1 for m in members if m[0] == "w" and m[1] in clipper.removed)
                self.writer.add_relation(r.replace(members=kept, tags=tags))
            else:
                self.writer.add_relation(r)

    # Keep the input header (it carries Geofabrik's replication timestamp).
    reader = osmium.io.Reader(str(input_path), osmium.osm.osm_entity_bits.NOTHING)
    header = reader.header()
    reader.close()

    output_path = Path(output_path)
    if output_path.exists():
        output_path.unlink()
    writer = osmium.SimpleWriter(str(output_path), header=header)
    try:
        handler = Handler(writer)
        handler.apply_file(str(input_path), locations=True, idx="flex_mem")
        handler.flush()
    finally:
        writer.close()
    return stats


def must_fail_targets(routes):
    """{test id: (lat, lon)} for every must-fail target in gate_routes.json."""
    places = routes.get("places", {})
    out = {}
    for t in routes.get("must_fail", []):
        p = t["to"] if isinstance(t["to"], dict) else places[t["to"]]
        out[t["id"]] = (p["lat"], p["lon"])
    return out


def main(argv=None):
    parser = argparse.ArgumentParser(description="Remove no-go and foreign roads from a Georgia OSM extract.")
    parser.add_argument("--config", default=str(DEFAULT_CONFIG))
    parser.add_argument("--gate-routes", default=str(DEFAULT_ROUTES),
                        help="gate_routes.json, for the raw-road distance of each must-fail target")
    parser.add_argument("--input", required=True, help="georgia-latest.osm.pbf")
    parser.add_argument("--output", required=True, help="clipped .osm.pbf to write")
    parser.add_argument("--zones", help="write the areas used as GeoJSON here")
    parser.add_argument("--report", help="write a JSON report here")
    args = parser.parse_args(argv)

    started = time.time()
    config = load_config(args.config)
    zones = build_zones(config)
    if args.zones:
        write_zones(zones, config, args.zones)

    routes = json.loads(Path(args.gate_routes).read_text(encoding="utf-8"))
    probe = RawRoadProbe(must_fail_targets(routes))
    clipper = WayClipper(zones, config, probe=probe)
    rel_stats = run_clip(args.input, args.output, clipper)

    report = {"clip_config_version": config["version"],
              "hard_buffer_m": config["hard_buffer_m"],
              "soft_band_outer_m": config.get("soft_band", {}).get("outer_m"),
              "ways": clipper.stats, "relations": rel_stats,
              "must_fail_targets": probe.report(),
              "sample_way_ids": clipper.samples,
              "seconds": round(time.time() - started, 1)}
    if args.report:
        Path(args.report).write_text(json.dumps(report, indent=2), encoding="utf-8")

    s = clipper.stats
    print(f"clip v{config['version']}: {s['road_ways']} road ways, {s['kept_whole']} kept whole, "
          f"{s['cut']} cut and {s['band_split']} split at the band ({s['extra_pieces']} extra pieces), "
          f"{s['removed']} removed ({s['removed_no_go']} no-go, {s['removed_outside_georgia']} outside "
          f"Georgia, {s['removed_ferry']} ferries), {s['band_destination']} band pieces and "
          f"{s['four_wd_destination']} 4wd ways made destination-only, "
          f"{rel_stats['relations_dropped']} relations dropped, {report['seconds']} s")
    if s["removed"] + s["cut"] == 0:
        # Georgia's extract always has roads crossing the lines. Zero means
        # the polygons did not load or the input is not Georgia.
        print("ERROR: nothing was clipped; refusing to continue", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
