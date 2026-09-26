#!/usr/bin/env python3
"""Geocoder gate: prove georgia_geocoder.sqlite is sound before it ships.

The release happens only if every check passes:

1. Meta. The database was built from the committed fold and config (the
   exact files, compared byte for byte), from the clipped extract the tiles
   were built from (sha256 from the tiles' manifest), and carries the ODbL
   notice and attribution.
2. Fold. The shared test vectors of config/geocoder_fold.json pass.
3. Counts. Rows per table and per kind lie in the bands of
   config/geocoder_gate.json, the full-text indexes cover the rows, and no
   table shrank by more than max_drop_vs_previous against the last release.
4. Zones, checked with shapely (independent of the builder's own
   point-in-polygon code) against the published nogo_zones.geojson: no
   street, address or POI inside no_go_hard or outside Georgia; every place
   inside Georgia, and occupied=1 exactly for the places inside no_go_hard.
   No street, address or POI has an occupied settlement as its city, and no
   legal district an occupied parent.
5. Labels. Every occupied place is labelled from name:ka (label_ka =
   name:ka, label_en its national romanisation), never from name or
   name:en, which there are the de facto authorities' forms.
6. Queries. Each known query (Georgian, Latin, chat Latin, Russian,
   Mtavruli; places, occupied places, streets, addresses, POIs, categories,
   pasted addresses with country and postcode, postpositions) returns the
   right top hit (or, with "top": n, one of the first n) near the right
   coordinates, through the reference search (geocoder_search.py) the app
   copies. No result of any query is a street, address or POI inside
   no_go_hard, and every place inside it is flagged and not routable.
7. Speed. No query takes longer than max_query_ms.

Usage (as in the workflow):
  python scripts/geocoder_gate.py --db build/geocoder/georgia_geocoder.sqlite \
      --zones build/nogo_zones.geojson --expect-source-sha256 <clipped pbf sha256> \
      --results build/geocoder/gate_results.json [--previous-manifest build/previous_manifest.json]
"""

import argparse
import json
import sqlite3
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from geocoder_build import (DEFAULT_CONFIG, SCHEMA_VERSION, TABLES,  # noqa: E402
                            distance_m, sha256_file)
from geocoder_fold import DEFAULT_SPEC as DEFAULT_FOLD_SPEC, Fold, check_vectors, romanise  # noqa: E402
from geocoder_search import Searcher, label  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_GATE = ROOT / "config" / "geocoder_gate.json"
BOUNDARY_TOLERANCE_DEG = 1e-5   # about 1 m: a point this close to the line may fall either way


def check_meta(con, fold_path, config_path, expect_sha=None, expect_tag=None):
    meta = {k: v for k, v in con.execute("SELECT key, value FROM meta")}
    problems = []
    if meta.get("schema_version") != str(SCHEMA_VERSION):
        problems.append(f"schema_version {meta.get('schema_version')!r}, expected {SCHEMA_VERSION}")
    if meta.get("fold_json") != Path(fold_path).read_text(encoding="utf-8"):
        problems.append("the database was built with another fold than config/geocoder_fold.json")
    if meta.get("config_json") != Path(config_path).read_text(encoding="utf-8"):
        problems.append("the database was built with another config than config/geocoder.json")
    if meta.get("licence") != "ODbL-1.0" or "OpenStreetMap contributors" not in meta.get("attribution", ""):
        problems.append("the ODbL licence or the OpenStreetMap attribution is missing from meta")
    if "ODbL" not in meta.get("notice", ""):
        problems.append("the ODbL notice is missing from meta")
    if expect_sha and meta.get("source_pbf_sha256") != expect_sha:
        problems.append(f"built from an extract with sha256 {meta.get('source_pbf_sha256')!r}, "
                        f"but the tiles were built from {expect_sha}")
    if expect_tag and meta.get("release_tag") != expect_tag:
        problems.append(f"release tag {meta.get('release_tag')!r}, expected {expect_tag!r}")
    if meta.get("fts") not in ("fts5", "fts4"):
        problems.append(f"unknown full-text engine {meta.get('fts')!r}")
    return problems, meta


