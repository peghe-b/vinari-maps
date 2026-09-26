#!/usr/bin/env bash
# Build the offline Georgia basemap (georgia.pmtiles) with Planetiler.
#
# Usage: scripts/build_basemap.sh <clipped pbf> <work dir> <log dir>
#   <clipped pbf> is georgia-clipped.osm.pbf from the build job, handed on as
#   the clipped-extract artifact only after the Valhalla safety gate passed,
#   so the map, the routing tiles and the search index share one extract.
#   The result is <work dir>/georgia.pmtiles, <work dir>/sources.json (what
#   was downloaded, with SHA-256) and <work dir>/build_info.json (the Java
#   version and the exact Planetiler arguments).
#
# Every pin and setting comes from config/basemap.json:
# - Planetiler 0.10.2's release jar (Planetiler Apache-2.0, which also
#   bundles LGPL GeoTools with the EPSG data, EDL JTS and ICU, plus the
#   planetiler-openmaptiles profile, BSD-3-Clause code and CC-BY-4.0 schema),
#   checked against its pinned SHA-256 before it runs. It runs only here and
#   is never distributed; its output is data, not a derivative of its code.
# - The three extra sources the OpenMapTiles profile reads (OSM water
#   polygons, Natural Earth, lake centre lines) are fetched here with curl,
#   not by Planetiler, and checked by basemap.py verify-sources. Planetiler
#   runs with --download=false, so it cannot fetch anything else.
# - Full quality, as the owner decided: every OpenMapTiles layer, POIs and
#   house numbers included; names in Georgian and English only
#   (--languages=ka,en; Planetiler's default list has no ka).
# - --osm-path, never --area (--area=georgia would mean the US state), and
#   explicit --bounds (Georgia's frozen border plus a margin), so a missing
#   PBF header can never turn this into a world build.
# Tool output goes to log files; only one line per step is printed.
set -euo pipefail

PBF="$(cd "$(dirname "${1:?clipped pbf}")" && pwd)/$(basename "$1")"
WORK="$(mkdir -p "${2:?work dir}" && cd "$2" && pwd)"
LOGS="$(mkdir -p "${3:?log dir}" && cd "$3" && pwd)"
HERE="$(cd "$(dirname "$0")" && pwd)"
CONFIG="$HERE/../config/basemap.json"

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

fetch() {  # fetch <url> <file>: three tries, keeps the server's Last-Modified time
  local url="$1" out="$2" attempt
  for attempt in 1 2 3; do
    if curl -fsSL --retry 5 --retry-delay 10 --connect-timeout 30 -R -o "$out.part" "$url"; then
      mv "$out.part" "$out"
      return 0
    fi
    echo "::warning::download of $url failed (try $attempt of 3)"
    rm -f "$out.part"
    sleep 30
  done
  return 1
}

cfg() { jq -er "$1" "$CONFIG"; }

test -s "$PBF" || { echo "::error::missing $PBF"; exit 1; }
mkdir -p "$WORK/tools" "$WORK/sources" "$WORK/tmp"
OUTPUT="$WORK/$(cfg .output)"
rm -f "$OUTPUT"

# 1. Planetiler, checked against the pinned SHA-256.
JAR="$WORK/tools/planetiler.jar"
fetch "$(cfg .planetiler.jar_url)" "$JAR" || { echo "::error::Planetiler download failed"; exit 1; }
if ! echo "$(cfg .planetiler.jar_sha256)  $JAR" | sha256sum -c --quiet -; then
  echo "::error::planetiler.jar does not match the pinned SHA-256"; exit 1
fi
echo "ok  planetiler.jar $(cfg .planetiler.version), sha256 matches the pin"

JAVA_MAJOR=$(java -XshowSettings:properties -version 2>&1 | sed -n 's/^ *java\.specification\.version = //p')
if [ "$JAVA_MAJOR" != "$(cfg .planetiler.java_major)" ]; then
  echo "::error::java $JAVA_MAJOR on PATH, expected $(cfg .planetiler.java_major)"; exit 1
fi
JAVA_VERSION=$(java -version 2>&1 | tr '\n' ' ' | sed 's/ *$//')
echo "ok  $JAVA_VERSION"

# 2. The extra sources, fetched here and checked before Planetiler reads them.
for i in $(seq 0 $(($(jq '.sources | length' "$CONFIG") - 1))); do
  name=$(cfg ".sources[$i].name")
  file=$(cfg ".sources[$i].file")
  started=$SECONDS
  fetch "$(cfg ".sources[$i].url")" "$WORK/sources/$file" || { echo "::error::$name download failed"; exit 1; }
  echo "ok  $name downloaded ($((SECONDS - started)) s, $(stat -c %s "$WORK/sources/$file") bytes)"
done
python3 "$HERE/basemap.py" verify-sources --dir "$WORK/sources" --out "$WORK/sources.json"

# 3. The map.
BOUNDS=$(python3 "$HERE/basemap.py" bounds)
ARGS=(
  --osm-path="$PBF"
  --water-polygons-path="$WORK/sources/$(cfg '.sources[] | select(.name == "water_polygons") | .file')"
  --natural-earth-path="$WORK/sources/$(cfg '.sources[] | select(.name == "natural_earth") | .file')"
  --lake-centerlines-path="$WORK/sources/$(cfg '.sources[] | select(.name == "lake_centerlines") | .file')"
  --download=false
  --fetch-wikidata=false
  --languages="$(jq -r '.build.languages | join(",")' "$CONFIG")"
  --bounds="$BOUNDS"
  --minzoom="$(cfg .build.minzoom)"
  --maxzoom="$(cfg .build.maxzoom)"
  --archive-name="$(cfg .build.archive_name)"
  --archive-description="$(cfg .build.archive_description)"
  --tmpdir="$WORK/tmp"
  --output="$OUTPUT"
  --force
)
printf '%s\n' "${ARGS[@]}" > "$LOGS/planetiler_args.txt"
step "planetiler (bounds $BOUNDS)" planetiler.log \
  java -Xmx"$(cfg .planetiler.java_heap)" -jar "$JAR" openmaptiles "${ARGS[@]}" || exit 1
# Planetiler writes three-letter levels (INF, WAR, ERR); show what it warned about.
grep -E " (WAR|ERR) " "$LOGS/planetiler.log" | tail -n 20 || true
test -s "$OUTPUT" || { echo "::error::Planetiler wrote no $OUTPUT"; exit 1; }
rm -rf "$WORK/tmp"

python3 - "$WORK/build_info.json" "$JAVA_VERSION" "$LOGS/planetiler_args.txt" <<'EOF'
import json, os, sys
out, java, args_file = sys.argv[1:]
args = []
for arg in open(args_file, encoding="utf-8").read().splitlines():
    key, eq, value = arg.partition("=")
    # Paths are the runner's; keep only the file names.
    args.append(key + eq + (os.path.basename(value) if value.startswith("/") else value))
json.dump({"java": java, "arguments": args}, open(out, "w", encoding="utf-8"), indent=2, ensure_ascii=False)
EOF
echo "ok  $(basename "$OUTPUT") $(du -h "$OUTPUT" | cut -f1)"
