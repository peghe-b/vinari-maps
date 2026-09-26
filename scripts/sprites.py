#!/usr/bin/env python3
"""Driving icons for the Vinari map styles, released as sprites.zip.

MapLibre loads a style's icons from one sprite sheet: sprite.png plus
sprite.json (where each icon sits), and sprite@2x.png/json for Retina
screens. The app ships them, so no icon is fetched from the internet.

Icons (config/sprites.json), each a 24 x 24 px badge: a coloured disc
with a white ring and a faint dark edge (so it reads on the day and the
night map alike), the glyph in white on top:

  fuel, charging-station, parking, speed-camera, police, hospital,
  border-crossing, ferry, and ferry-excluded (the ferry with a slash, for
  the note that routes never use ferries: the clip drops every ferry that
  leaves Georgia).

The glyphs are Maki 8.2.0 and Temaki 5.13.0 SVGs, both CC0-1.0, vendored
in config/sprites/ with their SHA-256 in the config. Every badge colour
keeps at least 3:1 contrast with the white glyph (WCAG 2.1 non-text
contrast; the unit tests check it).

Subcommands (as in the workflow):
  python3 scripts/sprites.py compose --out build/sprite-svg
      Write one badge SVG per icon (the vendored files must match their
      SHA-256). spreet (pinned in the workflow) then renders the folder:
        spreet build/sprite-svg build/sprites/sprite
        spreet --retina build/sprite-svg build/sprites/sprite@2x
  python3 scripts/sprites.py pack --sprites build/sprites --svg build/sprite-svg \
      --out build/glyphs-sprites/sprites.zip --report build/glyphs-sprites/sprites_report.json
      Zip the four sprite files, the badge SVGs (svg/, for drawing the same
      badges in the app's own lists), the CC0 texts, the MIT text of the
      badges and a NOTICE; read the
      zip back and gate exactly those bytes. On any failure the zip is
      deleted and the exit code is 1.
  python3 scripts/sprites.py check PATH   (a sprite folder or sprites.zip)

Gate (every check must pass):
  - sprite.json and sprite@2x.json list exactly the configured icons, each
    with whole-number x, y, width, height and pixelRatio 1 or 2;
  - both PNGs decode (every chunk's CRC checked), every icon lies inside
    its sheet, no two icons overlap, and no pixel outside the icons is
    visible (a shifted index would show up here);
  - each icon is 24 x 24 at 1x and 48 x 48 at 2x, mostly opaque, shows its
    badge colour, and shows white in the middle of the disc (the glyph);
  - svg/ holds exactly one badge SVG per icon.

Licences: Maki and Temaki are CC0-1.0 (no conditions; their texts ship in
the zip anyway). The badge designs (colours, ring, slash) and the sheets are
Vinari's, under the repository's MIT licence (its text ships in the zip);
whether to dedicate them to the public domain (CC0, which cannot be taken
back) is left to the owner. spreet is MIT and runs in CI only; the sheets
are made from the SVGs, not from spreet. Standard library only.
"""

import argparse
import json
import os
import re
import struct
import sys
import xml.etree.ElementTree as ET
import zlib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from pack_util import Results, file_entry, read_tree, read_zip, sha256_bytes, sha256_file, write_zip  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG = ROOT / "config" / "sprites.json"
ICON_DIR = ROOT / "config" / "sprites"

SVG_NS = "http://www.w3.org/2000/svg"
ET.register_namespace("", SVG_NS)

SHEETS = (("sprite", 1), ("sprite@2x", 2))
LICENCE_FOLDER = "LICENSES"
BADGE_LICENCE_NAME = "vinari-MIT.txt"
REPO_LICENSE = Path(__file__).resolve().parent.parent / "LICENSE"
SVG_FOLDER = "svg"
NOTICE_NAME = "NOTICE.txt"
MIN_CONTRAST = 3.0       # WCAG 2.1 non-text contrast, glyph against badge
MAX_SHEET_SIDE = 4096

# Pixel tests for each icon (fractions of its area).
MIN_OPAQUE = 0.45        # disc and ring cover 66% of the square (60% fully opaque, measured)
MIN_BADGE_COLOUR = 0.20  # pixels within COLOUR_TOLERANCE of the badge colour (28-43% measured)
MIN_CENTRE_WHITE = 0.05  # white pixels in the middle 40% x 40%, where only the glyph is white
                         # (23-56% measured)
COLOUR_TOLERANCE = 8