def db_counts(con):
    counts = {t: con.execute(f"SELECT count(*) FROM {t}").fetchone()[0] for t in TABLES}
    counts["places_occupied"] = con.execute("SELECT count(*) FROM places WHERE occupied = 1").fetchone()[0]
    counts["streets_virtual"] = con.execute("SELECT count(*) FROM streets WHERE kind = 'virtual'").fetchone()[0]
    kinds = {f"{t}.{k}": n for t in ("places", "pois")
             for k, n in con.execute(f"SELECT kind, count(*) FROM {t} GROUP BY kind")}
    return counts, kinds


def check_counts(counts, kinds, indexed, gate):
    problems = []
    for key, (lo, hi) in gate["counts"].items():
        n = counts.get(key, 0)
        if not lo <= n <= hi:
            problems.append(f"{key}: {n} rows, expected {lo} to {hi}")
    for key, (lo, hi) in gate["kinds"].items():
        n = kinds.get(key, 0)
        if not lo <= n <= hi:
            problems.append(f"{key}: {n}, expected {lo} to {hi}")
    for table, share in gate["indexed_share"].items():
        n = counts.get(table, 0)
        if n and indexed.get(table, 0) < share * n:
            problems.append(f"{table}: only {indexed.get(table, 0)} of {n} rows are in the search index")
    return problems


def check_previous(counts, previous_path, max_drop):
    if not previous_path or not Path(previous_path).exists():
        return [], "no previous release to compare with"
    previous = json.loads(Path(previous_path).read_text(encoding="utf-8"))
    before = ((previous.get("geocoder") or {}).get("counts")) or {}
    if not before:
        return [], f"previous release {previous.get('tag')} has no geocoder"
    problems = []
    for table in TABLES:
        if before.get(table) and counts.get(table, 0) < (1 - max_drop) * before[table]:
            problems.append(f"{table}: {counts.get(table, 0)} rows against {before[table]} in "
                            f"{previous.get('tag')}; more than {max_drop:.0%} fewer")
    return problems, f"compared with {previous.get('tag')}"


def indexed_counts(con, meta):
    stored = json.loads(meta.get("counts", "{}"))
    return {t: stored.get(f"{t}_indexed", 0) for t in TABLES}


class ShapelyZones:
    """The published zones through shapely: an implementation independent
    of the builder's own point-in-polygon code."""

    def __init__(self, path):
        import numpy as np
        import shapely
        from shapely.geometry import shape
        self.np, self.shapely = np, shapely
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        geoms = {f["properties"]["name"]: shape(f["geometry"]) for f in data["features"]}
        self.hard = geoms["no_go_hard"]
        self.georgia = geoms["georgia"]
        shapely.prepare(self.hard)
        shapely.prepare(self.georgia)

    def flags(self, lons, lats):
        x = self.np.asarray(lons, dtype=float)
        y = self.np.asarray(lats, dtype=float)
        return (self.shapely.contains_xy(self.hard, x, y), self.shapely.contains_xy(self.georgia, x, y))

    def near_line(self, geom, lon, lat):
        return self.shapely.distance(geom.boundary, self.shapely.points(lon, lat)) < BOUNDARY_TOLERANCE_DEG


