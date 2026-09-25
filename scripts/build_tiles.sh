#!/usr/bin/env bash
# Build car-only Valhalla routing tiles from the clipped Georgia extract.
#
# Usage: scripts/build_tiles.sh <work dir> <log dir>
#   <work dir> must contain georgia-clipped.osm.pbf (written by clip.py).
#   The result is <work dir>/valhalla_tiles.tar plus valhalla.json.
#
# Every Valhalla tool runs from the pinned image in $VALHALLA_IMAGE (see the
# workflow). The image is only used as a toolbox: its own start-up script is
# bypassed so that every setting below is explicit. Tool output goes to log
# files; only one line per step is printed.
#
# Two settings are left out on purpose:
# - Speeds: no mjolnir.default_speeds_config. The image's start-up script
#   would fetch OpenStreetMapSpeeds' default_speeds.json from an unpinned
#   URL; without it the tiles use Valhalla's built-in class speeds, the same
#   every week. The research's Phase 0 speed model (legal defaults plus
#   free-flow and constrained speeds) will come as a vendored, pinned file.
# - Live traffic: no traffic.tar. Live traffic is Phase 3 of the research,
#   written on the phone. When it comes, add -t to valhalla_build_extract
#   and publish traffic.tar with the tiles: it must match the tile set.
set -euo pipefail

WORK="$(cd "${1:?work dir}" && pwd)"
LOGS="$(mkdir -p "${2:?log dir}" && cd "$2" && pwd)"
IMAGE="${VALHALLA_IMAGE:?set VALHALLA_IMAGE to the pinned image}"
PBF=/custom_files/georgia-clipped.osm.pbf
CONFIG=/custom_files/valhalla.json

# Run one tool from the image as the runner's own user, so files written to
# the work dir stay readable and deletable by later steps.
tool() {
  local name="$1"; shift
  docker run --rm --user "$(id -u):$(id -g)" \
    -v "$WORK:/custom_files" -w /custom_files \
    --entrypoint "$name" "$IMAGE" "$@"
}

step() {  # step <label> <log file> <command...>; returns 1 on failure
  local label="$1" log="$2"; shift 2
  local started=$SECONDS
  if "$@" >"$LOGS/$log" 2>&1; then
    echo "ok  $label ($((SECONDS - started)) s)"
  else
    echo "::error::$label failed; last lines of $log:"
    tail -n 40 "$LOGS/$log"
    return 1
  fi
}

test -s "$WORK/georgia-clipped.osm.pbf" || { echo "::error::missing georgia-clipped.osm.pbf"; exit 1; }

# 1. Configuration. Car only: pedestrian-only, cycle-only and private
#    driveway ways are left out; roads under construction are left out.
#    Verbose /status is allowed so the gate can see that tiles, admin areas
#    and time zones loaded (this valhalla.json is used in CI only).
tool valhalla_build_config \
  --mjolnir-tile-dir /custom_files/valhalla_tiles \
  --mjolnir-tile-extract /custom_files/valhalla_tiles.tar \
  --mjolnir-admin /custom_files/admins.sqlite \
  --mjolnir-timezone /custom_files/timezones.sqlite \
  --mjolnir-include-driving True \
  --mjolnir-include-pedestrian False \
  --mjolnir-include-bicycle False \
  --mjolnir-include-driveways False \
  --mjolnir-include-construction False \
  --mjolnir-include-platforms False \
  --mjolnir-concurrency "$(nproc)" \
  --mjolnir-logging-color False \
  --service-limits-status-allow-verbose True \
  > "$WORK/valhalla.json"
python3 - "$WORK/valhalla.json" <<'EOF'
import json, sys
path = sys.argv[1]
config = json.load(open(path))
m = config["mjolnir"]
# valhalla_build_config always writes a traffic_extract path; with the key
# present the service tries to open a file that is never built.
m.pop("traffic_extract", None)
expected = {"include_driving": True, "include_pedestrian": False, "include_bicycle": False,
            "include_driveways": False, "include_construction": False}
wrong = {k: m.get(k) for k, v in expected.items() if m.get(k) is not v}
if config["service_limits"]["status"]["allow_verbose"] is not True:
    wrong["service_limits.status.allow_verbose"] = config["service_limits"]["status"]["allow_verbose"]
if wrong:
    sys.exit(f"valhalla.json has unexpected values: {wrong}")
json.dump(config, open(path, "w"), indent=2)
print("ok  config: car only, no construction, no traffic extract, verbose status")
EOF

# 2. Admin areas (driving side, country codes) and time zones (needed for
#    month-based closures such as Sh44 'no @ (Nov-Jun)').
step "admin areas" admins.log tool valhalla_build_admins --config "$CONFIG" "$PBF" || exit 1

# valhalla_build_timezones downloads timezone-boundary-builder 2025b from
# GitHub with curl -s and no retry, so a passing network blip is retried.
for attempt in 1 2 3; do
  if docker run --rm --user "$(id -u):$(id -g)" -w /tmp --entrypoint valhalla_build_timezones \
      "$IMAGE" > "$WORK/timezones.sqlite" 2> "$LOGS/timezones.log"; then
    echo "ok  time zones (try $attempt)"
    break
  fi
  echo "::warning::time zones failed (try $attempt of 3); last lines of timezones.log:"
  tail -n 5 "$LOGS/timezones.log"
  if [ "$attempt" = 3 ]; then
    echo "::error::time zones failed 3 times"; exit 1
  fi
  sleep 30
done

# Check what is inside, not just that the files exist.
python3 - "$WORK/timezones.sqlite" "$WORK/admins.sqlite" <<'EOF'
import sqlite3, sys
tz = sqlite3.connect(sys.argv[1]).execute(
    "select count(*) from tz_world where tzid = 'Asia/Tbilisi'").fetchone()[0]
ge = sqlite3.connect(sys.argv[2]).execute(
    "select count(*) from admins where iso_code = 'GE'").fetchone()[0]
if tz < 1 or ge < 1:
    sys.exit(f"::error::incomplete: {tz} Asia/Tbilisi time zone rows, {ge} admin rows for GE")
print(f"ok  contents: {tz} Asia/Tbilisi time zone row(s), {ge} admin row(s) for GE")
EOF

# 3. The graph itself, then one tar file for the phone (the app loads the
#    whole tar; lazy tile download is broken in valhalla-mobile, issue #113).
step "routing tiles" tiles.log tool valhalla_build_tiles -c "$CONFIG" "$PBF" || exit 1
step "tile tar" extract.log tool valhalla_build_extract -c "$CONFIG" -v || exit 1

# The loose tile folder is removed so that everything after this point
# (the safety gate included) reads exactly the tar that will be published.
rm -rf "$WORK/valhalla_tiles"
echo "ok  valhalla_tiles.tar $(du -h "$WORK/valhalla_tiles.tar" | cut -f1)"
