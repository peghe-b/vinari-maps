#!/usr/bin/env python3
"""Label glyphs for the Vinari map styles, released as glyphs.zip.

MapLibre draws label text from pre-rendered glyphs (signed distance fields,
SDF): one protobuf file per font stack and block of 256 codepoints, found
through the style's "glyphs" URL ".../{fontstack}/{range}.pbf". The app
ships these files, so labels never need a glyph server (the research's
offline rule: no tile, glyph or sprite URL may point at the internet).

Font stacks (config/glyphs.json). Each lists its fonts in fallback order;
the first font that has a character draws it:

  "Noto Sans Georgian Regular"   Noto Sans Georgian Regular, then Noto Sans Regular
  "Noto Sans Georgian Bold"      Noto Sans Georgian Bold, then Noto Sans Bold
  "Noto Sans Regular"            Noto Sans Regular, then Noto Sans Georgian Regular
  "Noto Sans Bold"               Noto Sans Bold, then Noto Sans Georgian Bold
  "Noto Sans Georgian Regular,Noto Sans Regular"   same fonts as the first
  "Noto Sans Georgian Bold,Noto Sans Bold"         same fonts as the second

Every stack is complete on its own. Noto Sans Georgian has no digits and no
Latin letters (5 codepoints below U+0100), so a stack of that font alone
would drop the "9" from "მე-9 ქუჩა"; the Noto Sans fallback adds Latin,
digits, punctuation, Greek and Cyrillic (Russian and the Abkhaz letters,
for names in the occupied regions). MapLibre asks for a text-font list as
one comma-joined stack, so the two comma stacks serve a style that lists
the Georgian font and the Latin one; a style that names one font finds its
stack too.

Mtavruli: MapLibre's text-transform "uppercase" turns Georgian into
Mtavruli (U+1C90-U+1CBF) on iOS and Android 10+, and silently skips every
character the glyphs lack, so a label in a font without Mtavruli vanishes.
Noto Sans Georgian has all 46 Mtavruli letters; the gate checks every
stack for them anyway.

Subcommands (as in the workflow):
  python3 scripts/glyphs.py fetch --out build/fonts
      Download the pinned fonts; each must match its size and SHA-256.
  python3 scripts/glyphs.py build --fonts build/fonts \
      --font-maker build/tools/font-maker-build/font-maker --out build/glyphs
      Run font-maker once per stack, into build/glyphs/<stack>/<range>.pbf.
  python3 scripts/glyphs.py pack --glyphs build/glyphs \
      --out build/glyphs-sprites/glyphs.zip --report build/glyphs-sprites/glyphs_report.json
      Zip the stacks with the OFL texts and a NOTICE, read the zip back and
      run the gate on exactly those bytes. On any failure the zip is
      deleted and the exit code is 1. The report is what manifest.py
      --asset-report reads.
  python3 scripts/glyphs.py check PATH   (a glyph folder or glyphs.zip)

Gate (every check must pass):
  - layout: each stack has all 256 range files, 0-255 to 65280-65535 (an
    empty range is a small valid file, so MapLibre never meets a missing
    file), and nothing else is in the zip but LICENSES/ and NOTICE.txt;
  - every file parses as a glyph PBF with exactly one font stack whose
    name is the folder name and whose range is the file name; every glyph
    has its required fields, an id inside the range and no duplicate, and
    a bitmap of exactly (width + 6) x (height + 6) bytes (font-maker pads
    3 px per side);
  - ranges 0-255, 4096-4351 (Georgian) and 7168-7423 (Mtavruli) hold
    glyphs in every stack;
  - every stack draws printable ASCII, all 48 Mkhedruli letters
    (U+10D0-U+10FF), all 46 Mtavruli letters, Russian Cyrillic, the Abkhaz
    letters ҟ ҩ ҵ ә ԥ, and „ “ – — № ₾;
  - each Bold stack draws "A" and "ა" differently from its Regular stack
    (catches a Bold stack built from the Regular file);
  - stacks built from the same font list are identical glyph for glyph.

Licences: the fonts are OFL-1.1 (no Reserved Font Name, so the stack
names may say "Noto"); glyph files made from them stay OFL-1.1 and ship
with the licence texts. font-maker is BSD-3-Clause; the FreeType it links
is used under the FreeType License. The tools run in CI only; nothing of
them is in the glyph files. Standard library only.
"""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from pack_util import (Results, file_entry, read_tree, read_zip, sha256_bytes,  # noqa: E402
                       sha256_file, write_zip)

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG = ROOT / "config" / "glyphs.json"
LICENCE_DIR = ROOT / "config" / "glyphs"
USER_AGENT = "vinari-maps glyph build (https://github.com/peghe-b/vinari-maps)"

