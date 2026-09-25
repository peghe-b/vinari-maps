#!/usr/bin/env python3
"""Freeze, or check, the boundary polygons that the safety clip uses.

The clip must never change on its own when someone edits OpenStreetMap, so
the polygons live in config/boundaries/ as frozen GeoJSON files. This script
is the only thing that writes the OSM copies, and a human runs it on purpose.

Two kinds of entry in config/clip.json:

  OSM copies    a relation or way copied from OSM as it is (Georgia,
                Abkhazia, South Ossetia).
  hand-made     a polygon a person drew or derived ("hand_made": true, today
                only the whole Perevi village). --refresh never overwrites it.
                Its OSM sources are kept as frozen copies under
                config/boundaries/sources/ so that --check can see when they
                move.

Two modes:

  --refresh   Download each OSM object (and each source of a hand-made
              polygon) from the OSM API, join its ways into closed rings and
              write the frozen file. Review the git diff by hand, update the
              osm_version values and bump "version" in clip.json.

  --check     Download each object again, rebuild its rings the same way and
              compare the geometry, not just the version number: moving a
              node of the line changes only that node's version, never the
              way's or the relation's. Prints the largest shift in metres.
              For a hand-made polygon it also checks that the live sources
              still lie inside it. Never changes a file. With
              --fail-on-change the exit code is 1 when anything moved, so a
              separate CI job fails and GitHub emails the owner; the build
              itself keeps using the frozen copies, which stay safe to use.

Refresh needs only the Python standard library. The metre figures in
--check need shapely and pyproj; without them it still compares hashes.
The data is (c) OpenStreetMap contributors, ODbL 1.0.
"""

import argparse
import datetime as dt
import hashlib
import json
import sys
import time
import urllib.request
from pathlib import Path

API = "https://api.openstreetmap.org/api/0.6"
USER_AGENT = "vinari-maps/1 (+https://github.com/peghe-b/vinari-maps)"
ROOT = Path(__file__).resolve().parent.parent
CONFIG = ROOT / "config" / "clip.json"

# A rebuilt ring whose points all lie within this distance of the frozen
# ones counts as unchanged (node order or start point may differ).
SAME_SHAPE_M = 0.5


def http_json(url, timeout=120, attempts=4):
    """GET a URL and parse the JSON answer. Retries a few times."""
    last_error = None
    for attempt in range(attempts):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.load(resp)
        except Exception as exc:  # network hiccup: wait and try again
            last_error = exc
            time.sleep(5 * (attempt + 1))
    raise RuntimeError(f"could not fetch {url}: {last_error}")


def boundary_entries(config):
    """The country polygon plus every no-go area, in a fixed order."""
    return [config["country"]] + list(config["no_go"])


def osm_sources(entry):
    """The OSM objects behind an entry: itself, or a hand-made polygon's sources."""
    return list(entry["derived_from"]) if entry.get("hand_made") else [entry]


def join_rings(way_node_lists):
    """Join open ways (lists of node ids) into closed rings.

    Ways may point either way round. Raises ValueError if a ring cannot be
    closed, which means the relation is broken in OSM and must not be used.
    """
    remaining = [list(w) for w in way_node_lists if len(w) >= 2]
    rings = []
    while remaining:
        ring = remaining.pop(0)
        while ring[0] != ring[-1]:
            end = ring[-1]
            for i, way in enumerate(remaining):
                if way[0] == end:
                    ring.extend(way[1:])
                    break
                if way[-1] == end:
                    ring.extend(reversed(way[:-1]))
                    break
            else:
                raise ValueError(f"ring cannot be closed at node {end}")
            remaining.pop(i)
        if len(ring) < 4:
            raise ValueError("ring with fewer than 3 distinct points")
        rings.append(ring)
    return rings


def signed_area(coords):
    """Shoelace formula in degrees; positive means counter-clockwise."""
    total = 0.0
    for (x1, y1), (x2, y2) in zip(coords, coords[1:]):
        total += x1 * y2 - x2 * y1
    return total / 2.0


def geometry_sha256(multipolygon_coords):
    """A hash of the rings that ignores ring order, start point and the
    closing point, so the same shape always gives the same hash."""
    rings = []
    for polygon in multipolygon_coords:
        for ring in polygon:
            pts = [(round(float(x), 7), round(float(y), 7)) for x, y in ring]
            if len(pts) > 1 and pts[0] == pts[-1]:
                pts = pts[:-1]
            if signed_area(pts + pts[:1]) < 0:
                pts.reverse()
            start = pts.index(min(pts))
            rings.append(pts[start:] + pts[:start])
    rings.sort()
    return hashlib.sha256(json.dumps(rings, separators=(",", ":")).encode()).hexdigest()


