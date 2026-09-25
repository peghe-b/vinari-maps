#!/usr/bin/env python3
"""Safety gate: prove the freshly built tiles cannot route into the
occupied areas or out of Georgia. The release job runs only if this passes.

It runs against a Valhalla service started from the new tiles (see the
workflow) and checks:

0. Setup. The test data has at least the baseline number of tests, the
   service reports the right version and (verbose /status) has tiles, admin
   areas and time zones, and the SHA-256 of the tar under test is recorded
   so manifest.py can refuse to publish any other file.
1. Edges. Every edge in the tile set (from valhalla_export_edges) must stay
   out of the no-go area and lie wholly inside Georgia. This is the direct
   proof; the route tests below are the practical one.
2. Must fail. Routes to towns and road stubs inside the occupied areas, and
   to roads just past Georgia's border crossings, must find nothing. Each
   test runs four requests:
   - control: the same origin to a legal point on the approach road, at
     least 600 m from the line, must route. Otherwise the test could pass
     only because the origin is cut off.
   - strict: the target may only snap to a road within 50 m of it. There
     must be no such road. clip.py measured in the raw input that a road
     did pass within 50 m, so this proves the clip removed it.
   - loose: Valhalla's default snapping (up to 35 km). It may return a
     route to the nearest legal road, but that route must stay out of the
     no-go area and inside Georgia, and if it ends in the 100-500 m band it
     must end on a destination-only edge (or a main road the band exempts).
   - app: the app's own request (config/gate_routes.json app_request, with
     its 300 m search cutoff). Where no legal ground lies within the cutoff,
     it must find nothing; elsewhere it follows the loose rules.
   These run without a date, which is the most permissive way to route.
3. App pre-check. The published nogo_zones.geojson, which the app uses to
   refuse destinations before routing, must refuse every must-fail target
   and allow every must-succeed point and control point.
4. Band. At points in the 100-500 m band on roads that used to stay free,
   /locate must find only destination-only edges.
5. Must succeed. Main Georgian routes must work in July and in January,
   stay out of the no-go area (alternates too) and, where listed, pass
   close to a given point, such as S1 where it runs 412 m from the line.
6. Seasonal. Pshaveli to Omalo over Sh44 must not use Sh44 on a December
   date, and must use it on a July date.

Usage (as in the workflow):
  python scripts/gate.py --url http://127.0.0.1:8002 --edges build/edges.bin \
      --tar build/valhalla/valhalla_tiles.tar --zones build/nogo_zones.geojson \
      --clip-report build/clip_report.json --results build/gate_results.json

Exit code 0 means every check passed; anything else blocks the release.
"""

import argparse
import hashlib
import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import numpy as np
import shapely
from shapely.geometry import LineString, Point, shape
from shapely.ops import transform

sys.path.insert(0, str(Path(__file__).resolve().parent))
from clip import build_zones, load_config  # noqa: E402  (same folder)

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_ROUTES = ROOT / "config" / "gate_routes.json"

# Valhalla error codes that mean "there is no route", as opposed to a
# broken request or a crashed service (which must fail the gate).
NO_ROUTE_CODES = {
    170: "locations are in unconnected regions",
    171: "no suitable edges near location",
    441: "location is unreachable",
    442: "no path could be found for input",
}
STRICT_SNAP_M = 50          # half the 100 m hard buffer: robust, never borderline
CONTROL_MIN_M = 600         # control points lie outside the 500 m band
ROUTE_COUNTRY_MARGIN_M = 50  # route shapes may round a hair past the border
EDGE_COUNTRY_MARGIN_M = 10   # edge points are OSM nodes kept inside Georgia
HARD_MARGIN_M = 1            # absorbs 1e-6 degree rounding of shapes
APP_GAP_MARGIN_M = 10        # slack on the app's search cutoff
LOCATE_TIE_M = 1.0           # /locate edges this close to the nearest one count as "the" edge
MIN_EDGES = 10_000           # a Georgia car graph has far more; raise it after the first green run

# The baseline the test data may never fall below. Raising a count is fine;
# lowering one needs a reviewed change to this file.
MIN_TESTS = {"must_fail": 35, "must_succeed": 9, "inside_no_go": 10, "band_checks": 7, "seasonal": 1}