RANGE_STARTS = tuple(range(0, 65536, 256))   # the whole Basic Multilingual Plane
SDF_BUFFER = 3          # font-maker (sdf-glyph-foundry) pads each bitmap by 3 px per side
MAX_GLYPH_SIDE = 255    # at 24 px no real glyph comes near this; larger means a broken file
LICENCE_FOLDER = "LICENSES"
NOTICE_NAME = "NOTICE.txt"

# Ranges that must hold glyphs in every stack.
REQUIRED_RANGES = ("0-255", "4096-4351", "7168-7423")


def _span(first, last):
    return set(range(first, last + 1))


# Characters every stack must draw. A label loses any character its stack
# lacks, silently, so these are what Georgian map labels need.
REQUIRED_CODEPOINTS = {
    "printable ASCII (Latin letters, digits, punctuation)": _span(0x20, 0x7E),
    "Georgian Mkhedruli": _span(0x10D0, 0x10FF),
    # Unicode leaves U+1CBB and U+1CBC unassigned: 46 letters.
    "Georgian Mtavruli": _span(0x1C90, 0x1CBA) | _span(0x1CBD, 0x1CBF),
    "Russian Cyrillic": _span(0x410, 0x44F) | {0x401, 0x451},
    "Abkhaz Cyrillic letters": {0x49F, 0x4A9, 0x4B5, 0x4D9, 0x525},
    "Georgian quotes, dashes, numero sign and lari sign": {0x201E, 0x201C, 0x2013, 0x2014,
                                                          0x2116, 0x20BE},
}

# Drawn differently by Regular and Bold: Latin A and Georgian ა.
WEIGHT_SAMPLES = (0x41, 0x10D0)


# ---------------------------------------------------------------------------
# Config

def load_config(path=DEFAULT_CONFIG):
    """Read config/glyphs.json and refuse anything inconsistent."""
    config = json.loads(Path(path).read_text(encoding="utf-8"))
    problems = []
    if not isinstance(config.get("version"), int):
        problems.append("version must be an integer")
    fonts = config.get("fonts") or []
    files = [f.get("file") for f in fonts]
    if len(set(files)) != len(files):
        problems.append("font file names must be unique")
    licence_files = {entry["file"] for entry in config.get("licence_files") or []}
    for font in fonts:
        name = font.get("file")
        if not name or "/" in name or not name.endswith((".ttf", ".otf")):
            problems.append(f"font {name!r}: file must be a plain .ttf or .otf name")
        if not str(font.get("url", "")).startswith("https://"):
            problems.append(f"font {name}: url must be https")
        if not re.fullmatch(r"[0-9a-f]{64}", str(font.get("sha256", ""))):
            problems.append(f"font {name}: sha256 must be 64 lowercase hex digits")
        if not isinstance(font.get("bytes"), int) or font["bytes"] <= 0:
            problems.append(f"font {name}: bytes must be a positive integer")
        if font.get("licence_file") not in licence_files:
            problems.append(f"font {name}: licence_file {font.get('licence_file')!r} is not listed")
    stacks = config.get("stacks") or []
    names = [s.get("name") for s in stacks]
    if not stacks:
        problems.append("no stacks")
    if len(set(names)) != len(names):
        problems.append("stack names must be unique")
    for stack in stacks:
        name = stack.get("name") or ""
        if (not name or name != name.strip() or name.startswith(".") or "/" in name
                or "\\" in name or name == LICENCE_FOLDER):
            problems.append(f"stack {name!r}: not usable as a folder name")
        if not stack.get("fonts"):
            problems.append(f"stack {name!r}: no fonts")
        for f in stack.get("fonts") or []:
            if f not in files:
                problems.append(f"stack {name!r}: font {f!r} is not in fonts")
    if problems:
        raise ValueError(f"{path}: " + "; ".join(problems))
    return config


