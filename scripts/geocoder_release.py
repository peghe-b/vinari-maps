#!/usr/bin/env python3
"""Package a gated georgia_geocoder.sqlite for the release.

It refuses unless the geocoder gate passed on exactly this database and the
database was built from exactly the clipped extract the tiles' manifest
lists (same SHA-256, same OSM time, same release tag). Then it writes, side
by side in --out-dir:

  georgia_geocoder.sqlite.gz   gzip (deflate), so both apps unpack it with
                               what the platform already has (Android
                               java.util.zip.GZIPInputStream, iOS libz
                               inflate with gzip headers). Reproducible byte
                               for byte: no file name, time 0.
  geocoder_report.json         the asset report manifest.py --asset-report
                               reads: section "geocoder", passed, files, and
                               binds_to (OSM time, clipped extract SHA-256,
                               clip config version, commit), which
                               manifest.py checks against build's manifest.
                               The rest becomes manifest.json's "geocoder"
                               section: sizes and SHA-256 of the packed and
                               unpacked file, versions, row counts, gate
                               results and the ODbL notice.

Usage (as in the workflow):
  python scripts/geocoder_release.py --db build/geocoder/georgia_geocoder.sqlite \
      --gate build/geocoder/gate_results.json --build-report build/geocoder/build_report.json \
      --manifest build/in/manifest.json --commit "$GITHUB_SHA" --out-dir build/geocoder/release
"""

import argparse
import gzip
import json
import os
import shutil
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from geocoder_build import COPYRIGHT_URL, NOTICE, ODBL_URL, ROOT, sha256_file  # noqa: E402

SECTION = "geocoder"
PACKED_NAME = "georgia_geocoder.sqlite.gz"
REPORT_NAME = "geocoder_report.json"
DEFAULT_CLIP_CONFIG = ROOT / "config" / "clip.json"


def pack(src, dst):
    """Deterministic gzip: level 9, no file name, mtime 0."""
    with open(src, "rb") as fin, open(dst, "wb") as raw:
        with gzip.GzipFile(filename="", mode="wb", compresslevel=9, fileobj=raw, mtime=0) as fout:
            shutil.copyfileobj(fin, fout, 1 << 20)


def problems_with(db_sha, gate, report, manifest, meta, clip_version):
    problems = []
    if not gate.get("passed"):
        problems.append("the geocoder gate did not pass")
    if gate.get("db_sha256") != db_sha:
        problems.append(f"the gate tested a database with sha256 {gate.get('db_sha256')}, this one is {db_sha}")
    if report.get("db", {}).get("sha256") != db_sha:
        problems.append("the build report describes another database")
    clipped = ((manifest.get("osm") or {}).get("clipped_pbf") or {}).get("sha256")
    if not clipped:
        problems.append("the tiles' manifest lists no clipped extract (osm.clipped_pbf)")
    elif meta.get("source_pbf_sha256") != clipped or report.get("source", {}).get("sha256") != clipped:
        problems.append(f"the geocoder was built from {meta.get('source_pbf_sha256')}, the tiles from {clipped}")
    if meta.get("osm_timestamp") != (manifest.get("osm") or {}).get("timestamp"):
        problems.append(f"OSM time {meta.get('osm_timestamp')!r}, the manifest has "
                        f"{(manifest.get('osm') or {}).get('timestamp')!r}")
    if meta.get("release_tag") != manifest.get("tag"):
        problems.append(f"the geocoder names release {meta.get('release_tag')!r}, the manifest {manifest.get('tag')!r}")
    if str(meta.get("clip_config_version")) != str(clip_version):
        problems.append(f"built for clip config v{meta.get('clip_config_version')}, the repository has v{clip_version}")
    return problems


