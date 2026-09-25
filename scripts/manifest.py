#!/usr/bin/env python3
"""Write manifest.json for a release, and print the release tag.

The manifest lets the app (and any person) check what a tile pack is:
file sizes and SHA-256 sums, the OSM data time, the Valhalla version and
image, the clip config version with the polygon versions it used, the
safety gate results, and the licence notice with the sources and the
method (the commit) that made the data. The app should refuse a pack whose
sha256 differs.

It refuses to write a manifest unless the gate passed, the gate's edge scan
ran and found a plausible number of edges, the gate tested the same clip
config version, and the gate tested exactly the tar being published.

Usage:
  python scripts/manifest.py --pbf build/georgia-latest.osm.pbf --pbf-url URL \
      --tar build/valhalla/valhalla_tiles.tar --zones build/nogo_zones.geojson \
      --clip-report build/clip_report.json --gate build/gate_results.json \
      --valhalla-config build/valhalla/valhalla.json --out build/manifest.json \
      [--previous-manifest build/previous_manifest.json]
  python scripts/manifest.py --tag-only --pbf build/georgia-latest.osm.pbf --commit "$GITHUB_SHA"
"""

import argparse
import datetime as dt
import hashlib
import json
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from clip import DEFAULT_CONFIG, load_config  # noqa: E402
from gate import MIN_EDGES  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent

TAG_PATTERN = re.compile(r"^osm-[0-9]{8}T[0-9]{6}Z(-[0-9a-f]{8})?$")

# A release with this share fewer edges than the one before is refused:
# it usually means part of the country went missing. If a reviewed change
# really removes that much, lower this in the same commit.
EDGE_DROP_LIMIT = 0.20

ODBL_URL = "https://opendatacommons.org/licenses/odbl/1-0/"


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def file_entry(path):
    path = Path(path)
    return {"name": path.name, "bytes": path.stat().st_size, "sha256": sha256(path)}


def osm_timestamp(pbf):
    """The replication time Geofabrik writes into the PBF header."""
    import osmium
    reader = osmium.io.Reader(str(pbf), osmium.osm.osm_entity_bits.NOTHING)
    try:
        value = reader.header().get("osmosis_replication_timestamp")
    finally:
        reader.close()
    if not value:
        raise ValueError("the PBF header has no osmosis_replication_timestamp")
    return value  # e.g. 2026-09-24T20:21:02Z


def release_tag(timestamp, commit=None):
    """Git tags cannot hold ':'. The commit is part of the tag, so a safety
    fix rebuilt on the same OSM data gets a new release.
    2026-09-24T20:21:02Z, 3f2a... -> osm-20260924T202102Z-3f2a1b0c"""
    tag = "osm-" + timestamp.replace("-", "").replace(":", "")
    if commit:
        tag += "-" + commit[:8].lower()
    if not TAG_PATTERN.match(tag):
        raise ValueError(f"refusing odd release tag {tag!r} (timestamp {timestamp!r}, commit {commit!r})")
    return tag


def gate_problems(gate, config, tar_sha256):
    """Why this gate result may not be published, or an empty list."""
    problems = []
    if not gate.get("passed"):
        problems.append("the safety gate did not pass")
    edge = next((r for r in gate.get("results", []) if r.get("name") == "edge scan"), None)
    if edge is None:
        problems.append("the gate ran without its edge scan (--skip-edges?)")
    elif not edge.get("passed") or (edge.get("edges") or 0) < MIN_EDGES:
        problems.append(f"the edge scan did not pass with at least {MIN_EDGES} edges")
    if gate.get("clip_config_version") != config["version"]:
        problems.append(f"the gate tested clip config v{gate.get('clip_config_version')}, "
                        f"config is v{config['version']}")
    if gate.get("tar_sha256") != tar_sha256:
        problems.append(f"the gate tested a tar with sha256 {gate.get('tar_sha256')}, "
                        f"but the tar to publish is {tar_sha256}")
    return problems


def edge_drop_problem(previous_path, edges):
    """Compare the edge count with the previous release's manifest."""
    if not previous_path or not Path(previous_path).exists():
        return None, "no previous release to compare with"
    previous = json.loads(Path(previous_path).read_text(encoding="utf-8"))
    before = (previous.get("gate") or {}).get("edges")
    if not before:
        return None, f"previous release {previous.get('tag')} has no edge count"
    note = f"edges {edges} against {before} in {previous.get('tag')}"
    if edges < (1 - EDGE_DROP_LIMIT) * before:
        return f"{note}: more than {EDGE_DROP_LIMIT:.0%} fewer; part of the country may be missing", note
    return None, note


