#!/usr/bin/env python3
"""Build the offline geocoder: georgia_geocoder.sqlite from the clipped extract.

It reads the SAME safety-clipped OSM extract the routing tiles are built
from (clip.py's output), so the search never offers a road the router has
lost, and writes one SQLite file with four searchable tables:

  places     cities, towns, villages, hamlets, suburbs, quarters,
             neighbourhoods and named localities
  streets    named highways, ways of one name merged per settlement, with a
             point on the street, its box and its settlement; plus 'virtual'
             streets that exist only as addr:street of addresses
  addresses  addr:housenumber with its street (or addr:place) and settlement
  pois       what drivers need (fuel with CNG/LPG, EV charging, parking, car
             wash, car repair, tyres, car parts, hospital, police, border
             control), everyday places (pharmacies, ATMs, banks, post
             offices, schools, shops, cafes, restaurants ...) and named
             destinations (airports, stations, passes, lakes and reservoirs,
             theatres, museums, cinemas, sights, hotels, resorts, malls ...),
             per config/geocoder.json; a POI whose name lacks its kind's word
             is also searchable as '<name> <word>' (kind_words)

Beside them, without a search index, the table cameras: speed and red-light
cameras and average-speed sections for the navigator's warnings
(scripts/geocoder_cameras.py, config cameras).

Each table has an FTS5 (or FTS4, --fts fts4) index over folded search keys
(scripts/geocoder_fold.py), and each row id is its rank by importance, so
the index returns the most important matches first.

The occupied territories (nogo_zones.geojson: no_go_hard, i.e. the occupied
areas plus 100 m, and the whole Perevi village): places there are KEPT with
occupied=1 (zone 'occupied', or 'buffer' for the 100 m band just outside the
drawn line) so the app can explain why it will not route there; streets,
addresses and POIs there are dropped, and no legal row ever takes an occupied
settlement as its city. An occupied place is labelled only from name:ka
(label_ka, and label_en romanised from it by the national system); name and
name:en there are the de facto authorities' forms and are never labels.
Everything outside Georgia's recognised border is dropped. Rows in the
100-500 m warning band carry band=1. A border_control near the occupation
line is no border crossing (kind line_checkpoint).

The database is an ODbL Derivative Database of OpenStreetMap; its meta table
carries the attribution and licence notice (ODbL 4.2/4.3) and the fold and
config it was built with.

Usage (as in the workflow):
  python scripts/geocoder_build.py --pbf build/valhalla/georgia-clipped.osm.pbf \
      --zones build/nogo_zones.geojson --manifest build/manifest.json \
      --out build/geocoder/georgia_geocoder.sqlite --report build/geocoder/build_report.json

Libraries: stdlib (sqlite3 with FTS5) plus pyosmium (BSD-2) for reading the
PBF; everything but read_pbf() runs without pyosmium, so the unit tests need
nothing installed.
"""

import argparse
import hashlib
import json
import math
import os
import re
import sqlite3
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from geocoder_fold import DEFAULT_SPEC as DEFAULT_FOLD_SPEC, Fold, romanise  # noqa: E402
import geocoder_cameras  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG = ROOT / "config" / "geocoder.json"
SCHEMA_VERSION = 3                              # 3: the cameras table
TABLES = ("places", "streets", "addresses", "pois")
EARTH_R = 6371008.8
M_PER_DEG = math.pi * EARTH_R / 180.0          # metres per degree of latitude
CLIP_PIECE_ID = 4_000_000_000                   # clip.py gives cut pieces ids from here
ODBL_URL = "https://opendatacommons.org/licenses/odbl/1-0/"
COPYRIGHT_URL = "https://www.openstreetmap.org/copyright"
LATIN_WORD = re.compile(r"[A-Za-z]{2,}")         # pois.joined_names: 'East Point' -> 'EastPoint'
NOTICE = ("Contains information from OpenStreetMap, which is made available here under the "
          "Open Database License (ODbL 1.0). © OpenStreetMap contributors. This search database "
          "is a Derivative Database of OpenStreetMap and is itself available under the ODbL 1.0.")


# ---------------------------------------------------------------------------
# Geometry (stdlib, lon/lat plane like the GeoJSON the app checks against)
# ---------------------------------------------------------------------------

def distance_m(lat1, lon1, lat2, lon2):
    p1, p2 = math.radians(lat1), math.radians(lat2)
    a = (math.sin((p2 - p1) / 2) ** 2
         + math.cos(p1) * math.cos(p2) * math.sin(math.radians(lon2 - lon1) / 2) ** 2)
    return 2 * EARTH_R * math.asin(min(1.0, math.sqrt(a)))


def box_distance_m(lat, lon, box):
    """Distance from a point to a (min_lat, min_lon, max_lat, max_lon) box."""
    min_lat, min_lon, max_lat, max_lon = box
    dy = max(min_lat - lat, 0.0, lat - max_lat) * M_PER_DEG
    dx = max(min_lon - lon, 0.0, lon - max_lon) * M_PER_DEG * math.cos(math.radians(lat))
    return math.hypot(dx, dy)


def clean_ring(ring):
    pts = [p for p in ring if p is not None]
    if len(pts) >= 2 and pts[0] == pts[-1]:
        pts = pts[:-1]
    return pts


def ring_area_centroid(ring):
    """Signed area (square degrees) and centroid of one ring of (lon, lat)."""
    pts = clean_ring(ring)
    if len(pts) < 3:
        return 0.0, None
    x0, y0 = pts[0]
    a = cx = cy = 0.0
    for i in range(len(pts)):
        x1, y1 = pts[i][0] - x0, pts[i][1] - y0
        x2, y2 = pts[(i + 1) % len(pts)][0] - x0, pts[(i + 1) % len(pts)][1] - y0
        cross = x1 * y2 - x2 * y1
        a += cross
        cx += (x1 + x2) * cross
        cy += (y1 + y2) * cross
    if abs(a) < 1e-18:
        return 0.0, None
    return a / 2.0, (cx / (3.0 * a) + x0, cy / (3.0 * a) + y0)