def check_zones(con, zones):
    problems, detail = [], {}
    for table in ("streets", "addresses", "pois"):
        rows = con.execute(f"SELECT id, lon, lat FROM {table}").fetchall()
        if not rows:
            continue
        in_hard, in_georgia = zones.flags([r[1] for r in rows], [r[2] for r in rows])
        bad_hard = [rows[i] for i in zones.np.nonzero(in_hard)[0]
                    if not zones.near_line(zones.hard, rows[i][1], rows[i][2])]
        bad_out = [rows[i] for i in zones.np.nonzero(~in_georgia)[0]
                   if not zones.near_line(zones.georgia, rows[i][1], rows[i][2])]
        detail[table] = {"rows": len(rows), "inside_no_go": len(bad_hard), "outside_georgia": len(bad_out)}
        if bad_hard:
            problems.append(f"{table}: {len(bad_hard)} rows inside no_go_hard, e.g. ids {[r[0] for r in bad_hard[:5]]}")
        if bad_out:
            problems.append(f"{table}: {len(bad_out)} rows outside Georgia, e.g. ids {[r[0] for r in bad_out[:5]]}")
    rows = con.execute("SELECT id, lon, lat, occupied FROM places").fetchall()
    if rows:
        in_hard, in_georgia = zones.flags([r[1] for r in rows], [r[2] for r in rows])
        wrong_flag = [r for r, h in zip(rows, in_hard) if bool(r[3]) != bool(h)
                      and not zones.near_line(zones.hard, r[1], r[2])]
        outside = [r for r, g in zip(rows, in_georgia) if not g and not zones.near_line(zones.georgia, r[1], r[2])]
        detail["places"] = {"rows": len(rows), "occupied": int(sum(1 for r in rows if r[3])),
                            "inside_no_go": int(in_hard.sum()), "wrong_flag": len(wrong_flag),
                            "outside_georgia": len(outside)}
        if wrong_flag:
            problems.append(f"places: {len(wrong_flag)} have occupied set wrong, e.g. ids {[r[0] for r in wrong_flag[:5]]}")
        if outside:
            problems.append(f"places: {len(outside)} outside Georgia, e.g. ids {[r[0] for r in outside[:5]]}")
    problems.extend(check_occupied_links(con, detail))
    return problems, detail


def check_occupied_links(con, detail=None):
    """No legal row may belong to an occupied settlement."""
    problems = []
    for table in ("streets", "addresses", "pois"):
        ids = [r[0] for r in con.execute(f"SELECT x.id FROM {table} x JOIN places p ON p.id = x.city_id "
                                         "WHERE p.occupied = 1 LIMIT 5")]
        if detail is not None:
            detail.setdefault(table, {})["city_occupied"] = len(ids)
        if ids:
            problems.append(f"{table}: rows whose city is an occupied place, e.g. ids {ids}")
    ids = [r[0] for r in con.execute("SELECT c.id FROM places c JOIN places p ON p.id = c.parent_id "
                                     "WHERE c.occupied = 0 AND p.occupied = 1 LIMIT 5")]
    if ids:
        problems.append(f"places: legal districts with an occupied parent, e.g. ids {ids}")
    return problems


def check_labels(con, config, fold_spec):
    """Occupied places are shown only by their Georgian name."""
    policy = config["places"].get("occupied_without_name_ka", "hide")
    problems, bad = [], []
    rows = con.execute("SELECT id, name, name_ka, name_en, name_ru, label_ka, label_en FROM places "
                       "WHERE occupied = 1").fetchall()
    for pid, name, name_ka, name_en, name_ru, label_ka, label_en in rows:
        if name_ka:
            ok = label_ka == name_ka and label_en == romanise(name_ka, fold_spec)
        else:
            ok = policy == "name_ru" and name_ru and label_ka == name_ru and label_en == name_ru
        if not ok:
            bad.append(pid)
    if bad:
        problems.append(f"{len(bad)} occupied places are not labelled from name:ka (policy {policy!r}), "
                        f"e.g. ids {bad[:5]}")
    missing = con.execute("SELECT count(*) FROM places WHERE label_ka IS NULL OR label_en IS NULL").fetchone()[0]
    if missing:
        problems.append(f"{missing} places have no label")
    return problems, {"occupied": len(rows), "wrong": len(bad), "unlabelled": missing}