def boundary_entry(config, entry):
    path = Path(config["_dir"]) / entry["file"]
    if entry.get("hand_made"):
        return {"name": entry["name"], "hand_made": True,
                "derived_from": [{k: s[k] for k in ("osm_type", "osm_id", "osm_version")}
                                 for s in entry["derived_from"]],
                "sha256": sha256(path)}
    return {"name": entry["name"], "osm_type": entry["osm_type"], "osm_id": entry["osm_id"],
            "osm_version": entry["osm_version"], "sha256": sha256(path)}


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--pbf", required=True)
    p.add_argument("--tag-only", action="store_true", help="just print the release tag")
    p.add_argument("--commit", default=os.environ.get("GITHUB_SHA"),
                   help="source commit; its first 8 hex digits end the tag (default: $GITHUB_SHA)")
    p.add_argument("--pbf-url")
    p.add_argument("--tar")
    p.add_argument("--zones")
    p.add_argument("--clip-report")
    p.add_argument("--gate")
    p.add_argument("--valhalla-config")
    p.add_argument("--previous-manifest", help="manifest.json of the latest release, if any")
    p.add_argument("--config", default=str(DEFAULT_CONFIG))
    p.add_argument("--out")
    args = p.parse_args(argv)

    timestamp = osm_timestamp(args.pbf)
    tag = release_tag(timestamp, args.commit)
    if args.tag_only:
        print(tag)
        return 0

    config = load_config(args.config)
    gate = json.loads(Path(args.gate).read_text(encoding="utf-8"))
    tar = file_entry(args.tar)
    problems = gate_problems(gate, config, tar["sha256"])
    edges = next((r.get("edges") for r in gate.get("results", []) if r.get("name") == "edge scan"), 0) or 0
    drop, drop_note = edge_drop_problem(args.previous_manifest, edges)
    print(f"edge count: {drop_note}")
    if drop:
        problems.append(drop)
    if problems:
        for problem in problems:
            print(f"ERROR: {problem}", file=sys.stderr)
        print("ERROR: no manifest written", file=sys.stderr)
        return 1

    clip_report = json.loads(Path(args.clip_report).read_text(encoding="utf-8"))
    mjolnir = json.loads(Path(args.valhalla_config).read_text(encoding="utf-8"))["mjolnir"]
    boundaries = [boundary_entry(config, e) for e in [config["country"]] + config["no_go"]]

    env = os.environ
    server = env.get("GITHUB_SERVER_URL", "https://github.com")
    repo = env.get("GITHUB_REPOSITORY")
    run_url = None
    if env.get("GITHUB_RUN_ID"):
        run_url = f"{server}/{repo}/actions/runs/{env['GITHUB_RUN_ID']}"
    method_url = f"{server}/{repo}/tree/{args.commit}" if repo and args.commit else None

    manifest = {
        "schema": 2,
        "tag": tag,
        "built_at": dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat(),
        "attribution": "© OpenStreetMap contributors",
        "licence": {"data": "ODbL-1.0", "data_licence_url": ODBL_URL,
                    "attribution_url": "https://www.openstreetmap.org/copyright",
                    "code": "MIT",
                    "notice": ("Contains information from OpenStreetMap and from timezone-boundary-builder "
                               "2025b, both made available under the Open Database License (ODbL 1.0). "
                               "These tiles are a derivative database, also under the ODbL 1.0.")},
        "sources": [
            {"name": "OpenStreetMap (Geofabrik extract)", "attribution": "© OpenStreetMap contributors",
             "licence": "ODbL-1.0"},
            {"name": "timezone-boundary-builder 2025b",
             "url": "https://github.com/evansiroky/timezone-boundary-builder/releases/tag/2025b",
             "licence": "ODbL-1.0"},
        ],
        "method": method_url,
        "files": [tar, file_entry(args.zones)],
        "osm": {
            "timestamp": timestamp,
            "source": args.pbf_url,
            "pbf": file_entry(args.pbf),
        },
        "valhalla": {
            "version": gate.get("valhalla_version"),
            "image": env.get("VALHALLA_IMAGE"),
            "costing": "auto only",
            "include_driving": mjolnir.get("include_driving"),
            "include_pedestrian": mjolnir.get("include_pedestrian"),
            "include_bicycle": mjolnir.get("include_bicycle"),
            "include_driveways": mjolnir.get("include_driveways"),
            "include_construction": mjolnir.get("include_construction"),
            "timezones": True,
            "admins": True,
            "speeds": "Valhalla's built-in road class defaults (no default_speeds_config)",
        },
        "clip": {
            "config_version": config["version"],
            "config_sha256": sha256(args.config),
            "hard_buffer_m": config["hard_buffer_m"],
            "soft_band_outer_m": config.get("soft_band", {}).get("outer_m"),
            "boundaries": boundaries,
            "stats": clip_report.get("ways"),
            "relations": clip_report.get("relations"),
        },
        "gate": {
            "passed": gate["passed"],
            "checks": gate["checks"],
            "failed": gate["failed"],
            "edges": edges,
            "tar_sha256": gate.get("tar_sha256"),
            "results": [{"name": r["name"], "passed": r["passed"],
                         **({"detail": r["detail"]} if r.get("detail") else {})}
                        for r in gate["results"]],
        },
        "source_commit": args.commit,
        "workflow_run": run_url,
    }
    Path(args.out).write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n",
                              encoding="utf-8")
    print(f"manifest: {manifest['tag']}, tar {tar['bytes']} bytes, sha256 {tar['sha256'][:12]}...")
    return 0


if __name__ == "__main__":
    sys.exit(main())