# Road classes the soft band may exempt (Valhalla's names for them).
MAIN_CLASSES = ("motorway", "trunk", "primary", "secondary")

EDGE_ROW_SEPARATOR = b"\x1e"  # the workflow passes --row $'\x1e' to valhalla_export_edges
EDGE_COLUMN_SEPARATOR = b"\0"


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------

def decode_polyline(encoded, precision=6):
    """Decode a Google/Valhalla encoded polyline (str or bytes) into
    parallel lists of lon and lat."""
    data = encoded.encode() if isinstance(encoded, str) else encoded
    factor = 10.0 ** precision
    lons, lats = [], []
    index, lat, lon, n = 0, 0, 0, len(data)
    while index < n:
        for which in (0, 1):
            result, shift = 0, 0
            while True:
                b = data[index] - 63
                index += 1
                result |= (b & 0x1F) << shift
                shift += 5
                if b < 0x20:
                    break
            delta = ~(result >> 1) if result & 1 else result >> 1
            if which == 0:
                lat += delta
            else:
                lon += delta
        lats.append(lat / factor)
        lons.append(lon / factor)
    return lons, lats


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def exempt_classes(config):
    """Main road classes the soft band leaves free (not in its class list)."""
    band = set(config.get("soft_band", {}).get("highway_classes", []))
    return tuple(c for c in MAIN_CLASSES if c not in band)


class Checker:
    """Geometry checks shared by the edge scan and the route tests."""

    def __init__(self, config):
        import pyproj
        checks = build_zones(config, hard_margin_m=HARD_MARGIN_M,
                             country_margin_m=EDGE_COUNTRY_MARGIN_M)
        self.hard = checks.hard                  # what results are checked against
        self.edge_country = checks.georgia
        self.route_country = build_zones(config, country_margin_m=ROUTE_COUNTRY_MARGIN_M).georgia
        self.exact = build_zones(config)         # for checking the test data itself
        shapely.prepare(self.route_country)
        self.to_m = pyproj.Transformer.from_crs(
            "EPSG:4326", config["metric_crs"], always_xy=True).transform
        self.occupied_m = transform(self.to_m, self.exact.occupied)
        self.georgia_m = transform(self.to_m, self.exact.georgia)
        self.hard_m = transform(self.to_m, self.exact.hard)
        self._legal_m = None
        self.exempt = exempt_classes(config)

    def in_hard(self, lat, lon):
        return bool(shapely.intersects_xy(self.exact.hard, lon, lat))

    def in_georgia(self, lat, lon):
        return bool(shapely.intersects_xy(self.exact.georgia, lon, lat))

    def in_band(self, lat, lon):
        band = self.exact.band_outer
        return band is not None and bool(shapely.intersects_xy(band, lon, lat))

    def point_m(self, lat, lon):
        return Point(*self.to_m(lon, lat))

    def line_distance_m(self, lat, lon):
        """Distance to the occupied line as drawn (0 inside)."""
        return self.occupied_m.distance(self.point_m(lat, lon))

    def border_distance_m(self, lat, lon):
        """Distance to Georgia's border, from either side."""
        return self.georgia_m.boundary.distance(self.point_m(lat, lon))

    def legal_gap_m(self, lat, lon):
        """Distance to the nearest ground inside Georgia and outside the
        no-go area, where a kept road could lie."""
        if self._legal_m is None:
            self._legal_m = self.georgia_m.difference(self.hard_m)
        return self._legal_m.distance(self.point_m(lat, lon))

    def shape_problems(self, lons, lats):
        """Why a route shape is not acceptable, or an empty list."""
        problems = []
        if len(lons) >= 2:
            if shapely.intersects(self.hard, LineString(zip(lons, lats))):
                problems.append("enters the no-go area")
        elif len(lons) == 1 and self.in_hard(lats[0], lons[0]):
            problems.append("ends in the no-go area")
        outside = ~shapely.intersects_xy(self.route_country, np.array(lons), np.array(lats))
        if outside.any():
            i = int(np.argmax(outside))
            problems.append(f"leaves Georgia near {lats[i]:.5f},{lons[i]:.5f}")
        return problems

    def distance_m(self, lons, lats, lat, lon):
        """Shortest distance in metres from a point to a route shape."""
        xs, ys = self.to_m(np.array(lons), np.array(lats))
        px, py = self.to_m(lon, lat)
        line = LineString(zip(xs, ys)) if len(xs) > 1 else Point(xs[0], ys[0])
        return line.distance(Point(px, py))