# Elements a glyph may use. Anything else (style sheets, <use>, images,
# gradients) fails loudly instead of rendering differently in spreet.
DRAWING_TAGS = {"path", "circle", "ellipse", "rect", "polygon", "polyline", "line", "g"}
SKIPPED_TAGS = {"title", "desc", "metadata"}


# ---------------------------------------------------------------------------
# Config

def parse_colour(value):
    if not re.fullmatch(r"#[0-9A-Fa-f]{6}", str(value)):
        raise ValueError(f"colour {value!r} is not #RRGGBB")
    return tuple(int(value[i:i + 2], 16) for i in (1, 3, 5))


def relative_luminance(rgb):
    def channel(c):
        c = c / 255
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4
    r, g, b = (channel(c) for c in rgb)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def contrast(a, b):
    la, lb = sorted((relative_luminance(a), relative_luminance(b)), reverse=True)
    return (la + 0.05) / (lb + 0.05)


def load_config(path=DEFAULT_CONFIG):
    config = json.loads(Path(path).read_text(encoding="utf-8"))
    problems = []
    names = [i.get("name") for i in config.get("icons") or []]
    if not names:
        problems.append("no icons")
    if len(set(names)) != len(names):
        problems.append("icon names must be unique")
    glyph = parse_colour(config["badge"]["glyph_colour"])
    for icon in config.get("icons") or []:
        name = icon.get("name") or ""
        if not re.fullmatch(r"[a-z0-9]+(-[a-z0-9]+)*", name):
            problems.append(f"icon {name!r}: use lowercase words joined by '-'")
        if icon.get("source") not in config["sources"]:
            problems.append(f"icon {name}: unknown source {icon.get('source')!r}")
        if not re.fullmatch(r"[0-9a-f]{64}", str(icon.get("sha256", ""))):
            problems.append(f"icon {name}: sha256 must be 64 lowercase hex digits")
        try:
            ratio = contrast(parse_colour(icon.get("colour")), glyph)
            if ratio < MIN_CONTRAST:
                problems.append(f"icon {name}: colour {icon['colour']} has {ratio:.2f}:1 contrast "
                                f"with the glyph, below {MIN_CONTRAST}:1")
        except ValueError as exc:
            problems.append(f"icon {name}: {exc}")
    if problems:
        raise ValueError(f"{path}: " + "; ".join(problems))
    return config


def source_bytes(config, icon, icon_dir=ICON_DIR):
    """A vendored glyph SVG, checked against its pinned SHA-256."""
    path = Path(icon_dir) / icon["source"] / icon["file"]
    data = path.read_bytes()
    if sha256_bytes(data) != icon["sha256"]:
        raise ValueError(f"{path}: sha256 {sha256_bytes(data)} does not match the config "
                         f"({icon['sha256']})")
    return data


# ---------------------------------------------------------------------------
# Badge SVGs

def _num(x):
    return f"{x:.4f}".rstrip("0").rstrip(".")


def _clean(element):
    """Copy a glyph element without ids or its own colours, so the badge's
    white fill applies to every part."""
    tag = element.tag.split("}")[-1]
    if tag not in DRAWING_TAGS:
        raise ValueError(f"unsupported SVG element <{tag}>")
    attrs = {}
    for key, value in element.attrib.items():
        local = key.split("}")[-1]
        if local == "style" or local == "class":
            raise ValueError(f"<{tag}> has a {local} attribute; inline colours only")
        if local == "id":
            continue
        if local in ("fill", "stroke") and value != "none":
            continue
        attrs[key] = value
    copy = ET.Element(f"{{{SVG_NS}}}{tag}", attrs)
    for child in element:
        child_tag = child.tag.split("}")[-1]
        if child_tag in SKIPPED_TAGS:
            continue
        copy.append(_clean(child))
    return copy


