#!/usr/bin/env python3
"""Unit tests for glyphs.py, sprites.py and manifest.py --asset-report, on
small made-up data. No downloads and no third-party packages needed.

Run from the maps/ folder:
    python3 scripts/test_glyphs_sprites.py
"""

import contextlib
import io
import json
import struct
import sys
import tempfile
import types
import unittest
import xml.etree.ElementTree as ET
import zlib
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import glyphs  # noqa: E402
import pack_util  # noqa: E402
import sprites  # noqa: E402

try:
    import manifest  # noqa: E402  (needs clip.py and gate.py, which need numpy/shapely)
except ImportError:
    # asset_report() uses neither; stand-ins let it be tested without them.
    sys.modules["clip"] = types.SimpleNamespace(DEFAULT_CONFIG=None, load_config=None)
    sys.modules["gate"] = types.SimpleNamespace(MIN_EDGES=0)
    import manifest  # noqa: E402

GLYPH_CONFIG = glyphs.load_config()
SPRITE_CONFIG = sprites.load_config()


# ---------------------------------------------------------------------------
# A small protobuf writer for glyph files

def _varint(n):
    out = bytearray()
    while True:
        byte = n & 0x7F
        n >>= 7
        if n:
            out.append(byte | 0x80)
        else:
            out.append(byte)
            return bytes(out)


def _key(field, wire):
    return _varint(field << 3 | wire)


def _bytes_field(field, data):
    return _key(field, 2) + _varint(len(data)) + data


def _zigzag(n):
    return (n << 1) ^ (n >> 63)


def encode_glyph(g):
    """Fields in font-maker's order; a key left out of g is left out of the file."""
    out = b""
    for field, key in ((1, "id"), (3, "width"), (4, "height"), (5, "left"), (6, "top"),
                       (7, "advance"), (2, "bitmap")):
        if key not in g:
            continue
        if key == "bitmap":
            if g[key]:
                out += _bytes_field(2, g[key])
        else:
            value = _zigzag(g[key]) if key in ("left", "top") else g[key]
            out += _key(field, 0) + _varint(value)
    return out


def encode_range(name, rng, glyph_list):
    stack = _bytes_field(1, name.encode()) + _bytes_field(2, rng.encode())
    for g in glyph_list:
        stack += _bytes_field(3, encode_glyph(g))
    return _bytes_field(1, stack)


def fake_glyph(cp, weight):
    """A glyph whose bitmap depends on the codepoint and the weight."""
    w, h = 4 + weight, 5
    bitmap = bytes(((cp * 7 + i * 3 + weight * 11) & 0xFF) for i in range((w + 6) * (h + 6)))
    return {"id": cp, "width": w, "height": h, "left": 1, "top": -25 + (cp % 7), "advance": w + 2,
            "bitmap": bitmap}


def all_required():
    cps = set()
    for group in glyphs.REQUIRED_CODEPOINTS.values():
        cps |= group
    return cps | {0x10A0, 0x2D00}


def fake_set(config=GLYPH_CONFIG, drop=None):
    """{"<stack>/<range>.pbf": bytes} as font-maker would write it. Bold
    stacks get other bitmaps than Regular ones; stacks with the same fonts
    get the same glyphs. drop: {stack: set of codepoints to leave out}."""
    files = {}
    for stack in config["stacks"]:
        weight = 1 if "Bold" in stack["fonts"][0] else 0
        skip = (drop or {}).get(stack["name"], set())
        by_range = {}
        for cp in all_required() - skip:
            by_range.setdefault(cp // 256 * 256, []).append(fake_glyph(cp, weight))
        for start in glyphs.RANGE_STARTS:
            rng = f"{start}-{start + 255}"
            files[f"{stack['name']}/{rng}.pbf"] = encode_range(
                stack["name"], rng, sorted(by_range.get(start, []), key=lambda g: g["id"]))
    return files


def failed_names(res):
    return [r["name"] for r in res.items if not r["passed"]]


# ---------------------------------------------------------------------------
# A small PNG writer (every filter type) and badge painter for sprite sheets

def _filter_row(kind, line, previous, step):
    out = bytearray(len(line))
    for i in range(len(line)):
        left = line[i - step] if i >= step else 0
        up = previous[i]
        up_left = previous[i - step] if i >= step else 0
        if kind == 0:
            predictor = 0
        elif kind == 1:
            predictor = left
        elif kind == 2:
            predictor = up
        elif kind == 3:
            predictor = (left + up) >> 1
        else:
            predictor = sprites._paeth(left, up, up_left)
        out[i] = (line[i] - predictor) & 0xFF
    return bytes(out)


def _chunk(kind, body):
    return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", zlib.crc32(kind + body) & 0xFFFFFFFF)


def encode_png(width, height, rows, colour_type=6, depth=8, palette=None, trns=None, filters=(0, 1, 2, 3, 4)):
    """rows: packed scanlines (bytes) without filter bytes."""
    channels = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}[colour_type]
    step = max(1, channels * depth // 8)
    raw, previous = bytearray(), bytes(len(rows[0]))
    for y, line in enumerate(rows):
        kind = filters[y % len(filters)]
        raw += bytes((kind,)) + _filter_row(kind, line, previous, step)
        previous = line
    body = _chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, depth, colour_type, 0, 0, 0))
    if palette is not None:
        body += _chunk(b"PLTE", palette)
    if trns is not None:
        body += _chunk(b"tRNS", trns)
    body += _chunk(b"IDAT", zlib.compress(bytes(raw), 9)) + _chunk(b"IEND", b"")
    return sprites.PNG_SIGNATURE + body


