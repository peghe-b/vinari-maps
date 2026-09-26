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

Files made by other jobs come in through --asset-report: a JSON report
written by that job's own gate (glyphs.py pack, sprites.py pack, basemap.py
report, geocoder_release.py), lying next to the files it lists. Each report
must say it passed, and every file it lists must be there with the same size
and SHA-256. Its files are appended to "files" (the tar and the zones stay
first) and the rest of the report becomes a section of the manifest named
by its "section" key.

In the workflow the build job writes the manifest with the tar and the zones
only; the release-manifest job then adds glyphs.zip, sprites.zip,
georgia.pmtiles and georgia_geocoder.sqlite.gz with --extend, which reads the
manifest the build job wrote and adds --asset-report files to it the same
way (so a failed glyph job never stops the routing tiles from being built
and gated). A report that carries "binds_to" must name all four facts, and
they must match that manifest: the same OSM data time, the same clipped
extract (--clipped-pbf records its SHA-256 in "osm"), the same clip config
version and the same source commit. A report of OpenStreetMap data (licence
ODbL-1.0, or section basemap or geocoder) is refused without binds_to, so a
map or a search database drawn from another extract cannot slip in.

--extend also writes two top-level keys the build does not: "credits", one
entry per part of the release with its files, attribution, licence and
sources (the app's Licences screen shows them all), and, when the release
has a basemap, "map_attribution", the credit the map must always show.

Usage:
  python scripts/manifest.py --pbf build/georgia-latest.osm.pbf --pbf-url URL \
      --tar build/valhalla/valhalla_tiles.tar --zones build/nogo_zones.geojson \
      --clip-report build/clip_report.json --gate build/gate_results.json \
      --valhalla-config build/valhalla/valhalla.json --out build/manifest.json \
      [--previous-manifest build/previous_manifest.json] \
      [--clipped-pbf build/valhalla/georgia-clipped.osm.pbf] [--asset-report REPORT ...]
  python scripts/manifest.py --tag-only --pbf build/georgia-latest.osm.pbf --commit "$GITHUB_SHA"
  python scripts/manifest.py --extend in/manifest.json \
      --asset-report in/glyphs-sprites/glyphs_report.json --asset-report in/glyphs-sprites/sprites_report.json \
      --asset-report in/basemap/basemap_report.json --asset-report in/geocoder/geocoder_report.json \
      --out build/manifest.json
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


SECTION_PATTERN = re.compile(r"^[a-z][a-z0-9_]*$")
# Top-level manifest keys an --asset-report section may not take.
RESERVED_SECTIONS = frozenset({
    "schema", "tag", "built_at", "attribution", "licence", "sources", "method", "files", "osm",
    "valhalla", "clip", "gate", "source_commit", "workflow_run", "credits", "map_attribution"})
# Sections that hold OpenStreetMap data: they must say which extract they came from.
OSM_SECTIONS = frozenset({"basemap", "geocoder"})
BINDING_KEYS = ("osm_timestamp", "clipped_pbf_sha256", "clip_config_version", "source_commit")
COPYRIGHT_URL = "https://www.openstreetmap.org/copyright"
OPENMAPTILES_URL = "https://openmaptiles.org/"
MAP_ATTRIBUTION = {
    "text": "© OpenMapTiles © OpenStreetMap contributors",
    "links": [{"text": "© OpenMapTiles", "url": OPENMAPTILES_URL},
              {"text": "© OpenStreetMap contributors", "url": COPYRIGHT_URL}],
    "rule": ("always visible in the map corner, also during navigation; never only behind an (i) button "
             "(OpenMapTiles CC-BY 4.0 and its LICENSE; OpenStreetMap ODbL 1.0)"),
}


def asset_report(path, taken_sections, taken_names):
    """Read one --asset-report. Returns (section, body, file entries, problems)."""
    path = Path(path)
    try:
        report = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return None, None, [], [f"{path}: cannot read the report ({exc})"]
    problems = []
    section = report.get("section")
    if not isinstance(section, str) or not SECTION_PATTERN.match(section):
        problems.append(f"{path}: bad section name {section!r}")
    elif section in taken_sections:
        problems.append(f"{path}: section {section!r} is already in the manifest")
    if report.get("passed") is not True:
        problems.append(f"{path}: its gate did not pass")
    listed = report.get("files")
    if not isinstance(listed, list) or not listed:
        problems.append(f"{path}: lists no files")
        listed = []
    entries = []
    for item in listed:
        name = item.get("name") if isinstance(item, dict) else None
        if not isinstance(name, str) or not name or "/" in name or name.startswith("."):
            problems.append(f"{path}: bad file name {name!r}")
            continue
        if name in taken_names or any(e["name"] == name for e in entries):
            problems.append(f"{path}: {name} would appear twice in the release")
            continue
        local = path.parent / name
        if not local.is_file():
            problems.append(f"{path}: {name} is not next to the report")
            continue
        entry = file_entry(local)
        if entry["sha256"] != item.get("sha256") or entry["bytes"] != item.get("bytes"):
            problems.append(f"{path}: {name} is {entry['bytes']} bytes, sha256 {entry['sha256']}, "
                            f"but the report says {item.get('bytes')} bytes, sha256 {item.get('sha256')}")
            continue
        entries.append(entry)
    body = {k: v for k, v in report.items() if k not in ("section", "files", "passed")}
    body["files"] = [e["name"] for e in entries]
    return section, body, entries, problems


def boundary_entry(config, entry):
    path = Path(config["_dir"]) / entry["file"]
    if entry.get("hand_made"):
        return {"name": entry["name"], "hand_made": True,
                "derived_from": [{k: s[k] for k in ("osm_type", "osm_id", "osm_version")}
                                 for s in entry["derived_from"]],
                "sha256": sha256(path)}
    return {"name": entry["name"], "osm_type": entry["osm_type"], "osm_id": entry["osm_id"],
            "osm_version": entry["osm_version"], "sha256": sha256(path)}


def binding_facts(manifest):
    """What a report's binds_to must match, from a manifest."""
    osm = manifest.get("osm") or {}
    return {"osm_timestamp": osm.get("timestamp"),
            "clipped_pbf_sha256": (osm.get("clipped_pbf") or {}).get("sha256"),
            "clip_config_version": (manifest.get("clip") or {}).get("config_version"),
            "source_commit": manifest.get("source_commit")}


def binding_problems(have, section, body, path):
    """A report that names the build it was made from ("binds_to") must
    name all four facts and match them, so a map drawn from another extract
    cannot slip in; a report of OpenStreetMap data must carry binds_to."""
    binds = body.get("binds_to")
    if binds is None:
        if section in OSM_SECTIONS or str(body.get("licence", "")).startswith("ODbL"):
            return [f"{path}: an OpenStreetMap-derived report ({section}) must carry binds_to"]
        return []
    if not isinstance(binds, dict) or not binds:
        return [f"{path}: binds_to must be a non-empty object"]
    problems = [f"{path}: binds_to lacks {key!r}" for key in BINDING_KEYS if key not in binds]
    for key, value in binds.items():
        if key not in have:
            problems.append(f"{path}: unknown binds_to key {key!r}")
        elif have[key] is None or value != have[key]:
            problems.append(f"{path}: made from {key} {value!r}, but the manifest has {have[key]!r}")
    return problems


def _credit_sources(body):
    """[{name, licence, attribution, url}] of a section, from its
    credit_sources (or its sources) list."""
    out = []
    for src in body.get("credit_sources") or body.get("sources") or []:
        if not isinstance(src, dict):
            continue
        entry = {k: src[k] for k in ("name", "licence", "attribution", "url", "licence_url", "about") if src.get(k)}
        if entry.get("name"):
            out.append(entry)
    return out


def credits(manifest, sections):
    """One entry per part of the release, in file order: what the app's
    Licences screen lists (attribution, licence, notice, sources)."""
    claimed = {name for body in sections.values() for name in body.get("files", [])}
    build_files = [f["name"] for f in manifest["files"] if f["name"] not in claimed]
    lic = manifest.get("licence") or {}
    out = [{"part": "routing", "files": build_files, "attribution": manifest.get("attribution"),
            "attribution_url": lic.get("attribution_url"), "licence": lic.get("data"),
            "licence_url": lic.get("data_licence_url"), "notice": lic.get("notice"),
            "sources": [{k: v for k, v in s.items() if k in ("name", "licence", "attribution", "url")}
                        for s in manifest.get("sources") or []]}]
    for section, body in sections.items():
        entry = {"part": section, "files": body.get("files", [])}
        for key in ("attribution", "attribution_url", "attribution_links", "licence", "licence_url",
                    "schema_licence", "schema_licence_url", "schema_licence_uri"):
            if body.get(key):
                entry[key] = body[key]
        if body.get("licence_note"):
            entry["notice"] = body["licence_note"]
        entry["sources"] = _credit_sources(body)
        out.append(entry)
    return out


def extend_manifest(args):
    """--extend: add --asset-report files to a manifest written earlier."""
    manifest = json.loads(Path(args.extend).read_text(encoding="utf-8"))
    problems = [] if args.asset_report else ["--extend needs at least one --asset-report"]
    if not isinstance(manifest.get("files"), list) or not manifest.get("tag"):
        problems.append(f"{args.extend} is not a manifest written by this script")
    names = {f.get("name") for f in manifest.get("files") or []} | {"manifest.json"}
    added = {}
    have = binding_facts(manifest)
    for report_path in args.asset_report:
        section, body, entries, report_problems = asset_report(
            report_path, RESERVED_SECTIONS | set(manifest) | set(added), set(names))
        if not report_problems:
            report_problems = binding_problems(have, section, body, report_path)
        problems.extend(report_problems)
        if not report_problems:
            added[section] = body
            manifest["files"].extend(entries)
            names |= {e["name"] for e in entries}
            print(f"{section}: " + ", ".join(f"{e['name']} {e['bytes']} bytes" for e in entries))
    if problems:
        for problem in problems:
            print(f"ERROR: {problem}", file=sys.stderr)
        print("ERROR: no manifest written", file=sys.stderr)
        return 1
    manifest.update(added)
    sections = {k: v for k, v in manifest.items() if k not in RESERVED_SECTIONS and isinstance(v, dict)
                and isinstance(v.get("files"), list)}
    manifest["credits"] = credits(manifest, sections)
    if "basemap" in manifest:
        links = manifest["basemap"].get("attribution_links") or MAP_ATTRIBUTION["links"]
        manifest["map_attribution"] = dict(MAP_ATTRIBUTION, links=links)
    Path(args.out).write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n",
                              encoding="utf-8")
    print(f"manifest: {manifest['tag']}, {len(manifest['files'])} files, {len(manifest['credits'])} credits")
    return 0


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--pbf", help="the raw extract (required except with --extend)")
    p.add_argument("--clipped-pbf", help="the clipped extract; its size and SHA-256 go into 'osm'")
    p.add_argument("--extend", help="manifest.json to add --asset-report files to, instead of writing one")
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
    p.add_argument("--asset-report", action="append", default=[],
                   help="report of a job that built more release files (repeatable)")
    p.add_argument("--config", default=str(DEFAULT_CONFIG))
    p.add_argument("--out")
    args = p.parse_args(argv)
    if args.extend:
        if not args.out:
            p.error("--extend needs --out")
        return extend_manifest(args)
    if not args.pbf:
        p.error("--pbf is required")

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
    zones = file_entry(args.zones)
    clipped = file_entry(args.clipped_pbf) if args.clipped_pbf else None
    have = {"osm_timestamp": timestamp, "clipped_pbf_sha256": clipped["sha256"] if clipped else None,
            "clip_config_version": config["version"], "source_commit": args.commit}
    sections, extra_files = {}, []
    for report_path in args.asset_report:
        section, body, entries, report_problems = asset_report(
            report_path, RESERVED_SECTIONS | set(sections),
            {tar["name"], zones["name"], "manifest.json"} | {e["name"] for e in extra_files})
        if not report_problems:
            report_problems = binding_problems(have, section, body, report_path)
        problems.extend(report_problems)
        if not report_problems:
            sections[section] = body
            extra_files.extend(entries)
            print(f"{section}: " + ", ".join(f"{e['name']} {e['bytes']} bytes" for e in entries))
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
        "files": [tar, zones] + extra_files,
        "osm": {
            "timestamp": timestamp,
            "source": args.pbf_url,
            "pbf": file_entry(args.pbf),
            **({"clipped_pbf": clipped} if clipped else {}),
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
    manifest.update(sections)
    Path(args.out).write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n",
                              encoding="utf-8")
    print(f"manifest: {manifest['tag']}, tar {tar['bytes']} bytes, sha256 {tar['sha256'][:12]}...")
    return 0


if __name__ == "__main__":
    sys.exit(main())
