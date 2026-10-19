#!/usr/bin/env python3
"""Road hazards for the navigator's voice: the hazards table of georgia_geocoder.sqlite.

geocoder_build.py feeds this module while it reads the clipped extract and
writes what build_hazards() returns into the table `hazards`, beside the
search tables and the cameras. Config: config/geocoder.json `hazards`.

What is taken from OpenStreetMap (hazards.kinds):

  bump   traffic_calming=bump / hump nodes ("წინ ხელოვნური ბორცვია, შეანელე.")
  rail   railway=level_crossing nodes ("წინ რკინიგზის გადასასვლელია.")

A hazard is kept only on a car road: the node must be one of the nodes of a
way whose highway is in hazards.road_classes. Its road_bearing (0..179) is
that road's line through the node, so the app can tell the road it is on
from a road that crosses its route there (a bump on a side street at a
junction). A level crossing is kept only while a railway still runs over it:
one of the ways through the node must be a railway in hazards.live_railways
(rail, narrow_gauge, light_rail); a crossing of a disused or abandoned line,
or of a line that is not in the extract, is dropped. A node on several car
ways takes the biggest road.

The occupied territories: a hazard inside no_go_hard or outside Georgia's
border is dropped, like every other row of the database; one in the 100-500 m
band has band=1.

OSM has only part of Georgia's bumps and crossings. The table is a hint for
the driver, never a complete list: a quiet road is not a road without bumps.

Old apps never read this table (it is new in schema 4); the table and its
columns can grow without breaking them.

Stdlib only, so the unit tests need nothing installed.
"""

from collections import Counter

from geocoder_cameras import CLASS_RANK, bearing, match_tags

SCHEMA = """
CREATE TABLE hazards (
  id INTEGER PRIMARY KEY,
  osm TEXT NOT NULL,                -- n123: the bump or crossing node
  kind TEXT NOT NULL,               -- bump / rail
  lat REAL NOT NULL, lon REAL NOT NULL,
  road_bearing INTEGER NOT NULL,    -- 0..179, the car road's line through the node
  road_class TEXT NOT NULL,         -- that road's highway value
  band INTEGER NOT NULL             -- 1: in the 100-500 m warning band near the occupation line
);
"""
INDEXES = "CREATE INDEX hazards_lat_lon ON hazards(lat, lon);\n"
COLUMNS = ("osm", "kind", "lat", "lon", "road_bearing", "road_class", "band")


class HazardCollector:
    """Hazard nodes, then the ways through them (car roads and railways).
    Nodes come before ways, as in a sorted PBF."""

    def __init__(self, cfg):
        self.cfg = cfg
        self.kinds = cfg["kinds"]                 # kind -> [tag conditions]
        self.road_classes = set(cfg["road_classes"])
        self.live_railways = set(cfg["live_railways"])
        self.nodes = {}                           # node id -> {"id", "kind", "lat", "lon"}
        self.roads = {}                           # node id -> [(class rank, way id, highway, bearing)]
        self.railways = {}                        # node id -> set of railway values through it
        self.stats = Counter()

    def node(self, osm_id, tags, lat, lon):
        for kind, conds in self.kinds.items():
            if match_tags(tags, conds):
                self.nodes[osm_id] = {"id": osm_id, "kind": kind, "lat": lat, "lon": lon}
                self.stats[f"hazard_nodes_{kind}"] += 1
                return

    def way(self, osm_id, tags, refs, coords):
        if not self.nodes:
            return
        highway = tags.get("highway")
        railway = tags.get("railway")
        is_road = highway in self.road_classes
        if not is_road and not railway:
            return
        for i, ref in enumerate(refs):
            if ref not in self.nodes:
                continue
            if railway:
                self.railways.setdefault(ref, set()).add(railway)
            if is_road:
                b = node_bearing(coords, i)
                if b is not None:
                    self.roads.setdefault(ref, []).append((CLASS_RANK.get(highway, 99), osm_id, highway, b))


def node_bearing(coords, i):
    """The way's direction through its i-th node, or None (one point)."""
    if coords[i] is None:
        return None
    before = next((coords[j] for j in range(i - 1, -1, -1) if coords[j] is not None and coords[j] != coords[i]), None)
    after = next((coords[j] for j in range(i + 1, len(coords)) if coords[j] is not None and coords[j] != coords[i]),
                 None)
    a = before or coords[i]
    b = after or coords[i]
    if a == b:
        return None
    return bearing(a[1], a[0], b[1], b[0])


def build_hazards(collector, zones, cfg, stats=None):
    """Rows for the hazards table (dicts with COLUMNS), sorted by osm id.
    zones: geocoder_build.Zones (zone(lat, lon) -> outside / occupied /
    buffer / band / None)."""
    stats = stats if stats is not None else Counter()
    c = collector
    rows = []
    for node_id in sorted(c.nodes):
        h = c.nodes[node_id]
        roads = sorted(c.roads.get(node_id, ()))
        if not roads:
            stats[f"hazards_off_road_{h['kind']}"] += 1
            continue
        if h["kind"] == "rail" and not (c.railways.get(node_id, set()) & c.live_railways):
            stats["hazards_rail_without_live_railway"] += 1
            continue
        zone = zones.zone(h["lat"], h["lon"])
        if zone in ("outside", "occupied", "buffer"):
            stats[f"hazards_dropped_{zone}"] += 1
            continue
        _, _, highway, b = roads[0]
        rows.append({"osm": f"n{node_id}", "kind": h["kind"], "lat": round(h["lat"], 7), "lon": round(h["lon"], 7),
                     "road_bearing": int(round(b)) % 180, "road_class": highway,
                     "band": 1 if zone == "band" else 0})
    for kind in c.kinds:
        stats[f"hazards_{kind}"] = sum(1 for r in rows if r["kind"] == kind)
    stats["hazards"] = len(rows)
    return rows


def write_hazards(con, rows):
    """Create and fill the hazards table on an open sqlite3 connection."""
    con.executescript(SCHEMA)
    con.executemany(f"INSERT INTO hazards (id, {', '.join(COLUMNS)}) VALUES ({', '.join('?' * (len(COLUMNS) + 1))})",
                    [(i + 1, *[r[k] for k in COLUMNS]) for i, r in enumerate(rows)])
    con.executescript(INDEXES)
    return len(rows)


def check_config(cfg, car_classes):
    """Problems with config hazards (a list of strings)."""
    problems = []
    if not isinstance(cfg, dict):
        return ["'hazards' is missing"]
    kinds = cfg.get("kinds")
    if not isinstance(kinds, dict) or set(kinds) != {"bump", "rail"}:
        problems.append("hazards.kinds must give tag conditions for exactly bump and rail (the app knows these)")
    else:
        for kind, conds in kinds.items():
            if not isinstance(conds, list) or not conds or not all(isinstance(x, dict) and x for x in conds):
                problems.append(f"hazards.kinds.{kind} must be a list of tag conditions")
    classes = cfg.get("road_classes")
    if not isinstance(classes, list) or not classes or not set(classes) <= set(car_classes):
        problems.append("hazards.road_classes must be car road classes")
    rails = cfg.get("live_railways")
    if not isinstance(rails, list) or not rails or not all(isinstance(r, str) and r for r in rails):
        problems.append("hazards.live_railways must be a list of railway values")
    return problems


if __name__ == "__main__":
    raise SystemExit("geocoder_hazards is a module of geocoder_build.py")