def rgba_png(width, height, rgba):
    rows = [bytes(rgba[y * width * 4:(y + 1) * width * 4]) for y in range(height)]
    return encode_png(width, height, rows)


def paint_badge(rgba, sheet_w, x0, y0, size, colour, glyph=True):
    """Roughly what spreet draws: disc, white ring, white glyph in the middle."""
    c = size / 2
    for yy in range(size):
        for xx in range(size):
            d = ((xx + 0.5 - c) ** 2 + (yy + 0.5 - c) ** 2) ** 0.5
            o = ((y0 + yy) * sheet_w + x0 + xx) * 4
            if d <= 9.75 * size / 24:
                px = colour
                if glyph and abs(xx + 0.5 - c) < size * 0.12 and abs(yy + 0.5 - c) < size * 0.3:
                    px = (255, 255, 255)
                rgba[o:o + 4] = bytes(px + (255,))
            elif d <= 11 * size / 24:
                rgba[o:o + 4] = bytes((255, 255, 255, 255))
            elif d <= 11.75 * size / 24:
                rgba[o:o + 4] = bytes((0, 0, 0, 51))


def fake_sheet(ratio, config=SPRITE_CONFIG, skip_glyph=(), colour_override=None):
    size = config["badge"]["size_px"] * ratio
    names = [i["name"] for i in config["icons"]]
    cols = 4
    width, height = cols * size, ((len(names) + cols - 1) // cols) * size
    rgba = bytearray(width * height * 4)
    index = {}
    for n, icon in enumerate(config["icons"]):
        x, y = (n % cols) * size, (n // cols) * size
        colour = sprites.parse_colour((colour_override or {}).get(icon["name"], icon["colour"]))
        paint_badge(rgba, width, x, y, size, colour, glyph=icon["name"] not in skip_glyph)
        index[icon["name"]] = {"height": size, "pixelRatio": ratio, "width": size, "x": x, "y": y}
    return index, width, height, rgba


def fake_sprite_files(**kwargs):
    files = {}
    for base, ratio in sprites.SHEETS:
        index, w, h, rgba = fake_sheet(ratio, **kwargs)
        files[f"{base}.json"] = json.dumps(index).encode()
        files[f"{base}.png"] = rgba_png(w, h, rgba)
    for icon in SPRITE_CONFIG["icons"]:
        files[f"svg/{icon['name']}.svg"] = sprites.badge_svg(
            icon, sprites.source_bytes(SPRITE_CONFIG, icon), SPRITE_CONFIG["badge"]).encode()
    return files


# ---------------------------------------------------------------------------
# Glyphs

class GlyphPbfTest(unittest.TestCase):
    def test_round_trip(self):
        g = fake_glyph(0x10D0, 0)
        data = encode_range("Noto Sans Georgian Regular", "4096-4351", [g])
        found = glyphs.parse_glyphs(data)
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]["name"], "Noto Sans Georgian Regular")
        self.assertEqual(found[0]["range"], "4096-4351")
        self.assertEqual(found[0]["glyphs"], [g])

    def test_negative_top_survives(self):
        g = dict(fake_glyph(65, 0), top=-25, left=-3)
        found = glyphs.parse_glyphs(encode_range("x", "0-255", [g]))
        self.assertEqual(found[0]["glyphs"][0]["top"], -25)
        self.assertEqual(found[0]["glyphs"][0]["left"], -3)

    def test_empty_range_is_valid(self):
        found = glyphs.parse_glyphs(encode_range("x", "256-511", []))
        self.assertEqual(found[0]["glyphs"], [])
        self.assertEqual(glyphs.range_problems(found, "x", "256-511", 256), [])

    def test_truncated_file_is_an_error(self):
        data = encode_range("x", "0-255", [fake_glyph(65, 0)])
        with self.assertRaises(glyphs.PbfError):
            glyphs.parse_glyphs(data[:-3])

    def test_unknown_fields_are_ignored(self):
        data = encode_range("x", "0-255", [fake_glyph(65, 0)]) + _key(15, 0) + _varint(7)
        self.assertEqual(len(glyphs.parse_glyphs(data)), 1)

    def test_range_problems(self):
        good = fake_glyph(65, 0)
        cases = {
            "outside the range": [dict(good, id=300)],
            "appears twice": [good, good],
            "bytes, expected": [dict(good, bitmap=good["bitmap"][:-1])],
            "lacks": [{k: v for k, v in good.items() if k != "advance"}],
        }
        for words, glyph_list in cases.items():
            found = glyphs.parse_glyphs(encode_range("x", "0-255", glyph_list))
            problems = glyphs.range_problems(found, "x", "0-255", 0)
            self.assertTrue(any(words in p for p in problems), (words, problems))
        found = glyphs.parse_glyphs(encode_range("y", "0-255", [good]))
        self.assertTrue(glyphs.range_problems(found, "x", "0-255", 0))