def licence_members(config, licence_dir=LICENCE_DIR):
    """The vendored OFL texts, checked against their pinned SHA-256."""
    members = {}
    for entry in config["licence_files"]:
        data = (Path(licence_dir) / entry["file"]).read_bytes()
        if sha256_bytes(data) != entry["sha256"]:
            raise ValueError(f"{entry['file']}: sha256 {sha256_bytes(data)} does not match "
                             f"the config ({entry['sha256']})")
        members[f"{LICENCE_FOLDER}/{entry['file']}"] = data
    return members


# ---------------------------------------------------------------------------
# Fonts

def font_problems(font, data):
    problems = []
    if len(data) != font["bytes"]:
        problems.append(f"{font['file']}: {len(data)} bytes, expected {font['bytes']}")
    if sha256_bytes(data) != font["sha256"]:
        problems.append(f"{font['file']}: sha256 {sha256_bytes(data)}, expected {font['sha256']}")
    return problems


def download(url, limit, timeout=120, attempts=4):
    """GET a URL (at most limit bytes). Retries a few times."""
    last_error = None
    for attempt in range(attempts):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.read(limit + 1)
        except Exception as exc:  # network hiccup: wait and try again
            last_error = exc
            time.sleep(5 * (attempt + 1))
    raise RuntimeError(f"could not fetch {url}: {last_error}")


def fetch(config, out_dir):
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    for font in config["fonts"]:
        target = out / font["file"]
        if target.is_file() and not font_problems(font, target.read_bytes()):
            print(f"ok  {font['file']} (already here)")
            continue
        data = download(font["url"], font["bytes"])
        problems = font_problems(font, data)
        if problems:
            raise SystemExit("::error::" + "; ".join(problems))
        part = target.with_name(target.name + ".part")
        part.write_bytes(data)
        part.replace(target)
        print(f"ok  {font['file']} {font['family']} {font['style']} {font['version']}, "
              f"{len(data)} bytes, sha256 {font['sha256'][:12]}...")


# ---------------------------------------------------------------------------
# font-maker

def build(config, fonts_dir, font_maker, out_dir, log_path=None):
    """Run font-maker once per stack; the result is out_dir/<stack>/<range>.pbf."""
    fonts_dir, out = Path(fonts_dir), Path(out_dir)
    for font in config["fonts"]:  # the files font-maker reads are the pinned ones
        problems = font_problems(font, (fonts_dir / font["file"]).read_bytes())
        if problems:
            raise SystemExit("::error::" + "; ".join(problems) + " (run fetch first)")
    shutil.rmtree(out, ignore_errors=True)
    out.mkdir(parents=True)
    work = out.with_name(out.name + ".work")
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir()
    log = open(log_path, "a", encoding="utf-8") if log_path else None
    try:
        for index, stack in enumerate(config["stacks"]):
            target = work / str(index)  # font-maker insists on a folder that does not exist yet
            cmd = ([str(font_maker), "--name", stack["name"], str(target)]
                   + [str(fonts_dir / f) for f in stack["fonts"]])
            started = time.time()
            proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                  text=True, errors="replace")
            if log:
                log.write(f"$ {' '.join(cmd)}\n{proc.stdout}\n")
            produced = target / stack["name"]
            if proc.returncode != 0 or not produced.is_dir():
                tail = "\n".join(proc.stdout.splitlines()[-20:])
                raise SystemExit(f"::error::font-maker failed for '{stack['name']}' "
                                 f"(exit {proc.returncode}):\n{tail}")
            shutil.move(str(produced), str(out / stack["name"]))
            count = sum(1 for _ in (out / stack["name"]).glob("*.pbf"))
            print(f"ok  {stack['name']}: {count} range files ({time.time() - started:.1f} s)")
    finally:
        if log:
            log.close()
    shutil.rmtree(work, ignore_errors=True)


