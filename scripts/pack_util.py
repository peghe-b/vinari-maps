"""Helpers shared by glyphs.py and sprites.py: hashing, reproducible zips,
and gate results in the same shape as gate.py's.

Standard library only, so the glyphs-sprites job needs no pip install.
"""

import hashlib
import zipfile
from pathlib import Path, PurePosixPath

# The earliest time a zip can hold. A fixed time, sorted names and fixed
# modes make the same input give the same zip, byte for byte.
ZIP_TIME = (1980, 1, 1, 0, 0, 0)


def sha256_bytes(data):
    return hashlib.sha256(data).hexdigest()


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def file_entry(path):
    """The same shape manifest.py uses for every released file."""
    path = Path(path)
    return {"name": path.name, "bytes": path.stat().st_size, "sha256": sha256_file(path)}


def safe_member(name):
    """A zip member name that cannot escape the folder it is unpacked into."""
    p = PurePosixPath(name)
    return (bool(name) and not name.startswith("/") and "\\" not in name
            and ".." not in p.parts and not name.endswith("/"))


def read_tree(root):
    """Every file under root as {relative posix path: bytes}."""
    root = Path(root)
    return {p.relative_to(root).as_posix(): p.read_bytes()
            for p in sorted(root.rglob("*")) if p.is_file()}


def write_zip(path, members):
    """Write {name: bytes} as a reproducible zip (deflate level 9)."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    bad = [n for n in members if not safe_member(n)]
    if bad:
        raise ValueError(f"unsafe zip member names: {bad[:5]}")
    tmp = path.with_name(path.name + ".part")
    with zipfile.ZipFile(tmp, "w") as z:
        for name in sorted(members):
            info = zipfile.ZipInfo(name, date_time=ZIP_TIME)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.create_system = 3          # Unix, whatever machine writes it
            info.external_attr = 0o644 << 16
            z.writestr(info, members[name], compresslevel=9)
    tmp.replace(path)


def read_zip(path):
    """Every file in a zip as {name: bytes}. Refuses duplicate or unsafe names,
    so the gate sees exactly what an unzip on the phone would write."""
    files = {}
    with zipfile.ZipFile(path) as z:
        bad = z.testzip()
        if bad is not None:
            raise ValueError(f"{path}: CRC error in {bad}")
        for info in z.infolist():
            if info.is_dir():
                continue
            if not safe_member(info.filename):
                raise ValueError(f"{path}: unsafe member name {info.filename!r}")
            if info.filename in files:
                raise ValueError(f"{path}: {info.filename!r} appears twice")
            files[info.filename] = z.read(info)
    return files


class Results:
    """Gate results, printed as they come, in gate.py's shape."""

    def __init__(self, quiet=False):
        self.items = []
        self.quiet = quiet

    def record(self, name, problems, detail=None):
        problems = list(problems)
        self.items.append({"name": name, "passed": not problems, "problems": problems,
                           "detail": detail or {}})
        if not self.quiet:
            shown = problems[:20] + ([f"... and {len(problems) - 20} more"] if len(problems) > 20 else [])
            print(("PASS  " if not problems else "FAIL  ") + name +
                  ("" if not problems else "\n      " + "\n      ".join(shown)))

    def guarded(self, name, func):
        """Run func() -> (problems, detail); a crash is a failure, never a pass."""
        try:
            problems, detail = func()
        except Exception as exc:
            problems, detail = [f"error: {exc}"], {}
        self.record(name, problems, detail)

    @property
    def passed(self):
        return bool(self.items) and all(r["passed"] for r in self.items)

    def summary(self):
        failed = sum(1 for r in self.items if not r["passed"])
        return {"passed": self.passed, "checks": len(self.items), "failed": failed,
                "results": self.items}