def badge_svg(icon, data, badge):
    """One icon as a badge SVG (a string)."""
    root = ET.fromstring(data)
    if root.tag != f"{{{SVG_NS}}}svg":
        raise ValueError(f"{icon['file']}: not an SVG document")
    view_box = root.get("viewBox")
    if view_box:
        min_x, min_y, width, height = (float(v) for v in re.split(r"[\s,]+", view_box.strip()))
    else:
        min_x, min_y = 0.0, 0.0
        width, height = float(root.get("width")), float(root.get("height"))
    if width <= 0 or height <= 0:
        raise ValueError(f"{icon['file']}: empty viewBox")

    size = badge["size_px"]
    centre = size / 2
    scale = badge["glyph_px"] / max(width, height)
    dx = (size - width * scale) / 2 - min_x * scale
    dy = (size - height * scale) / 2 - min_y * scale
    colour = icon["colour"].upper()
    glyph = badge["glyph_colour"].upper()

    out = ET.Element(f"{{{SVG_NS}}}svg", {"width": str(size), "height": str(size),
                                         "viewBox": f"0 0 {size} {size}"})

    def circle(radius, fill, **extra):
        ET.SubElement(out, f"{{{SVG_NS}}}circle", {"cx": _num(centre), "cy": _num(centre),
                                                   "r": _num(radius), "fill": fill, **extra})

    circle(badge["edge_radius"], "#000000", **{"fill-opacity": _num(badge["edge_opacity"])})
    circle(badge["ring_radius"], glyph)
    circle(badge["disc_radius"], colour)
    group = ET.SubElement(out, f"{{{SVG_NS}}}g", {
        "fill": glyph, "transform": f"translate({_num(dx)} {_num(dy)}) scale({_num(scale)})"})
    drawn = 0
    for child in root:
        tag = child.tag.split("}")[-1]
        if tag in SKIPPED_TAGS:
            continue
        group.append(_clean(child))
        drawn += 1
    if not drawn:
        raise ValueError(f"{icon['file']}: nothing to draw")
    if icon.get("slash"):
        # A slash from top left to bottom right, cut out of the glyph by a
        # wider stroke in the badge colour (the way SF Symbols draws ".slash").
        a, b = size * 0.27, size * 0.73
        for stroke, width_px in ((colour, 4.0), (glyph, 1.75)):
            ET.SubElement(out, f"{{{SVG_NS}}}line", {
                "x1": _num(a), "y1": _num(a), "x2": _num(b), "y2": _num(b), "stroke": stroke,
                "stroke-width": _num(width_px), "stroke-linecap": "round"})
    return ET.tostring(out, encoding="unicode") + "\n"


def compose(config, out_dir, icon_dir=ICON_DIR, quiet=False):
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    for old in out.glob("*.svg"):
        old.unlink()
    for icon in config["icons"]:
        svg = badge_svg(icon, source_bytes(config, icon, icon_dir), config["badge"])
        (out / f"{icon['name']}.svg").write_text(svg, encoding="utf-8")
    if not quiet:
        print(f"ok  {len(config['icons'])} badge SVGs in {out}")


# ---------------------------------------------------------------------------
# PNG reading (non-interlaced, every colour type and bit depth)

PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


def _paeth(a, b, c):
    p = a + b - c
    pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
    if pa <= pb and pa <= pc:
        return a
    return b if pb <= pc else c