# ---------------------------------------------------------------------------
# 1. Edge scan
# ---------------------------------------------------------------------------

def read_rows(path, separator=EDGE_ROW_SEPARATOR, chunk=1 << 20):
    """Yield the rows of a file split by a one-byte separator, streaming."""
    buffer = b""
    with open(path, "rb") as f:
        while True:
            block = f.read(chunk)
            if not block:
                break
            buffer += block
            parts = buffer.split(separator)
            buffer = parts.pop()
            yield from parts
    if buffer:
        yield buffer


def scan_edges(path, checker, batch_size=20000):
    """Read valhalla_export_edges output (rows split by 0x1E, columns by NUL:
    encoded shape, then street names) and check every edge."""
    shapely.prepare(checker.edge_country)
    counts = {"edges": 0, "points": 0, "no_go_edges": 0, "outside_edges": 0, "bad_rows": 0}
    examples = {"no_go_edges": [], "outside_edges": [], "bad_rows": []}
    xs, ys, owner, names = [], [], [], []

    def check_batch():
        if not names:
            return
        x, y, idx = np.array(xs), np.array(ys), np.array(owner)
        lines = shapely.linestrings(np.column_stack([x, y]), indices=idx)
        hits = np.nonzero(shapely.intersects(checker.hard, lines))[0]
        counts["no_go_edges"] += len(hits)
        for i in hits[:max(0, 20 - len(examples["no_go_edges"]))]:
            first = int(np.argmax(idx == i))
            examples["no_go_edges"].append({"names": names[i],
                                            "first_point": [float(y[first]), float(x[first])]})
        # Whole lines, not just their points: a long edge between two
        # points inside Georgia can still cut across a bend of the border.
        outside = np.nonzero(~shapely.covers(checker.edge_country, lines))[0]
        counts["outside_edges"] += len(outside)
        for i in outside[:max(0, 20 - len(examples["outside_edges"]))]:
            first = int(np.argmax(idx == i))
            examples["outside_edges"].append({"names": names[i],
                                              "first_point": [float(y[first]), float(x[first])]})
        xs.clear(), ys.clear(), owner.clear(), names.clear()

    for raw in read_rows(path):
        cols = raw.split(EDGE_COLUMN_SEPARATOR)
        if not cols[0].strip():
            continue
        try:
            lons, lats = decode_polyline(cols[0].strip())
        except (IndexError, ValueError):
            counts["bad_rows"] += 1
            if len(examples["bad_rows"]) < 5:
                examples["bad_rows"].append(raw[:80].decode("utf-8", "replace"))
            continue
        if len(lons) < 2:
            continue
        owner.extend([len(names)] * len(lons))
        xs.extend(lons)
        ys.extend(lats)
        names.append([c.decode("utf-8", "replace") for c in cols[1:4]])
        counts["edges"] += 1
        counts["points"] += len(lons)
        if len(names) >= batch_size:
            check_batch()
    check_batch()

    problems = []
    if counts["edges"] < MIN_EDGES:
        problems.append(f"only {counts['edges']} edges exported; the tile set looks empty or broken "
                        "(or the export was not run with --row $'\\x1e')")
    if counts["no_go_edges"]:
        problems.append(f"{counts['no_go_edges']} edge(s) touch the no-go area, e.g. "
                        f"{examples['no_go_edges'][:3]}")
    if counts["outside_edges"]:
        problems.append(f"{counts['outside_edges']} edge(s) not wholly inside Georgia, e.g. "
                        f"{examples['outside_edges'][:3]}")
    if counts["bad_rows"]:
        problems.append(f"{counts['bad_rows']} unparseable rows, e.g. {examples['bad_rows'][:2]}")
    return {"name": "edge scan", "passed": not problems, "problems": problems,
            **counts, "examples": examples}


# ---------------------------------------------------------------------------
# 2-6. Service tests
# ---------------------------------------------------------------------------