def fetch_polygon(entry, timeout=120):
    """Download one relation or closed way and return (geometry, osm meta)."""
    kind, oid = entry["osm_type"], entry["osm_id"]
    data = http_json(f"{API}/{kind}/{oid}/full.json", timeout=timeout)
    elements = data["elements"]
    nodes = {e["id"]: (e["lon"], e["lat"]) for e in elements if e["type"] == "node"}
    ways = {e["id"]: e for e in elements if e["type"] == "way"}

    if kind == "way":
        top = ways[oid]
        outer_ways = [top["nodes"]]
    else:
        top = next(e for e in elements if e["type"] == "relation" and e["id"] == oid)
        roles = {m["role"] for m in top["members"] if m["type"] == "way"}
        if "inner" in roles:
            # None of our areas has holes today. Supporting them needs a
            # point-in-polygon step to pair holes with outers; fail loudly
            # instead of guessing.
            raise ValueError(f"relation {oid} has inner rings; extend join_rings first")
        outer_ways = [ways[m["ref"]]["nodes"] for m in top["members"]
                      if m["type"] == "way" and m["role"] in ("outer", "")]

    polygons = []
    for ring in join_rings(outer_ways):
        coords = [[round(nodes[n][0], 7), round(nodes[n][1], 7)] for n in ring]
        if signed_area(coords) < 0:  # GeoJSON wants outer rings counter-clockwise
            coords.reverse()
        polygons.append([coords])

    meta = {
        "osm_type": kind,
        "osm_id": oid,
        "osm_version": top["version"],
        "osm_timestamp": top["timestamp"],
        "name": top.get("tags", {}).get("name"),
        "name:en": top.get("tags", {}).get("name:en"),
        "name:ka": top.get("tags", {}).get("name:ka"),
    }
    return {"type": "MultiPolygon", "coordinates": polygons}, meta


def to_geojson_text(feature):
    """Valid GeoJSON with one point per line, so a git diff shows exactly
    which part of the line moved when a boundary is refreshed."""
    lines = ['{"type":"FeatureCollection","features":[{"type":"Feature",',
             '"properties":' + json.dumps(feature["properties"], ensure_ascii=False) + ",",
             '"geometry":{"type":"MultiPolygon","coordinates":[']
    polygons = feature["geometry"]["coordinates"]
    for pi, polygon in enumerate(polygons):
        lines.append("[")
        for ri, ring in enumerate(polygon):
            lines.append("[")
            for ci, (x, y) in enumerate(ring):
                lines.append(f"[{x},{y}]" + ("," if ci < len(ring) - 1 else ""))
            lines.append("]" + ("," if ri < len(polygon) - 1 else ""))
        lines.append("]" + ("," if pi < len(polygons) - 1 else ""))
    lines.append("]}}]}")
    return "\n".join(lines) + "\n"


def refresh(config):
    fetched_at = dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()
    for entry in boundary_entries(config):
        if entry.get("hand_made"):
            print(f"{entry['file']}: hand-made, never overwritten; refreshing its OSM sources only. "
                  "Check by eye that the polygon still covers them.")
        for source in osm_sources(entry):
            geometry, meta = fetch_polygon(source)
            meta.update({
                "fetched_at": fetched_at,
                "source": f"{API}/{source['osm_type']}/{source['osm_id']}/full",
                "licence": "ODbL 1.0, (c) OpenStreetMap contributors",
                "used_as": entry["name"] if not entry.get("hand_made")
                else f"source of the hand-made polygon {entry['file']}",
            })
            feature = {"type": "Feature", "properties": meta, "geometry": geometry}
            out = ROOT / "config" / source["file"]
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(to_geojson_text(feature), encoding="utf-8")
            points = sum(len(p[0]) for p in geometry["coordinates"])
            print(f"{source['osm_type']} {source['osm_id']} v{meta['osm_version']}: "
                  f"{len(geometry['coordinates'])} ring(s), {points} points -> {out.relative_to(ROOT)}")
            if meta["osm_version"] != source["osm_version"]:
                print(f"  NOTE: clip.json says v{source['osm_version']}; update it and bump 'version'.")


