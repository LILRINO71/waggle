"""cad_convert / cad_measure (waggle/tools_cad.py): a fake FreeCAD process for the plumbing, the real engine when present."""
import json
from pathlib import Path

import pytest
from waggle import policy
from waggle import tools_common as tc
from waggle import tools_cad

CUBE_STL = """solid cube
{}endsolid cube
"""


def cube_stl(size=10.0):
    """An ASCII STL cube (12 triangles), enough for a real engine to open."""
    s = size
    v = [(0, 0, 0), (s, 0, 0), (s, s, 0), (0, s, 0), (0, 0, s), (s, 0, s), (s, s, s), (0, s, s)]
    quads = [(0, 3, 2, 1), (4, 5, 6, 7), (0, 1, 5, 4), (2, 3, 7, 6), (1, 2, 6, 5), (0, 4, 7, 3)]
    out = ""
    for a, b, c, d in quads:
        for tri in ((a, b, c), (a, c, d)):
            out += " facet normal 0 0 0\n  outer loop\n" + "".join("   vertex %g %g %g\n" % v[i] for i in tri) \
                   + "  endloop\n endfacet\n"
    return CUBE_STL.format(out)


@pytest.fixture
def writable(tmp_path, monkeypatch):
    monkeypatch.setattr(tc, "writable_roots", lambda settings=None: [policy.norm(tmp_path)])
    return tmp_path


class FakeFreeCAD:
    """Stands in for tc.run([freecadcmd, job.py]): reads the job JSON the way the script would and writes a result."""

    def __init__(self, monkeypatch, result):
        self.jobs, self.result = [], result
        monkeypatch.setattr(tools_cad, "freecad_exe", lambda: "freecadcmd.exe")
        monkeypatch.setattr(tc, "run", self.run)

    def run(self, cmd, timeout=120, cwd=None, env=None, **kw):
        job = json.loads(Path(env["WAGGLE_CAD_JOB"]).read_text(encoding="utf-8"))
        self.jobs.append((cmd, job))
        res = self.result(job) if callable(self.result) else self.result
        if res is not None:
            Path(job["result"]).write_text(json.dumps(res), encoding="utf-8")
        if res and res.get("ok") and job["mode"] == "convert":
            Path(job["dst"]).write_bytes(b"solid")
        return 0, "", ""


def test_embedded_freecad_script_is_valid_python():
    compile(tools_cad.SCRIPT, "freecad_job.py", "exec")


def test_convert_builds_the_job_and_reports_the_new_file(writable, monkeypatch):
    src = writable / "bracket.step"
    src.write_text("step")
    fake = FakeFreeCAD(monkeypatch, {"ok": True, "facets": 160, "closed_solid": True})
    res = json.loads(tools_cad.cad_convert({"path": str(src), "to": "STL", "quality": "fine", "scale": "25.4"}))
    assert res["ok"] and res["to"] == "stl" and res["facets"] == 160 and res["quality"] == "fine"
    assert Path(res["output"]["path"]) == writable / "bracket.stl"
    cmd, job = fake.jobs[0]
    assert job["mode"] == "convert" and job["scale"] == 25.4 and job["lin"] == 0.01 and job["src"] == str(src)
    assert Path(cmd[1]).name == "job.py"
    # a second run never overwrites the first
    res2 = json.loads(tools_cad.cad_convert({"path": str(src), "to": "stl"}))
    assert Path(res2["output"]["path"]).name == "bracket (2).stl"


def test_convert_refuses_bad_requests(writable, monkeypatch):
    src = writable / "a.stl"
    src.write_text("x")
    FakeFreeCAD(monkeypatch, {"ok": True})
    for args, word in [({"path": str(src), "to": "dwg"}, "cannot write"), ({"path": str(src), "to": "stl"}, "already"),
                       ({"path": str(src), "to": "step", "scale": "0"}, "scale"),
                       ({"path": str(writable / "a.pdf"), "to": "stl"}, "not found"), ({"path": ""}, "no input")]:
        out = json.loads(tools_cad.cad_convert(args))
        assert not out["ok"] and word in out["error"], (args, out)


def test_convert_failure_leaves_no_file_and_says_why(writable, monkeypatch):
    src = writable / "a.step"
    src.write_text("x")
    FakeFreeCAD(monkeypatch, {"ok": False, "error": "RuntimeError: bad shape", "trace": "tb"})
    out = json.loads(tools_cad.cad_convert({"path": str(src), "to": "stl"}))
    assert not out["ok"] and "bad shape" in out["error"]
    assert not (writable / "a.stl").exists()


def test_freecad_that_writes_no_result_is_reported(writable, monkeypatch):
    src = writable / "a.step"
    src.write_text("x")
    FakeFreeCAD(monkeypatch, lambda job: None)
    out = json.loads(tools_cad.cad_measure({"path": str(src)}))
    assert not out["ok"] and "did not finish" in out["error"]


def test_measure_adds_weight_for_a_known_material(writable, monkeypatch):
    src = writable / "a.step"
    src.write_text("x")
    FakeFreeCAD(monkeypatch, {"ok": True, "kind": "solid model", "volume_mm3": 7497.35, "bbox_mm": {"size": [40, 20, 10]}})
    out = json.loads(tools_cad.cad_measure({"path": str(src), "material": "Aluminum 6061"}))
    assert out["ok"] and out["mass_g"]["mass"] == 20.2 and out["mass_g"]["material"] == "aluminum"
    bad = json.loads(tools_cad.cad_measure({"path": str(src), "material": "unobtainium"}))
    assert "unknown material" in bad["mass_g"]["error"] and "steel" in bad["mass_g"]["known"]
    plain = json.loads(tools_cad.cad_measure({"path": str(src)}))
    assert "mass_g" not in plain and "units" in plain


def test_missing_freecad_is_a_clear_failure(writable, monkeypatch):
    src = writable / "a.step"
    src.write_text("x")
    monkeypatch.setattr(tc, "which", lambda *a, **k: "")
    out = json.loads(tools_cad.cad_measure({"path": str(src)}))
    assert not out["ok"] and "FreeCAD is not installed" in out["error"] and out["hint"]


def test_mass_lookup():
    assert tools_cad.mass_g(1000, "PETG filament") == (1.3, "petg", 1.27)
    assert tools_cad.mass_g(1000, "") is None


@pytest.mark.skipif(not tc.which("freecadcmd", extra=tools_cad.FREECAD_GLOBS), reason="FreeCAD is not installed")
def test_real_freecad_measures_and_converts_a_mesh(writable):
    stl = writable / "cube.stl"
    stl.write_text(cube_stl(10.0))
    m = json.loads(tools_cad.cad_measure({"path": str(stl), "material": "pla"}))
    assert m["ok"] and m["bbox_mm"]["size"] == [10.0, 10.0, 10.0] and m["facets"] == 12
    assert m["closed_solid"] and abs(m["volume_mm3"] - 1000) < 1 and m["mass_g"]["mass"] == 1.2
    c = json.loads(tools_cad.cad_convert({"path": str(stl), "to": "obj", "scale": 2}))
    assert c["ok"] and Path(c["output"]["path"]).read_text().count("\nv ") == 8
    m2 = json.loads(tools_cad.cad_measure({"path": c["output"]["path"]}))
    assert m2["ok"] and m2["bbox_mm"]["size"] == [20.0, 20.0, 20.0]