class Valhalla:
    def __init__(self, url):
        self.url = url.rstrip("/")

    def _post(self, action, request, timeout=180):
        """POST a request. Returns (code, body): code 0 for an answer, a
        Valhalla 'no route' code, or raises on anything else."""
        req = urllib.request.Request(self.url + "/" + action, data=json.dumps(request).encode(),
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return 0, json.load(resp)
        except urllib.error.HTTPError as exc:
            payload = exc.read()
            try:
                answer = json.loads(payload)
            except ValueError:
                raise RuntimeError(f"HTTP {exc.code} without JSON: {payload[:200]!r}")
            code = answer.get("error_code")
            if exc.code == 400 and code in NO_ROUTE_CODES:
                return code, answer
            raise RuntimeError(f"Valhalla error {code}: {answer.get('error')}")

    def status(self, verbose=False):
        if not verbose:
            with urllib.request.urlopen(self.url + "/status", timeout=30) as resp:
                return json.load(resp)
        code, body = self._post("status", {"verbose": True}, timeout=120)
        if code:
            raise RuntimeError(f"status answered {code}")
        return body

    def locate(self, lat, lon, search_cutoff=None):
        location = {"lat": lat, "lon": lon}
        if search_cutoff:
            location["search_cutoff"] = search_cutoff
        code, body = self._post("locate", {"locations": [location], "costing": "auto", "verbose": True})
        if code:
            raise RuntimeError(f"locate answered {code}")
        return body

    def route(self, a, b, date=None, bidirectional=True, target_cutoff=None,
              costing="auto", alternates=2, date_type=None):
        """Ask for a car route. Returns (code, body) as _post does.
        date_type 0 means 'leave now' (what the app sends); a date string
        means 'depart at' that local time."""
        target = {"lat": b["lat"], "lon": b["lon"]}
        if target_cutoff:
            target["search_cutoff"] = target_cutoff
        request = {
            "locations": [{"lat": a["lat"], "lon": a["lon"]}, target],
            "costing": costing,
            "alternates": alternates,
            "directions_type": "none",
            "prioritize_bidirectional": bidirectional,
        }
        if date:
            request["date_time"] = {"type": 1, "value": date}  # depart at, local time
        elif date_type is not None:
            request["date_time"] = {"type": date_type}
        return self._post("route", request)

    def app_route(self, a, b, app):
        """The request the app sends (gate_routes.json app_request)."""
        return self.route(a, b, bidirectional=app["prioritize_bidirectional"],
                          target_cutoff=app["search_cutoff_m"], costing=app["costing"],
                          alternates=app["alternates"], date_type=app["date_time_type"])


def trips(body):
    """The main trip and every alternate, as (length_km, lons, lats)."""
    out = []
    for trip in [body["trip"]] + [alt["trip"] for alt in body.get("alternates", [])]:
        lons, lats = [], []
        for leg in trip["legs"]:
            lo, la = decode_polyline(leg["shape"])
            lons += lo
            lats += la
        out.append((trip["summary"]["length"], lons, lats))
    return out


def located_edges(answer):
    """The edges /locate put the point on (all within LOCATE_TIE_M of the closest)."""
    edges = (answer[0].get("edges") or []) if answer else []
    if not edges:
        return []
    nearest = min(e.get("distance", 0.0) for e in edges)
    return [e for e in edges if e.get("distance", 0.0) <= nearest + LOCATE_TIE_M]


def free_edges(edges, exempt):
    """Edges a through-route may use freely although they lie in the band."""
    bad = []
    for e in edges:
        info = e.get("edge", {})
        cls = info.get("classification", {}).get("classification")
        if not info.get("destination_only") and cls not in exempt:
            bad.append(f"way {e.get('edge_info', {}).get('way_id')} ({cls})")
    return bad


def end_problems(label, found, v, checker):
    """Where a route that should not exist, but may, ends: record the
    distance to the line; in the band, the end edge must be destination-only."""
    problems, detail = [], {}
    _, lons, lats = found[0]
    end_lat, end_lon = lats[-1], lons[-1]
    detail[f"{label}_end_to_line_m"] = round(checker.line_distance_m(end_lat, end_lon))
    if checker.in_band(end_lat, end_lon):
        edges = located_edges(v.locate(end_lat, end_lon, search_cutoff=STRICT_SNAP_M))
        if not edges:
            problems.append(f"{label}: /locate finds no edge at the route's end {end_lat:.6f},{end_lon:.6f}")
        bad = free_edges(edges, checker.exempt)
        if bad:
            problems.append(f"{label}: ends in the 100-500 m band at {end_lat:.6f},{end_lon:.6f} on a road "
                            f"open to through traffic: {', '.join(bad)}")
    return problems, detail


def run_must_succeed(test, places, dates, v, checker):
    problems, detail = [], {}
    a, b = places[test["from"]], places[test["to"]]
    for label, date in dates:
        code, body = v.route(a, b, date=date)
        if code:
            problems.append(f"{label}: no route ({code} {NO_ROUTE_CODES[code]})")
            continue
        found = trips(body)
        detail[label] = {"km": round(found[0][0], 1), "alternates": len(found) - 1}
        if found[0][0] > test["max_km"]:
            problems.append(f"{label}: {found[0][0]:.0f} km, limit {test['max_km']} km")
        for n, (_, lons, lats) in enumerate(found):
            for p in checker.shape_problems(lons, lats):
                problems.append(f"{label}: {'route' if n == 0 else f'alternate {n}'} {p}")
        for spot in test.get("must_pass_near", []):
            d = checker.distance_m(found[0][1], found[0][2], spot["lat"], spot["lon"])
            if d > spot["within_m"]:
                problems.append(f"{label}: misses the check point {spot['lat']},{spot['lon']} "
                                f"by {d:.0f} m (limit {spot['within_m']} m)")
    return problems, detail


def target_of(test, places):
    return test["to"] if isinstance(test["to"], dict) else places[test["to"]]


def run_must_fail(test, places, v, checker, app, raw_roads):
    problems, detail = [], {}
    a = places[test["from"]]
    b = target_of(test, places)

    def shapes(label, found):
        for n, (_, lons, lats) in enumerate(found):
            for p in checker.shape_problems(lons, lats):
                problems.append(f"{label}: {'route' if n == 0 else f'alternate {n}'} {p}")

    # control: the origin reaches the approach road
    control = test.get("control")
    if not control:
        problems.append("no control leg: the test could pass only because the origin does not route")
    else:
        code, body = v.route(a, control, target_cutoff=STRICT_SNAP_M)
        if code:
            problems.append(f"control: no route to {control['name']} ({code} {NO_ROUTE_CODES[code]}), "
                            "so this test proves nothing")
        else:
            found = trips(body)
            detail["control"] = f"route of {found[0][0]:.1f} km"
            shapes("control", found)

    # strict: no road within STRICT_SNAP_M of the target
    if raw_roads is not None:
        raw = raw_roads.get(test["id"]) or {}
        detail["raw_road_m"] = raw.get("raw_road_m")
        if raw.get("raw_road_m") is None or raw["raw_road_m"] > STRICT_SNAP_M:
            problems.append(f"strict: the input extract had no road within {STRICT_SNAP_M} m of the target "
                            f"(nearest {raw.get('raw_road_m')} m), so the strict test proves nothing; "
                            "move the target onto the road")
    code, _ = v.route(a, b, target_cutoff=STRICT_SNAP_M)
    detail["strict"] = f"no route ({code})" if code else "ROUTE FOUND"
    if not code:
        problems.append(f"strict: a road exists within {STRICT_SNAP_M} m of the target")

    # loose: Valhalla's default snapping
    code, body = v.route(a, b)
    if code:
        detail["loose"] = f"no route ({code})"
    else:
        found = trips(body)
        detail["loose"] = f"snapped to the nearest road, route of {found[0][0]:.1f} km"
        shapes("loose", found)
        more, info = end_problems("loose", found, v, checker)
        problems += more
        detail.update(info)

    # app: the app's own request
    gap = checker.legal_gap_m(b["lat"], b["lon"])
    detail["legal_ground_m"] = round(gap)
    code, body = v.app_route(a, b, app)
    if code:
        detail["app"] = f"no route ({code})"
    else:
        found = trips(body)
        detail["app"] = f"route of {found[0][0]:.1f} km"
        if gap > app["search_cutoff_m"] + APP_GAP_MARGIN_M:
            problems.append(f"app: a route was found although the nearest legal ground is {gap:.0f} m from "
                            f"the target, beyond the app's {app['search_cutoff_m']} m search cutoff")
        shapes("app", found)
        more, info = end_problems("app", found, v, checker)
        problems += more
        detail.update(info)
    return problems, detail


def run_band_check(test, v, checker):
    edges = located_edges(v.locate(test["lat"], test["lon"], search_cutoff=STRICT_SNAP_M))
    if not edges:
        return [f"/locate finds no car edge within {STRICT_SNAP_M} m; the way moved or was deleted: "
                "pick a new point on the same road"], {}
    detail = {"edges": [f"way {e.get('edge_info', {}).get('way_id')} "
                        f"{e.get('edge', {}).get('classification', {}).get('classification')} "
                        f"destination_only={e.get('edge', {}).get('destination_only')}" for e in edges]}
    bad = free_edges(edges, ())
    return ([f"open to through traffic in the band: {', '.join(bad)}"] if bad else []), detail


def run_seasonal(test, places, v, checker):
    problems, detail = [], {}
    a, b, road = places[test["from"]], places[test["to"]], test["road"]
    # The app sends prioritize_bidirectional=true; Valhalla's other search
    # (one direction, time-aware) is checked too in case that flag is lost.
    for bidirectional in (True, False):
        label = f"closed, {'bidirectional' if bidirectional else 'one-direction'} search"
        code, body = v.route(a, b, date=test["closed_date"], bidirectional=bidirectional)
        if code:
            detail[label] = f"no route ({code})"
            continue
        for n, (km, lons, lats) in enumerate(trips(body)):
            d = checker.distance_m(lons, lats, road["lat"], road["lon"])
            if n == 0:
                detail[label] = f"route {km:.1f} km, {d:.0f} m from the seasonal road"
            if d <= road["within_m"]:
                # Valhalla 3.6.3 never applies a month rule on a road whose
                # tile has no time zone (baldr/datetime.cc returns early), so
                # this usually means timezones.sqlite was missing at build time.
                problems.append(f"{label}: {'route' if n == 0 else f'alternate {n}'} uses the "
                                f"seasonal road on {test['closed_date']} (were time zones built?)")
    code, body = v.route(a, b, date=test["open_date"])
    if code:
        problems.append(f"open date {test['open_date']}: no route, so the test proves nothing")
    else:
        km, lons, lats = trips(body)[0]
        d = checker.distance_m(lons, lats, road["lat"], road["lon"])
        detail["open"] = f"route {km:.1f} km, {d:.0f} m from the seasonal road"
        if d > road["within_m"]:
            problems.append(f"open date: route does not use the seasonal road ({d:.0f} m away)")
    return problems, detail


def check_counts(data):
    """The test data may never shrink below the reviewed baseline."""
    problems = []
    for key, minimum in MIN_TESTS.items():
        have = len(data.get(key, []))
        if have < minimum:
            problems.append(f"{key} has {have} tests, the baseline is {minimum}")
    return problems


def check_test_data(data, checker):
    """The test points themselves must be where the tests assume."""
    problems = []
    places = data["places"]
    for t in data["must_succeed"]:
        for key in (t["from"], t["to"]):
            p = places[key]
            if checker.in_hard(p["lat"], p["lon"]) or not checker.in_georgia(p["lat"], p["lon"]):
                problems.append(f"{t['id']}: {key} is not a legal destination")
    ids = [t["id"] for t in data["must_fail"]]
    if len(ids) != len(set(ids)):
        problems.append("must_fail test ids are not unique")
    for t in data["must_fail"]:
        p = target_of(t, places)
        if not checker.in_hard(p["lat"], p["lon"]) and checker.in_georgia(p["lat"], p["lon"]):
            problems.append(f"{t['id']}: target is neither in the no-go area nor outside Georgia")
        src = places[t["from"]]
        if checker.in_hard(src["lat"], src["lon"]):
            problems.append(f"{t['id']}: start point is inside the no-go area")
        c = t.get("control")
        if not c:
            problems.append(f"{t['id']}: no control leg")
            continue
        if not checker.in_georgia(c["lat"], c["lon"]):
            problems.append(f"{t['id']}: control point is outside Georgia")
        line = checker.line_distance_m(c["lat"], c["lon"])
        border = checker.border_distance_m(c["lat"], c["lon"])
        if min(line, border) < CONTROL_MIN_M - 1:
            problems.append(f"{t['id']}: control point is {min(line, border):.0f} m from the line or "
                            f"border, less than {CONTROL_MIN_M} m")
    for p in data.get("inside_no_go", []):
        if not checker.in_hard(p["lat"], p["lon"]):
            problems.append(f"{p['name']} is not inside the no-go area")
    for b in data.get("band_checks", []):
        if checker.in_hard(b["lat"], b["lon"]) or not checker.in_band(b["lat"], b["lon"]):
            problems.append(f"{b['id']}: not in the 100-500 m band")
    return problems


def check_app_zones(path, data, config):
    """The published nogo_zones.geojson is what the app uses to refuse a
    destination before routing. It must refuse every must-fail target and
    allow every legal test point."""
    collection = json.loads(Path(path).read_text(encoding="utf-8"))
    parts = {f["properties"]["name"]: f for f in collection["features"]}
    problems = []
    for name in ("no_go_hard", "georgia", "soft_band_outer"):
        if name not in parts:
            problems.append(f"{path} has no '{name}' area")
        elif parts[name]["properties"].get("clip_config_version") != config["version"]:
            problems.append(f"{path}: '{name}' is from clip config "
                            f"v{parts[name]['properties'].get('clip_config_version')}")
    if problems:
        return problems, {}
    hard = shape(parts["no_go_hard"]["geometry"])
    georgia = shape(parts["georgia"]["geometry"])
    band = shape(parts["soft_band_outer"]["geometry"])
    for geom in (hard, georgia, band):
        shapely.prepare(geom)

    def refused(p):
        return (bool(shapely.intersects_xy(hard, p["lon"], p["lat"]))
                or not bool(shapely.intersects_xy(georgia, p["lon"], p["lat"])))

    places = data["places"]
    for t in data["must_fail"]:
        if not refused(target_of(t, places)):
            problems.append(f"{t['id']}: the app's zones would let a driver pick this target")
        if t.get("control") and refused(t["control"]):
            problems.append(f"{t['id']}: the app's zones would refuse the legal control point")
    for t in data["must_succeed"]:
        for key in (t["from"], t["to"]):
            if refused(places[key]):
                problems.append(f"{t['id']}: the app's zones would refuse {key}")
    for b in data.get("band_checks", []):
        if refused(b) or not bool(shapely.intersects_xy(band, b["lon"], b["lat"])):
            problems.append(f"{b['id']}: the app's zones would not ask for confirmation there")
    return problems, {"must_fail_refused": len(data["must_fail"])}


# ---------------------------------------------------------------------------

def main(argv=None):
    parser = argparse.ArgumentParser(description="Safety gate for the Georgia routing tiles.")
    parser.add_argument("--url", default="http://127.0.0.1:8002")
    parser.add_argument("--config", default=str(ROOT / "config" / "clip.json"))
    parser.add_argument("--routes", default=str(DEFAULT_ROUTES))
    parser.add_argument("--edges", help="valhalla_export_edges output; required unless --skip-edges")
    parser.add_argument("--skip-edges", action="store_true", help="only for local experiments")
    parser.add_argument("--tar", help="the tile tar under test; its SHA-256 goes into the results")
    parser.add_argument("--zones", help="nogo_zones.geojson written by clip.py (published for the app)")
    parser.add_argument("--clip-report", help="clip_report.json written by clip.py")
    parser.add_argument("--results", help="write the full results as JSON here")
    parser.add_argument("--expect-version", help="fail unless /status reports this Valhalla version")
    args = parser.parse_args(argv)

    config = load_config(args.config)
    data = json.loads(Path(args.routes).read_text(encoding="utf-8"))
    checker = Checker(config)
    results = []
    summary = {"valhalla_version": None, "tar_sha256": None, "edges": None,
               "clip_config_version": config["version"]}

    def record(name, problems, detail=None):
        results.append({"name": name, "passed": not problems, "problems": problems,
                        "detail": detail or {}})
        mark = "PASS" if not problems else "FAIL"
        print(f"{mark}  {name}" + ("" if not problems else "\n      " + "\n      ".join(problems)))

    def guarded(name, func):
        try:
            problems, detail = func()
        except Exception as exc:  # a crash is a failure, never a pass
            problems, detail = [f"error: {exc}"], {}
        record(name, problems, detail)

    if args.tar:
        summary["tar_sha256"] = sha256_file(args.tar)
        print(f"tar under test: sha256 {summary['tar_sha256']}")
    else:
        record("tar under test", ["--tar is required, so manifest.py can match the published file"])

    if data.get("clip_config_version") != config["version"]:
        record("test data matches clip config",
               [f"gate_routes.json was made for clip config v{data.get('clip_config_version')}, "
                f"config is v{config['version']}; re-check the stub points"])
    record("test data size", check_counts(data))
    guarded("test data sanity", lambda: (check_test_data(data, checker), {}))
    if args.zones:
        guarded("app pre-check (nogo_zones.geojson)", lambda: check_app_zones(args.zones, data, config))
    else:
        record("app pre-check (nogo_zones.geojson)", ["--zones is required"])
    raw_roads = None
    if args.clip_report:
        report = json.loads(Path(args.clip_report).read_text(encoding="utf-8"))
        raw_roads = report.get("must_fail_targets", {})
        if report.get("clip_config_version") != config["version"]:
            record("clip report matches clip config", [f"clip_report.json is from clip config "
                                                        f"v{report.get('clip_config_version')}"])
    else:
        record("raw road distances", ["--clip-report is required: without it a strict test cannot "
                                      "show that a road was there to remove"])

    if args.skip_edges:
        print("WARN  edge scan skipped (--skip-edges); never use this in CI")
    elif not args.edges:
        record("edge scan", ["--edges is required (or --skip-edges for local tests)"])
    else:
        started = time.time()
        try:
            edge_result = scan_edges(args.edges, checker)
        except Exception as exc:
            edge_result = {"name": "edge scan", "passed": False, "problems": [f"error: {exc}"], "edges": 0}
        edge_result["seconds"] = round(time.time() - started, 1)
        results.append(edge_result)
        summary["edges"] = edge_result.get("edges")
        print(f"{'PASS' if edge_result['passed'] else 'FAIL'}  edge scan: {edge_result.get('edges')} edges, "
              f"{edge_result.get('points')} points" +
              ("" if edge_result["passed"] else "\n      " + "\n      ".join(edge_result["problems"])))

    v = Valhalla(args.url)
    service_ok = True
    try:
        status = v.status(verbose=True)
        version = str(status.get("version"))
        summary["valhalla_version"] = version
        if "has_tiles" not in status:
            problems = ["verbose /status is off: build_tiles.sh must set "
                        "--service-limits-status-allow-verbose True"]
        else:
            problems = [f"{k} is {status.get(k)!r}" for k in ("has_tiles", "has_admins", "has_timezones")
                        if status.get(k) is not True]
        record("valhalla status", problems,
               {k: status.get(k) for k in ("has_tiles", "has_admins", "has_timezones", "has_live_traffic")})
        if args.expect_version:
            # Release images may add the git commit, e.g. "3.6.3-e2f017b".
            ok = version == args.expect_version or version.startswith(args.expect_version + "-")
            record(f"valhalla version {version}",
                   [] if ok else [f"service reports {version}, expected {args.expect_version}"])
    except Exception as exc:
        service_ok = False
        record("valhalla status", [f"the service did not answer /status: {exc}"])

    if service_ok:
        places = data["places"]
        app = data["app_request"]
        dates = [("July", data["summer_date"]), ("January", data["winter_date"])]
        groups = [
            ("must succeed", data["must_succeed"], lambda t: run_must_succeed(t, places, dates, v, checker)),
            ("must fail", data["must_fail"], lambda t: run_must_fail(t, places, v, checker, app, raw_roads)),
            ("band", data.get("band_checks", []), lambda t: run_band_check(t, v, checker)),
            ("seasonal", data["seasonal"], lambda t: run_seasonal(t, places, v, checker)),
        ]
        for group, tests, runner in groups:
            for test in tests:
                guarded(f"{group}: {test['id']}", lambda t=test, r=runner: r(t))

    failed = [r for r in results if not r["passed"]]
    summary.update({"passed": not failed, "checks": len(results), "failed": len(failed),
                    "results": results})
    if args.results:
        Path(args.results).write_text(json.dumps(summary, indent=2, ensure_ascii=False),
                                      encoding="utf-8")
    print(f"gate: {len(results) - len(failed)}/{len(results)} checks passed "
          f"(Valhalla {summary['valhalla_version']})")
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