class PolygonIndex:
    """Even-odd point-in-polygon over any set of rings (outer rings, holes,
    several polygons), with the edges sorted into latitude bands so a test
    looks at a handful of edges instead of all of them."""

    def __init__(self, rings):
        edges, flat = [], []
        min_x = min_y = math.inf
        max_x = max_y = -math.inf
        for ring in rings:
            pts = clean_ring(ring)
            if len(pts) < 3:
                continue
            for x, y in pts:
                min_x, max_x = min(min_x, x), max(max_x, x)
                min_y, max_y = min(min_y, y), max(max_y, y)
            for (x1, y1), (x2, y2) in zip(pts, pts[1:] + pts[:1]):
                if y1 != y2:
                    edges.append((x1, y1, x2, y2))
                elif x1 != x2:
                    flat.append((x1, y1, x2, y2))   # no use for containment; near_edge needs them
        self.flat = flat
        self.edge_count = len(edges)
        self.empty = not edges
        self.box = (min_y, min_x, max_y, max_x)  # like box_distance_m: lat first
        if self.empty:
            return
        self.min_x, self.min_y, self.max_x, self.max_y = min_x, min_y, max_x, max_y
        n = max(1, min(16384, len(edges) // 2))
        self.n = n
        self.band_h = (max_y - min_y) / n or 1.0
        bands = [[] for _ in range(n)]
        for e in edges:
            lo = int((min(e[1], e[3]) - min_y) / self.band_h)
            hi = int((max(e[1], e[3]) - min_y) / self.band_h)
            for b in range(max(lo, 0), min(hi, n - 1) + 1):
                bands[b].append(e)
        self.bands = [tuple(b) for b in bands]
        area = 0.0
        for ring in rings:
            area += abs(ring_area_centroid(ring)[0])
        self.area = area  # outer and inner rings both add: only used to compare sizes

    def near_edge(self, x, y, dist_m):
        """True when an edge lies within dist_m of the point (lon x, lat y)."""
        if self.empty:
            return False
        d_lat = dist_m / M_PER_DEG
        if y < self.min_y - d_lat or y > self.max_y + d_lat:
            return False
        kx = M_PER_DEG * math.cos(math.radians(y))
        lo = max(int((y - d_lat - self.min_y) / self.band_h), 0)
        hi = min(int((y + d_lat - self.min_y) / self.band_h), self.n - 1)
        for edges in [self.bands[b] for b in range(lo, hi + 1)] + [self.flat]:
            for x1, y1, x2, y2 in edges:
                ax, ay = (x1 - x) * kx, (y1 - y) * M_PER_DEG
                bx, by = (x2 - x) * kx, (y2 - y) * M_PER_DEG
                dx, dy = bx - ax, by - ay
                t = 0.0 if dx == dy == 0 else max(0.0, min(1.0, -(ax * dx + ay * dy) / (dx * dx + dy * dy)))
                if math.hypot(ax + t * dx, ay + t * dy) <= dist_m:
                    return True
        return False

    def contains(self, x, y):
        if self.empty or not (self.min_x <= x <= self.max_x and self.min_y <= y <= self.max_y):
            return False
        b = min(int((y - self.min_y) / self.band_h), self.n - 1)
        inside = False
        for x1, y1, x2, y2 in self.bands[b]:
            if (y1 > y) != (y2 > y) and x < x1 + (y - y1) * (x2 - x1) / (y2 - y1):
                inside = not inside
        return inside


def representative_point(rings, outer_count=None):
    """(lon, lat) inside the area: the area-weighted centroid of the outer
    rings if it lies inside, else the middle of the widest inside stretch of
    the horizontal line through it."""
    outers = rings if outer_count is None else rings[:outer_count]
    total = sx = sy = 0.0
    for ring in outers:
        a, c = ring_area_centroid(ring)
        if c is not None:
            total += abs(a)
            sx += c[0] * abs(a)
            sy += c[1] * abs(a)
    pts = [p for r in rings for p in clean_ring(r)]
    if not pts:
        return None
    if total <= 0:
        return (sum(p[0] for p in pts) / len(pts), sum(p[1] for p in pts) / len(pts))
    cx, cy = sx / total, sy / total
    poly = PolygonIndex(rings)
    if poly.contains(cx, cy):
        return (cx, cy)
    xs = []
    for ring in rings:
        r = clean_ring(ring)
        for (x1, y1), (x2, y2) in zip(r, r[1:] + r[:1]):
            if (y1 > cy) != (y2 > cy):
                xs.append(x1 + (cy - y1) * (x2 - x1) / (y2 - y1))
    xs.sort()
    best = None
    for a, b in zip(xs[0::2], xs[1::2]):
        if best is None or b - a > best[1] - best[0]:
            best = (a, b)
    if best is not None:
        return ((best[0] + best[1]) / 2.0, cy)
    return pts[0]


def assemble_rings(ways):
    """Join member ways (node ids, coordinates) end to end into closed
    rings. Returns (rings, number of ways left unjoined)."""
    rings, open_ = [], []
    for refs, coords in ways:
        if len(refs) < 2:
            continue
        if refs[0] == refs[-1] and len(refs) >= 4:
            rings.append(list(coords))
        else:
            open_.append((list(refs), list(coords)))
    left = 0
    while open_:
        refs, coords = open_.pop()
        grown = True
        while refs[0] != refs[-1] and grown:
            grown = False
            for i, (r2, c2) in enumerate(open_):
                if r2[0] == refs[-1]:
                    refs, coords = refs + r2[1:], coords + c2[1:]
                elif r2[-1] == refs[-1]:
                    refs, coords = refs + r2[-2::-1], coords + c2[-2::-1]
                elif r2[-1] == refs[0]:
                    refs, coords = r2[:-1] + refs, c2[:-1] + coords
                elif r2[0] == refs[0]:
                    refs, coords = r2[:0:-1] + refs, c2[:0:-1] + coords
                else:
                    continue
                open_.pop(i)
                grown = True
                break
        if refs[0] == refs[-1] and len(refs) >= 4:
            rings.append(coords)
        else:
            left += 1
    return rings, left


def geojson_rings(geometry):
    if geometry["type"] == "Polygon":
        polys = [geometry["coordinates"]]
    elif geometry["type"] == "MultiPolygon":
        polys = geometry["coordinates"]
    else:
        raise ValueError(f"unexpected geometry type {geometry['type']}")
    return [[tuple(p[:2]) for p in ring] for poly in polys for ring in poly]


class Zones:
    """nogo_zones.geojson as point tests. zone() returns 'outside' (not in
    Georgia), 'occupied' (in the occupied area as drawn, Perevi included),
    'buffer' (in no_go_hard but not in the drawn area), 'band' (in the
    100-500 m warning band) or None."""

    def __init__(self, georgia, hard, band_outer=None, occupied=None):
        self.georgia = georgia
        self.hard = hard
        self.band_outer = band_outer
        self.occupied = occupied

    @classmethod
    def from_geojson(cls, path):
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        polys = {f["properties"]["name"]: PolygonIndex(geojson_rings(f["geometry"]))
                 for f in data["features"]}
        for required in ("georgia", "no_go_hard"):
            if required not in polys or polys[required].empty:
                raise ValueError(f"{path}: no '{required}' polygon")
        return cls(polys["georgia"], polys["no_go_hard"], polys.get("soft_band_outer"),
                   polys.get("occupied"))

    def near_hard(self, lat, lon, dist_m):
        """Within dist_m of the no_go_hard line (or inside it)."""
        return self.hard.contains(lon, lat) or self.hard.near_edge(lon, lat, dist_m)

    def zone(self, lat, lon):
        if not self.georgia.contains(lon, lat):
            return "outside"
        if self.hard.contains(lon, lat):
            if self.occupied is not None and not self.occupied.contains(lon, lat):
                return "buffer"
            return "occupied"
        if self.band_outer is not None and self.band_outer.contains(lon, lat):
            return "band"
        return None


class PointGrid:
    """Points in square cells for 'what is near here' questions."""

    def __init__(self, cell_deg):
        self.cell = cell_deg
        self.cells = defaultdict(list)

    def key(self, lat, lon):
        return (int(math.floor(lat / self.cell)), int(math.floor(lon / self.cell)))

    def add(self, lat, lon, item):
        self.cells[self.key(lat, lon)].append((lat, lon, item))

    def near(self, lat, lon, radius_m):
        span_lat = radius_m / M_PER_DEG
        span_lon = span_lat / max(0.2, math.cos(math.radians(lat)))
        r0, c0 = self.key(lat - span_lat, lon - span_lon)
        r1, c1 = self.key(lat + span_lat, lon + span_lon)
        for r in range(r0, r1 + 1):
            for c in range(c0, c1 + 1):
                yield from self.cells.get((r, c), ())


class AreaGrid:
    """Polygons found by the cells their boxes cover."""

    def __init__(self, cell_deg=0.05):
        self.cell = cell_deg
        self.cells = defaultdict(list)

    def add(self, poly, item):
        if poly.empty:
            return
        min_lat, min_lon, max_lat, max_lon = poly.box
        for r in range(int(math.floor(min_lat / self.cell)), int(math.floor(max_lat / self.cell)) + 1):
            for c in range(int(math.floor(min_lon / self.cell)), int(math.floor(max_lon / self.cell)) + 1):
                self.cells[(r, c)].append((poly, item))

    def containing(self, lat, lon):
        key = (int(math.floor(lat / self.cell)), int(math.floor(lon / self.cell)))
        return [(poly, item) for poly, item in self.cells.get(key, ()) if poly.contains(lon, lat)]


class UnionFind:
    def __init__(self, n):
        self.parent = list(range(n))

    def find(self, i):
        while self.parent[i] != i:
            self.parent[i] = self.parent[self.parent[i]]
            i = self.parent[i]
        return i

    def union(self, a, b):
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.parent[max(ra, rb)] = min(ra, rb)

    def groups(self):
        out = defaultdict(list)
        for i in range(len(self.parent)):
            out[self.find(i)].append(i)
        return list(out.values())


def cluster_points(points, radius_m):
    """Single-linkage clusters of [(lat, lon), ...] (each point joins any
    point within about radius_m, via grid cells). Lists of indices."""
    uf = UnionFind(len(points))
    cell_lat = radius_m / M_PER_DEG
    owner = {}
    for i, (lat, lon) in enumerate(points):
        cell_lon = cell_lat / max(0.2, math.cos(math.radians(lat)))
        r, c = int(math.floor(lat / cell_lat)), int(math.floor(lon / cell_lon))
        for dr in (-1, 0, 1):
            for dc in (-1, 0, 1):
                j = owner.get((r + dr, c + dc))
                if j is not None and distance_m(lat, lon, *points[j]) <= 2 * radius_m:
                    uf.union(i, j)
        owner.setdefault((r, c), i)
    return uf.groups()


# ---------------------------------------------------------------------------
# Configuration and tags
# ---------------------------------------------------------------------------

def load_config(path=DEFAULT_CONFIG):
    path = Path(path)
    config = json.loads(path.read_text(encoding="utf-8"))
    problems = []
    if not isinstance(config.get("version"), int) or config["version"] < 1:
        problems.append("'version' must be a positive integer")
    kinds = config["places"]["kinds"]
    for key in ("settlement_kinds", "district_kinds", "admin_area_kinds"):
        for kind in config["places"][key]:
            if kind not in kinds:
                problems.append(f"places.{key}: unknown kind {kind!r}")
    seen = set()
    for rule in config["pois"]["rules"]:
        if not rule.get("match"):
            problems.append(f"poi rule {rule.get('kind')!r} has no match")
        if rule.get("group") not in ("driver", "destination"):
            problems.append(f"poi rule {rule.get('kind')!r}: group must be driver or destination")
        for name, cond in rule.get("attrs", {}).items():
            conds = cond if isinstance(cond, list) else [cond]
            if not conds or not all(isinstance(c, dict) and c for c in conds):
                problems.append(f"poi rule {rule.get('kind')!r}: attr {name!r} needs tag conditions")
        words = rule.get("kind_words", [])
        if not isinstance(words, list) or not all(isinstance(w, str) and w.strip() for w in words):
            problems.append(f"poi rule {rule.get('kind')!r}: kind_words must be a list of words")
        seen.add(rule["kind"])
    for name, cat in config["categories"].items():
        if name.startswith("_"):
            continue
        if cat["kind"] not in seen:
            problems.append(f"category {name!r} points at unknown POI kind {cat['kind']!r}")
    cams = config.get("cameras")
    if not isinstance(cams, dict):
        problems.append("'cameras' is missing")
    else:
        for key in ("nodes", "red_light", "average_speed"):
            conds = cams.get(key)
            if not isinstance(conds, list) or not conds or not all(isinstance(c, dict) and c for c in conds):
                problems.append(f"cameras.{key} must be a list of tag conditions")
        if not set((cams.get("relations") or {}).values()) <= set(geocoder_cameras.KINDS):
            problems.append(f"cameras.relations: kinds must be among {geocoder_cameras.KINDS}")
        if cams.get("direction_degrees") not in ("facing", "travel"):
            problems.append("cameras.direction_degrees must be 'facing' or 'travel'")
        for key in ("snap_m", "parallel_m", "parallel_deg", "dedup_m", "heading_off_road_max_deg"):
            if not isinstance(cams.get(key), (int, float)) or cams[key] < 0:
                problems.append(f"cameras.{key} must be a number >= 0")
    if problems:
        raise ValueError("bad geocoder config: " + "; ".join(problems))
    return config


def tag_values(value):
    return [v.strip() for v in (value or "").split(";") if v.strip()]


def match_tags(tags, condition):
    """condition: {key: [values]}; '*' = any value; '!key' = key absent. A
    list of such conditions matches when any of them does."""
    if isinstance(condition, list):
        return any(match_tags(tags, c) for c in condition)
    for key, values in condition.items():
        if key.startswith("!"):
            if key[1:] in tags:
                return False
            continue
        value = tags.get(key)
        if value is None:
            return False
        if "*" not in values and not any(v in values for v in tag_values(value)):
            return False
    return True


class Names:
    """The names of one object: the four main columns and the rest."""

    def __init__(self, tags, config):
        cfg = config["name_tags"]
        self.main = {k.replace(":", "_"): (tags.get(k) or "").strip() or None for k in cfg["main"]}
        seen = {v for v in self.main.values() if v}
        self.alt = []
        for key in cfg["other"]:
            for v in tag_values(tags.get(key)):
                if v not in seen:
                    seen.add(v)
                    self.alt.append(v)
        self.brand = []
        for key in cfg["brand"]:
            for v in tag_values(tags.get(key)):
                if v not in self.brand:
                    self.brand.append(v)

    @property
    def any(self):
        return any(self.main.values()) or bool(self.alt)

    @property
    def display(self):
        m = self.main
        return m["name"] or m["name_ka"] or m["name_en"] or m["name_ru"] or (self.alt[0] if self.alt else None)

    def all(self):
        return [v for v in self.main.values() if v] + self.alt


def has_georgian(text):
    return any("\u10a0" <= ch <= "\u10ff" or "\u1c90" <= ch <= "\u1cbf" or "\u2d00" <= ch <= "\u2d2f"
               for ch in text or "")


def labels(main, fold_spec, occupied=False, fallback=None):
    """(label_ka, label_en): what the app shows. An occupied place is
    labelled only from name:ka (label_en romanised from it, never name:en,
    which there is the de facto authorities' form); (None, None) without
    name:ka. Elsewhere label_ka is name:ka, else a Georgian name, else any
    name; label_en is name:en, else the romanised Georgian label."""
    ka = main.get("name_ka")
    if occupied:
        return (ka, romanise(ka, fold_spec)) if ka else (None, None)
    if not ka and has_georgian(main.get("name")):
        ka = main["name"]
    en = main.get("name_en") or (romanise(ka, fold_spec) if ka else None)
    ka = ka or main.get("name") or main.get("name_en") or main.get("name_ru") or fallback
    return ka, en or ka


# ---------------------------------------------------------------------------
# Collecting objects (format independent; read_pbf feeds it)
# ---------------------------------------------------------------------------

INTERESTING_KEYS = ("place", "amenity", "shop", "craft", "healthcare", "barrier", "aeroway",
                    "railway", "public_transport", "mountain_pass", "landuse", "natural",
                    "waterway", "tourism", "historic", "leisure", "office", "boundary",
                    "addr:housenumber")
CAR_CLASSES = {"motorway", "trunk", "primary", "secondary", "tertiary", "unclassified",
               "residential", "living_street", "service", "road", "track", "motorway_link",
               "trunk_link", "primary_link", "secondary_link", "tertiary_link"}


class Collector:
    def __init__(self, config):
        self.config = config
        self.place_kinds = config["places"]["kinds"]
        self.admin_levels = {str(v) for v in config["places"]["admin_levels"]}
        self.street_classes = config["streets"]["classes"]
        self.rules = config["pois"]["rules"]
        self.relations = {}           # id -> (tags, [(way id, role)], label node ids)
        self.member_ways = set()
        self.member_geom = {}         # way id -> (refs, coords)
        self.places = []              # dicts: osm, kind, lat, lon, tags, rings
        self.place_areas = []         # place=* areas: osm, kind, tags, rings
        self.admin_areas = []         # osm, tags, rings, labels
        self.pois = []                # osm, rule, lat, lon, tags
        self.addresses = []           # osm, lat, lon, tags, building
        self.street_ways = []         # id, tags, cls, coords
        self.pass_nodes = {}          # mountain pass node id -> index in pois
        self.cams = geocoder_cameras.CameraCollector(config["cameras"])
        self.stats = Counter()

    # which objects matter

    def poi_rule(self, tags):
        for rule in self.rules:
            if any(match_tags(tags, cond) for cond in rule["match"]):
                if rule.get("named") and not (Names(tags, self.config).any or tags.get("brand")):
                    return None
                return rule
        return None

    def wants_relation(self, tags):
        if self.cams.wants_relation(tags):
            return True
        if tags.get("type") not in ("multipolygon", "boundary"):
            return False
        if tags.get("boundary") == "administrative":
            return tags.get("admin_level") in self.admin_levels and bool(tags.get("name"))
        return (tags.get("place") in self.place_kinds or "addr:housenumber" in tags
                or self.poi_rule(tags) is not None)

    def relation(self, rel_id, tags, members):
        if self.cams.wants_relation(tags):
            self.cams.relation(rel_id, tags, members)
            return
        ways = [(ref, role) for typ, ref, role in members if typ == "w" and role in ("outer", "inner", "")]
        labels = [ref for typ, ref, role in members if typ == "n" and role in ("label", "admin_centre")]
        if not ways:
            return
        self.relations[rel_id] = (tags, ways, labels)
        self.member_ways.update(ref for ref, _ in ways)

    def way_matters(self, way_id, has_key):
        """Cheap pre-check before the tags are copied (has_key: callable)."""
        return way_id in self.member_ways or "highway" in has_key or any(k in has_key for k in INTERESTING_KEYS)

    # objects

    def node(self, osm_id, tags, lat, lon):
        self.cams.node(osm_id, tags, lat, lon)
        self._object(f"n{osm_id}", tags, point=(lon, lat), node_id=osm_id)

    def way(self, osm_id, tags, refs, coords):
        self.cams.way(osm_id, tags, refs, coords)
        if osm_id in self.member_ways:
            self.member_geom[osm_id] = (list(refs), list(coords))
        if not tags:
            return
        highway = tags.get("highway")
        if highway:
            cls = highway
            if highway == "construction":
                cls = tags.get("construction") if tags.get("construction") in self.street_classes else "construction"
            if cls in self.street_classes and Names(tags, self.config).any:
                pts = [c for c in coords if c is not None]
                if pts:
                    self.street_ways.append({"id": osm_id, "tags": tags, "cls": cls, "coords": pts})
            if highway in CAR_CLASSES and self.pass_nodes:
                for ref in refs:
                    idx = self.pass_nodes.get(ref)
                    if idx is not None:
                        self.pois[idx]["on_road"] = True
        closed = len(refs) >= 4 and refs[0] == refs[-1]
        pts = [c for c in coords if c is not None]
        if not pts:
            return
        if closed:
            self._object(f"w{osm_id}", tags, rings=[pts])
        else:
            mid = pts[len(pts) // 2]
            self._object(f"w{osm_id}", tags, point=mid, open_way=True)

    def finish_relations(self):
        for rel_id, (tags, ways, labels) in self.relations.items():
            outer, inner = [], []
            missing = 0
            for ref, role in ways:
                geom = self.member_geom.get(ref)
                if geom is None:
                    missing += 1
                    continue
                (inner if role == "inner" else outer).append(geom)
            rings_out, left_o = assemble_rings(outer)
            rings_in, left_i = assemble_rings(inner)
            if missing or left_o or left_i:
                self.stats["relations_incomplete"] += 1
            if not rings_out:
                self.stats["relations_without_area"] += 1
                continue
            self._object(f"r{rel_id}", tags, rings=rings_out + rings_in,
                         outer_count=len(rings_out), labels=labels)
        self.member_geom.clear()

    def _object(self, osm, tags, point=None, rings=None, outer_count=None, labels=(),
                node_id=None, open_way=False):
        rep = [point]

        def where():
            if rep[0] is None:
                rep[0] = representative_point(rings, outer_count)
            return rep[0]

        kind = tags.get("place")
        if kind in self.place_kinds and Names(tags, self.config).any:
            if rings is not None:
                self.place_areas.append({"osm": osm, "kind": kind, "tags": tags, "rings": rings})
            elif not open_way:
                lon, lat = where()
                self.places.append({"osm": osm, "kind": kind, "lat": lat, "lon": lon, "tags": tags})
        if (rings is not None and tags.get("boundary") == "administrative"
                and tags.get("admin_level") in self.admin_levels and tags.get("name")):
            self.admin_areas.append({"osm": osm, "tags": tags, "rings": rings, "labels": list(labels)})
        rule = self.poi_rule(tags)
        if rule is not None:
            p = where()
            if p is not None:
                self.pois.append({"osm": osm, "rule": rule, "lat": p[1], "lon": p[0], "tags": tags,
                                  "on_road": False})
                if node_id is not None and rule["kind"] == "mountain_pass":
                    self.pass_nodes[node_id] = len(self.pois) - 1
        if tags.get("addr:housenumber") and (tags.get("addr:street") or tags.get("addr:place")):
            p = where()
            if p is not None:
                self.addresses.append({"osm": osm, "lat": p[1], "lon": p[0], "tags": tags,
                                       "building": rings is not None})


def read_pbf(path, collector):
    """Two passes with pyosmium: relations first (to know which ways are
    area members), then nodes and ways with their locations."""
    import osmium

    started = time.time()
    for rel in osmium.FileProcessor(str(path), osmium.osm.RELATION):
        tags = {t.k: t.v for t in rel.tags}
        if collector.wants_relation(tags):
            collector.relation(rel.id, tags, [(m.type, m.ref, m.role) for m in rel.members])
    collector.stats["seconds_relations"] = round(time.time() - started, 1)

    fp = (osmium.FileProcessor(str(path), osmium.osm.NODE | osmium.osm.WAY)
          .with_locations()
          .with_filter(osmium.filter.EmptyTagFilter().enable_for(osmium.osm.NODE)))
    for obj in fp:
        if obj.is_node():
            if obj.location.valid():
                collector.node(obj.id, {t.k: t.v for t in obj.tags}, obj.location.lat, obj.location.lon)
            continue
        if not collector.way_matters(obj.id, obj.tags):
            continue
        refs, coords = [], []
        for n in obj.nodes:
            refs.append(n.ref)
            coords.append((n.location.lon, n.location.lat) if n.location.valid() else None)
        collector.way(obj.id, {t.k: t.v for t in obj.tags}, refs, coords)
    collector.finish_relations()
    missing = collector.cams.missing_members()
    if missing:
        # An enforcement relation's untagged device node on no way: the pass
        # above never saw it (untagged nodes are filtered out).
        fp = (osmium.FileProcessor(str(path), osmium.osm.NODE)
              .with_filter(osmium.filter.IdFilter(missing)))
        for obj in fp:
            if obj.location.valid():
                collector.cams.coords[obj.id] = (obj.location.lon, obj.location.lat)
    collector.stats["seconds_read"] = round(time.time() - started, 1)


def pbf_header_timestamp(path):
    import osmium
    reader = osmium.io.Reader(str(path), osmium.osm.osm_entity_bits.NOTHING)
    try:
        return reader.header().get("osmosis_replication_timestamp")
    finally:
        reader.close()


# ---------------------------------------------------------------------------
# From collected objects to table rows
# ---------------------------------------------------------------------------

def clamp01(v):
    return max(0.0, min(1.0, v))


class Builder:
    def __init__(self, config, fold, zones, collector):
        self.config = config
        self.fold = fold
        self.zones = zones
        self.c = collector
        self.stats = Counter()
        self.samples = defaultdict(list)
        self._keys_cache = {}
        self._index_cache = {}

    def _sample(self, key, value):
        if len(self.samples[key]) < 20:
            self.samples[key].append(value)

    def words(self, text):
        """fold.keys(text), remembered: the same names come back thousands of times."""
        text = text or ""
        got = self._keys_cache.get(text)
        if got is None:
            got = self._keys_cache[text] = tuple(self.fold.keys(text))
        return got

    def name_key(self, text):
        return " ".join(self.words(text))

    def keys_of(self, texts):
        seen, out = set(), []
        for text in texts:
            got = self._index_cache.get(text)
            if got is None:
                got = self._index_cache[text] = tuple(self.fold.index_keys(text))
            for k in got:
                if k not in seen:
                    seen.add(k)
                    out.append(k)
        return out

    # -- places -------------------------------------------------------------

    def build_places(self):
        cfg = self.config["places"]
        kinds = cfg["kinds"]
        places = []
        for p in self.c.places:
            places.append(dict(p, rings=None))
        # A place=* area: its polygon belongs to the place node of the same
        # name inside it; without one, the area is the place.
        by_name = defaultdict(list)
        for i, p in enumerate(places):
            by_name[self.name_key(Names(p["tags"], self.config).display)].append(i)
        for area in self.c.place_areas:
            poly = PolygonIndex(area["rings"])
            key = self.name_key(Names(area["tags"], self.config).display)
            linked = [i for i in by_name.get(key, ()) if places[i]["rings"] is None
                      and poly.contains(places[i]["lon"], places[i]["lat"])]
            if linked:
                places[linked[0]]["rings"] = area["rings"]
                self.stats["place_areas_linked"] += 1
                continue
            p = representative_point(area["rings"])
            if p is None:
                continue
            places.append({"osm": area["osm"], "kind": area["kind"], "lat": p[1], "lon": p[0],
                           "tags": area["tags"], "rings": area["rings"]})
            self.stats["place_areas_as_places"] += 1

        rows = []
        hide_unnamed = cfg.get("occupied_without_name_ka", "hide") == "hide"
        for p in places:
            zone = self.zones.zone(p["lat"], p["lon"])
            if zone == "outside":
                self.stats["places_outside_georgia"] += 1
                continue
            names = Names(p["tags"], self.config)
            occupied = zone in ("occupied", "buffer")
            if occupied and not names.main["name_ka"]:
                if hide_unnamed:
                    self.stats["places_occupied_without_name_ka_hidden"] += 1
                    self._sample("places_occupied_without_name_ka", names.display)
                    continue
                if not names.main["name_ru"]:
                    self.stats["places_occupied_without_any_label"] += 1
                    continue
            pop = 0
            try:
                pop = int(float((p["tags"].get("population") or "0").replace(",", "").replace(" ", "")))
            except ValueError:
                pass
            if occupied:
                label = labels(names.main, self.fold.spec, occupied=True)
                if label[0] is None:   # occupied_without_name_ka: name_ru
                    label = (names.main["name_ru"], names.main["name_ru"])
            else:
                label = labels(names.main, self.fold.spec)
            rows.append({"osm": p["osm"], "kind": p["kind"], "lat": p["lat"], "lon": p["lon"],
                         "names": names, "population": pop or None, "tags": p["tags"],
                         "rings": p["rings"], "zone": zone, "occupied": 1 if occupied else 0,
                         "band": 1 if zone == "band" else 0, "parent": None, "poly": None,
                         "label": label})
            if occupied:
                self.stats["places_occupied"] += 1
                self._sample("places_occupied", f"{label[0]} ({names.display})")
        self.places = rows
        self.apply_aliases()

        # Settlement areas: place polygons, and admin boundaries named like a
        # city or town that lies inside them (or is their label).
        settlement = set(cfg["settlement_kinds"])
        by_osm = {r["osm"]: i for i, r in enumerate(rows)}
        by_key = defaultdict(list)
        self.settlement_names = defaultdict(list)   # any folded name -> settlement indices
        for i, r in enumerate(rows):
            if r["kind"] in settlement:
                by_key[self.name_key(r["names"].display)].append(i)
                for key in {self.name_key(v) for v in r["names"].all()}:
                    self.settlement_names[key].append(i)
                if r["rings"]:
                    r["poly"] = PolygonIndex(r["rings"])
        admin_kinds = set(cfg["admin_area_kinds"])
        for area in self.c.admin_areas:
            poly = PolygonIndex(area["rings"])
            if poly.empty:
                continue
            key = self.name_key(area["tags"].get("name"))
            candidates = [by_osm.get(f"n{ref}") for ref in area["labels"]]
            candidates = [i for i in candidates if i is not None and rows[i]["kind"] in admin_kinds
                          and self.name_key(rows[i]["names"].display) == key]
            if not candidates:
                candidates = [i for i in by_key.get(key, ()) if rows[i]["kind"] in admin_kinds
                              and poly.contains(rows[i]["lon"], rows[i]["lat"])]
            if not candidates:
                continue
            i = candidates[0]
            self.stats["admin_areas_matched"] += 1
            if rows[i]["poly"] is None or poly.area > rows[i]["poly"].area:
                rows[i]["poly"] = poly   # the larger of the place area and the admin area
        # Two sets of settlement grids: every settlement (for rows inside
        # no_go_hard), and legal ones only (for everything else), so a legal
        # street near the line never belongs to an occupied town.
        max_reach = max(k["reach_km"] for k in kinds.values()) * 1000.0
        self.max_reach_m = max_reach
        self.area_grid_all, self.area_grid = AreaGrid(), AreaGrid()
        self.point_grid_all = PointGrid(cell_deg=max(0.02, max_reach / M_PER_DEG / 2))
        self.point_grid = PointGrid(cell_deg=max(0.02, max_reach / M_PER_DEG / 2))
        for i, r in enumerate(rows):
            if r["kind"] not in settlement:
                continue
            if r["poly"] is not None:
                self.area_grid_all.add(r["poly"], i)
                if not r["occupied"]:
                    self.area_grid.add(r["poly"], i)
            if kinds[r["kind"]]["reach_km"] > 0:
                self.point_grid_all.add(r["lat"], r["lon"], i)
                if not r["occupied"]:
                    self.point_grid.add(r["lat"], r["lon"], i)

        # Districts get the city or town they lie in (an occupied district may
        # get an occupied one; a legal district never does).
        district = set(cfg["district_kinds"])
        for r in rows:
            if r["kind"] in district:
                r["parent"] = self.settlement_of(r["lat"], r["lon"], kinds_allowed=("city", "town"),
                                                 occupied=bool(r["occupied"]))

        # Importance.
        full = math.log10(cfg["population_full"] + 1)
        for r in rows:
            imp = kinds[r["kind"]]["base"]
            if r["population"]:
                imp += cfg["population_weight"] * clamp01(math.log10(r["population"] + 1) / full)
            imp += cfg["capital_bonus"].get(r["tags"].get("capital", ""), 0.0)
            if r["tags"].get("wikidata"):
                imp += cfg["wikidata_bonus"]
            r["importance"] = imp
        for r in rows:
            if r["parent"] is not None:
                r["importance"] += cfg["parent_weight"] * rows[r["parent"]]["importance"]
        for r in rows:
            r["importance"] = round(clamp01(r["importance"]), 4)
        self.stats["places"] = len(rows)

    def settlement_of(self, lat, lon, kinds_allowed=None, occupied=False):
        """Index of the settlement a point belongs to: the smallest
        settlement area around it, else the nearest settlement within its
        reach (distance divided by reach, lowest wins). Occupied
        settlements count only for a row that is itself occupied."""
        area_grid = self.area_grid_all if occupied else self.area_grid
        point_grid = self.point_grid_all if occupied else self.point_grid
        best = None
        for poly, i in area_grid.containing(lat, lon):
            if kinds_allowed and self.places[i]["kind"] not in kinds_allowed:
                continue
            if best is None or poly.area < best[0]:
                best = (poly.area, i)
        if best is not None:
            return best[1]
        kinds = self.config["places"]["kinds"]
        pick = None
        for plat, plon, i in point_grid.near(lat, lon, self.max_reach_m):
            kind = self.places[i]["kind"]
            if kinds_allowed and kind not in kinds_allowed:
                continue
            reach = kinds[kind]["reach_km"] * 1000.0
            d = distance_m(lat, lon, plat, plon)
            if d <= reach and (pick is None or d / reach < pick[0]):
                pick = (d / reach, i)
        return pick[1] if pick else None

    def city_of(self, lat, lon, addr_city=None):
        """The settlement of a legal object: its addr:city when a legal
        settlement of that name lies within 30 km, else settlement_of()."""
        if addr_city:
            best = None
            for i in self.settlement_names.get(self.name_key(addr_city), ()):
                if self.places[i]["occupied"]:
                    continue
                d = distance_m(lat, lon, self.places[i]["lat"], self.places[i]["lon"])
                if d <= 30_000 and (best is None or d < best[0]):
                    best = (d, i)
            if best is not None:
                return best[1]
        return self.settlement_of(lat, lon)

    def apply_aliases(self):
        """config 'aliases': extra search names for well-known places."""
        entries = (self.config.get("aliases") or {}).get("entries", [])
        for entry in entries:
            kinds = set(entry.get("kinds") or [])
            hit = 0
            for r in self.places:
                m = r["names"].main
                if entry["name_ka"] not in (m["name_ka"], m["name"]) or (kinds and r["kind"] not in kinds):
                    continue
                have = set(r["names"].all())
                for name in entry["names"]:
                    if name not in have:
                        r["names"].alt.append(name)
                        have.add(name)
                hit += 1
            self.stats["aliases_applied" if hit else "aliases_unmatched"] += 1
            if not hit:
                self._sample("aliases_unmatched", entry["name_ka"])

    # -- streets ------------------------------------------------------------

    def build_streets(self):
        cfg = self.config["streets"]
        classes = cfg["classes"]
        groups = defaultdict(list)
        for w in self.c.street_ways:
            names = Names(w["tags"], self.config)
            w["names"] = names
            groups[self.name_key(names.display)].append(w)
        clusters = []
        for key, ways in groups.items():
            if not key:
                continue
            uf = UnionFind(len(ways))
            cell_lat = cfg["cluster_m"] / M_PER_DEG
            owner = {}
            for j, w in enumerate(ways):
                cells = set()
                for lon, lat in w["coords"]:
                    cell_lon = cell_lat / max(0.2, math.cos(math.radians(lat)))
                    cells.add((int(math.floor(lat / cell_lat)), int(math.floor(lon / cell_lon))))
                for r, c in cells:
                    for dr in (-1, 0, 1):
                        for dc in (-1, 0, 1):
                            k = owner.get((r + dr, c + dc))
                            if k is not None:
                                uf.union(j, k)
                for cell in cells:
                    owner.setdefault(cell, j)
            for members in uf.groups():
                clusters.append((key, [ways[j] for j in members]))

        entities = []
        for key, ways in clusters:
            e = self._street_entity(key, ways)
            if e is not None:
                entities.append(e)
        # Merge pieces of one street in one settlement that the first pass
        # kept apart (a gap in the mapping, a square in between).
        merged = []
        by_key_city = defaultdict(list)
        for e in entities:
            by_key_city[(e["key"], e["city"])].append(e)
        for (key, city), group in by_key_city.items():
            if len(group) == 1 or city is None:
                merged.extend(group)
                continue
            uf = UnionFind(len(group))
            for a in range(len(group)):
                for b in range(a + 1, len(group)):
                    if self._box_gap_m(group[a]["box"], group[b]["box"]) <= cfg["merge_m"]:
                        uf.union(a, b)
            for members in uf.groups():
                if len(members) == 1:
                    merged.append(group[members[0]])
                else:
                    ways = [w for m in members for w in group[m]["ways"]]
                    e = self._street_entity(key, ways)
                    if e is not None:
                        merged.append(e)
                        self.stats["streets_merged"] += len(members) - 1
        rows = []
        for e in merged:
            if e["zone"] in ("outside", "occupied", "buffer"):
                self.stats[f"streets_dropped_{e['zone']}"] += 1
                self._sample("streets_dropped", e["names"].display)
                continue
            city = e["city"]
            city_imp = self.places[city]["importance"] if city is not None else 0.0
            full = math.log10(cfg["length_full_m"])
            imp = (cfg["class_weight"] * classes.get(e["kind"], 0.3)
                   + cfg["length_weight"] * clamp01(math.log10(max(e["length_m"], 1.0)) / full)
                   + cfg["city_weight"] * city_imp)
            e["importance"] = round(clamp01(imp), 4)
            e["virtual"] = 0
            rows.append(e)
        self.streets = rows
        self.stats["streets"] = len(rows)

    @staticmethod
    def _box_gap_m(a, b):
        """Distance between two (min_lat, min_lon, max_lat, max_lon) boxes."""
        lat = (a[0] + a[2] + b[0] + b[2]) / 4
        dy = max(b[0] - a[2], a[0] - b[2], 0.0) * M_PER_DEG
        dx = max(b[1] - a[3], a[1] - b[3], 0.0) * M_PER_DEG * math.cos(math.radians(lat))
        return math.hypot(dx, dy)

    def _street_entity(self, key, ways):
        classes = self.config["streets"]["classes"]
        pts = [p for w in ways for p in w["coords"]]
        if not pts:
            return None
        mean_lat = sum(p[1] for p in pts) / len(pts)
        mean_lon = sum(p[0] for p in pts) / len(pts)
        k = math.cos(math.radians(mean_lat)) ** 2
        lon, lat = min(pts, key=lambda p: (p[1] - mean_lat) ** 2 + k * (p[0] - mean_lon) ** 2)
        length = 0.0
        weights = defaultdict(Counter)
        alt = []
        for w in ways:
            wl = sum(distance_m(a[1], a[0], b[1], b[0]) for a, b in zip(w["coords"], w["coords"][1:]))
            length += wl
            for col, value in w["names"].main.items():
                if value:
                    weights[col][value] += wl + 1.0
            for v in w["names"].alt:
                if v not in alt:
                    alt.append(v)
        main = {col: (weights[col].most_common(1)[0][0] if weights[col] else None)
                for col in ("name", "name_ka", "name_en", "name_ru")}
        extra = [v for col in weights for v in weights[col] if v != main[col]]
        alt = [v for v in extra + alt if v not in main.values()]
        names = _FixedNames(main, list(dict.fromkeys(alt)))
        kind = max((w["cls"] for w in ways), key=lambda c: classes.get(c, 0))
        ids = sorted(w["id"] for w in ways)
        real = [i for i in ids if i < CLIP_PIECE_ID]
        box = (min(p[1] for p in pts), min(p[0] for p in pts), max(p[1] for p in pts), max(p[0] for p in pts))
        return {"key": key, "ways": ways, "osm": f"w{(real or ids)[0]}", "kind": kind,
                "lat": lat, "lon": lon, "box": box, "names": names, "length_m": length,
                "zone": self.zones.zone(lat, lon), "city": self.settlement_of(lat, lon)}

    # -- addresses ----------------------------------------------------------

    def build_addresses(self):
        cfg = self.config["addresses"]
        fold = self.fold
        # Street lookup: whole folded name, and single words.
        by_name = defaultdict(list)
        by_word = defaultdict(set)
        for i, s in enumerate(self.streets):
            words = set()
            for v in s["names"].all():
                by_name[self.name_key(v)].append(i)
                words.update(k for k in self.words(v) if k not in fold.type_words)
            s["words"] = words
            for w in words:
                by_word[w].add(i)

        kept = []
        for a in self.c.addresses:
            zone = self.zones.zone(a["lat"], a["lon"])
            if zone in ("outside", "occupied", "buffer"):
                self.stats[f"addresses_dropped_{zone}"] += 1
                continue
            a["zone"] = zone
            kept.append(a)

        virtual_groups = defaultdict(list)
        for a in kept:
            tags = a["tags"]
            a["city"] = self.city_of(a["lat"], a["lon"], tags.get("addr:city"))
            a["street_idx"] = None
            street = tags.get("addr:street")
            if not street:
                continue
            best = None
            for i in by_name.get(self.name_key(street), ()):
                d = box_distance_m(a["lat"], a["lon"], self.streets[i]["box"])
                if d <= cfg["link_m"] and (best is None or d < best[0]):
                    best = (d, i)
            if best is None:
                words = [k for k in self.words(street) if k not in fold.type_words]
                if words:
                    candidates = set.intersection(*(by_word.get(w, set()) for w in words))
                    for i in candidates:
                        d = box_distance_m(a["lat"], a["lon"], self.streets[i]["box"])
                        if d <= cfg["link_m"] and (best is None or d < best[0]):
                            best = (d, i)
            if best is not None:
                a["street_idx"] = best[1]
                self.stats["addresses_linked"] += 1
            else:
                virtual_groups[(self.name_key(street), a["city"])].append(a)

        # Streets that exist only in addr:street.
        for (key, city), group in virtual_groups.items():
            if not key:
                continue
            for members in cluster_points([(a["lat"], a["lon"]) for a in group],
                                          self.config["streets"]["merge_m"]):
                addrs = [group[m] for m in members]
                idx = len(self.streets)
                self.streets.append(self._virtual_street(key, city, addrs))
                for a in addrs:
                    a["street_idx"] = idx
                self.stats["streets_virtual"] += 1

        # One row per address: copies within dedup_m are merged, the
        # building wins.
        groups = defaultdict(list)
        for a in kept:
            hn = self.name_key(a["tags"]["addr:housenumber"])
            if not hn:
                continue
            where = a["street_idx"] if a["street_idx"] is not None else \
                ("place", self.name_key(a["tags"].get("addr:place")), a["city"])
            groups[(where, hn)].append(a)
        rows = []
        for (where, hn), group in groups.items():
            for members in cluster_points([(a["lat"], a["lon"]) for a in group], cfg["dedup_m"]):
                copies = [group[m] for m in members]
                best = min(copies, key=lambda a: (not a["building"], a["osm"]))
                self.stats["addresses_merged"] += len(copies) - 1
                city = best["city"]
                city_imp = self.places[city]["importance"] if city is not None else 0.0
                street_imp = self.streets[best["street_idx"]]["importance"] if best["street_idx"] is not None else 0.0
                imp = (cfg["base"] + cfg["city_weight"] * city_imp + cfg["street_weight"] * street_imp
                       + (cfg["building_bonus"] if best["building"] else 0))
                rows.append({"osm": best["osm"], "lat": best["lat"], "lon": best["lon"],
                             "housenumber": best["tags"]["addr:housenumber"].strip(),
                             "street_raw": best["tags"].get("addr:street"),
                             "street_en": best["tags"].get("addr:street:en"),
                             "place_raw": best["tags"].get("addr:place"),
                             "street_idx": best["street_idx"], "city": city,
                             "band": 1 if best["zone"] == "band" else 0,
                             "importance": round(clamp01(imp), 4)})
        self.addresses = rows
        self.stats["addresses"] = len(rows)

    def _virtual_street(self, key, city, addrs):
        streets = Counter(a["tags"]["addr:street"] for a in addrs)
        english = Counter(a["tags"].get("addr:street:en") for a in addrs if a["tags"].get("addr:street:en"))
        mean_lat = sum(a["lat"] for a in addrs) / len(addrs)
        mean_lon = sum(a["lon"] for a in addrs) / len(addrs)
        k = math.cos(math.radians(mean_lat)) ** 2
        rep = min(addrs, key=lambda a: (a["lat"] - mean_lat) ** 2 + k * (a["lon"] - mean_lon) ** 2)
        main = {"name": streets.most_common(1)[0][0], "name_ka": None,
                "name_en": english.most_common(1)[0][0] if english else None, "name_ru": None}
        alt = [v for v in list(streets) + list(english) if v not in main.values()]
        city_imp = self.places[city]["importance"] if city is not None else 0.0
        box = (min(a["lat"] for a in addrs), min(a["lon"] for a in addrs),
               max(a["lat"] for a in addrs), max(a["lon"] for a in addrs))
        cfg = self.config["streets"]
        imp = cfg["class_weight"] * 0.3 + cfg["city_weight"] * city_imp
        return {"key": key, "ways": [], "osm": None, "kind": "virtual", "lat": rep["lat"], "lon": rep["lon"],
                "box": box, "names": _FixedNames(main, alt), "length_m": 0.0, "zone": rep["zone"],
                "city": city, "importance": round(clamp01(imp), 4), "virtual": 1,
                "words": set()}

    # -- POIs ---------------------------------------------------------------

    def build_pois(self):
        cfg = self.config["pois"]
        line_m = float(cfg.get("line_checkpoint_km", 0)) * 1000.0
        kept = []
        for p in self.c.pois:
            zone = self.zones.zone(p["lat"], p["lon"])
            if zone in ("outside", "occupied", "buffer"):
                self.stats[f"pois_dropped_{zone}"] += 1
                continue
            p["zone"] = zone
            p["names"] = Names(p["tags"], self.config)
            if p["rule"]["kind"] == "border_control" and line_m and self.zones.near_hard(p["lat"], p["lon"], line_m):
                # At the occupation line, which is no border: never a border crossing.
                if not p["names"].any:
                    self.stats["pois_dropped_line_checkpoint_unnamed"] += 1
                    continue
                p["rule"] = dict(p["rule"], kind="line_checkpoint")
                self._sample("pois_line_checkpoint", p["names"].display)
            kept.append(p)
        groups = defaultdict(list)
        for p in kept:
            label = p["names"].display or (p["names"].brand[0] if p["names"].brand else "")
            groups[(p["rule"]["kind"], self.name_key(label))].append(p)
        rows = []
        for (kind, key), group in groups.items():
            for members in cluster_points([(p["lat"], p["lon"]) for p in group], cfg["dedup_m"]):
                copies = [group[m] for m in members]
                best = max(copies, key=lambda p: (len(p["tags"]), p["osm"]))
                self.stats["pois_merged"] += len(copies) - 1
                rule = best["rule"]
                tags = best["tags"]
                attrs = [name for name, cond in rule.get("attrs", {}).items() if match_tags(tags, cond)]
                on_road = any(p.get("on_road") for p in copies)
                imp = (rule["prior"]
                       + (cfg["named_weight"] if best["names"].any else 0)
                       + (cfg["brand_weight"] if best["names"].brand else 0)
                       + (cfg["wikidata_weight"] if tags.get("wikidata") else 0)
                       + (cfg["on_road_bonus"] if on_road else 0))
                # After the importance: a search name is no name of its own
                # (a brand-only ATM gets no named_weight for 'X ბანკომატი').
                extra = self.poi_search_names(rule, best["names"])
                if extra:
                    best["names"].alt.extend(extra)
                    self.stats["poi_search_names"] += len(extra)
                rows.append({"osm": best["osm"], "kind": kind, "group": rule["group"],
                             "lat": best["lat"], "lon": best["lon"], "names": best["names"],
                             "brand": best["names"].brand[0] if best["names"].brand else None,
                             "brands": best["names"].brand,
                             "operator": tags.get("operator"),
                             "attrs": ";".join(attrs) or None,
                             "city": self.city_of(best["lat"], best["lon"], tags.get("addr:city")),
                             "band": 1 if best["zone"] == "band" else 0,
                             "importance": round(clamp01(imp), 4)})
                self.stats[f"poi_{kind}"] += 1
        self.pois = rows
        self.stats["pois"] = len(rows)

    def poi_search_names(self, rule, names):
        """Extra search names of one POI (stored in alt_names: searched,
        never shown). kind_words: '<name> <word>' for each word of the rule's
        kind_words that its names do not already hold as a query would read
        it (the word, its stem, or a longer word it begins), in the order
        listed, once per main name that reads differently ('ისთ ფოინთი
        mall', 'East Point mall'), so 'ამირანი კინო' finds the cinema
        Amirani. joined_names: a name of two Latin words also joined
        ('East Point' -> 'EastPoint')."""
        # Each main name that reads differently ('ისთ ფოინთი', 'East Point'),
        # or the brand of a POI without a name.
        bases, seen = [], set()
        for text in [v for v in names.main.values() if v] or names.brand[:1]:
            key = self.words(text)
            if key and key not in seen:
                seen.add(key)
                bases.append(text)
        if not bases:
            return []
        have = set()
        for text in names.all() + names.brand:
            have.update(self.words(text))
        extra = []
        for word in rule.get("kind_words", ()):
            q = self.fold.parse_query(word)
            tokens = q.required + q.optional
            if not tokens or all(self._holds(have, t) for t in tokens):
                continue
            for base in bases:
                extra.append(f"{base} {word}")
            have.update(self.words(word))
        if self.config["pois"].get("joined_names"):
            known = set(names.all())
            for text in names.main.values():
                parts = (text or "").split()
                if len(parts) == 2 and all(LATIN_WORD.fullmatch(p) for p in parts):
                    joined = "".join(parts)
                    if joined not in known and self.name_key(joined) not in have:
                        known.add(joined)
                        extra.append(joined)
                        have.update(self.words(joined))
        return extra

    def _holds(self, keys, token):
        """A name word (of keys) matches this query token as the search's
        text score would: the word, its stem, or a longer word it begins."""
        return any(k in token.keys or (self.fold.name_bases(k) & token.prefix_set)
                   or any(k.startswith(p) for p in token.prefixes) for k in keys)

    # -- search keys --------------------------------------------------------

    def brand_words(self, brands):
        table = self.config["brands"]
        if not hasattr(self, "_brand_index"):
            index = {}
            for canon, words in table.items():
                if canon.startswith("_"):
                    continue
                for w in [canon] + words:
                    index[self.name_key(w)] = [canon] + words
            self._brand_index = index
        out = []
        for b in brands:
            out.extend(self._brand_index.get(self.name_key(b), ()))
        return out

    def search_keys(self):
        keys = {"places": [], "streets": [], "addresses": [], "pois": []}
        for r in self.places:
            keys["places"].append(self.keys_of(r["names"].all()))
        for s in self.streets:
            keys["streets"].append(self.keys_of(s["names"].all()))
        for a in self.addresses:
            texts = []
            if a["street_idx"] is not None:
                texts.extend(self.streets[a["street_idx"]]["names"].all())
            texts.extend(v for v in (a["street_raw"], a["street_en"], a["place_raw"], a["housenumber"]) if v)
            keys["addresses"].append(self.keys_of(texts))
        for p in self.pois:
            texts = p["names"].all() + p["brands"] + self.brand_words(p["brands"])
            if p["kind"] in ("fuel", "charging") and not p["brands"] and p["operator"]:
                texts.append(p["operator"])
            keys["pois"].append(self.keys_of(texts))
        return keys

    def build(self):
        self.build_places()
        self.build_streets()
        self.build_addresses()
        self.build_pois()
        self.stats.update(self.c.cams.stats)
        self.cameras = geocoder_cameras.build_cameras(self.c.cams, self.zones, self.config["cameras"], self.stats)
        return self


class _FixedNames(Names):
    """Names already chosen (merged streets, virtual streets)."""

    def __init__(self, main, alt):  # noqa: D107 (no tag parsing here)
        self.main = dict(main)
        self.alt = list(alt)
        self.brand = []


# ---------------------------------------------------------------------------
# Writing the database
# ---------------------------------------------------------------------------

SCHEMA = """
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE places (
  id INTEGER PRIMARY KEY,           -- rank: 1 is the most important
  osm TEXT NOT NULL,                -- n123 / w123 / r123
  kind TEXT NOT NULL,               -- city town village hamlet suburb quarter neighbourhood locality
  lat REAL NOT NULL, lon REAL NOT NULL,
  name TEXT, name_ka TEXT, name_en TEXT, name_ru TEXT,
  alt_names TEXT,                   -- other names, joined with '|'
  population INTEGER,
  importance REAL NOT NULL,         -- 0..1
  parent_id INTEGER,                -- places.id of the city/town a district lies in (never occupied for a legal place)
  occupied INTEGER NOT NULL,        -- 1: inside no_go_hard: explain, never route
  zone TEXT,                        -- occupied / buffer (100 m outside the drawn line) / band / NULL
  label_ka TEXT,                    -- what to show; for occupied=1 only from name:ka, never name
  label_en TEXT                     -- Latin label; for occupied=1 romanised from name:ka, never name:en
);
CREATE TABLE streets (
  id INTEGER PRIMARY KEY,
  osm TEXT,                         -- one of its ways (NULL for a virtual street)
  kind TEXT NOT NULL,               -- highest highway class; 'virtual' = only in addr:street
  lat REAL NOT NULL, lon REAL NOT NULL,   -- a point on the street
  min_lat REAL NOT NULL, min_lon REAL NOT NULL, max_lat REAL NOT NULL, max_lon REAL NOT NULL,
  name TEXT, name_ka TEXT, name_en TEXT, name_ru TEXT, alt_names TEXT,
  city_id INTEGER,                  -- places.id (never an occupied place)
  length_m INTEGER NOT NULL,
  importance REAL NOT NULL,
  band INTEGER NOT NULL,            -- 1: point in the 100-500 m warning band
  label_ka TEXT, label_en TEXT
);
CREATE TABLE addresses (
  id INTEGER PRIMARY KEY,
  osm TEXT NOT NULL,
  lat REAL NOT NULL, lon REAL NOT NULL,
  housenumber TEXT NOT NULL,
  street_id INTEGER,                -- streets.id (its names are the address's street); NULL: only addr:place
  place TEXT,                       -- addr:place as tagged
  city_id INTEGER,
  importance REAL NOT NULL,
  band INTEGER NOT NULL
);
CREATE TABLE pois (
  id INTEGER PRIMARY KEY,
  osm TEXT NOT NULL,
  kind TEXT NOT NULL,               -- config pois.rules[].kind (fuel, parking, pharmacy, theatre ...), or
                                    -- line_checkpoint (at the occupation line: no border)
  grp TEXT NOT NULL,                -- driver / destination
  lat REAL NOT NULL, lon REAL NOT NULL,
  name TEXT, name_ka TEXT, name_en TEXT, name_ru TEXT,
  alt_names TEXT,                   -- other names and search-only names (kind_words, joined_names), '|'
  brand TEXT,
  attrs TEXT,                       -- e.g. 'cng;lpg' for fuel, see config pois.rules[].attrs
  city_id INTEGER,
  importance REAL NOT NULL,
  band INTEGER NOT NULL,
  label_ka TEXT, label_en TEXT      -- NULL for an unnamed POI without a brand
);
CREATE TABLE categories (
  phrase TEXT NOT NULL,             -- folded words, space separated
  category TEXT NOT NULL,
  kind TEXT NOT NULL,               -- pois.kind
  attr TEXT                         -- pois.attrs value to require, or NULL
);
"""

INDEXES = """
CREATE INDEX pois_kind_lat ON pois(kind, lat);
CREATE INDEX addresses_street ON addresses(street_id);
CREATE INDEX streets_city ON streets(city_id);
CREATE INDEX categories_phrase ON categories(phrase);
"""


def fts_create(engine, table):
    if engine == "fts5":
        return (f"CREATE VIRTUAL TABLE {table}_fts USING fts5(key, content='', detail=none, "
                f"columnsize=0, prefix='2 3', tokenize='ascii')")
    if engine == "fts4":
        return f"CREATE VIRTUAL TABLE {table}_fts USING fts4(content=\"\", key, prefix=\"2,3\", tokenize=simple)"
    raise ValueError(f"unknown full-text engine {engine!r}")


def check_engine(engine):
    con = sqlite3.connect(":memory:")
    try:
        con.execute(fts_create(engine, "probe"))
    except sqlite3.OperationalError as exc:
        raise RuntimeError(f"this SQLite ({sqlite3.sqlite_version}) cannot make {engine} tables: {exc}")
    finally:
        con.close()


def order_rows(rows):
    order = sorted(range(len(rows)), key=lambda i: (-rows[i]["importance"], rows[i]["osm"] or "", i))
    new_id = {old: pos + 1 for pos, old in enumerate(order)}
    return order, new_id


def write_database(path, builder, meta, engine="fts5"):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        path.unlink()
    check_engine(engine)
    keys = builder.search_keys()
    b = builder

    p_order, p_id = order_rows(b.places)
    s_order, s_id = order_rows(b.streets)
    a_order, a_id = order_rows(b.addresses)
    o_order, o_id = order_rows(b.pois)

    def pid(i):
        return p_id[i] if i is not None else None

    def alt(names):
        return "|".join(names.alt) or None

    fold_spec = builder.fold.spec

    con = sqlite3.connect(str(path))
    con.execute("PRAGMA page_size=4096")
    con.execute("PRAGMA journal_mode=OFF")
    con.execute("PRAGMA synchronous=OFF")
    con.executescript(SCHEMA)
    for table in TABLES:
        con.execute(fts_create(engine, table))

    counts = Counter()
    rows = []
    for i in p_order:
        r = b.places[i]
        m = r["names"].main
        rows.append((p_id[i], r["osm"], r["kind"], round(r["lat"], 7), round(r["lon"], 7), m["name"], m["name_ka"],
                     m["name_en"], m["name_ru"], alt(r["names"]), r["population"], r["importance"],
                     pid(r["parent"]), r["occupied"], r["zone"], r["label"][0], r["label"][1]))
    con.executemany("INSERT INTO places VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
    rows = []
    for i in s_order:
        s = b.streets[i]
        m = s["names"].main
        rows.append((s_id[i], s["osm"], s["kind"], round(s["lat"], 7), round(s["lon"], 7),
                     *[round(v, 7) for v in s["box"]], m["name"], m["name_ka"], m["name_en"], m["name_ru"],
                     alt(s["names"]), pid(s["city"]), int(round(s["length_m"])), s["importance"],
                     1 if s["zone"] == "band" else 0, *labels(m, fold_spec)))
    con.executemany("INSERT INTO streets VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
    rows = []
    for i in a_order:
        a = b.addresses[i]
        rows.append((a_id[i], a["osm"], round(a["lat"], 7), round(a["lon"], 7), a["housenumber"],
                     s_id[a["street_idx"]] if a["street_idx"] is not None else None,
                     a["place_raw"], pid(a["city"]), a["importance"], a["band"]))
    con.executemany("INSERT INTO addresses VALUES (?,?,?,?,?,?,?,?,?,?)", rows)
    rows = []
    for i in o_order:
        p = b.pois[i]
        m = p["names"].main
        label = labels(m, fold_spec, fallback=p["brand"]) if (p["names"].any or p["brand"]) else (None, None)
        rows.append((o_id[i], p["osm"], p["kind"], p["group"], round(p["lat"], 7), round(p["lon"], 7),
                     m["name"], m["name_ka"], m["name_en"], m["name_ru"], alt(p["names"]), p["brand"],
                     p["attrs"], pid(p["city"]), p["importance"], p["band"], *label))
    con.executemany("INSERT INTO pois VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)

    for table, order, ids in (("places", p_order, p_id), ("streets", s_order, s_id),
                              ("addresses", a_order, a_id), ("pois", o_order, o_id)):
        col = "rowid" if engine == "fts5" else "docid"
        batch = [(ids[i], " ".join(keys[table][i])) for i in order if keys[table][i]]
        con.executemany(f"INSERT INTO {table}_fts({col}, key) VALUES (?, ?)", batch)
        counts[f"{table}_indexed"] = len(batch)
        con.execute(f"INSERT INTO {table}_fts({table}_fts) VALUES ('optimize')")

    cats = []
    for name, cat in builder.config["categories"].items():
        if name.startswith("_"):
            continue
        for word in cat["words"]:
            phrase = " ".join(builder.fold.keys(word))
            if phrase:
                cats.append((phrase, name, cat["kind"], cat.get("attr")))
    con.executemany("INSERT INTO categories VALUES (?,?,?,?)", sorted(set(cats)))
    con.executescript(INDEXES)
    counts["cameras"] = geocoder_cameras.write_cameras(con, builder.cameras)

    for table in TABLES:
        counts[table] = con.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
    counts["places_occupied"] = con.execute("SELECT count(*) FROM places WHERE occupied=1").fetchone()[0]
    counts["streets_virtual"] = con.execute("SELECT count(*) FROM streets WHERE kind='virtual'").fetchone()[0]
    counts["categories"] = len(set(cats))
    kinds = {f"{t}.{k}": n for t in ("places", "pois")
             for k, n in con.execute(f"SELECT kind, count(*) FROM {t} GROUP BY kind")}
    meta = dict(meta)
    meta.update({"counts": json.dumps(dict(sorted(counts.items()))),
                 "kinds": json.dumps(dict(sorted(kinds.items()))),
                 "fts": engine})
    con.executemany("INSERT INTO meta VALUES (?, ?)", sorted((k, str(v)) for k, v in meta.items()))
    con.commit()
    con.execute("ANALYZE")
    con.commit()
    con.execute("VACUUM")
    con.close()
    return dict(counts), kinds


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def base_meta(config_path, fold_path, fold, config, source=None):
    source = source or {}
    env = os.environ
    server = env.get("GITHUB_SERVER_URL", "https://github.com")
    repo = env.get("GITHUB_REPOSITORY")
    commit = env.get("GITHUB_SHA")
    return {
        "schema_version": SCHEMA_VERSION,
        "fold_version": fold.version,
        "fold_json": Path(fold_path).read_text(encoding="utf-8"),
        "config_version": config["version"],
        "config_json": Path(config_path).read_text(encoding="utf-8"),
        "attribution": "© OpenStreetMap contributors",
        "attribution_url": COPYRIGHT_URL,
        "licence": "ODbL-1.0",
        "licence_url": ODBL_URL,
        "notice": NOTICE,
        "method": f"{server}/{repo}/tree/{commit}" if repo and commit else "scripts/geocoder_build.py",
        "source_pbf_sha256": source.get("sha256", ""),
        "source_pbf_bytes": source.get("bytes", ""),
        "osm_timestamp": source.get("timestamp", ""),
        "release_tag": source.get("tag", ""),
        "clip_config_version": source.get("clip_config_version", ""),
        "row_ids": "Row ids rank rows by importance (1 = most important), so a full-text query "
                   "in rowid order returns the most important matches first.",
    }


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--pbf", required=True, help="the clipped extract (clip.py output)")
    p.add_argument("--zones", required=True, help="nogo_zones.geojson from clip.py")
    p.add_argument("--manifest", help="the tiles' manifest.json: the extract must be the one it lists")
    p.add_argument("--out", required=True)
    p.add_argument("--report")
    p.add_argument("--config", default=str(DEFAULT_CONFIG))
    p.add_argument("--fold", default=str(DEFAULT_FOLD_SPEC))
    p.add_argument("--fts", choices=("fts5", "fts4"), default="fts5")
    args = p.parse_args(argv)

    started = time.time()
    check_engine(args.fts)
    config = load_config(args.config)
    fold = Fold.load(args.fold)
    zones = Zones.from_geojson(args.zones)
    source = {"sha256": sha256_file(args.pbf), "bytes": os.path.getsize(args.pbf),
              "timestamp": pbf_header_timestamp(args.pbf)}
    if args.manifest:
        manifest = json.loads(Path(args.manifest).read_text(encoding="utf-8"))
        listed = ((manifest.get("osm") or {}).get("clipped_pbf") or {}).get("sha256")
        if listed != source["sha256"]:
            print(f"ERROR: {args.pbf} has sha256 {source['sha256']}, but the tiles' manifest lists "
                  f"the clipped extract as {listed!r}; the geocoder must use the same extract",
                  file=sys.stderr)
            return 1
        source["tag"] = manifest.get("tag", "")
        source["clip_config_version"] = (manifest.get("clip") or {}).get("config_version", "")

    collector = Collector(config)
    read_pbf(args.pbf, collector)
    builder = Builder(config, fold, zones, collector).build()
    meta = base_meta(args.config, args.fold, fold, config, source)
    counts, kinds = write_database(args.out, builder, meta, engine=args.fts)

    report = {"schema_version": SCHEMA_VERSION, "fold_version": fold.version,
              "config_version": config["version"], "fts": args.fts,
              "source": source, "counts": counts, "kinds": kinds,
              "stats": dict(sorted((builder.stats + collector.stats).items())),
              "samples": dict(builder.samples),
              "db": {"name": Path(args.out).name, "bytes": os.path.getsize(args.out),
                     "sha256": sha256_file(args.out)},
              "seconds": round(time.time() - started, 1)}
    if args.report:
        Path(args.report).parent.mkdir(parents=True, exist_ok=True)
        Path(args.report).write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"geocoder: {counts['places']} places ({counts['places_occupied']} occupied), "
          f"{counts['streets']} streets ({counts['streets_virtual']} virtual), {counts['addresses']} addresses, "
          f"{counts['pois']} POIs, {counts['cameras']} cameras; {report['db']['bytes']} bytes, {report['seconds']} s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