# ---------------------------------------------------------------------------
# Glyph PBF reading (glyphs.proto: glyphs{fontstack=1}, fontstack{name=1,
# range=2, glyphs=3}, glyph{id=1, bitmap=2, width=3, height=4, left=5 (sint32),
# top=6 (sint32), advance=7})

class PbfError(ValueError):
    pass


def _varint(buf, pos):
    result, shift = 0, 0
    while True:
        if pos >= len(buf):
            raise PbfError("truncated varint")
        byte = buf[pos]
        pos += 1
        result |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return result, pos
        shift += 7
        if shift > 63:
            raise PbfError("varint longer than 10 bytes")


def _fields(buf):
    """(field number, wire type, value) for each field of one message."""
    pos, end = 0, len(buf)
    while pos < end:
        key, pos = _varint(buf, pos)
        field, wire = key >> 3, key & 7
        if field == 0:
            raise PbfError("field number 0")
        if wire == 0:
            value, pos = _varint(buf, pos)
        elif wire == 2:
            length, pos = _varint(buf, pos)
            if pos + length > end:
                raise PbfError("length runs past the end")
            value = bytes(buf[pos:pos + length])
            pos += length
        elif wire in (1, 5):
            size = 8 if wire == 1 else 4
            if pos + size > end:
                raise PbfError("fixed field runs past the end")
            value = bytes(buf[pos:pos + size])
            pos += size
        else:
            raise PbfError(f"unsupported wire type {wire}")
        yield field, wire, value


def _zigzag(n):
    return (n >> 1) ^ -(n & 1)


GLYPH_FIELDS = {1: ("id", 0), 2: ("bitmap", 2), 3: ("width", 0), 4: ("height", 0),
                5: ("left", 0), 6: ("top", 0), 7: ("advance", 0)}


def _glyph(buf):
    glyph = {}
    for field, wire, value in _fields(buf):
        if field not in GLYPH_FIELDS:
            continue
        key, want = GLYPH_FIELDS[field]
        if wire != want:
            raise PbfError(f"glyph field {key} has wire type {wire}")
        glyph[key] = _zigzag(value) if key in ("left", "top") else value
    return glyph


def parse_glyphs(data):
    """Decode a glyph PBF into [{"name", "range", "glyphs": [...]}]."""
    stacks = []
    for field, wire, value in _fields(data):
        if field != 1:
            continue  # the format allows extensions; ignore unknown fields
        if wire != 2:
            raise PbfError("fontstack is not a message")
        stack = {"name": None, "range": None, "glyphs": []}
        for f, w, v in _fields(value):
            if f in (1, 2):
                if w != 2:
                    raise PbfError("fontstack name/range is not a string")
                try:
                    stack["name" if f == 1 else "range"] = v.decode("utf-8")
                except UnicodeDecodeError:
                    raise PbfError("fontstack name/range is not UTF-8")
            elif f == 3:
                if w != 2:
                    raise PbfError("glyph is not a message")
                stack["glyphs"].append(_glyph(v))
        stacks.append(stack)
    return stacks


def range_problems(found, stack, rng, start):
    """What is wrong with one decoded range file (at most a few lines)."""
    problems = []
    if len(found) != 1:
        return [f"{rng}.pbf: {len(found)} font stacks, expected 1"]
    fs = found[0]
    if fs["name"] != stack:
        problems.append(f"{rng}.pbf: stack name {fs['name']!r}, expected {stack!r}")
    if fs["range"] != rng:
        problems.append(f"{rng}.pbf: range {fs['range']!r}, expected {rng!r}")
    seen = set()
    for g in fs["glyphs"]:
        missing = [k for k in ("id", "width", "height", "left", "top", "advance") if k not in g]
        if missing:
            problems.append(f"{rng}.pbf: a glyph lacks {', '.join(missing)}")
            continue
        cp = g["id"]
        if not start <= cp <= start + 255:
            problems.append(f"{rng}.pbf: glyph U+{cp:04X} is outside the range")
        if cp in seen:
            problems.append(f"{rng}.pbf: glyph U+{cp:04X} appears twice")
        seen.add(cp)
        w, h = g["width"], g["height"]
        if w > MAX_GLYPH_SIDE or h > MAX_GLYPH_SIDE:
            problems.append(f"{rng}.pbf: glyph U+{cp:04X} is {w}x{h} px")
        elif w and h:
            want = (w + 2 * SDF_BUFFER) * (h + 2 * SDF_BUFFER)
            if len(g.get("bitmap", b"")) != want:
                problems.append(f"{rng}.pbf: glyph U+{cp:04X} bitmap has {len(g.get('bitmap', b''))} "
                                f"bytes, expected {want} for {w}x{h}")
        elif g.get("bitmap"):
            problems.append(f"{rng}.pbf: glyph U+{cp:04X} has a bitmap but size {w}x{h}")
        if len(problems) >= 5:
            problems.append(f"{rng}.pbf: (further problems in this file not listed)")
            break
    return problems


