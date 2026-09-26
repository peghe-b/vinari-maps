# vinari-maps

Offline map, search and car-routing data for Georgia, built every week for
the Vinari navigator. A GitHub Actions workflow downloads the OpenStreetMap
extract of Georgia, removes every road the navigator must never use, builds
[Valhalla](https://github.com/valhalla/valhalla) 3.6.3 routing tiles for
cars, proves with a safety gate that the result cannot route into the
occupied territories, draws the offline map and builds the offline search
database from that same clipped extract, gates each of them, and publishes
everything as one GitHub Release.

Map data © OpenStreetMap contributors. Map layers © OpenMapTiles.

## What a release contains

| File | What it is |
|---|---|
| `valhalla_tiles.tar` | Car-only Valhalla 3.6.3 tiles for Georgia, with time zones and admin areas. The app loads the whole tar (lazy tile download is broken in valhalla-mobile, issue #113). |
| `manifest.json` | SHA-256 and size of every file, the OSM data time, the Valhalla version and image digest, the clip config version with the polygon versions it used, clip statistics, every gate result and the edge count, one section per further file (glyphs, sprites, basemap, geocoder) with its own gate, `credits` (one entry per part: files, attribution, licence, notice, sources), `map_attribution` (the credit the map must always show) and the method (the source commit). The app must refuse any file whose SHA-256 does not match. |
| `nogo_zones.geojson` | The areas used by the clip: the no-go area (occupied areas plus 100 m), the outer edge of the 500 m warning band, the occupied areas as drawn, and Georgia's border. The app uses it for its own checks (see "What the app must do"). |
| `georgia.pmtiles` | The offline map: OpenMapTiles 3.16 schema vector tiles, zoom 0 to 14, every layer (buildings, land cover, land use, water, house numbers, POIs), names in Georgian and English, drawn by Planetiler from the same safety-clipped extract as the tiles. A road the router may never use is not on the map either; place labels, buildings and POIs in the occupied areas stay. |
| `georgia_geocoder.sqlite.gz` | The offline search database (SQLite with FTS5, gzip): places, streets, addresses and driver POIs from the same clipped extract. Places in the occupied territories are kept only to explain (occupied=1); nothing else there is searchable. `scripts/geocoder_search.py` is the reference search the app copies. |
| `glyphs.zip` | MapLibre label glyphs for the map styles, made from Noto Sans Georgian and Noto Sans (SIL Open Font License 1.1; the licence texts are inside under `LICENSES/`). |
| `sprites.zip` | The driving icon sprite sheets (1x and 2x) and their badge SVGs: glyphs from Maki and Temaki (CC0 1.0), badge designs MIT (texts inside under `LICENSES/`). |

The release tag is the OSM data time plus the first 8 hex digits of the
source commit, for example `osm-20260924T202102Z-3f2a1b0c`. New data or new
code always gets a new tag, so a safety fix ships on the next run even on
the same Geofabrik file. If a complete release with that tag already exists
(the same code on the same data), the run publishes nothing; a draft or a
release with missing files under that tag is deleted and published again.

## Licence duties for anyone who ships these files (the Vinari app included)

The tiles, nogo_zones.geojson, georgia.pmtiles and georgia_geocoder.sqlite.gz
are Derivative Databases of OpenStreetMap under the ODbL 1.0: roads are
deleted, access tags rewritten, time zones and admin areas added, names
folded into search keys, so none of them is a trivial transformation.

1. Credit OpenStreetMap. Show "© OpenStreetMap contributors" linked to
   <https://www.openstreetmap.org/copyright>. For the routing engine alone
   it may sit in a corner of the map, next to it, or on a splash screen, and
   may collapse after an "x" tap, on map interaction or after five seconds
   when it then stays reachable from an (i) button or the About screen
   ([OSMF Attribution Guidelines](https://osmfoundation.org/wiki/Licence/Attribution_Guidelines)).
   The turn instructions themselves need no credit.

   1b. The map credit. Every map drawn from georgia.pmtiles shows
   "[© OpenMapTiles](https://openmaptiles.org/) [© OpenStreetMap contributors](https://www.openstreetmap.org/copyright)"
   in the map corner at all times, turn-by-turn mode included, and never only
   behind an (i) button: the OpenMapTiles schema is CC-BY 4.0 and its
   LICENSE (v3.16) asks for a visible, linked credit in the corner of a
   browsable map, with no collapse allowance. Take the text and the links
   from manifest.json `map_attribution` (or set the style source's
   `attribution` to it and draw it yourself); do not rely on MapLibre's
   collapsible attribution control.
2. Offer the data (ODbL 4.6). The app's About/Licences screen links to this
   repository's Releases (every derivative database above, entire and free)
   and to the source at the commit named in manifest.json (the method).
   Never delete a release that an app version can still download, and never
   rewrite this repository's history.
3. Keep the notices with the data (ODbL 4.2, CC-BY 4.0, OFL 1.1). Ship
   manifest.json with the files, show its `credits` on the Licences screen
   and its `map_attribution` on the map. The Licences screen lists, from
   `credits`: OpenStreetMap (ODbL 1.0) with the OSM water polygons and
   osm-lakelines (both ODbL); the OpenMapTiles schema (CC-BY 4.0,
   <https://creativecommons.org/licenses/by/4.0/>); Natural Earth (public
   domain; the credit is optional but kept); the fonts: Noto Sans Georgian
   and Noto Sans, "Copyright 2022 The Noto Project Authors", with the full
   SIL Open Font License 1.1 texts from glyphs.zip `LICENSES/` (they must go
   with every copy); Maki and Temaki (CC0 1.0, nothing required, credited
   anyway) and the badge designs (MIT, text in sprites.zip `LICENSES/`).
   georgia.pmtiles carries its ODbL notice inside (the PMTiles
   `description`), and the search database in its `meta` table.
4. Share-alike (ODbL 4.4). Any data Vinari merges into these files (its own
   cameras, speed limits, closures) becomes part of the derivative database
   and must be published under the ODbL too. Keep the Roads Department
   closures as request-time exclusions in the app, never baked into the
   tiles, the map or the search database.
5. Technical protection (ODbL 4.7). An app store package, a Play asset pack
   or iOS Data Protection may put a copy behind technical measures; that is
   allowed as long as the same database stays available without them, which
   the public GitHub Release does. Keep it there.

## The safety clip, and why it exists

The Law of Georgia on Occupied Territories (Art 4(1)-(2),
[matsne.gov.ge/en/document/view/19132](https://matsne.gov.ge/en/document/view/19132))
lets foreign citizens and stateless persons enter Abkhazia only from the
Zugdidi municipality direction and the Tskhinvali region only from the Gori
municipality direction; every other direction, Russia included, is
prohibited. Criminal Code Art 322-1
([matsne.gov.ge/en/document/view/16426](https://matsne.gov.ge/en/document/view/16426))
punishes that entry with a fine or 2 to 4 years in prison, and 3 to 5 years
when done jointly by more than one person, repeatedly, with violence or the
threat of violence, or by someone already convicted of it. These two
articles do not cover Georgian citizens; for them the danger is detention by
the de facto authorities and Russian forces. Art 10(2) extends the law to
Perevi village (Sachkhere), Kurta, Eredvi, Azhara and Akhalgori. OSM draws
part of Perevi on the Georgian side of the line; the clip covers the whole
village, which follows a literal reading of Art 10(2) and is untested in
practice. People are detained at the occupation line every year, and on
6 November 2023 Tamaz Ginturi was shot dead near Kirbali. A navigator must
never lead a driver there, not even to a spot a few metres past the line.

Tag-based or request-time filters leak: in September 2026, 105 OSM ways
crossed the South Ossetia line (65 of them drivable for Valhalla) and 38
crossed the Abkhazia line, `access=permit` and `access=unknown` stay
drivable, one OSM edit can open a road, and a phone can send
`ignore_access`. So the roads are deleted from the data before the tiles are
built. `scripts/clip.py` does this:

1. **Hard no-go area.** Abkhazia (OSM relation 1152720), South Ossetia /
   Tskhinvali region (1152717) and the whole Perevi village, pushed outward
   by **100 m** in a metric projection. OSM has no boundary for Perevi, so
   its polygon is hand-made: the convex hull of the village's landuse
   patches (relation 15344012) and its occupied part (way 1167376334),
   pushed out by 100 m. Every road piece inside the no-go area is removed:
   a road that runs into it is cut at its last node outside, and a road
   whose straight segment clips a corner of it is cut there too. The buffer
   absorbs OSM line precision (7 to 34 m at the known crossings) and
   touches no Georgian village centre. It must stay well below 410 m,
   because the S1/E60 motorway passes 412 m from the line near Khurvaleti;
   `clip.py` refuses anything above 300 m.
2. **Outside Georgia.** Everything outside Georgia's recognised border
   (relation 28699) is removed, and every straight segment that is kept
   must lie wholly inside the border, so a long segment cannot cut across a
   bend of it. The Geofabrik extract reaches about 5 km into Russia, so
   without this the graph would contain Upper Lars (A-161), the north side
   of the Roki tunnel (A-164), the Psou bridges, Veseloye and the Russian
   side of the Mamison road. A ferry or car train that leaves Georgia or
   touches the no-go area is dropped whole: a boat that leaves the country
   has no legal use in these tiles.
3. **Soft band, 100 to 500 m.** Minor roads (secondary and below) are split
   where they enter or leave the band, and every piece inside it becomes
   destination-only (`motorcar=destination`), so Valhalla does not use it
   as a through-route but a village there stays reachable. A segment counts
   as in the band when either end is inside it or the straight line
   between them crosses it. Motorway, trunk and primary roads are never
   touched, as in the research. Roads where cars are already banned stay
   banned. Fences have moved 50 to 500 m past the OSM line in places, which
   is why the band exists; the app adds a spoken warning near the line.
4. **4x4-only roads** (`4wd_only=yes`) become destination-only as well.
   This does the job of the custom `graph.lua` the research proposed,
   without keeping a fork of Valhalla's tag script.
5. **Relations.** A turn restriction that mentions a removed way is
   dropped. One on a cut or split way is kept when its via node lies on
   exactly one surviving piece of that way (the member then points at that
   piece), and dropped otherwise. Other relations lose the members that no
   longer exist and list every surviving piece of a split way. Boundaries,
   buildings and all nodes pass through, so Valhalla's admin and time-zone
   lookups work.

The clip also records, for each must-fail target of the gate, how far it
lies from the nearest car-class road in the raw extract (clip_report.json).

### Frozen polygons

The polygons live in `config/boundaries/` as frozen GeoJSON, with the OSM
version they came from. `config/clip.json` names each object, its version
and the buffer sizes, and carries its own `version` number, which goes into
`manifest.json`. The Perevi polygon is marked hand-made (`"hand_made": true`,
with a geometry hash): `fetch_boundaries.py --refresh` never overwrites it,
and its OSM sources are kept under `config/boundaries/sources/`.

The build never updates the polygons by itself. A separate weekly job,
`boundaries`, rebuilds each polygon from live OSM and compares the geometry
itself, not only the version number (moving one node of the line changes
only that node's version). It prints the largest shift in metres, checks
that the Perevi sources still lie inside the hand-made polygon, and fails
when anything moved, so GitHub emails the owner. It never blocks the build
or the release, which keep using the frozen copies. To accept a change:

```sh
python3 scripts/fetch_boundaries.py --refresh   # rewrites the OSM copies in config/boundaries/
git diff config/boundaries/                     # review the moved line by eye
# then update osm_version values, bump "version" in config/clip.json, add a
# changelog line, and re-check the points in config/gate_routes.json
```

## The safety gate

`scripts/gate.py` runs against a Valhalla service started from the new tar,
inside the runner. The release happens only if every check passes.

- **Setup.** The test data has at least the reviewed baseline (35 must-fail,
  9 must-succeed, 10 no-go points, 7 band points; lowering these needs a
  change to `gate.py`). Valhalla reports 3.6.3 and, through verbose
  `/status`, that tiles, admin areas and time zones loaded. The SHA-256 of
  the tar under test goes into gate_results.json, and `manifest.py` refuses
  to publish any other tar, a gate run without its edge scan, or a gate
  run for another clip config version.
- **Edge scan.** Every edge in the tiles (`valhalla_export_edges`) must stay
  out of the no-go area and lie wholly inside Georgia (whole lines, not
  only their points). Rows are split by 0x1E, so a street name with a
  newline cannot break the scan; an unparseable row fails it.
- **Must fail.** Towns: Gori to Tskhinvali, Tbilisi to Akhalgori, Zugdidi to
  Gali, Sachkhere to Java, Tsalenjikha to Tkvarcheli, Kutaisi to Sukhumi,
  Sachkhere to both parts of Perevi, Mestia to Azhara and to Chkhalta
  (Kodori). Across the border: road points 195 to 435 m past the Upper
  Lars (A-161), Mamison, Sadakhlo (M-6), Red Bridge (M2) and Sarpi (D 010)
  crossings, all inside the Geofabrik extract. Stubs: the inner end of 20
  known roads across the line, including Ochake-Chegali, Saberio-Pakhulani,
  the Racha side of the Mamison road and the Svaneti road into Kodori.
  Each test runs four requests:
  - control: the same origin to a legal point on the approach road, at
    least 600 m from the line and the border, must route, or the test could
    pass only because the origin is cut off;
  - strict: the target may only snap within 50 m; there must be no road.
    The raw extract must have had a road there (clip_report.json), or the
    test proves nothing and fails;
  - loose: Valhalla's default snapping (up to 35 km); any route returned
    must stay out of the no-go area and inside Georgia, and if it ends in
    the band it must end on a destination-only edge (checked with
    `/locate`) or on a main road the band exempts;
  - app: the app's own request (see below). Where no legal ground lies
    within its 300 m cutoff it must find nothing; elsewhere the loose rules
    apply.
- **App pre-check.** The published `nogo_zones.geojson` must refuse every
  must-fail target, allow every must-succeed and control point, and ask for
  confirmation at every band point.
- **Band.** At 7 points in the band on roads that used to stay open to
  through traffic, `/locate` must find only destination-only edges.
- **Must succeed**, in July and in January, with alternates, never entering
  the no-go area: Tbilisi to Batumi, Kutaisi, Telavi and Stepantsminda,
  Zugdidi to Mestia, Gori to Khashuri, Tbilisi to Gori and back (must pass
  S1 at its closest point to the line), Batumi to Sarpi.
- **Seasonal.** Pshaveli to Omalo over Sh44 (`no @ (Nov-Jun)`) must not use
  Sh44 on 15 December, with both of Valhalla's search types, and must use it
  on 15 July.
- The test points themselves are checked: every place the law names
  (Kurta, Eredvi, Azhara, Akhalgori, Perevi and its four built-up patches)
  must be inside the no-go area, every control point legal and at least
  600 m from the line and the border, every band point in the band.

Coordinates and their sources are in `config/gate_routes.json`.

## What the app must do

The tiles cannot stop Valhalla's default snapping (up to 35 km) from
turning "navigate to Tskhinvali" into a route to the nearest legal road,
which can end about 120 m from the line. After the clip that road is
destination-only, but only the app can refuse the request itself. The
contract, which the gate tests with the settings in
`config/gate_routes.json` `app_request`:

1. Before routing, refuse any origin or destination inside `no_go_hard` or
   outside `georgia` in the release's `nogo_zones.geojson`, and ask for
   confirmation inside `soft_band_outer`.
2. Send the destination with `search_cutoff` 300 m (costing `auto`,
   `date_time` type 0, `prioritize_bidirectional` true, `alternates` 2), and
   reject a result whose snapped end lies more than 300 m from the
   requested point.
3. Check every returned shape against `no_go_hard` and `georgia` as a second
   layer, and refuse to navigate a route that enters either.
4. Search (georgia_geocoder.sqlite.gz, searched the way
   `scripts/geocoder_search.py` does): places inside `no_go_hard` are kept
   with `occupied=1` so the app can explain instead of routing; streets,
   addresses and POIs there are not in the database, and no legal row has an
   occupied settlement as its city. The reference search returns
   `routable` (false for them) and a `reason`: `occupied` (inside the
   occupied territories as drawn, Perevi included) or `occupation_line`
   (Georgian-controlled, but within 100 m of the line). Show the matching
   legal explanation for each reason, never a route, never a "navigate"
   button. A query that names an occupied place beside other words
   ("rustaveli sokhumi", "სოხუმის აეროპორტი") returns that place first
   (`anchor: occupied`). For an occupied place show `label_ka` / `label_en`
   (from name:ka, romanised by the national system) and never `name` or
   `name_en`: there they are the de facto authorities' forms (Ленингор,
   Sukhum). Occupied places without name:ka are left out for now (config
   `places.occupied_without_name_ka`, an owner decision). A
   `barrier=border_control` near the occupation line is kind
   `line_checkpoint`, never a border crossing.
   Two ranking rules the app must copy exactly (config/geocoder.json
   `ranking.exact_first` and `ranking.other_settlement_penalty`, with
   their `_note`s):
   a place or named feature whose name is exactly the whole query comes
   before the streets and POIs that only carry that name, and before every
   row found by reading a word as a settlement ("თბილისის ზღვა" is the
   reservoir, not "ზღვა" in Tbilisi). Only carrying the name means: a
   street named after a settlement or feature and nothing else
   ("ერედვის ქუჩა"), a route that lists it ("სენაკი — ფოთი — სარფი"), a
   street or POI within 10 km of it, any street or POI for a city or town.
   A district (suburb, quarter, neighbourhood) holds nothing by name alone,
   so Batumi's "შოთა რუსთაველის ქუჩა" is not held below Rustavi's
   "შოთა რუსთაველის დასახლება"; a POI that only shares a far place's name
   (hotel "ალმა" in Tbilisi, hamlet ალმა far away) keeps its rank. An
   occupied place holds rows too, so its bare name explains first, except
   that with a position it holds rows near the user (inside the local
   search box) only as a route that lists it or a neighbour within 10 km:
   "თავისუფლება" in Tbilisi is Freedom Square, without a position the
   occupied village. And when the app has no position and the query names
   no settlement, streets and addresses that do not fit like the best rows
   of the most important settlement among the best-fitting rows rank lower:
   the best fit is an exact house number before one that holds it among
   others ("20-22") before one it only begins ("49ა"), a current name
   before an old_name, then the text score. So "ჭავჭავაძის 37" is
   Tbilisi's when Tbilisi has a 37, but Batumi's exact 49 beats Tbilisi's
   49ა, and a street with Lermontov's name now beats one that had it.

When the app's request changes, change `app_request` in the same release.

## Pipeline

`.github/workflows/build.yml`, weekly (Monday 03:37 UTC) and on demand.
The token has no rights by default; each job asks for its own, and every job
but `publish` can only read.

- `boundaries`: `fetch_boundaries.py --check --fail-on-change`. It never
  blocks anything else.
- `build`:
  1. Install the pinned Python packages; run `scripts/test_clip.py`.
  2. Download `georgia-latest.osm.pbf` (following Geofabrik's redirect to
     the dated file, which must match the expected URL) and verify its `.md5`.
  3. `clip.py`.
  4. `build_tiles.sh`: Valhalla config (car only: no pedestrian, bicycle or
     driveway ways, no construction; verbose status on), admin areas, time
     zones (downloaded with up to three tries, then their content checked),
     tiles, tar.
  5. Start `valhalla_service` on the tar, export all edges, run `gate.py`,
     save the service log.
  6. `manifest.py` (compares the edge count with the previous release and
     refuses a drop of more than 20%) for the tar and the zones, then hand
     on the tar, the manifest and the zones, the manifest and zones alone,
     and the clipped extract, each as a 3-day artifact (so "Re-run failed
     jobs" works the next day).
- `glyphs-sprites`, beside `build` (it reads no OSM data): builds
  maplibre/font-maker at a pinned commit, downloads the pinned Noto fonts,
  renders and gates `glyphs.zip`; draws the badges, renders them with the
  pinned spreet and gates `sprites.zip`.
- `basemap`, after `build`: Java 21 (Temurin, `actions/setup-java`),
  `build_basemap.sh` (the pinned Planetiler jar on the clipped extract, every
  layer, `--languages=ka,en`), then `basemap.py gate` and `basemap.py
  report`. Its step limits are 60 + 30 minutes inside a 120-minute job.
- `geocoder`, after `build`: checks that the clipped extract and the zones
  are the ones build's manifest lists, builds the database, runs
  `geocoder_gate.py` (known queries through the reference search), packs it
  and writes its report.
- `release-manifest`, after all four: `manifest.py --extend` adds
  glyphs.zip, sprites.zip, georgia.pmtiles and georgia_geocoder.sqlite.gz to
  build's manifest. Every report must have passed and match its file; the
  map's and the search database's must name the same OSM time, clipped
  extract, clip config and commit (all four); it writes `credits` and
  `map_attribution`.
- `publish` (the only job with write access, no checkout, no third-party
  code but `download-artifact`): runs only after every job above but
  `boundaries` passed, and only on the default branch (scheduled runs
  always are). It checks that the final manifest keeps everything build
  wrote, lists exactly the six files, records a passed gate for glyphs,
  sprites, basemap and geocoder and carries the credits; checks every file
  against its SHA-256; and uploads exactly the files the manifest lists with
  `gh release create`.

Pins: the Valhalla image is `ghcr.io/valhalla/valhalla-scripted:3.6.3` by
digest; the four third-party actions (`actions/checkout`,
`actions/upload-artifact`, `actions/download-artifact`,
`actions/setup-java`) by commit SHA; font-maker by commit (its submodules
by that commit); spreet, the Noto fonts, the Planetiler jar and the Maki
and Temaki SVGs by SHA-256; Natural Earth and the lake lines by size (the
OSM water polygons change every few days and are recorded, not pinned);
the Python packages with their dependencies by version and hash in
`requirements.lock` (`requirements.txt` lists the four direct ones).
Tiles must be built with the same Valhalla version the app embeds
(valhalla-mobile 0.6.3 embeds 3.6.3): when the app moves, move the image
pin and rebuild in the same release.

Two settings are left out on purpose. Speeds: no `default_speeds_config`,
so the tiles use Valhalla's built-in class speeds, the same every week (the
image's own start-up script would fetch an unpinned file); the research's
Phase 0 speed model will come as a vendored, pinned file. Live traffic: no
`traffic.tar`; when the phone-side traffic of Phase 3 comes, build it with
`valhalla_build_extract -t` and publish it with the tiles, since it must
match the tile set.

### Where the workflow runs

GitHub reads workflows only from `.github/workflows/` at the root of a
repository. This folder must become the root of its own public repository
(the User-Agent in `fetch_boundaries.py` already names
`peghe-b/vinari-maps`); committed inside another repository it never runs.
After the first push, start the workflow once by hand, check the run time
and the gate counts, and then set `MIN_EDGES` in `scripts/gate.py` to about
70% of the `edges` value in gate_results.json. That first run also
calibrates the other thresholds that are still estimates, and they will
probably block the first release: the basemap size band
(`config/basemap.json` gate `min_bytes`/`max_bytes`), the geocoder row and
kind bands (`config/geocoder_gate.json` `counts`, `kinds`), and the basemap
job's run time, disk and memory (its "Disk use" step prints them). Watch
the font-maker compile in `glyphs-sprites` too: it has not been built on
ubuntu-24.04 yet.

## Running things locally

Do not build tiles, the map or the search database on a laptop; the
workflow is the build machine. The unit tests need no map downloads, only
three Python packages:

```sh
python3 -m venv .venv && .venv/bin/pip install shapely pyproj osmium
.venv/bin/python scripts/test_clip.py
.venv/bin/python scripts/test_basemap.py
.venv/bin/python scripts/test_geocoder.py
python3 scripts/test_glyphs_sprites.py        # standard library only
python3 scripts/geocoder_fold.py --check      # the fold's shared vectors
```

On Python 3.12 the exact CI set installs with
`.venv/bin/pip install --only-binary :all: --require-hashes -r requirements.lock`.
Without shapely (or pyosmium) the tests that need it are skipped rather than
failed; the workflow fails on any skip.

## Known limits

- The OSM line is not the fence on the ground. The 100 m buffer, the
  500 m band and the app's own checks lower the risk; they cannot remove it.
- The legal reading above has not been reviewed by a Georgian lawyer. Open
  question: whether a navigator that offered routes into the territories
  would be "otherwise facilitating" "international overland traffic" under
  Art 6(1) of the law (Criminal Code Art 322-2). The clip keeps Vinari clear
  of that question too.
- Seasonal and sudden closures that OSM does not tag (Jvari, Dariali,
  Korsha to Shatili, Ushguli to Lasdili) are not in these tiles; the app
  handles them from the Roads Department feed.
- Month rules such as Sh44's `no @ (Nov-Jun)` work only with time zones.
  Valhalla 3.6.3 treats a rule as never active when it cannot find the time
  zone (`is_conditional_active` returns false; seen in a local test on
  2026-09-26: without time zones Sh44 routed in December). The build adds
  time zones and the seasonal gate checks the result, but the app's
  Valhalla also needs a time zone database at run time: valhalla-mobile
  bundles one on iOS; on Android this is unverified. The app must send
  `date_time` (type 0) on every request, or month rules are skipped.
- The gate's points follow OSM ways. When a stub or band way is edited or
  deleted, its check can fail and block the release until someone moves the
  point. That is on purpose.
- `MIN_EDGES` is a placeholder until the first green run (see "Where the
  workflow runs"); until then the 20% comparison with the previous release
  is the stronger check.
- GitHub may throttle or suspend release downloads it judges significantly
  excessive (Acceptable Use Policies, section 9). That is fine for a few
  hundred installs. Before many phones download the tar, deliver it
  through App Store and Play asset packs (free), and keep the GitHub
  release as the public ODbL copy the app links to.
- GitHub disables scheduled workflows in public repositories after 60 days
  without repository activity; re-enable the workflow if that happens.
- The geocoder's ranking was tuned on a local build over a sample of the
  2026-09-25/26 data, not on the whole country; the known queries of
  `config/geocoder_gate.json` are what holds it in place. The first full
  run failed five of them (a village, a resort village and an occupied
  place losing to roads that carry their names, a reservoir losing to a
  street found through "Tbilisi", an address in Kutaisi before Tbilisi's);
  config v3 fixed them with general rules, checked on the sample plus the
  real OSM objects behind those five, and swept old against new over every
  street, POI and place name of the sample, with and without a position,
  and 1500 house numbers. Against the released rules, the remaining
  changes are intended: without a position the capital's street of the
  same name comes first (15 street names in the Batumi-heavy sample), a
  POI next to a hamlet or locality of its own name comes right after it,
  and a hotel named after a town ("ყაზბეგი" in Batumi) comes after the
  town.
- Owner decisions still open: what search shows for the occupied places
  that have no name:ka (hidden until decided;
  `places.occupied_without_name_ka` in `config/geocoder.json`), and whether
  the badge designs stay MIT or are dedicated to CC0
  (`config/sprites.json` changelog).

## Licences

- Code in this repository: MIT (see `LICENSE`, which also names the data
  and third-party files it does not cover).
- Map data and every released database (tiles, zones, georgia.pmtiles,
  georgia_geocoder.sqlite.gz): ODbL 1.0, © OpenStreetMap contributors.
  georgia.pmtiles also follows the OpenMapTiles schema (CC-BY 4.0).
- glyphs.zip: SIL Open Font License 1.1 (Noto Sans Georgian and Noto Sans,
  Copyright 2022 The Noto Project Authors). sprites.zip: Maki and Temaki
  glyphs CC0 1.0, badge designs MIT.
- Tools that run in CI and never ship: Valhalla 3.6.3 image (Valhalla MIT;
  the image also carries SpatiaLite MPL-1.1/GPL-2.0+/LGPL-2.1+, GEOS
  LGPL-2.1, ZeroMQ MPL-2.0), pyosmium BSD-2-Clause, shapely BSD-3-Clause
  (its wheel bundles GEOS, LGPL-2.1), pyproj MIT, numpy BSD-3-Clause (its
  wheel bundles GCC runtime libraries under the GCC Runtime Library
  Exception), requests Apache-2.0, urllib3 MIT, idna BSD-3-Clause,
  charset-normalizer MIT, certifi MPL-2.0; Planetiler Apache-2.0 (the jar
  also bundles LGPL GeoTools with the EPSG dataset terms, EDL JTS, ICU and
  others per its NOTICE.md), planetiler-openmaptiles BSD-3-Clause, Eclipse
  Temurin 21 GPL-2.0 with the Classpath Exception; maplibre/font-maker
  BSD-3-Clause (LICENSE.txt checked at 714ccaea) with sdf-glyph-foundry and
  protozero BSD-2-Clause, cxxopts and gulrak/filesystem MIT, Ubuntu's
  FreeType (FreeType License), Boost (BSL-1.0), clang (Apache-2.0 with LLVM
  exception) and CMake (BSD-3-Clause); spreet MIT; actions/checkout,
  actions/upload-artifact, actions/download-artifact, actions/setup-java and
  gh (MIT); and the runner's GNU tools (GPL). Running them is not
  distribution, and no code from any of them is in the released files: the
  map, the glyphs and the sprites are data made by these tools, not
  derivatives of their code.
- Data sources: OpenStreetMap via Geofabrik (ODbL 1.0);
  timezone-boundary-builder 2025b (data ODbL 1.0, code MIT), downloaded by
  `valhalla_build_timezones` (re-check the release it names whenever the
  Valhalla image pin moves); OSM water polygons from osmdata.openstreetmap.de
  (ODbL 1.0); osm-lakelines v12 (data ODbL 1.0, code MIT); Natural Earth
  (public domain); the OpenMapTiles schema 3.16 (CC-BY 4.0).
- When the app's map style ships: the research plans the "Vinari Day" style
  from OSM Liberty, whose style JSON is BSD-3-Clause and whose design derives
  from Mapbox's OSM Bright (CC-BY 3.0); add both to the Licences screen, and
  point its Roboto font stacks at the Noto stacks of glyphs.zip so no
  Apache-2.0 font is needed.
- Nothing GPL or AGPL ships in the tiles, the map, the search database, the
  glyphs, the sprites or the app.