def resolve_near(case, gate):
    near = case.get("near")
    if near is None:
        return None
    if isinstance(near, str):
        return tuple(gate["near"][near])
    return tuple(near)


def evaluate(case, hits, near):
    """Problems with the top hit of one known query (with "top": n, the
    first of the n best hits that meets every expectation passes)."""
    if not hits:
        return ["no result"]
    found = [check_hit(case, hit, near) for hit in hits[:max(1, int(case.get("top", 1)))]]
    if any(not f for f in found):
        return []
    return found[0]


def check_hit(case, top, near):
    problems = []
    tables = case["table"] if isinstance(case["table"], list) else [case["table"]]
    if top["table"] not in tables:
        problems.append(f"top hit is a {top['table']} row, expected {' or '.join(tables)}")
    if "kind" in case and top.get("kind") != case["kind"]:
        problems.append(f"top hit kind {top.get('kind')!r}, expected {case['kind']!r}")
    if "occupied" in case and (top.get("occupied") or 0) != case["occupied"]:
        problems.append(f"top hit occupied={top.get('occupied')}, expected {case['occupied']}")
    if "name" in case and case["name"] not in (top.get("name"), top.get("name_ka"), top.get("label_ka")):
        problems.append(f"top hit is {label(top)!r}, expected {case['name']!r}")
    if "attr" in case and case["attr"] not in (top.get("attrs") or "").split(";"):
        problems.append(f"top hit attrs {top.get('attrs')!r} lack {case['attr']!r}")
    at = near if case.get("at") == "near" else case.get("at")
    if at is not None:
        d = distance_m(at[0], at[1], top["lat"], top["lon"]) / 1000.0
        if d > case["km"]:
            problems.append(f"top hit is {d:.2f} km from the expected point (limit {case['km']} km)")
    return problems


def describe(hit):
    if hit is None:
        return None
    return {"table": hit["table"], "kind": hit.get("kind"), "name": label(hit), "id": hit["id"],
            "lat": hit["lat"], "lon": hit["lon"], "occupied": hit.get("occupied"), "routable": hit.get("routable"),
            "score": hit["score"], **({"partial": True} if hit.get("partial") else {})}