def _glyph_key(g):
    if g is None:
        return None
    return (g.get("width"), g.get("height"), g.get("left"), g.get("top"), g.get("advance"),
            g.get("bitmap", b""))


def check_glyphs(files, config, extras=(), results=None):
    """Gate a glyph set given as {"<stack>/<start>-<end>.pbf": bytes}, plus the
    extra members a zip may hold. Returns (Results, per-stack stats)."""
    res = results if results is not None else Results()
    stacks = [s["name"] for s in config["stacks"]]
    expected = {f"{s}/{a}-{a + 255}.pbf" for s in stacks for a in RANGE_STARTS}
    missing = sorted(expected - set(files))
    unexpected = sorted(set(files) - expected - set(extras))
    problems = []
    if missing:
        problems.append(f"{len(missing)} range files missing, e.g. {missing[:4]}")
    if unexpected:
        problems.append(f"{len(unexpected)} unexpected files, e.g. {unexpected[:6]}")
    zero = sorted(n for n in expected & set(files) if not files[n])
    if zero:
        problems.append(f"{len(zero)} range files have 0 bytes, e.g. {zero[:4]}")
    res.record(f"layout: {len(stacks)} stacks x {len(RANGE_STARTS)} range files", problems,
               {"stacks": len(stacks), "range_files": len(expected & set(files))})

    decoded, stats = {}, {}
    for stack in stacks:
        glyphs, problems = {}, []
        total_bytes, ranges_with = 0, 0
        for start in RANGE_STARTS:
            rng = f"{start}-{start + 255}"
            data = files.get(f"{stack}/{rng}.pbf")
            if data is None:
                continue  # reported by the layout check
            total_bytes += len(data)
            try:
                found = parse_glyphs(data)
            except PbfError as exc:
                problems.append(f"{rng}.pbf: {exc}")
                continue
            problems.extend(range_problems(found, stack, rng, start))
            count = 0
            for g in (found[0]["glyphs"] if found else []):
                cp = g.get("id")
                if cp is not None and start <= cp <= start + 255 and cp not in glyphs:
                    glyphs[cp] = g
                    count += 1
            ranges_with += 1 if count else 0
        stats[stack] = {"glyphs": len(glyphs), "bytes": total_bytes, "ranges_with_glyphs": ranges_with}
        res.record(f"{stack}: every range file is a valid glyph PBF", problems, stats[stack])

        empty = []
        for rng in REQUIRED_RANGES:
            first, last = (int(x) for x in rng.split("-"))
            if not any(first <= cp <= last for cp in glyphs):
                empty.append(f"{rng}.pbf holds no glyphs")
        res.record(f"{stack}: ranges {', '.join(REQUIRED_RANGES)} hold glyphs", empty)

        problems = []
        for group, codepoints in REQUIRED_CODEPOINTS.items():
            lost = sorted(cp for cp in codepoints if cp not in glyphs)
            if lost:
                problems.append(f"{group}: {len(lost)} missing, e.g. "
                                + ", ".join(f"U+{cp:04X}" for cp in lost[:8]))
        res.record(f"{stack}: draws Georgian (Mkhedruli and Mtavruli), Latin, digits and Cyrillic",
                   problems)
        decoded[stack] = glyphs

    for regular in stacks:
        bold = regular.replace("Regular", "Bold")
        if "Regular" not in regular or bold not in decoded:
            continue
        problems = []
        for cp in WEIGHT_SAMPLES:
            a, b = decoded[regular].get(cp), decoded[bold].get(cp)
            if a is not None and b is not None and _glyph_key(a) == _glyph_key(b):
                problems.append(f"U+{cp:04X} is drawn the same in both; "
                                "is the Bold stack built from a Regular font?")
        res.record(f"'{bold}' differs from '{regular}'", problems)

    groups = {}
    for stack in config["stacks"]:
        groups.setdefault(tuple(stack["fonts"]), []).append(stack["name"])
    for fonts, names in groups.items():
        for other in names[1:]:
            first = names[0]
            a, b = decoded.get(first, {}), decoded.get(other, {})
            differ = sorted(cp for cp in set(a) | set(b) if _glyph_key(a.get(cp)) != _glyph_key(b.get(cp)))
            res.record(f"'{other}' draws exactly like '{first}' (same fonts)",
                       [f"{len(differ)} codepoints differ, e.g. "
                        + ", ".join(f"U+{cp:04X}" for cp in differ[:8])] if differ else [])
    return res, stats