class Geometry:
    """Metre-based comparisons (needs shapely and pyproj)."""

    def __init__(self, metric_crs):
        import pyproj
        from shapely.geometry import shape
        from shapely.ops import transform
        self.shape, self.transform = shape, transform
        self.to_m = pyproj.Transformer.from_crs("EPSG:4326", metric_crs, always_xy=True).transform

    def metric(self, geometry):
        return self.transform(self.to_m, self.shape(geometry))

    def max_shift_m(self, frozen, live):
        """Hausdorff distance between the two outlines, in metres."""
        return self.metric(frozen).boundary.hausdorff_distance(self.metric(live).boundary)

    def outside_m(self, inner, outer):
        """How far the inner shape pokes out of the outer one (0 if inside)."""
        import shapely
        a, b = self.metric(inner), self.metric(outer)
        rest = a.difference(b)
        if rest.is_empty:
            return 0.0
        points = shapely.points(shapely.get_coordinates(rest))
        return float(shapely.distance(b, points).max())


def compare_source(source, geo, timeout):
    """Compare one frozen OSM copy with live OSM. Returns (changed, text, live_geometry)."""
    frozen = json.loads((ROOT / "config" / source["file"]).read_text(encoding="utf-8"))
    frozen_feature = frozen["features"][0]
    frozen_version = frozen_feature["properties"]["osm_version"]
    live_geometry, meta = fetch_polygon(source, timeout=timeout)
    label = f"{source['osm_type']} {source['osm_id']}"
    versions = f"frozen v{frozen_version}, live v{meta['osm_version']} ({meta['osm_timestamp']})"
    if source["osm_version"] != frozen_version:
        return True, f"{label}: clip.json says v{source['osm_version']} but the file is v{frozen_version}", live_geometry
    same_hash = (geometry_sha256(frozen_feature["geometry"]["coordinates"])
                 == geometry_sha256(live_geometry["coordinates"]))
    if same_hash:
        return False, f"{label}: geometry identical ({versions})", live_geometry
    if geo is None:
        return True, f"{label}: geometry differs, shift unknown (shapely/pyproj missing; {versions})", live_geometry
    shift = geo.max_shift_m(frozen_feature["geometry"], live_geometry)
    if shift <= SAME_SHAPE_M:
        return False, f"{label}: same shape, node order differs, largest shift {shift:.2f} m ({versions})", live_geometry
    return True, f"{label}: the line moved, largest shift {shift:.0f} m ({versions})", live_geometry


def check(config, timeout=30):
    """Compare every frozen polygon with live OSM. Returns the number of
    entries that changed; problems fetching are warnings, not changes."""
    try:
        geo = Geometry(config["metric_crs"])
    except ImportError:
        geo = None
        print("::warning::shapely/pyproj not installed: comparing hashes only, no metre figures")
    changed = 0
    for entry in boundary_entries(config):
        entry_changed = False
        notes = []
        for source in osm_sources(entry):
            try:
                moved, text, live = compare_source(source, geo, timeout)
            except Exception as exc:
                print(f"::warning::could not check {source['osm_type']} {source['osm_id']}: {exc}")
                continue
            notes.append(text)
            entry_changed |= moved
            if entry.get("hand_made") and geo is not None:
                own = json.loads((ROOT / "config" / entry["file"]).read_text(encoding="utf-8"))
                poke = geo.outside_m(live, own["features"][0]["geometry"])
                if poke > SAME_SHAPE_M:
                    entry_changed = True
                    notes.append(f"{source['osm_type']} {source['osm_id']} now reaches {poke:.0f} m "
                                 f"outside the hand-made polygon {entry['file']}")
        if entry_changed:
            changed += 1
            print(f"::warning::{entry['name']}: " + "; ".join(notes) + ". The build keeps using the "
                  "frozen copy. A human should review the change and run "
                  "scripts/fetch_boundaries.py --refresh if it is legitimate"
                  + (" (and redraw the hand-made polygon if it no longer covers the village)."
                     if entry.get("hand_made") else "."))
        else:
            print(f"ok {entry['name']}: " + "; ".join(notes))
    return changed


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--refresh", action="store_true", help="download and rewrite the frozen OSM copies")
    mode.add_argument("--check", action="store_true", help="compare the frozen geometry with live OSM")
    parser.add_argument("--fail-on-change", action="store_true",
                        help="with --check: exit 1 when a line moved (for the separate CI job)")
    args = parser.parse_args()

    config = json.loads(CONFIG.read_text(encoding="utf-8"))
    if args.refresh:
        refresh(config)
        return 0
    changed = check(config)
    print(f"boundary check: {changed} changed upstream")
    return 1 if (changed and args.fail_on_change) else 0


if __name__ == "__main__":
    sys.exit(main())