def read_png(data):
    """Decode a PNG into (width, height, RGBA bytearray)."""
    if not data.startswith(PNG_SIGNATURE):
        raise ValueError("not a PNG file")
    pos, ihdr, palette, trns, idat = 8, None, None, None, []
    while True:
        if pos + 12 > len(data):
            raise ValueError("PNG ends before IEND")
        length, kind = struct.unpack(">I4s", data[pos:pos + 8])
        body = data[pos + 8:pos + 8 + length]
        if len(body) != length or pos + 12 + length > len(data):
            raise ValueError(f"truncated {kind!r} chunk")
        crc, = struct.unpack(">I", data[pos + 8 + length:pos + 12 + length])
        if zlib.crc32(kind + body) & 0xFFFFFFFF != crc:
            raise ValueError(f"CRC error in {kind!r} chunk")
        pos += 12 + length
        if kind == b"IHDR":
            ihdr = struct.unpack(">IIBBBBB", body)
        elif kind == b"PLTE":
            palette = body
        elif kind == b"tRNS":
            trns = body
        elif kind == b"IDAT":
            idat.append(body)
        elif kind == b"IEND":
            break
    if ihdr is None:
        raise ValueError("no IHDR chunk")
    width, height, depth, colour_type, compression, filter_method, interlace = ihdr
    channels = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}.get(colour_type)
    if channels is None or compression or filter_method:
        raise ValueError(f"unsupported PNG header {ihdr}")
    if interlace:
        raise ValueError("interlaced PNG; the gate reads only non-interlaced files")
    if not 0 < width <= MAX_SHEET_SIDE or not 0 < height <= MAX_SHEET_SIDE:
        raise ValueError(f"sheet is {width}x{height} px")
    if depth not in ((1, 2, 4, 8, 16) if colour_type == 0 else
                     (1, 2, 4, 8) if colour_type == 3 else (8, 16)):
        raise ValueError(f"bit depth {depth} is invalid for colour type {colour_type}")
    if colour_type == 3 and palette is None:
        raise ValueError("palette image without PLTE")

    bits = channels * depth
    step = max(1, bits // 8)
    stride = (width * bits + 7) // 8
    raw = zlib.decompress(b"".join(idat))
    if len(raw) != height * (stride + 1):
        raise ValueError(f"image data has {len(raw)} bytes, expected {height * (stride + 1)}")

    rows, previous = [], bytearray(stride)
    for y in range(height):
        start = y * (stride + 1)
        kind = raw[start]
        line = bytearray(raw[start + 1:start + 1 + stride])
        if kind == 1:
            for i in range(step, stride):
                line[i] = (line[i] + line[i - step]) & 0xFF
        elif kind == 2:
            for i in range(stride):
                line[i] = (line[i] + previous[i]) & 0xFF
        elif kind == 3:
            for i in range(stride):
                left = line[i - step] if i >= step else 0
                line[i] = (line[i] + ((left + previous[i]) >> 1)) & 0xFF
        elif kind == 4:
            for i in range(stride):
                left = line[i - step] if i >= step else 0
                up_left = previous[i - step] if i >= step else 0
                line[i] = (line[i] + _paeth(left, previous[i], up_left)) & 0xFF
        elif kind != 0:
            raise ValueError(f"unknown filter type {kind} in row {y}")
        rows.append(line)
        previous = line

    def samples(line):
        if depth == 8:
            return list(line)
        if depth == 16:
            return [line[i] for i in range(0, len(line), 2)]  # the high byte is enough here
        per_byte, mask = 8 // depth, (1 << depth) - 1
        out = []
        for byte in line:
            for k in range(per_byte):
                out.append((byte >> (8 - depth * (k + 1))) & mask)
        return out[:width * channels]

    scale = 255 // ((1 << depth) - 1) if depth < 8 else 1
    rgba = bytearray(width * height * 4)
    for y, line in enumerate(rows):
        s = samples(line)
        for x in range(width):
            o = (y * width + x) * 4
            if colour_type == 6:
                rgba[o:o + 4] = bytes(s[x * 4:x * 4 + 4])
            elif colour_type == 4:
                g, a = s[x * 2], s[x * 2 + 1]
                rgba[o:o + 4] = bytes((g, g, g, a))
            elif colour_type == 2:
                r, g, b = s[x * 3:x * 3 + 3]
                a = 255
                if trns is not None and len(trns) == 6 and depth == 8 and \
                        (r, g, b) == struct.unpack(">HHH", trns):
                    a = 0
                rgba[o:o + 4] = bytes((r, g, b, a))
            elif colour_type == 0:
                v = s[x]
                a = 0 if trns is not None and len(trns) == 2 and depth <= 8 and \
                    v == struct.unpack(">H", trns)[0] else 255
                g = v * scale
                rgba[o:o + 4] = bytes((g, g, g, a))
            else:
                index = s[x]
                if 3 * index + 3 > len(palette):
                    raise ValueError(f"palette index {index} out of range")
                a = trns[index] if trns is not None and index < len(trns) else 255
                rgba[o:o + 4] = palette[3 * index:3 * index + 3] + bytes((a,))
    return width, height, rgba


# ---------------------------------------------------------------------------
# Gate

INDEX_KEYS = {"x", "y", "width", "height", "pixelRatio"}


def index_problems(index, names, ratio):
    problems = []
    if not isinstance(index, dict):
        return ["the index is not a JSON object"]
    missing, extra = sorted(set(names) - set(index)), sorted(set(index) - set(names))
    if missing:
        problems.append(f"missing icons: {missing}")
    if extra:
        problems.append(f"unexpected icons: {extra}")
    for name, entry in sorted(index.items()):
        if not isinstance(entry, dict):
            problems.append(f"{name}: not an object")
            continue
        if not INDEX_KEYS <= set(entry):
            problems.append(f"{name}: lacks {sorted(INDEX_KEYS - set(entry))}")
            continue
        if any(type(entry[k]) is not int for k in ("x", "y", "width", "height")):
            problems.append(f"{name}: x, y, width and height must be whole numbers")
            continue
        if entry["x"] < 0 or entry["y"] < 0 or entry["width"] <= 0 or entry["height"] <= 0:
            problems.append(f"{name}: bad rectangle {entry}")
        if entry["pixelRatio"] != ratio:
            problems.append(f"{name}: pixelRatio {entry['pixelRatio']}, expected {ratio}")
        if entry.get("sdf"):
            problems.append(f"{name}: marked sdf; these are full-colour icons")
    return problems


def sheet_problems(index, png, config, ratio):
    """Pixels and rectangles of one sheet. Returns (problems, detail)."""
    width, height, rgba = read_png(png)
    problems = []
    size = config["badge"]["size_px"] * ratio
    colours = {i["name"]: parse_colour(i["colour"]) for i in config["icons"]}
    glyph = parse_colour(config["badge"]["glyph_colour"])
    rects = {n: (e["x"], e["y"], e["width"], e["height"]) for n, e in index.items()
             if isinstance(e, dict) and INDEX_KEYS <= set(e)}
    covered = bytearray(width * height)
    for name, (x, y, w, h) in sorted(rects.items()):
        if x + w > width or y + h > height:
            problems.append(f"{name}: {w}x{h} at {x},{y} runs off the {width}x{height} sheet")
            continue
        if (w, h) != (size, size):
            problems.append(f"{name}: {w}x{h} px, expected {size}x{size}")
        overlap = False
        for yy in range(y, y + h):
            for xx in range(x, x + w):
                if covered[yy * width + xx]:
                    overlap = True
                covered[yy * width + xx] = 1
        if overlap:
            problems.append(f"{name}: overlaps another icon")
        opaque = badge = 0
        centre_white = centre_total = 0
        lo, hi = int(w * 0.3), int(w * 0.7)
        target = colours.get(name)
        for yy in range(h):
            for xx in range(w):
                o = ((y + yy) * width + x + xx) * 4
                r, g, b, a = rgba[o], rgba[o + 1], rgba[o + 2], rgba[o + 3]
                if a >= 250:
                    opaque += 1
                    if target and max(abs(r - target[0]), abs(g - target[1]),
                                      abs(b - target[2])) <= COLOUR_TOLERANCE:
                        badge += 1
                if lo <= xx < hi and lo <= yy < hi:
                    centre_total += 1
                    if a >= 250 and max(abs(r - glyph[0]), abs(g - glyph[1]),
                                        abs(b - glyph[2])) <= COLOUR_TOLERANCE:
                        centre_white += 1
        area = w * h
        if opaque < MIN_OPAQUE * area:
            problems.append(f"{name}: only {opaque / area:.0%} opaque (expected at least {MIN_OPAQUE:.0%})")
        if badge < MIN_BADGE_COLOUR * area:
            problems.append(f"{name}: badge colour on {badge / area:.0%} of the icon "
                            f"(expected at least {MIN_BADGE_COLOUR:.0%})")
        if centre_total and centre_white < MIN_CENTRE_WHITE * centre_total:
            problems.append(f"{name}: no glyph in the middle of the badge "
                            f"({centre_white / centre_total:.0%} white)")
    stray = sum(1 for i in range(width * height) if not covered[i] and rgba[i * 4 + 3])
    if stray:
        problems.append(f"{stray} visible pixels lie outside every icon (index and sheet disagree)")
    return problems, {"width": width, "height": height, "icons": len(rects)}


def check_sprites(files, config, extras=(), results=None):
    """Gate {"sprite.json": bytes, "sprite.png": ..., "sprite@2x.json": ...,
    "sprite@2x.png": ..., "svg/<name>.svg": ...} plus allowed extra members."""
    res = results if results is not None else Results()
    names = [i["name"] for i in config["icons"]]
    svg_names = {f"{SVG_FOLDER}/{n}.svg" for n in names}
    sheet_names = {f"{base}.{ext}" for base, _ in SHEETS for ext in ("json", "png")}
    missing = sorted((sheet_names | svg_names) - set(files))
    unexpected = sorted(set(files) - sheet_names - svg_names - set(extras))
    res.record("layout: sprite.json/png, sprite@2x.json/png and one badge SVG per icon",
               ([f"missing: {missing}"] if missing else []) +
               ([f"unexpected: {unexpected}"] if unexpected else []))

    indexes = {}
    for base, ratio in SHEETS:
        def one_sheet(base=base, ratio=ratio):
            index = json.loads(files[f"{base}.json"].decode("utf-8"))
            problems = index_problems(index, names, ratio)
            indexes[base] = index
            more, detail = sheet_problems(index, files[f"{base}.png"], config, ratio)
            return problems + more, detail
        res.guarded(f"{base}.json and {base}.png agree, every icon drawn", one_sheet)

    def same_icons():
        one, two = indexes.get("sprite"), indexes.get("sprite@2x")
        if not isinstance(one, dict) or not isinstance(two, dict):
            return ["an index did not load"], {}
        problems = []
        for name in sorted(set(one) & set(two)):
            a, b = one[name], two[name]
            if (2 * a.get("width", 0), 2 * a.get("height", 0)) != (b.get("width"), b.get("height")):
                problems.append(f"{name}: {a.get('width')}x{a.get('height')} at 1x but "
                                f"{b.get('width')}x{b.get('height')} at 2x")
        if set(one) != set(two):
            problems.append("the two indexes list different icons")
        return problems, {}
    res.guarded("1x and 2x list the same icons at double size", same_icons)

    def svgs():
        problems = []
        size = str(config["badge"]["size_px"])
        for name in sorted(svg_names & set(files)):
            root = ET.fromstring(files[name])
            if root.tag != f"{{{SVG_NS}}}svg" or root.get("width") != size or root.get("height") != size:
                problems.append(f"{name}: not a {size}x{size} SVG")
        return problems, {}
    res.guarded("badge SVGs", svgs)
    return res


# ---------------------------------------------------------------------------
# Packing

def licence_members(config, icon_dir=ICON_DIR):
    members = {}
    for key, source in sorted(config["sources"].items()):
        data = (Path(icon_dir) / source["licence_file"]).read_bytes()
        if sha256_bytes(data) != source["licence_sha256"]:
            raise ValueError(f"{source['licence_file']}: sha256 does not match the config")
        members[f"{LICENCE_FOLDER}/{key}-{Path(source['licence_file']).name}"] = data
    return members


def badge_licence(path=REPO_LICENSE):
    """The MIT text of the badges: the repository LICENSE up to its DATA part."""
    text = Path(path).read_text(encoding="utf-8").split("\n---")[0].rstrip() + "\n"
    if "MIT License" not in text or "Vinari" not in text:
        raise ValueError(f"{path}: not the repository's MIT licence")
    return text.encode("utf-8")


def notice(config, commit, spreet):
    server = os.environ.get("GITHUB_SERVER_URL", "https://github.com")
    repo = os.environ.get("GITHUB_REPOSITORY", "peghe-b/vinari-maps")
    lines = [
        "Vinari driving icons: a MapLibre sprite sheet. Point the style's \"sprite\" at",
        "\"<folder where this zip was unpacked>/sprite\"; MapLibre adds .json/.png and @2x.",
        "",
        "Icons (24 x 24 at 1x), each a badge with a glyph from:",
    ]
    for icon in config["icons"]:
        source = config["sources"][icon["source"]]
        lines.append(f"  {icon['name']}: {icon['source']} {source['version']} {icon['file']}"
                     f"{' with a slash' if icon.get('slash') else ''}, badge {icon['colour']}"
                     f" ({icon['for']})")
    lines += [
        "",
        "Maki (https://github.com/mapbox/maki) and Temaki (https://github.com/rapideditor/temaki)",
        "are dedicated to the public domain under CC0 1.0 (texts in LICENSES/). The badge designs",
        f"and the sheets are (c) 2026 Vinari, MIT (LICENSES/{BADGE_LICENCE_NAME}); the glyphs inside",
        "them are Maki and Temaki, CC0 1.0. svg/ holds each badge as an SVG.",
        "",
        f"Made by {server}/{repo} at commit {commit or 'unknown'}, rendered with spreet",
        f"{spreet or 'unknown'} (MIT).",
    ]
    return ("\n".join(lines) + "\n").encode("utf-8")


def pack(config, config_path, sprite_dir, svg_dir, out_zip, report_path, commit=None,
         spreet=None, spreet_sha256=None, icon_dir=ICON_DIR, quiet=False):
    """Zip the sheets and badges, gate exactly the zipped bytes, write the
    report. Returns 0 when everything passed, else 1 (and the zip is removed)."""
    res = Results(quiet=quiet)
    out_zip = Path(out_zip)
    members = {}
    for base, _ in SHEETS:
        for ext in ("json", "png"):
            path = Path(sprite_dir) / f"{base}.{ext}"
            if path.is_file():
                members[path.name] = path.read_bytes()
    for name, data in read_tree(svg_dir).items():
        members[f"{SVG_FOLDER}/{name}"] = data
    licences = licence_members(config, icon_dir)
    licences[f"{LICENCE_FOLDER}/{BADGE_LICENCE_NAME}"] = badge_licence()
    members.update(licences)
    members[NOTICE_NAME] = notice(config, commit, spreet)
    write_zip(out_zip, members)

    try:
        published = read_zip(out_zip)
        res.record("sprites.zip holds exactly the files written",
                   [] if published == members else ["the zip read back differs from what was written"])
    except Exception as exc:
        published = {}
        res.record("sprites.zip holds exactly the files written", [f"error: {exc}"])
    check_sprites(published, config, extras=set(licences) | {NOTICE_NAME}, results=res)

    report = {
        "section": "sprites",
        "passed": res.passed,
        "files": [file_entry(out_zip)] if out_zip.exists() else [],
        "licence": "MIT (badges), CC0-1.0 (glyphs)",
        "licence_note": (f"The badge designs and the sheets are (c) 2026 Vinari, MIT (LICENSES/{BADGE_LICENCE_NAME} "
                         "in the zip); the glyphs inside them are Maki and Temaki, CC0 1.0 (no conditions)."),
        "attribution": "Badges (c) 2026 Vinari; glyphs from Maki (Mapbox) and Temaki (Rapid Editor), CC0 1.0",
        "credit_sources": [{"name": f"{k.capitalize()} {v['version']}", "licence": v["licence"], "url": v["repo"]}
                           for k, v in sorted(config["sources"].items())],
        "layout": "sprite.json, sprite.png, sprite@2x.json, sprite@2x.png, svg/<icon>.svg, LICENSES/, NOTICE.txt",
        "config_version": config["version"],
        "config_sha256": sha256_file(config_path),
        "icons": [{"name": i["name"], "source": i["source"],
                   "source_version": config["sources"][i["source"]]["version"],
                   "source_commit": config["sources"][i["source"]]["commit"],
                   "file": i["file"], "sha256": i["sha256"], "colour": i["colour"],
                   "slash": bool(i.get("slash")), "for": i["for"]} for i in config["icons"]],
        "tool": {"name": "spreet", "repo": "https://github.com/flother/spreet", "version": spreet,
                 "sha256": spreet_sha256, "licence": "MIT"},
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
    say(f"sprite gate: {summary['checks'] - summary['failed']}/{summary['checks']} checks passed")
    if res.passed:
        entry = report["files"][0]
        say(f"sprites.zip: {entry['bytes']} bytes, sha256 {entry['sha256'][:12]}...")
        return 0
    say("::error::sprite gate failed; sprites.zip removed")
    return 1


# ---------------------------------------------------------------------------

def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--config", default=str(DEFAULT_CONFIG))
    sub = p.add_subparsers(dest="command", required=True)
    c = sub.add_parser("compose", help="write one badge SVG per icon")
    c.add_argument("--out", required=True)
    k = sub.add_parser("pack", help="zip, gate the zip, write the report")
    k.add_argument("--sprites", required=True, help="folder with sprite.json/png and sprite@2x.json/png")
    k.add_argument("--svg", required=True, help="the folder compose wrote")
    k.add_argument("--out", required=True)
    k.add_argument("--report", required=True)
    k.add_argument("--commit", default=os.environ.get("GITHUB_SHA"))
    k.add_argument("--spreet-version")
    k.add_argument("--spreet-sha256")
    ch = sub.add_parser("check", help="gate a sprite folder or sprites.zip")
    ch.add_argument("path")
    args = p.parse_args(argv)

    config = load_config(args.config)
    if args.command == "compose":
        compose(config, args.out)
        return 0
    if args.command == "pack":
        return pack(config, args.config, args.sprites, args.svg, args.out, args.report,
                    args.commit, args.spreet_version, args.spreet_sha256)
    path = Path(args.path)
    files = read_zip(path) if path.is_file() else read_tree(path)
    extras = {n for n in files if n.startswith(LICENCE_FOLDER + "/") or n == NOTICE_NAME}
    return 0 if check_sprites(files, config, extras=extras).passed else 1


if __name__ == "__main__":
    sys.exit(main())
