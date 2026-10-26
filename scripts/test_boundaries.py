#!/usr/bin/env python3
"""Self-test for fetch_boundaries.py --check. No downloads, standard library only.

Run from the maps/ folder:
    python3 scripts/test_boundaries.py

The OSM API is replaced by made-up answers. The case that matters: on
22 Oct 2026 an OSM edit left Georgia's border (relation 28699) as a ring
that does not close at node 14308410565, and the old check printed a
warning and stayed green. A boundary that cannot be checked must make
--fail-on-change exit 1 and name the relation, the node and the way.
"""

import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import fetch_boundaries as fb  # noqa: E402

# A small square, as four nodes and two ways that together close.
NODES = {1: (44.0, 42.0), 2: (44.1, 42.0), 3: (44.1, 42.1), 4: (44.0, 42.1),
         14308410565: (44.05, 42.15)}
WAY_A = [1, 2, 3]
WAY_B = [3, 4, 1]
# The broken version: way B's end was moved to a new node, so it no longer
# meets way A again (the shape of the 22 Oct edit).
WAY_B_BROKEN = [3, 4, 14308410565]


def full_json(rel_id, version, ways):
    """An OSM API /relation/{id}/full.json answer."""
    used = sorted({n for nodes in ways.values() for n in nodes})
    elements = [{"type": "node", "id": n, "lon": NODES[n][0], "lat": NODES[n][1]} for n in used]
    elements += [{"type": "way", "id": wid, "nodes": nodes} for wid, nodes in ways.items()]
    elements.append({"type": "relation", "id": rel_id, "version": version,
                     "timestamp": "2026-10-22T05:08:51Z", "tags": {"name": "Test"},
                     "members": [{"type": "way", "ref": wid, "role": "outer"} for wid in ways]})
    return {"elements": elements}


class BoundaryCheckTest(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        (root / "config" / "boundaries").mkdir(parents=True)
        self.entry = {"name": "Georgia (test)", "osm_type": "relation", "osm_id": 28699,
                      "osm_version": 541, "file": "boundaries/georgia_28699.geojson"}
        self.config = {"metric_crs": "EPSG:32638", "country": self.entry, "no_go": []}
        (root / "config" / "clip.json").write_text(json.dumps(self.config))
        # Freeze the healthy square exactly as --refresh would.
        healthy = full_json(28699, 541, {111: WAY_A, 380082879: WAY_B})
        with mock.patch.object(fb, "http_json", return_value=healthy):
            geometry, meta = fb.fetch_polygon(self.entry)
        feature = {"type": "Feature", "properties": meta, "geometry": geometry}
        (root / "config" / self.entry["file"]).write_text(fb.to_geojson_text(feature))
        self.patches = [mock.patch.object(fb, "ROOT", root),
                        mock.patch.object(fb, "CONFIG", root / "config" / "clip.json"),
                        # hashes only, so the test needs no shapely/pyproj
                        mock.patch.object(fb, "Geometry", side_effect=ImportError),
                        mock.patch.object(fb.time, "sleep")]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()
        self.tmp.cleanup()

    def run_main(self, answer, *flags):
        out = io.StringIO()
        kwargs = {"side_effect": answer} if isinstance(answer, Exception) else {"return_value": answer}
        with mock.patch.object(fb, "http_json", **kwargs), \
                mock.patch.object(sys, "argv", ["fetch_boundaries.py", "--check", *flags]), \
                contextlib.redirect_stdout(out):
            code = fb.main()
        return code, out.getvalue()

    def test_join_rings_names_the_node_and_the_way(self):
        with self.assertRaises(fb.BrokenInOSM) as ctx:
            fb.join_rings([WAY_A, WAY_B_BROKEN], [111, 380082879])
        self.assertIn("node 14308410565", str(ctx.exception))
        self.assertIn("way 380082879", str(ctx.exception))
        self.assertIsInstance(ctx.exception, ValueError)  # older callers catch ValueError

    def test_healthy_boundary_stays_green(self):
        code, out = self.run_main(full_json(28699, 541, {111: WAY_A, 380082879: WAY_B}), "--fail-on-change")
        self.assertEqual(code, 0, out)
        self.assertIn("ok Georgia (test)", out)
        self.assertIn("0 changed upstream, 0 could not be checked", out)
        self.assertNotIn("::error", out)

    def test_broken_ring_fails_the_job_and_names_relation_node_and_way(self):
        code, out = self.run_main(full_json(28699, 542, {111: WAY_A, 380082879: WAY_B_BROKEN}),
                                  "--fail-on-change")
        self.assertEqual(code, 1, out)
        self.assertIn("::error title=Boundary broken in OSM::", out)
        self.assertIn("relation 28699", out)
        self.assertIn("node 14308410565", out)
        self.assertIn("way 380082879", out)
        self.assertIn("frozen copy (v541)", out)
        self.assertIn("0 changed upstream, 1 could not be checked", out)
        self.assertIn("NOT CHECKED Georgia (test)", out)

    def test_missing_member_way_fails_the_job(self):
        answer = full_json(28699, 542, {111: WAY_A, 380082879: WAY_B})
        answer["elements"] = [e for e in answer["elements"]
                              if not (e["type"] == "way" and e["id"] == 380082879)]
        code, out = self.run_main(answer, "--fail-on-change")
        self.assertEqual(code, 1, out)
        self.assertIn("lists way 380082879 but OSM did not return it", out)

    def test_download_that_keeps_failing_fails_the_job(self):
        code, out = self.run_main(RuntimeError("could not fetch https://api.openstreetmap.org/x: timed out"),
                                  "--fail-on-change")
        self.assertEqual(code, 1, out)
        self.assertIn("::error title=Boundary could not be checked::", out)
        self.assertIn("relation 28699", out)

    def test_without_fail_on_change_the_exit_code_stays_zero(self):
        code, out = self.run_main(full_json(28699, 542, {111: WAY_A, 380082879: WAY_B_BROKEN}))
        self.assertEqual(code, 0, out)
        self.assertIn("1 could not be checked", out)


if __name__ == "__main__":
    unittest.main(verbosity=2)
