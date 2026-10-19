#!/usr/bin/env python3
"""Speed cameras for the navigator: the cameras table of georgia_geocoder.sqlite.

geocoder_build.py feeds this module while it reads the clipped extract, and
writes what build_cameras() returns into the table `cameras`, beside the
search tables (one file the app already downloads). Config:
config/geocoder.json `cameras`.

What is taken from OpenStreetMap:

  highway=speed_camera nodes   one row each, kind fixed, or red_light /
                               average_speed when their tags say so
                               (cameras.red_light, cameras.average_speed)
  type=enforcement relations   enforcement=maxspeed and traffic_signals: their
                               camera (role device) gets the relation's
                               maxspeed and the direction from its from and to
                               members; a relation whose device is no camera
                               node becomes a row of its own (osm r123).
                               enforcement=average_speed: one row per section,
                               from its from member (lat, lon) to its to
                               member (end_lat, end_lon).

Every row is matched to the car road it stands on (or the nearest within
cameras.snap_m), so the app can check that a camera is on its route and
facing its way:

  heading       the travel direction the camera checks, 0..359 degrees
                clockwise from north, or NULL for both directions (or not
                known). In this order: the road's one-way direction when the
                camera node lies on a one-way road; the from -> to direction
                of its enforcement relation; its direction tag. forward and
                backward are along the way. A number or compass point is the
                way the camera LOOKS (cameras.direction_degrees 'facing'), so
                the traffic it checks travels the opposite way: of the 54
                degree tags on one-way roads in Georgia in September 2026, 47
                point against the traffic. A direction more than
                cameras.heading_off_road_max_deg off the road is dropped
                (NULL), and a kept one is turned onto the road's own line.
  road_bearing  the road's line at the camera, 0..179 degrees (NULL: no car
                road within snap_m)

The occupied territories: a camera (or either end of a section) inside
no_go_hard or outside Georgia's border is dropped, like every other row of
the database; one in the 100-500 m band has band=1.

A speed limit no camera on its road can have is a tagging slip, not a sign
(cameras.maxspeed_floor, cameras.maxspeed_road_ratio; plausible_maxspeed): in
September 2026, 13 Tbilisi camera nodes from one mapper said maxspeed=20, one
of them on the Kakheti highway (trunk S5) where the cameras beside it say 80,
and the navigator read "twenty" to drivers on an 80 road. Then the road's own
limit is used when it is plausible, else none: no number is better than a
wrong one.

Average-speed sections from the Ministry of Internal Affairs
(config/average_speed_sections.json, cameras.sections): OSM in Georgia has
almost none (two section cameras without an end in September 2026), while
the ministry's press releases on police.ge name each section by its road, its
start and end (a km post or a junction) and its limit. The file holds only
the sections whose both ends could be placed with confidence (an OSM km post
of that road whose number matches and whose nearby village or junction is
the one the release names, or the junction itself), with the evidence, the
release and its date; the rest are listed there as not placed. Here each end
is put on the carriageway of that road (cameras.sections.road: its ref or
name) that runs the section's way, within cameras.sections.snap_m: a km post
often stands on the other carriageway of a divided road. A section whose end
finds no such road (the road was rebuilt or renamed in OSM) is dropped and
counted (sections_unplaced), never guessed. Its limit is the release's,
unless the road's own explicit OSM limit there says otherwise: then none
(sections_maxspeed_conflict), since one of the two is out of date. Such a row
has osm 'mia-<release>-<item>', maxspeed_src 'mia' and heading_src 'section'.

OSM has only part of Georgia's cameras. The table is a hint for the driver,
never a complete list, and the road signs always have priority.

Stdlib only, so the unit tests need nothing installed.
"""

import json
import math
from collections import Counter
from pathlib import Path

EARTH_R = 6371008.8
M_PER_DEG = math.pi * EARTH_R / 180.0
CAR_CLASSES = ("motorway", "trunk", "primary", "secondary", "tertiary", "motorway_link", "trunk_link",
               "primary_link", "secondary_link", "tertiary_link", "unclassified", "residential",
               "living_street", "road", "service", "track")
CLASS_RANK = {c: i for i, c in enumerate(CAR_CLASSES)}   # lower is the bigger road
COMPASS = {name: i * 22.5 for i, name in enumerate(
    ("N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE", "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW"))}
KINDS = ("fixed", "red_light", "average_speed")