class GlyphGateTest(unittest.TestCase):
    def check(self, files, extras=()):
        res, _ = glyphs.check_glyphs(files, GLYPH_CONFIG, extras=extras,
                                     results=pack_util.Results(quiet=True))
        return res

    def test_complete_set_passes(self):
        res = self.check(fake_set())
        self.assertTrue(res.passed, failed_names(res))

    def test_missing_mtavruli_fails(self):
        res = self.check(fake_set(drop={"Noto Sans Georgian Bold": {0x1C9A}}))
        self.assertEqual(failed_names(res), [
            "Noto Sans Georgian Bold: draws Georgian (Mkhedruli and Mtavruli), Latin, digits and Cyrillic",
            "'Noto Sans Georgian Bold,Noto Sans Bold' draws exactly like 'Noto Sans Georgian Bold' (same fonts)"])

    def test_georgian_without_digits_fails(self):
        res = self.check(fake_set(drop={"Noto Sans Georgian Regular": set(range(0x30, 0x3A))}))
        self.assertIn("Noto Sans Georgian Regular: draws Georgian (Mkhedruli and Mtavruli), Latin, "
                      "digits and Cyrillic", failed_names(res))

    def test_empty_required_range_fails(self):
        files = fake_set()
        files["Noto Sans Regular/7168-7423.pbf"] = encode_range("Noto Sans Regular", "7168-7423", [])
        failed = failed_names(self.check(files))
        self.assertIn("Noto Sans Regular: ranges 0-255, 4096-4351, 7168-7423 hold glyphs", failed)

    def test_missing_range_file_fails(self):
        files = fake_set()
        del files["Noto Sans Bold/4096-4351.pbf"]
        self.assertIn("layout: 6 stacks x 256 range files", failed_names(self.check(files)))

    def test_zero_byte_range_file_fails(self):
        files = fake_set()
        files["Noto Sans Bold/65280-65535.pbf"] = b""
        self.assertIn("layout: 6 stacks x 256 range files", failed_names(self.check(files)))

    def test_unexpected_file_fails_unless_allowed(self):
        files = fake_set()
        files["NOTICE.txt"] = b"hello"
        self.assertIn("layout: 6 stacks x 256 range files", failed_names(self.check(files)))
        self.assertTrue(self.check(files, extras={"NOTICE.txt"}).passed)

    def test_wrong_stack_name_inside_fails(self):
        files = fake_set()
        name = "Noto Sans Regular/0-255.pbf"
        files[name] = files[name].replace(b"Noto Sans Regular", b"Noto Sans Rxgular")
        self.assertIn("Noto Sans Regular: every range file is a valid glyph PBF",
                      failed_names(self.check(files)))

    def test_bold_built_from_regular_fails(self):
        config = json.loads(json.dumps(GLYPH_CONFIG))
        for stack in config["stacks"]:
            if stack["name"] == "Noto Sans Bold":
                stack["fonts"] = ["NotoSans-Regular.ttf", "NotoSansGeorgian-Regular.ttf"]
        self.assertIn("'Noto Sans Bold' differs from 'Noto Sans Regular'",
                      failed_names(self.check(fake_set(config))))

    def test_same_fonts_must_draw_the_same(self):
        files = fake_set()
        stack = "Noto Sans Georgian Regular,Noto Sans Regular"
        glyph_list = [fake_glyph(cp, 1) for cp in range(0x10D0, 0x1100)]
        glyph_list += [fake_glyph(cp, 0) for cp in (0x10A0,)]
        files[f"{stack}/4096-4351.pbf"] = encode_range(stack, "4096-4351", sorted(glyph_list, key=lambda g: g["id"]))
        self.assertIn(f"'{stack}' draws exactly like 'Noto Sans Georgian Regular' (same fonts)",
                      failed_names(self.check(files)))