def asset_report(db_path, packed, db_sha, gate, meta, clip_version, commit):
    sqlite_needs = ("FTS5: the iOS system SQLite has it; Android's framework SQLite does not, so Android "
                    "opens the file with a bundled SQLite (for example androidx.sqlite:sqlite-bundled, "
                    "Apache-2.0)" if meta["fts"] == "fts5" else "FTS4: stock SQLite on iOS and Android")
    return {
        "section": SECTION,
        "passed": True,
        "files": [{"name": packed.name, "bytes": packed.stat().st_size, "sha256": sha256_file(packed)}],
        "binds_to": {"osm_timestamp": meta["osm_timestamp"], "clipped_pbf_sha256": meta["source_pbf_sha256"],
                     "clip_config_version": clip_version, "source_commit": commit},
        "format": "SQLite 3 database, gzip-compressed",
        "compression": "gzip",
        "sqlite": {"name": Path(db_path).name, "bytes": os.path.getsize(db_path), "sha256": db_sha},
        "schema_version": int(meta["schema_version"]),
        "fold_version": int(meta["fold_version"]),
        "config_version": int(meta["config_version"]),
        "fts": meta["fts"],
        "sqlite_needs": sqlite_needs,
        "tables": ["places", "streets", "addresses", "pois", "categories", "meta"],
        "occupied": ("Places inside no_go_hard are kept with occupied=1 so the app can explain; the app never "
                     "routes to them (the reference search returns routable=false and a reason: 'occupied', or "
                     "'occupation_line' for the 100 m band just outside the drawn line). Show label_ka / label_en, "
                     "never name, for them: name and name:en there are the de facto authorities' forms. Streets, "
                     "addresses and POIs there are not in the database, and none of the others has an occupied "
                     "settlement as its city."),
        "attribution": "© OpenStreetMap contributors",
        "attribution_url": COPYRIGHT_URL,
        "licence": "ODbL-1.0",
        "licence_url": ODBL_URL,
        "licence_note": NOTICE + (" If an app store or the app puts it behind technical protection (DRM, encryption "
                                  "at rest), the unrestricted copy in this GitHub Release must stay available (ODbL "
                                  "4.7 parallel distribution); never merge other map data into it (ODbL 4.4)."),
        "credit_sources": [{"name": "OpenStreetMap (the safety-clipped extract)", "licence": "ODbL-1.0",
                            "attribution": "© OpenStreetMap contributors", "url": COPYRIGHT_URL,
                            "licence_url": ODBL_URL}],
        "counts": gate.get("counts"),
        "kinds": gate.get("kinds"),
        "gate": {"passed": gate["passed"], "checks": gate["checks"], "failed": gate["failed"],
                 "db_sha256": gate["db_sha256"], "queries": len(gate.get("queries", [])),
                 "slowest_query_ms": gate.get("slowest_query_ms"),
                 "results": [{"name": r["name"], "passed": r["passed"]} for r in gate["results"]]},
    }


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--db", required=True)
    p.add_argument("--gate", required=True)
    p.add_argument("--build-report", required=True)
    p.add_argument("--manifest", required=True, help="build's manifest.json (the tiles')")
    p.add_argument("--commit", default=os.environ.get("GITHUB_SHA"), help="source commit (default: $GITHUB_SHA)")
    p.add_argument("--clip-config", default=str(DEFAULT_CLIP_CONFIG))
    p.add_argument("--out-dir", required=True)
    args = p.parse_args(argv)

    db_sha = sha256_file(args.db)
    gate = json.loads(Path(args.gate).read_text(encoding="utf-8"))
    report = json.loads(Path(args.build_report).read_text(encoding="utf-8"))
    manifest = json.loads(Path(args.manifest).read_text(encoding="utf-8"))
    clip_version = json.loads(Path(args.clip_config).read_text(encoding="utf-8"))["version"]
    con = sqlite3.connect(f"file:{Path(args.db).resolve()}?mode=ro", uri=True)
    meta = {k: v for k, v in con.execute("SELECT key, value FROM meta")}
    con.close()
    problems = problems_with(db_sha, gate, report, manifest, meta, clip_version)
    if not args.commit:
        problems.append("no source commit (--commit or $GITHUB_SHA)")
    if problems:
        for problem in problems:
            print(f"ERROR: {problem}", file=sys.stderr)
        print("ERROR: geocoder not packaged", file=sys.stderr)
        return 1

    out = Path(args.out_dir)
    out.mkdir(parents=True, exist_ok=True)
    packed = out / PACKED_NAME
    pack(args.db, packed)
    body = asset_report(args.db, packed, db_sha, gate, meta, clip_version, args.commit)
    (out / REPORT_NAME).write_text(json.dumps(body, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    f = body["files"][0]
    print(f"geocoder: {f['name']} {f['bytes']} bytes (sqlite {body['sqlite']['bytes']}), sha256 {f['sha256'][:12]}...")
    return 0


if __name__ == "__main__":
    sys.exit(main())