# ---------------------------------------------------------------------------
# Packing

def notice(config, commit, tool_commit, freetype):
    fonts = {f["file"]: f for f in config["fonts"]}
    server = os.environ.get("GITHUB_SERVER_URL", "https://github.com")
    repo = os.environ.get("GITHUB_REPOSITORY", "peghe-b/vinari-maps")
    lines = [
        "Vinari map glyphs: MapLibre glyph files (signed distance fields, 24 px) for the",
        "Vinari map styles. Point the style's \"glyphs\" at",
        "\"<folder where this zip was unpacked>/{fontstack}/{range}.pbf\".",
        "",
        "Font stacks, each with its fonts in fallback order (the first font that has a",
        "character draws it). Every stack has all 256 range files.",
    ]
    for stack in config["stacks"]:
        used = ", ".join(f"{fonts[f]['family']} {fonts[f]['style']} {fonts[f]['version']}"
                         for f in stack["fonts"])
        lines.append(f"  {stack['name']}: {used}")
    lines += [
        "",
        "Fonts: Noto Sans Georgian and Noto Sans, Copyright 2022 The Noto Project Authors,",
        "licensed under the SIL Open Font License, Version 1.1 (full texts in LICENSES/).",
        "These glyph files are made from those fonts and are under the same licence: they",
        "may be bundled with any software, but not sold on their own.",
        "",
        f"Made by {server}/{repo} at commit {commit or 'unknown'},",
        f"with maplibre/font-maker {tool_commit or 'unknown'} (BSD-3-Clause) and",
        f"FreeType {freetype or 'unknown'} (used under the FreeType License).",
    ]
    return ("\n".join(lines) + "\n").encode("utf-8")