class GlyphConfigTest(unittest.TestCase):
    def test_fonts_are_pinned(self):
        for font in GLYPH_CONFIG["fonts"]:
            self.assertRegex(font["url"], r"^https://raw\.githubusercontent\.com/notofonts/"
                                          r"notofonts\.github\.io/[0-9a-f]{40}/fonts/")
            self.assertEqual(font["licence"], "OFL-1.1")

    def test_licence_texts_match(self):
        members = glyphs.licence_members(GLYPH_CONFIG)
        self.assertEqual(len(members), 2)
        for data in members.values():
            self.assertIn(b"SIL OPEN FONT LICENSE Version 1.1", data)
            self.assertNotIn(b"Reserved Font Name", data.split(b"\n\n")[0])

    def test_style_font_names_exist(self):
        names = {s["name"] for s in GLYPH_CONFIG["stacks"]}
        for wanted in ("Noto Sans Georgian Regular", "Noto Sans Georgian Bold", "Noto Sans Regular",
                       "Noto Sans Georgian Regular,Noto Sans Regular"):
            self.assertIn(wanted, names)

    def test_every_georgian_stack_falls_back_to_noto_sans(self):
        for stack in GLYPH_CONFIG["stacks"]:
            families = [f.split("-")[0] for f in stack["fonts"]]
            self.assertEqual(sorted(families), ["NotoSans", "NotoSansGeorgian"], stack["name"])

    def test_font_problems(self):
        font = GLYPH_CONFIG["fonts"][0]
        self.assertEqual(len(glyphs.font_problems(font, b"x" * font["bytes"])), 1)
        self.assertEqual(len(glyphs.font_problems(font, b"x")), 2)

    def test_bad_stack_names_are_refused(self):
        for bad in ("../x", "LICENSES", " Noto", ".hidden"):
            config = json.loads(json.dumps(GLYPH_CONFIG))
            config["stacks"][0]["name"] = bad
            with tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp) / "glyphs.json"
                path.write_text(json.dumps(config))
                with self.assertRaises(ValueError):
                    glyphs.load_config(path)


FAKE_FONT_MAKER = """#!{python}
import sys, pathlib
args = sys.argv[1:]
if "--fail" in open(args[-1], "rb").read().decode("latin-1"):
    print("boom"); sys.exit(3)
name = args[args.index("--name") + 1]
out = pathlib.Path(args[args.index("--name") + 2])
if out.exists():
    print("ERROR: output directory exists"); sys.exit(1)
(out / name).mkdir(parents=True)
for start in (0, 256):
    (out / name / f"{{start}}-{{start + 255}}.pbf").write_bytes(b"x")
print("Wrote", name)
"""