SCHEMA = """
CREATE TABLE cameras (
  id INTEGER PRIMARY KEY,
  osm TEXT NOT NULL,                -- n123 (the camera node) or r123 (an enforcement relation)
  kind TEXT NOT NULL,               -- fixed / red_light / average_speed
  lat REAL NOT NULL, lon REAL NOT NULL,   -- the camera; for a section with an end, its start
  end_lat REAL, end_lon REAL,       -- average_speed: the end of the section, else NULL
  maxspeed INTEGER,                 -- km/h, NULL when neither the camera nor its road says
  maxspeed_src TEXT,                -- camera / relation / road / NULL
  heading INTEGER,                  -- 0..359, travel direction checked; NULL = both directions or unknown
  heading_src TEXT,                 -- oneway / relation / direction / NULL
  road_bearing INTEGER,             -- 0..179, the road's line at the camera; NULL = no car road within snap_m
  road_dist_m REAL,                 -- metres from the camera to that road (0 when the node is on it)
  road_class TEXT,                  -- its highway value
  road_ref TEXT,                    -- its ref (e.g. 'S1'), or NULL
  road_name TEXT,                   -- its name:ka, else name, or NULL
  band INTEGER NOT NULL             -- 1: in the 100-500 m warning band near the occupation line
);
"""
INDEXES = "CREATE INDEX cameras_lat_lon ON cameras(lat, lon);\n"
COLUMNS = ("osm", "kind", "lat", "lon", "end_lat", "end_lon", "maxspeed", "maxspeed_src", "heading",
           "heading_src", "road_bearing", "road_dist_m", "road_class", "road_ref", "road_name", "band")


# ---------------------------------------------------------------------------
# Geometry (short distances: a flat plane around the point is enough)
# ---------------------------------------------------------------------------

def distance_m(lat1, lon1, lat2, lon2):
    p1, p2 = math.radians(lat1), math.radians(lat2)
    a = (math.sin((p2 - p1) / 2) ** 2
         + math.cos(p1) * math.cos(p2) * math.sin(math.radians(lon2 - lon1) / 2) ** 2)
    return 2 * EARTH_R * math.asin(min(1.0, math.sqrt(a)))