def pack(config, config_path, glyph_dir, out_zip, report_path, commit=None, tool_commit=None,
         freetype=None, licence_dir=LICENCE_DIR, quiet=False):
    """Zip the glyphs, gate exactly the zipped bytes, write the report.
    Returns 0 when everything passed, else 1 (and the zip is removed)."""
    res = Results(quiet=quiet)
    out_zip = Path(out_zip)
    members = read_tree(glyph_dir)
    licences = licence_members(config, licence_dir)
    members.update(licences)
    members[NOTICE_NAME] = notice(config, commit, tool_commit, freetype)
    write_zip(out_zip, members)

    try:
        published = read_zip(out_zip)
        res.record("glyphs.zip holds exactly the files written",
                   [] if published == members else ["the zip read back differs from what was written"])
    except Exception as exc:
        published = {}
        res.record("glyphs.zip holds exactly the files written", [f"error: {exc}"])
    _, stats = check_glyphs(published, config, extras=set(licences) | {NOTICE_NAME}, results=res)

    fonts = {f["file"]: f for f in config["fonts"]}
    report = {
        "section": "glyphs",
        "passed": res.passed,
        "files": [file_entry(out_zip)] if out_zip.exists() else [],
        "licence": "OFL-1.1",
        "licence_url": "https://openfontlicense.org",
        "attribution": "Noto Sans Georgian and Noto Sans, Copyright 2022 The Noto Project Authors",
        "licence_note": ("Glyphs rendered from Noto Sans Georgian and Noto Sans (Copyright 2022 The "
                         "Noto Project Authors), SIL Open Font License 1.1; the licence texts are "
                         "in the zip under LICENSES/ and must go with every copy (the app shows them "
                         "on its Licences screen). Bundle with the app; never sell on their own."),
        "credit_sources": [{"name": fam, "licence": "OFL-1.1",
                            "attribution": "Copyright 2022 The Noto Project Authors"}
                           for fam in dict.fromkeys(fonts[f]["family"] for f in fonts)],
        "layout": "<fontstack>/<start>-<end>.pbf, 256 ranges per stack, plus LICENSES/ and NOTICE.txt",
        "config_version": config["version"],
        "config_sha256": sha256_file(config_path),
        "stacks": [{"name": s["name"], "fonts": s["fonts"], **stats.get(s["name"], {})}
                   for s in config["stacks"]],
        "fonts": [{k: fonts[f][k] for k in ("file", "family", "style", "version", "url", "bytes",
                                            "sha256", "licence")} for f in fonts],
        "tool": {"name": "font-maker", "repo": "https://github.com/maplibre/font-maker",
                 "commit": tool_commit, "licence": "BSD-3-Clause",
                 "freetype": freetype, "freetype_licence": "FTL (FreeType License)",
                 "sdf": "24 px, 3 px buffer, cutoff 0.25"},
        "gate": res.summary(),
    }
    if not res.passed:
        out_zip.unlink(missing_ok=True)
        report["files"] = []
    Path(report_path).parent.mkdir(parents=True, exist_ok=True)
    Path(report_path).write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n",
                                 encoding="utf-8")
    summary = res.summary()
    say = (lambda *a: None) if quiet else print
    say(f"glyph gate: {summary['checks'] - summary['failed']}/{summary['checks']} checks passed")
    if res.passed:
        entry = report["files"][0]
        say(f"glyphs.zip: {entry['bytes']} bytes, sha256 {entry['sha256'][:12]}...")
        return 0
    say("::error::glyph gate failed; glyphs.zip removed")
    return 1


# ---------------------------------------------------------------------------

def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--config", default=str(DEFAULT_CONFIG))
    sub = p.add_subparsers(dest="command", required=True)
    f = sub.add_parser("fetch", help="download the pinned fonts")
    f.add_argument("--out", required=True)
    b = sub.add_parser("build", help="run font-maker once per stack")
    b.add_argument("--fonts", required=True)
    b.add_argument("--font-maker", required=True)
    b.add_argument("--out", required=True)
    b.add_argument("--log")
    k = sub.add_parser("pack", help="zip, gate the zip, write the report")
    k.add_argument("--glyphs", required=True)
    k.add_argument("--out", required=True)
    k.add_argument("--report", required=True)
    k.add_argument("--commit", default=os.environ.get("GITHUB_SHA"))
    k.add_argument("--font-maker-commit")
    k.add_argument("--freetype", help="FreeType version font-maker linked (for the notice)")
    c = sub.add_parser("check", help="gate a glyph folder or glyphs.zip")
    c.add_argument("path")
    args = p.parse_args(argv)

    config = load_config(args.config)
    if args.command == "fetch":
        fetch(config, args.out)
        return 0
    if args.command == "build":
        build(config, args.fonts, args.font_maker, args.out, args.log)
        return 0
    if args.command == "pack":
        return pack(config, args.config, args.glyphs, args.out, args.report, args.commit,
                    args.font_maker_commit, args.freetype)
    path = Path(args.path)
    files = read_zip(path) if path.is_file() else read_tree(path)
    extras = {n for n in files if n.startswith(LICENCE_FOLDER + "/") or n == NOTICE_NAME}
    res, _ = check_glyphs(files, config, extras=extras)
    return 0 if res.passed else 1


if __name__ == "__main__":
    sys.exit(main())