def run_queries(searcher, gate, zones=None):
    """Returns (problems, per-query results, slowest ms)."""
    problems, results, slowest = [], [], 0.0
    for case in gate["queries"]:
        near = resolve_near(case, gate)
        started = time.perf_counter()
        hits = searcher.search(case["q"], near=near, limit=20)
        ms = (time.perf_counter() - started) * 1000.0
        slowest = max(slowest, ms)
        found = evaluate(case, hits, near)
        if zones is not None and hits:
            bad = [h for h in hits if h["table"] != "places"]
            if bad:
                in_hard, _ = zones.flags([h["lon"] for h in bad], [h["lat"] for h in bad])
                offenders = [describe(h)["name"] for h, f in zip(bad, in_hard) if f]
                if offenders:
                    found.append(f"offers destinations inside no_go_hard: {offenders[:3]}")
            flagged = [h for h in hits if h["table"] == "places" and (not h.get("occupied") or h.get("routable"))]
            if flagged:
                in_hard, _ = zones.flags([h["lon"] for h in flagged], [h["lat"] for h in flagged])
                if in_hard.any():
                    found.append("returns a place inside no_go_hard without occupied=1 and routable=false")
        results.append({"q": case["q"], "near": near, "passed": not found, "ms": round(ms, 1),
                        "top": describe(hits[0] if hits else None),
                        **({"problems": found} if found else {})})
        problems.extend(f"{case['q']!r}: {p}" for p in found)
    return problems, results, slowest


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--db", required=True)
    p.add_argument("--zones", required=True)
    p.add_argument("--results", required=True)
    p.add_argument("--expect-source-sha256", help="sha256 of the clipped extract the tiles used")
    p.add_argument("--expect-tag", help="the release tag of the tiles' manifest")
    p.add_argument("--previous-manifest")
    p.add_argument("--gate-config", default=str(DEFAULT_GATE))
    p.add_argument("--config", default=str(DEFAULT_CONFIG))
    p.add_argument("--fold", default=str(DEFAULT_FOLD_SPEC))
    args = p.parse_args(argv)

    gate = json.loads(Path(args.gate_config).read_text(encoding="utf-8"))
    db_sha = sha256_file(args.db)
    con = sqlite3.connect(f"file:{Path(args.db).resolve()}?mode=ro", uri=True)
    results = []

    def record(name, problems, detail=None):
        results.append({"name": name, "passed": not problems, **({"problems": problems} if problems else {}),
                        **({"detail": detail} if detail is not None else {})})
        state = "ok  " if not problems else "FAIL"
        print(f"{state} {name}" + (f": {detail}" if isinstance(detail, str) else ""))
        for problem in problems[:25]:
            print(f"     {problem}")

    def guarded(name, func):
        try:
            return func()
        except Exception as exc:  # a crashed check is a failed check
            record(name, [f"{type(exc).__name__}: {exc}"])
            return None

    meta = {}
    got = guarded("meta", lambda: check_meta(con, args.fold, args.config, args.expect_source_sha256,
                                             args.expect_tag))
    if got is not None:
        problems, meta = got
        record("meta", problems, f"schema {meta.get('schema_version')}, fold v{meta.get('fold_version')}, "
                                 f"config v{meta.get('config_version')}, {meta.get('fts')}")
    fold = Fold.load(args.fold)
    record("fold vectors", check_vectors(fold))

    counts, kinds = db_counts(con)
    indexed = indexed_counts(con, meta)
    record("row counts", check_counts(counts, kinds, indexed, gate), {"counts": counts, "kinds": kinds})
    problems, note = check_previous(counts, args.previous_manifest, gate["max_drop_vs_previous"])
    record("previous release", problems, note)
    size = Path(args.db).stat().st_size
    lo, hi = gate["size_bytes"]
    record("size", [] if lo <= size <= hi else [f"{size} bytes, expected {lo} to {hi}"], f"{size} bytes")

    got = guarded("labels", lambda: check_labels(con, json.loads(Path(args.config).read_text(encoding="utf-8")),
                                                 json.loads(Path(args.fold).read_text(encoding="utf-8"))))
    if got is not None:
        record("labels", got[0], got[1])

    zones = guarded("zones (shapely)", lambda: ShapelyZones(args.zones))
    if zones is not None:
        got = guarded("zones (shapely)", lambda: check_zones(con, zones))
        if got is not None:
            record("zones (shapely)", got[0], got[1])

    query_results, slowest = [], 0.0
    searcher = Searcher(con)
    got = guarded("known queries", lambda: run_queries(searcher, gate, zones))
    if got is not None:
        problems, query_results, slowest = got
        passed = sum(1 for r in query_results if r["passed"])
        record("known queries", problems, f"{passed}/{len(query_results)} passed")
        record("query speed", [] if slowest <= gate["max_query_ms"] else
               [f"slowest query took {slowest:.0f} ms (limit {gate['max_query_ms']} ms)"], f"slowest {slowest:.0f} ms")
    if len(gate["queries"]) < 30:
        record("query count", [f"only {len(gate['queries'])} known queries; at least 30 are required"])

    failed = sum(1 for r in results if not r["passed"])
    out = {"passed": failed == 0, "checks": len(results), "failed": failed, "db_sha256": db_sha,
           "db_bytes": size, "counts": counts, "kinds": kinds, "results": results,
           "queries": query_results, "slowest_query_ms": round(slowest, 1)}
    Path(args.results).parent.mkdir(parents=True, exist_ok=True)
    Path(args.results).write_text(json.dumps(out, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"geocoder gate: {len(results) - failed}/{len(results)} checks passed")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