def bearing(lat1, lon1, lat2, lon2):
    """Initial bearing from the first point to the second, 0..360."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dl = math.radians(lon2 - lon1)
    y = math.sin(dl) * math.cos(p2)
    x = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dl)
    return math.degrees(math.atan2(y, x)) % 360.0


def angle_diff(a, b):
    """Smallest angle between two directions, 0..180."""
    return abs((a - b + 180.0) % 360.0 - 180.0)


def segment_distance_m(lat, lon, a, b):
    """Distance from a point to the segment a-b (each (lon, lat))."""
    kx = M_PER_DEG * math.cos(math.radians(lat))
    ax, ay = (a[0] - lon) * kx, (a[1] - lat) * M_PER_DEG
    bx, by = (b[0] - lon) * kx, (b[1] - lat) * M_PER_DEG
    dx, dy = bx - ax, by - ay
    t = 0.0 if dx == dy == 0 else max(0.0, min(1.0, -(ax * dx + ay * dy) / (dx * dx + dy * dy)))
    return math.hypot(ax + t * dx, ay + t * dy)


def onto_line(direction, line):
    """The direction along the line (line or line + 180) nearest to direction."""
    return line % 360.0 if angle_diff(direction, line) <= 90.0 else (line + 180.0) % 360.0


# ---------------------------------------------------------------------------
# Tags
# ---------------------------------------------------------------------------

def tag_values(value):
    return [v.strip() for v in (value or "").split(";") if v.strip()]


def match_tags(tags, conditions):
    """conditions: a list of {key: [values]}; '*' = any value. True when one
    of them holds entirely (the same rules as geocoder_build.match_tags)."""
    for cond in conditions:
        ok = True
        for key, values in cond.items():
            got = tag_values(tags.get(key))
            if not got or ("*" not in values and not any(v in values for v in got)):
                ok = False
                break
        if ok:
            return True
    return False


def parse_maxspeed(value, zones=None, allowed=(5, 150)):
    """'60', '60 km/h', '40 mph', 'GE:urban' -> km/h; anything else (none,
    signals, walk, two values, out of range) -> None: better no number than a
    wrong one read to the driver."""
    values = tag_values(value)
    if len(values) != 1:
        return None
    v = values[0].strip()
    if zones and v in zones:
        kmh = zones[v]
    else:
        low = v.lower().replace(" ", "")
        factor = 1.0
        for unit, f in (("km/h", 1.0), ("kmh", 1.0), ("kph", 1.0), ("mph", 1.609344)):
            if low.endswith(unit):
                low, factor = low[:-len(unit)], f
                break
        try:
            kmh = float(low) * factor
        except ValueError:
            return None
        if not math.isfinite(kmh):
            return None
    kmh = int(round(kmh))
    return kmh if allowed[0] <= kmh <= allowed[1] else None


def parse_direction(value):
    """direction tag -> ('along', +1/-1) for forward/backward, ('degrees', d)
    for one number or compass point, ('both', None) for both/all or several
    values, None when absent or unreadable."""
    values = tag_values(value)
    if not values:
        return None
    if len(values) > 1:
        return ("both", None)
    v = values[0]
    low = v.lower()
    if low == "forward":
        return ("along", 1)
    if low == "backward":
        return ("along", -1)
    if low in ("both", "all"):
        return ("both", None)
    if v.upper() in COMPASS:
        return ("degrees", COMPASS[v.upper()])
    try:
        d = float(v)
    except ValueError:
        return None
    if not math.isfinite(d):
        return None
    return ("degrees", d % 360.0)


def oneway_sign(tags):
    """+1 one-way along the way, -1 against it, 0 both ways."""
    v = (tags.get("oneway") or "").lower()
    if v in ("yes", "true", "1"):
        return 1
    if v in ("-1", "reverse"):
        return -1
    if v in ("no", "false", "0", "alternating", "reversible"):
        return 0
    if tags.get("highway") == "motorway" or tags.get("junction") in ("roundabout", "circular"):
        return 1
    return 0


# ---------------------------------------------------------------------------
# Collecting (geocoder_build.Collector hands over the objects it reads)
# ---------------------------------------------------------------------------

class CameraCollector:
    """Camera nodes, enforcement relations, the locations of their member
    nodes and the car roads near any camera. Relations come first (the
    reader's first pass), then nodes, then ways, as in a sorted PBF."""

    def __init__(self, cfg):
        self.cfg = cfg
        self.node_rules = cfg["nodes"]
        self.relation_kinds = cfg["relations"]
        self.cameras = {}             # node id -> {"id", "lat", "lon", "tags"}
        self.relations = []           # (relation id, tags, [(type, ref, role)])
        self.watch = set()            # member node ids whose location is needed
        self.sections = list(cfg.get("_sections") or ())   # curated sections (load_sections)
        self.anchors = [(s[e]["lat"], s[e]["lon"]) for s in self.sections for e in ("from", "to")]
        self.coords = {}              # member node id -> (lon, lat)
        self.roads = {}               # way id -> {"id", "tags", "refs", "coords"}
        self.cell = float(cfg.get("grid_deg", 0.02))
        self.margin_deg = (max(cfg["snap_m"], cfg["parallel_m"]) + 20.0) / M_PER_DEG
        self._grid = None
        self.stats = Counter()

    def wants_relation(self, tags):
        return tags.get("type") == "enforcement" and tags.get("enforcement") in self.relation_kinds

    def relation(self, rel_id, tags, members):
        nodes = [(t, ref, role) for t, ref, role in members if t == "n" and role in ("device", "from", "to")]
        self.relations.append((rel_id, dict(tags), nodes))
        self.watch.update(ref for _, ref, _ in nodes)
        self.stats[f"relations_{tags.get('enforcement')}"] += 1

    def node(self, osm_id, tags, lat, lon):
        if osm_id in self.watch:
            self.coords[osm_id] = (lon, lat)
        if match_tags(tags, self.node_rules):
            self.cameras[osm_id] = {"id": osm_id, "lat": lat, "lon": lon, "tags": dict(tags)}

    def missing_members(self):
        """Member nodes whose location no tagged node or way gave (an untagged
        device node that is on no way): the reader looks them up by id."""
        return self.watch - set(self.coords)

    def _grid_cells(self):
        if self._grid is None:
            grid = {}
            points = [(c["lat"], c["lon"]) for c in self.cameras.values()]
            points += [(lat, lon) for lon, lat in self.coords.values()]
            points += self.anchors
            for lat, lon in points:
                grid.setdefault((int(math.floor(lat / self.cell)), int(math.floor(lon / self.cell))), True)
            self._grid = grid
        return self._grid

    def way(self, osm_id, tags, refs, coords):
        member = False
        if self.watch:
            for ref, c in zip(refs, coords):
                if c is not None and ref in self.watch:
                    member = True
                    if ref not in self.coords:
                        self.coords[ref] = c
                        if self._grid is not None:
                            self._grid[(int(math.floor(c[1] / self.cell)), int(math.floor(c[0] / self.cell)))] = True
        if tags.get("highway") not in CLASS_RANK or not (self.cameras or self.coords or self.anchors):
            return
        pts = [c for c in coords if c is not None]
        if not pts:
            return
        if not member and not any(ref in self.cameras for ref in refs):
            m = self.margin_deg
            lat0 = min(p[1] for p in pts) - m
            lat1 = max(p[1] for p in pts) + m
            k = max(math.cos(math.radians(lat0)), 0.2)
            lon0 = min(p[0] for p in pts) - m / k
            lon1 = max(p[0] for p in pts) + m / k
            grid = self._grid_cells()
            r0, r1 = int(math.floor(lat0 / self.cell)), int(math.floor(lat1 / self.cell))
            c0, c1 = int(math.floor(lon0 / self.cell)), int(math.floor(lon1 / self.cell))
            if (r1 - r0 + 1) * (c1 - c0 + 1) <= len(grid):
                near = any((r, c) in grid for r in range(r0, r1 + 1) for c in range(c0, c1 + 1))
            else:
                near = any(r0 <= r <= r1 and c0 <= c <= c1 for r, c in grid)
            if not near:
                return
        self.roads[osm_id] = {"id": osm_id, "tags": dict(tags), "refs": list(refs), "coords": list(coords)}


# ---------------------------------------------------------------------------
# From collected objects to rows
# ---------------------------------------------------------------------------

class RoadIndex:
    """The collected car roads as segments in grid cells."""

    def __init__(self, roads, cell_deg=0.005):
        self.cell = cell_deg
        self.grid = {}
        self.on_node = {}             # node id -> [(road, index in refs)]
        for road in roads.values():
            refs, coords = road["refs"], road["coords"]
            for i, ref in enumerate(refs):
                self.on_node.setdefault(ref, []).append((road, i))
            for i in range(len(coords) - 1):
                a, b = coords[i], coords[i + 1]
                if a is None or b is None or a == b:
                    continue
                r0, r1 = sorted((self._r(a[1]), self._r(b[1])))
                c0, c1 = sorted((self._r(a[0]), self._r(b[0])))
                for r in range(r0, r1 + 1):
                    for c in range(c0, c1 + 1):
                        self.grid.setdefault((r, c), []).append((road, i))

    def _r(self, v):
        return int(math.floor(v / self.cell))

    def near(self, lat, lon, radius_m):
        """[(distance, road, segment index)] within radius_m, nearest first,
        one entry per road."""
        d_lat = radius_m / M_PER_DEG
        d_lon = d_lat / max(math.cos(math.radians(lat)), 0.2)
        best = {}
        for r in range(self._r(lat - d_lat), self._r(lat + d_lat) + 1):
            for c in range(self._r(lon - d_lon), self._r(lon + d_lon) + 1):
                for road, i in self.grid.get((r, c), ()):
                    d = segment_distance_m(lat, lon, road["coords"][i], road["coords"][i + 1])
                    if d <= radius_m and (road["id"] not in best or d < best[road["id"]][0]):
                        best[road["id"]] = (d, road, i)
        return sorted(best.values(), key=lambda x: (x[0], CLASS_RANK[x[1]["tags"]["highway"]], x[1]["id"]))


def seg_bearing(road, i):
    a, b = road["coords"][i], road["coords"][i + 1]
    return bearing(a[1], a[0], b[1], b[0])


def node_bearing(road, i):
    """The way's direction through its i-th node (from the node before to
    the node after)."""
    coords = road["coords"]
    before = next((coords[j] for j in range(i - 1, -1, -1) if coords[j] is not None and coords[j] != coords[i]), None)
    after = next((coords[j] for j in range(i + 1, len(coords)) if coords[j] is not None and coords[j] != coords[i]),
                 None)
    a = before or coords[i]
    b = after or coords[i]
    if a is None or b is None or a == b:
        return None
    return bearing(a[1], a[0], b[1], b[0])


class Road:
    """The road a camera stands on: travel bearing along the way (way order),
    its tags, the distance, and whether the camera node is one of its nodes."""

    def __init__(self, road, along, dist, on_way):
        self.way = road
        self.tags = road["tags"]
        self.along = along
        self.dist = dist
        self.on_way = on_way


def road_of(index, cam_id, lat, lon, cfg):
    """(the camera's road or None, [roads on the camera node], [roads near]).
    A camera node on a car way belongs to it (the biggest road when it sits
    on a junction); otherwise the nearest car road within snap_m."""
    on = []
    for road, i in index.on_node.get(cam_id, ()) if cam_id is not None else ():
        along = node_bearing(road, i)
        if along is not None:
            on.append(Road(road, along, 0.0, True))
    near = [Road(road, seg_bearing(road, i), d, False)
            for d, road, i in index.near(lat, lon, max(cfg["snap_m"], cfg["parallel_m"]))]
    if on:
        on.sort(key=lambda r: (CLASS_RANK[r.tags["highway"]], r.way["id"]))
        return on[0], on, near
    snapped = [r for r in near if r.dist <= cfg["snap_m"]]
    return (snapped[0] if snapped else None), on, near


def oneway_heading(road, on, near, cfg):
    """The only direction traffic can take past the camera, or None.

    On the node: every way through it along the camera road's line must be
    one-way the same direction (a node that splits one road into two ways).
    Beside the road: only when no other road runs parallel within
    parallel_m, since the other carriageway of a divided road may be the
    one the camera watches."""
    if road is None:
        return None
    sign = oneway_sign(road.tags)
    if not sign:
        return None
    heading = road.along if sign > 0 else (road.along + 180.0) % 360.0
    tol = cfg["parallel_deg"]
    if road.on_way:
        for other in on:
            if other is road or angle_diff(other.along % 180.0, road.along % 180.0) > tol:
                continue          # a crossing road
            s = oneway_sign(other.tags)
            h = other.along if s > 0 else (other.along + 180.0) % 360.0
            if not s or angle_diff(h, heading) > tol:
                return None
        return heading
    for other in near:
        if other.way is road.way:
            continue
        if angle_diff(other.along % 180.0, road.along % 180.0) <= tol:
            s = oneway_sign(other.tags)
            h = other.along if s > 0 else (other.along + 180.0) % 360.0
            if not s or angle_diff(h, heading) > tol:
                return None
    return heading


def tag_heading(tags, road, on, cfg, stats):
    """The direction tag as a travel heading, or None (both directions)."""
    parsed = parse_direction(tags.get("direction"))
    if parsed is None:
        if tags.get("direction"):
            stats["direction_unreadable"] += 1
        return None
    how, value = parsed
    if how == "both":
        stats["direction_both"] += 1
        return None
    if how == "along":
        if road is None or not road.on_way:
            stats["direction_along_without_way"] += 1
            return None
        same_line = [r for r in on if angle_diff(r.along % 180.0, road.along % 180.0) <= cfg["parallel_deg"]]
        if any(angle_diff(r.along, road.along) > cfg["parallel_deg"] for r in same_line):
            stats["direction_along_ambiguous"] += 1     # two ways drawn head to head at the camera
            return None
        return road.along if value > 0 else (road.along + 180.0) % 360.0
    travel = (value + 180.0) % 360.0 if cfg["direction_degrees"] == "facing" else value
    if road is None:
        return travel
    along = onto_line(travel, road.along)
    if angle_diff(travel, along) > cfg["heading_off_road_max_deg"]:
        stats["direction_across_road"] += 1
        return None
    return along


def road_maxspeed(road, heading, cfg):
    if road is None:
        return None
    zones = cfg.get("maxspeed_zones")
    allowed = tuple(cfg["maxspeed_range"])
    if heading is not None:
        key = "maxspeed:forward" if angle_diff(heading, road.along) <= 90.0 else "maxspeed:backward"
        v = parse_maxspeed(road.tags.get(key), zones, allowed)
        if v is not None:
            return v
    return parse_maxspeed(road.tags.get("maxspeed"), zones, allowed)


def plausible_maxspeed(speed, src, road, heading, cfg, stats):
    """(speed, src) unless no camera on that road can have it: below the
    road class's floor (cameras.maxspeed_floor; residential and smaller roads
    have none, 20 is real in a living zone), or a camera's or relation's own
    limit below cameras.maxspeed_road_ratio of its road's explicit limit
    (20 on a 60 road). Then the road's own limit when it passes the floor,
    else (None, None)."""
    if speed is None:
        return speed, src
    road_class = road.tags.get("highway") if road is not None else None
    floor = cfg["maxspeed_floor"].get(road_class, 0)
    road_speed = road_maxspeed(road, heading, cfg) if src != "road" else None
    below_road = road_speed is not None and speed < road_speed * cfg["maxspeed_road_ratio"]
    if speed >= floor and not below_road:
        return speed, src
    stats["maxspeed_implausible"] += 1
    if road_speed is not None and road_speed >= floor:
        return road_speed, "road"
    return None, None


def road_fields(road):
    if road is None:
        return {"road_bearing": None, "road_dist_m": None, "road_class": None, "road_ref": None, "road_name": None}
    return {"road_bearing": int(round(road.along)) % 180, "road_dist_m": round(road.dist, 1),
            "road_class": road.tags.get("highway"), "road_ref": road.tags.get("ref") or None,
            "road_name": road.tags.get("name:ka") or road.tags.get("name") or None}


def build_cameras(collector, zones, cfg, stats=None):
    """Rows for the cameras table (dicts with COLUMNS), sorted by osm id.
    zones: geocoder_build.Zones (zone(lat, lon) -> outside / occupied /
    buffer / band / None)."""
    stats = stats if stats is not None else Counter()
    c = collector
    index = RoadIndex(c.roads)
    zones_kmh = cfg.get("maxspeed_zones")
    allowed = tuple(cfg["maxspeed_range"])

    def legal(lat, lon):
        zone = zones.zone(lat, lon)
        if zone in ("outside", "occupied", "buffer"):
            stats[f"cameras_dropped_{zone}"] += 1
            return None
        return zone or "legal"

    # Enforcement relations first: they add to the camera nodes they name.
    from_rel = {}                 # camera node id -> (kind, maxspeed, (from, to) or None, relation id)
    rel_rows = []
    consumed = set()
    for rel_id, tags, members in c.relations:
        kind = c.relation_kinds[tags["enforcement"]]
        devices = [ref for _, ref, role in members if role == "device"]
        ends = {role: c.coords.get(ref) for _, ref, role in members if role in ("from", "to")}
        start, end = ends.get("from"), ends.get("to")
        rel_speed = parse_maxspeed(tags.get("maxspeed"), zones_kmh, allowed)
        if kind == "average_speed":
            if start and end:
                rel_rows.append({"osm": f"r{rel_id}", "kind": kind, "cam": None, "lat": start[1], "lon": start[0],
                                 "end": end, "tags": tags, "rel_speed": rel_speed, "rel_dir": (start, end)})
                consumed.update(d for d in devices if d in c.cameras)
                stats["sections"] += 1
            else:
                for d in devices:
                    if d in c.cameras:
                        from_rel[d] = (kind, rel_speed, None, rel_id)
                stats["sections_without_ends"] += 1
            continue
        cams = [d for d in devices if d in c.cameras]
        direction = (start, end) if start and end and start != end else None
        for d in cams:
            from_rel[d] = (kind, rel_speed, direction, rel_id)
        if not cams:
            where = next((c.coords[d] for d in devices if d in c.coords), None) or end or start
            if where is None:
                stats["relations_without_location"] += 1
                continue
            rel_rows.append({"osm": f"r{rel_id}", "kind": kind, "cam": None, "lat": where[1], "lon": where[0],
                             "end": None, "tags": tags, "rel_speed": rel_speed, "rel_dir": direction})
            stats["relations_as_rows"] += 1

    items = []
    for cam_id in sorted(c.cameras):
        if cam_id in consumed:
            stats["cameras_in_sections"] += 1
            continue
        cam = c.cameras[cam_id]
        kind = "fixed"
        if match_tags(cam["tags"], cfg["red_light"]):
            kind = "red_light"
        elif match_tags(cam["tags"], cfg["average_speed"]):
            kind = "average_speed"
        rel = from_rel.get(cam_id)
        if rel is not None and rel[0] != "fixed":
            kind = rel[0]
        items.append({"osm": f"n{cam_id}", "kind": kind, "cam": cam_id, "lat": cam["lat"], "lon": cam["lon"],
                      "end": None, "tags": cam["tags"], "rel_speed": rel[1] if rel else None,
                      "rel_dir": rel[2] if rel else None})
    items.extend(rel_rows)

    rows = []
    for it in items:
        zone = legal(it["lat"], it["lon"])
        if zone is None:
            continue
        if it["end"] is not None:
            end_zone = legal(it["end"][1], it["end"][0])
            if end_zone is None:
                continue
            zone = "band" if "band" in (zone, end_zone) else zone
        road, on, near = road_of(index, it["cam"], it["lat"], it["lon"], cfg)
        if road is None:
            stats["cameras_without_road"] += 1
        tags = it["tags"]

        heading, src = oneway_heading(road, on, near, cfg), "oneway"
        tagged = tag_heading(tags, road, on, cfg, stats) if tags.get("direction") else None
        if heading is not None and tagged is not None and angle_diff(heading, tagged) > 90.0:
            stats["direction_against_oneway"] += 1
        if heading is None and it["rel_dir"] is not None:
            a, b = it["rel_dir"]
            travel = bearing(a[1], a[0], b[1], b[0])
            heading, src = (onto_line(travel, road.along) if road is not None else travel), "relation"
        if heading is None and tagged is not None:
            heading, src = tagged, "direction"
        if heading is None:
            src = None
        stats[f"heading_{src or 'both'}"] += 1

        speed, speed_src = parse_maxspeed(tags.get("maxspeed"), zones_kmh, allowed), "camera"
        if it["osm"].startswith("r"):
            speed, speed_src = it["rel_speed"], "relation"
        if speed is None and it["rel_speed"] is not None:
            speed, speed_src = it["rel_speed"], "relation"
        if speed is None:
            speed, speed_src = road_maxspeed(road, heading, cfg), "road"
        if speed is None:
            speed_src = None
        speed, speed_src = plausible_maxspeed(speed, speed_src, road, heading, cfg, stats)
        stats[f"maxspeed_{speed_src or 'none'}"] += 1

        row = {"osm": it["osm"], "kind": it["kind"], "lat": round(it["lat"], 7), "lon": round(it["lon"], 7),
               "end_lat": round(it["end"][1], 7) if it["end"] else None,
               "end_lon": round(it["end"][0], 7) if it["end"] else None,
               "maxspeed": speed, "maxspeed_src": speed_src,
               "heading": int(round(heading)) % 360 if heading is not None else None,
               "heading_src": src, "band": 1 if zone == "band" else 0}
        row.update(road_fields(road))
        rows.append(row)

    rows.extend(section_rows(c, index, zones, cfg, stats))
    rows = dedup(rows, cfg["dedup_m"], stats)
    rows.sort(key=lambda r: osm_key(r["osm"]))
    for kind in KINDS:
        stats[f"cameras_{kind}"] = sum(1 for r in rows if r["kind"] == kind)
    stats["cameras"] = len(rows)
    return rows


def osm_key(osm):
    """Sort key of an osm value: nodes (n123), relations (r123), then the
    ministry's sections (mia-<release>-<item>)."""
    if osm.startswith("mia-"):
        release, item = osm[4:].split("-")
        return (2, int(release), int(item.rstrip("ab")), item[-1] if item[-1] in "ab" else "")
    return (0 if osm[0] == "n" else 1, int(osm[1:]), 0, "")


def dedup(rows, radius_m, stats):
    """One camera drawn twice (same kind, same heading or both without one,
    within radius_m) is kept once: the copy with a section end, then with a
    speed limit, then the lowest id."""
    def same(a, b):
        if a["kind"] != b["kind"] or (a["heading"] is None) != (b["heading"] is None):
            return False
        if a["heading"] is not None and angle_diff(a["heading"], b["heading"]) > 30:
            return False
        return distance_m(a["lat"], a["lon"], b["lat"], b["lon"]) <= radius_m

    def rank(r):
        return (r["end_lat"] is None, r["maxspeed"] is None, r["osm"][0] != "n", osm_key(r["osm"]))

    kept = []
    for r in sorted(rows, key=rank):
        if any(same(r, k) for k in kept):
            stats["cameras_merged"] += 1
            continue
        kept.append(r)
    return kept


# ---------------------------------------------------------------------------
# The ministry's average-speed sections (config/average_speed_sections.json)
# ---------------------------------------------------------------------------

def road_matches(tags, want):
    """want: {"ref": "ს 1"} or {"name": "..."}: the road's ref list holds the
    ref, or its name or name:ka is the name."""
    if "ref" in want:
        return want["ref"] in tag_values(tags.get("ref"))
    return want.get("name") in (tags.get("name"), tags.get("name:ka"))


def project(lat, lon, a, b):
    """The point of segment a-b (each (lon, lat)) nearest to (lat, lon), as (lat, lon)."""
    kx = M_PER_DEG * math.cos(math.radians(lat))
    ax, ay = (a[0] - lon) * kx, (a[1] - lat) * M_PER_DEG
    bx, by = (b[0] - lon) * kx, (b[1] - lat) * M_PER_DEG
    dx, dy = bx - ax, by - ay
    t = 0.0 if dx == dy == 0 else max(0.0, min(1.0, -(ax * dx + ay * dy) / (dx * dx + dy * dy)))
    return lat + (ay + t * dy) / M_PER_DEG, lon + (ax + t * dx) / kx


def place_on_road(index, lat, lon, travel, want, snap_m, line_max_deg):
    """(Road, (lat, lon) on it, heading) for the carriageway of the wanted road
    nearest to (lat, lon) within snap_m that carries traffic going `travel`
    (degrees): its line within line_max_deg of travel, and a one-way road only
    in its own direction. None when there is no such road."""
    for d, road, i in index.near(lat, lon, snap_m):
        if not road_matches(road["tags"], want):
            continue
        line = seg_bearing(road, i)
        along = onto_line(travel, line)
        if angle_diff(travel, along) > line_max_deg:
            continue                      # a ramp or a crossing piece of the same road
        sign = oneway_sign(road["tags"])
        if sign:
            allowed = line if sign > 0 else (line + 180.0) % 360.0
            if angle_diff(allowed, along) > 90.0:
                continue                  # the other carriageway
        point = project(lat, lon, road["coords"][i], road["coords"][i + 1])
        return Road(road, line, 0.0, False), point, along
    return None


def section_rows(collector, index, zones, cfg, stats):
    """Rows for the curated sections whose both ends land on their road."""
    scfg = cfg.get("sections") or {}
    out = []
    for s in collector.sections:
        a, b = s["from"], s["to"]
        travel = bearing(a["lat"], a["lon"], b["lat"], b["lon"])
        start = place_on_road(index, a["lat"], a["lon"], travel, s["road"], scfg["snap_m"], scfg["line_max_deg"])
        end = place_on_road(index, b["lat"], b["lon"], travel, s["road"], scfg["snap_m"], scfg["line_max_deg"])
        if start is None or end is None:
            stats["sections_unplaced"] += 1
            stats[f"sections_unplaced {s['id']}"] += 1
            continue
        road, (lat, lon), heading = start
        _, (end_lat, end_lon), _ = end
        zone, end_zone = zones.zone(lat, lon), zones.zone(end_lat, end_lon)
        if {zone, end_zone} & {"outside", "occupied", "buffer"}:
            stats["sections_dropped_zone"] += 1
            continue
        speed, src = s["maxspeed"], "mia"
        own = road_maxspeed(road, heading, cfg)
        if own is not None and own != speed:
            stats["sections_maxspeed_conflict"] += 1
            stats[f"sections_maxspeed_conflict {s['id']} {speed} vs {own}"] += 1
            speed, src = None, None
        row = {"osm": s["id"], "kind": "average_speed", "lat": round(lat, 7), "lon": round(lon, 7),
               "end_lat": round(end_lat, 7), "end_lon": round(end_lon, 7),
               "maxspeed": speed, "maxspeed_src": src,
               "heading": int(round(heading)) % 360, "heading_src": "section",
               "band": 1 if "band" in (zone, end_zone) else 0}
        row.update(road_fields(road))
        out.append(row)
        stats["sections_placed"] += 1
    return out


def load_sections(path, problems):
    """The curated sections file: its sections (each with id, road, from, to,
    maxspeed) after a check; problems collects what is wrong."""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    sources = data.get("sources") or {}
    out, seen = [], set()
    for s in data.get("sections") or []:
        sid = s.get("id", "")
        parts = sid.split("-")
        if (len(parts) != 3 or parts[0] != "mia" or not parts[1].isdigit()
                or not parts[2].rstrip("ab").isdigit() or len(parts[2]) - len(parts[2].rstrip("ab")) > 1):
            problems.append(f"sections: id {sid!r} must be mia-<release>-<item> (the item with a or b when "
                            f"one item of the release names both directions)")
            continue
        if sid in seen:
            problems.append(f"sections: {sid} twice")
        seen.add(sid)
        if s.get("source") not in sources:
            problems.append(f"sections: {sid} names an unknown source {s.get('source')!r}")
        road = s.get("road")
        if not isinstance(road, dict) or len(road) != 1 or not (road.get("ref") or road.get("name")):
            problems.append(f"sections: {sid} road must be {{'ref': ...}} or {{'name': ...}}")
        for end in ("from", "to"):
            e = s.get(end) or {}
            if not (isinstance(e.get("lat"), (int, float)) and 40.9 <= e["lat"] <= 43.7
                    and isinstance(e.get("lon"), (int, float)) and 39.8 <= e["lon"] <= 46.9):
                problems.append(f"sections: {sid} {end} must have lat and lon in Georgia")
            if not e.get("what") or not e.get("evidence"):
                problems.append(f"sections: {sid} {end} needs 'what' (the release's words) and 'evidence'")
        if not isinstance(s.get("maxspeed"), int) or not 20 <= s["maxspeed"] <= 130:
            problems.append(f"sections: {sid} maxspeed must be km/h (20-130)")
        if not problems:
            gap = distance_m(s["from"]["lat"], s["from"]["lon"], s["to"]["lat"], s["to"]["lon"])
            if not 200 <= gap <= 40000:
                problems.append(f"sections: {sid} is {round(gap)} m long (the app takes 0.2-40 km)")
        out.append(s)
    return out


def write_cameras(con, rows):
    """Create and fill the cameras table on an open sqlite3 connection."""
    con.executescript(SCHEMA)
    con.executemany(f"INSERT INTO cameras (id, {', '.join(COLUMNS)}) VALUES ({', '.join('?' * (len(COLUMNS) + 1))})",
                    [(i + 1, *[r[k] for k in COLUMNS]) for i, r in enumerate(rows)])
    con.executescript(INDEXES)
    return len(rows)