class GlyphToolTest(unittest.TestCase):
    """fetch() and build() without the network or the real font-maker."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.config = json.loads(json.dumps(GLYPH_CONFIG))
        (self.dir / "src").mkdir()
        for font in self.config["fonts"]:
            data = f"fake {font['file']}".encode()
            (self.dir / "src" / font["file"]).write_bytes(data)
            font.update(bytes=len(data), sha256=pack_util.sha256_bytes(data),
                        url=(self.dir / "src" / font["file"]).as_uri())
        self.tool = self.dir / "font-maker"
        self.tool.write_text(FAKE_FONT_MAKER.format(python=sys.executable))
        self.tool.chmod(0o755)

    def tearDown(self):
        self.tmp.cleanup()

    def run(self, result=None):
        with contextlib.redirect_stdout(io.StringIO()):  # keep the test log to unittest's own lines
            return super().run(result)

    def test_fetch_checks_and_skips(self):
        glyphs.fetch(self.config, self.dir / "fonts")
        for font in self.config["fonts"]:
            self.assertEqual(pack_util.sha256_file(self.dir / "fonts" / font["file"]), font["sha256"])
        glyphs.fetch(self.config, self.dir / "fonts")  # already there: no download
        self.config["fonts"][0]["sha256"] = "0" * 64
        (self.dir / "fonts" / self.config["fonts"][0]["file"]).unlink()
        with self.assertRaises(SystemExit):
            glyphs.fetch(self.config, self.dir / "fonts")

    def test_build_runs_once_per_stack(self):
        glyphs.fetch(self.config, self.dir / "fonts")
        glyphs.build(self.config, self.dir / "fonts", self.tool, self.dir / "glyphs",
                     log_path=self.dir / "run.log")
        made = sorted(p.name for p in (self.dir / "glyphs").iterdir())
        self.assertEqual(made, sorted(s["name"] for s in self.config["stacks"]))
        self.assertTrue((self.dir / "glyphs" / "Noto Sans Georgian Regular,Noto Sans Regular" / "0-255.pbf").is_file())
        self.assertFalse((self.dir / "glyphs.work").exists())
        self.assertIn("Wrote Noto Sans Bold", (self.dir / "run.log").read_text())

    def test_build_refuses_unpinned_fonts_and_tool_failures(self):
        glyphs.fetch(self.config, self.dir / "fonts")
        (self.dir / "fonts" / "NotoSans-Bold.ttf").write_bytes(b"swapped")
        with self.assertRaises(SystemExit):
            glyphs.build(self.config, self.dir / "fonts", self.tool, self.dir / "glyphs")
        data = b"--fail"
        for font in self.config["fonts"]:
            if font["file"] == "NotoSans-Bold.ttf":
                font.update(bytes=len(data), sha256=pack_util.sha256_bytes(data))
        (self.dir / "fonts" / "NotoSans-Bold.ttf").write_bytes(data)
        with self.assertRaises(SystemExit):
            glyphs.build(self.config, self.dir / "fonts", self.tool, self.dir / "glyphs")


class GlyphPackTest(unittest.TestCase):
    def write_set(self, root, files):
        for name, data in files.items():
            path = Path(root) / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)

    def test_pack_gates_the_zip_and_is_reproducible(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.write_set(Path(tmp) / "glyphs", fake_set())
            shas = []
            for n in range(2):
                out, report = Path(tmp) / f"out{n}" / "glyphs.zip", Path(tmp) / f"out{n}" / "glyphs_report.json"
                code = glyphs.pack(GLYPH_CONFIG, glyphs.DEFAULT_CONFIG, Path(tmp) / "glyphs", out, report,
                                   commit="abc", tool_commit="def", freetype="2.13.2", quiet=True)
                self.assertEqual(code, 0)
                data = json.loads(report.read_text())
                self.assertTrue(data["passed"])
                self.assertEqual(data["section"], "glyphs")
                self.assertEqual(data["files"][0]["sha256"], pack_util.sha256_file(out))
                shas.append(data["files"][0]["sha256"])
                members = pack_util.read_zip(out)
                self.assertIn("NOTICE.txt", members)
                self.assertIn("LICENSES/OFL-notofonts-georgian.txt", members)
                self.assertIn("Noto Sans Georgian Regular/7168-7423.pbf", members)
                # What the manifest's credits take: the OFL copyright notice and both families.
                self.assertEqual(data["licence"], "OFL-1.1")
                self.assertIn("Copyright 2022 The Noto Project Authors", data["attribution"])
                self.assertEqual({c["name"] for c in data["credit_sources"]}, {"Noto Sans Georgian", "Noto Sans"})
            self.assertEqual(shas[0], shas[1])

    def test_failed_gate_leaves_no_zip(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.write_set(Path(tmp) / "glyphs", fake_set(drop={"Noto Sans Bold": {0x10D0}}))
            out, report = Path(tmp) / "glyphs.zip", Path(tmp) / "glyphs_report.json"
            code = glyphs.pack(GLYPH_CONFIG, glyphs.DEFAULT_CONFIG, Path(tmp) / "glyphs", out, report,
                               quiet=True)
            self.assertEqual(code, 1)
            self.assertFalse(out.exists())
            data = json.loads(report.read_text())
            self.assertFalse(data["passed"])
            self.assertEqual(data["files"], [])


# ---------------------------------------------------------------------------
# Sprites

class PngTest(unittest.TestCase):
    def test_rgba_every_filter(self):
        w, h = 7, 10
        rgba = bytes((x * 37 + y * 11 + c * 50) & 0xFF for y in range(h) for x in range(w) for c in range(4))
        for kind in range(5):
            rows = [rgba[y * w * 4:(y + 1) * w * 4] for y in range(h)]
            got = sprites.read_png(encode_png(w, h, rows, filters=(kind,)))
            self.assertEqual(got, (w, h, bytearray(rgba)), f"filter {kind}")

    def test_palette_four_bit_with_transparency(self):
        palette = bytes((0, 0, 0, 255, 0, 0, 0, 255, 0))
        trns = bytes((0, 128))  # index 2 stays opaque
        w, h = 5, 3
        indexes = [[(x + y) % 3 for x in range(w)] for y in range(h)]
        rows = []
        for line in indexes:
            packed = bytearray()
            for i in range(0, w, 2):
                hi, lo = line[i], line[i + 1] if i + 1 < w else 0
                packed.append(hi << 4 | lo)
            rows.append(bytes(packed))
        _, _, rgba = sprites.read_png(encode_png(w, h, rows, colour_type=3, depth=4,
                                                 palette=palette, trns=trns))
        for y in range(h):
            for x in range(w):
                i = indexes[y][x]
                o = (y * w + x) * 4
                want = tuple(palette[3 * i:3 * i + 3]) + ((0, 128, 255)[i],)
                self.assertEqual(tuple(rgba[o:o + 4]), want)

    def test_grey_alpha(self):
        rows = [bytes((10, 20, 30, 40)), bytes((50, 60, 70, 80))]
        _, _, rgba = sprites.read_png(encode_png(2, 2, rows, colour_type=4))
        self.assertEqual(tuple(rgba[:8]), (10, 10, 10, 20, 30, 30, 30, 40))

    def test_crc_error_is_refused(self):
        png = bytearray(rgba_png(2, 2, bytes(16)))
        png[20] ^= 1  # inside IHDR
        with self.assertRaises(ValueError):
            sprites.read_png(bytes(png))

    def test_interlaced_is_refused(self):
        png = rgba_png(2, 2, bytes(16))
        ihdr = struct.pack(">IIBBBBB", 2, 2, 8, 6, 0, 0, 1)
        png = png[:8] + _chunk(b"IHDR", ihdr) + png[8 + 25:]
        with self.assertRaises(ValueError):
            sprites.read_png(png)


class SpriteGateTest(unittest.TestCase):
    def check(self, files):
        return sprites.check_sprites(files, SPRITE_CONFIG, results=pack_util.Results(quiet=True))

    def test_good_sheets_pass(self):
        res = self.check(fake_sprite_files())
        self.assertTrue(res.passed, [r for r in res.items if not r["passed"]])

    def test_missing_icon_fails(self):
        files = fake_sprite_files()
        index = json.loads(files["sprite.json"])
        del index["police"]
        files["sprite.json"] = json.dumps(index).encode()
        failed = failed_names(self.check(files))
        self.assertIn("sprite.json and sprite.png agree, every icon drawn", failed)

    def test_shifted_index_fails(self):
        files = fake_sprite_files()
        index = json.loads(files["sprite@2x.json"])
        index["fuel"]["x"] += 3
        files["sprite@2x.json"] = json.dumps(index).encode()
        failed = failed_names(self.check(files))
        self.assertIn("sprite@2x.json and sprite@2x.png agree, every icon drawn", failed)

    def test_wrong_pixel_ratio_fails(self):
        files = fake_sprite_files()
        index = json.loads(files["sprite@2x.json"])
        index["parking"]["pixelRatio"] = 1
        files["sprite@2x.json"] = json.dumps(index).encode()
        self.assertIn("sprite@2x.json and sprite@2x.png agree, every icon drawn",
                      failed_names(self.check(files)))

    def test_1x_sheet_in_2x_slot_fails(self):
        files = fake_sprite_files()
        index = {k: dict(v, pixelRatio=2) for k, v in json.loads(files["sprite.json"]).items()}
        files["sprite@2x.json"] = json.dumps(index).encode()
        files["sprite@2x.png"] = files["sprite.png"]
        failed = failed_names(self.check(files))
        self.assertIn("1x and 2x list the same icons at double size", failed)

    def test_missing_glyph_fails(self):
        res = self.check(fake_sprite_files(skip_glyph={"hospital"}))
        problems = [p for r in res.items for p in r["problems"]]
        self.assertTrue(any(p.startswith("hospital: no glyph") for p in problems), problems)

    def test_wrong_colour_fails(self):
        res = self.check(fake_sprite_files(colour_override={"speed-camera": "#00FF00"}))
        problems = [p for r in res.items for p in r["problems"]]
        self.assertTrue(any(p.startswith("speed-camera: badge colour") for p in problems), problems)

    def test_missing_svg_fails(self):
        files = fake_sprite_files()
        del files["svg/ferry.svg"]
        self.assertIn("layout: sprite.json/png, sprite@2x.json/png and one badge SVG per icon",
                      failed_names(self.check(files)))


class BadgeTest(unittest.TestCase):
    def test_vendored_icons_match_their_pins(self):
        for icon in SPRITE_CONFIG["icons"]:
            sprites.source_bytes(SPRITE_CONFIG, icon)  # raises on a mismatch

    def test_requested_icons_are_there(self):
        names = {i["name"] for i in SPRITE_CONFIG["icons"]}
        for wanted in ("fuel", "parking", "speed-camera", "police", "hospital", "border-crossing",
                       "ferry-excluded"):
            self.assertIn(wanted, names)

    def test_sources_are_cc0(self):
        for key, source in SPRITE_CONFIG["sources"].items():
            self.assertEqual(source["licence"], "CC0-1.0")
            self.assertRegex(source["commit"], r"^[0-9a-f]{40}$")
        for data in sprites.licence_members(SPRITE_CONFIG).values():
            self.assertIn(b"CC0 1.0 Universal", data)

    def test_badges(self):
        ns = {"s": sprites.SVG_NS}
        for icon in SPRITE_CONFIG["icons"]:
            svg = sprites.badge_svg(icon, sprites.source_bytes(SPRITE_CONFIG, icon), SPRITE_CONFIG["badge"])
            root = ET.fromstring(svg)
            self.assertEqual((root.get("width"), root.get("height")), ("24", "24"))
            fills = [c.get("fill") for c in root.findall("s:circle", ns)]
            self.assertEqual(fills, ["#000000", "#FFFFFF", icon["colour"].upper()])
            group = root.find("s:g", ns)
            self.assertEqual(group.get("fill"), "#FFFFFF")
            for element in list(group.iter())[1:]:
                self.assertIsNone(element.get("id"))
                self.assertIn(element.get("fill"), (None, "none"))
            self.assertEqual(len(root.findall("s:line", ns)), 2 if icon.get("slash") else 0)

    def test_contrast_rule(self):
        self.assertGreaterEqual(sprites.contrast((0, 122, 255), (255, 255, 255)), 3.0)
        self.assertLess(sprites.contrast((255, 149, 0), (255, 255, 255)), 3.0)  # iOS orange fails
        config = json.loads(json.dumps(SPRITE_CONFIG))
        config["icons"][0]["colour"] = "#FF9500"
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "sprites.json"
            path.write_text(json.dumps(config))
            with self.assertRaises(ValueError):
                sprites.load_config(path)

    def test_unsupported_svg_is_refused(self):
        icon = dict(SPRITE_CONFIG["icons"][0])
        for body in ('<style>path{fill:red}</style><path d="M0 0h1v1z"/>',
                     '<path style="fill:red" d="M0 0h1v1z"/>',
                     '<use href="#a"/>'):
            data = f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 15 15">{body}</svg>'.encode()
            with self.assertRaises(ValueError):
                sprites.badge_svg(icon, data, SPRITE_CONFIG["badge"])

    def test_compose_and_pack(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            sprites.compose(SPRITE_CONFIG, tmp / "svg", quiet=True)
            self.assertEqual(sorted(p.stem for p in (tmp / "svg").glob("*.svg")),
                             sorted(i["name"] for i in SPRITE_CONFIG["icons"]))
            (tmp / "sheets").mkdir()
            for name, data in fake_sprite_files().items():
                if not name.startswith("svg/"):
                    (tmp / "sheets" / name).write_bytes(data)
            out, report = tmp / "out" / "sprites.zip", tmp / "out" / "sprites_report.json"
            code = sprites.pack(SPRITE_CONFIG, sprites.DEFAULT_CONFIG, tmp / "sheets", tmp / "svg", out,
                                report, commit="abc", spreet="spreet 0.13.1", spreet_sha256="0" * 64,
                                quiet=True)
            self.assertEqual(code, 0)
            data = json.loads(report.read_text())
            self.assertTrue(data["passed"])
            self.assertEqual(data["files"][0]["sha256"], pack_util.sha256_file(out))
            members = pack_util.read_zip(out)
            for name in ("sprite.json", "sprite.png", "sprite@2x.json", "sprite@2x.png",
                         "svg/ferry-excluded.svg", "NOTICE.txt", "LICENSES/maki-LICENSE.txt",
                         "LICENSES/temaki-LICENSE.md", "LICENSES/vinari-MIT.txt"):
                self.assertIn(name, members)
            # The badge designs are not dedicated to the public domain (an owner decision).
            self.assertIn(b"MIT License", members["LICENSES/vinari-MIT.txt"])
            self.assertNotIn(b"DATA", members["LICENSES/vinari-MIT.txt"])
            self.assertIn(b"MIT", members["NOTICE.txt"])
            self.assertNotIn(b"sheets made from them are CC0", members["NOTICE.txt"])
            self.assertEqual(data["licence"], "MIT (badges), CC0-1.0 (glyphs)")
            self.assertEqual([c["licence"] for c in data["credit_sources"]], ["CC0-1.0", "CC0-1.0"])


# ---------------------------------------------------------------------------
# manifest.py --asset-report

class AssetReportTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        (self.dir / "glyphs.zip").write_bytes(b"zip bytes")
        self.report = {"section": "glyphs", "passed": True,
                       "files": [pack_util.file_entry(self.dir / "glyphs.zip")],
                       "licence": "OFL-1.1", "gate": {"passed": True, "checks": 3, "failed": 0}}

    def tearDown(self):
        self.tmp.cleanup()

    def run_report(self, report, sections=frozenset(), names=frozenset()):
        path = self.dir / "glyphs_report.json"
        path.write_text(json.dumps(report))
        return manifest.asset_report(path, set(manifest.RESERVED_SECTIONS) | set(sections), set(names))

    def test_good_report(self):
        section, body, entries, problems = self.run_report(self.report)
        self.assertEqual(problems, [])
        self.assertEqual(section, "glyphs")
        self.assertEqual(entries, self.report["files"])
        self.assertEqual(body["files"], ["glyphs.zip"])
        self.assertEqual(body["licence"], "OFL-1.1")
        self.assertNotIn("section", body)

    def test_changed_file_is_refused(self):
        (self.dir / "glyphs.zip").write_bytes(b"other bytes")
        self.assertTrue(self.run_report(self.report)[3])

    def test_failed_gate_is_refused(self):
        self.assertTrue(self.run_report(dict(self.report, passed=False))[3])

    def test_reserved_or_repeated_section_is_refused(self):
        self.assertTrue(self.run_report(dict(self.report, section="gate"))[3])
        self.assertTrue(self.run_report(self.report, sections={"glyphs"})[3])
        self.assertTrue(self.run_report(dict(self.report, section="../x"))[3])

    def test_name_clash_is_refused(self):
        self.assertTrue(self.run_report(self.report, names={"glyphs.zip"})[3])

    def test_missing_file_is_refused(self):
        (self.dir / "glyphs.zip").unlink()
        self.assertTrue(self.run_report(self.report)[3])

    def test_no_files_is_refused(self):
        self.assertTrue(self.run_report(dict(self.report, files=[]))[3])


if __name__ == "__main__":
    unittest.main(verbosity=1)
