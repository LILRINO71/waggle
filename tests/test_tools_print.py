"""print_estimate (waggle/tools_print.py): fake slicer for the plumbing, the real PrusaSlicer when installed."""
import json
from pathlib import Path

import pytest
from test_tools_cad import cube_stl
from waggle import policy
from waggle import tools_cad, tools_print
from waggle import tools_common as tc

GCODE_TAIL = """; filament used [mm] = 1614.90
; filament used [cm3] = 3.88
; filament used [g] = 4.82
; total filament used [g] = 4.82
; estimated printing time (normal mode) = 1d 2h 23m 8s
; estimated printing time (silent mode) = 23m 10s
"""


@pytest.fixture
def writable(tmp_path, monkeypatch):
    monkeypatch.setattr(tc, "writable_roots", lambda settings=None: [policy.norm(tmp_path)])
    return tmp_path


class FakeSlicer:
    def __init__(self, monkeypatch, tail=GCODE_TAIL, out="Slicing result exported", write=True):
        self.calls = []
        self.tail, self.out, self.write = tail, out, write
        monkeypatch.setattr(tools_print, "slicer_exe", lambda: "prusa-slicer-console.exe")
        monkeypatch.setattr(tc, "run", self.run)

    def run(self, cmd, timeout=120, cwd=None, **kw):
        cfg = Path(cmd[cmd.index("--load") + 1])
        self.calls.append((cmd, cfg.read_text(encoding="utf-8")))
        gcode = Path(cmd[cmd.index("--output") + 1])
        if self.write:
            gcode.write_text("G1 X1\n" * 5 + self.tail, encoding="utf-8")
        return (0 if self.write else 1), self.out, ""


def test_parse_the_estimate_block():
    facts = tools_print.parse_gcode_tail(GCODE_TAIL)
    assert facts == {"time": "1d 2h 23m 8s", "seconds": 86400 + 7200 + 1380 + 8, "filament_g": 4.82, "filament_m": 1.61}
    assert tools_print.parse_gcode_tail("nothing here") == {}


def test_config_describes_the_ender_3_max():
    cfg = tools_print.config_text("ender3max", "petg", 0.28, 15, True)
    for line in ("bed_shape = 0x0,300x0,300x300,0x300", "max_print_height = 340", "filament_density = 1.27",
                 "layer_height = 0.28", "fill_density = 15%", "support_material = 1", "gcode_flavor = marlin2"):
        assert line in cfg.splitlines()


def test_estimate_slices_in_a_temp_folder_and_keeps_nothing(writable, monkeypatch):
    stl = writable / "part.stl"
    stl.write_text("x")
    fake = FakeSlicer(monkeypatch)
    out = json.loads(tools_print.print_estimate({"path": str(stl), "material": "petg", "infill": 10}))
    assert out["ok"] and out["print_time"] == "1d 2h 23m 8s" and out["filament_g"] == 4.82
    assert "PETG" in out["assumptions"] and "10% infill" in out["assumptions"] and "Ender-3 Max" in out["printer"]
    cmd, cfg = fake.calls[0]
    assert "fill_density = 10%" in cfg and str(stl) == cmd[-1]
    assert [p.name for p in writable.iterdir() if p.name != "home"] == ["part.stl"]  # no gcode left near the part


def test_step_files_go_through_freecad_first(writable, monkeypatch):
    step = writable / "bracket.step"
    step.write_text("x")
    seen = {}

    def fake_job(job, timeout=180):
        seen.update(job)
        Path(job["dst"]).write_text("solid")
        return {"ok": True}

    monkeypatch.setattr(tools_cad, "run_job", fake_job)
    FakeSlicer(monkeypatch)
    out = json.loads(tools_print.print_estimate({"path": str(step)}))
    assert out["ok"] and seen["mode"] == "convert" and seen["dst"].endswith("bracket.stl")


def test_a_part_that_does_not_fit_is_said_plainly(writable, monkeypatch):
    stl = writable / "big.stl"
    stl.write_text("x")
    FakeSlicer(monkeypatch, write=False, out="Objects could not fit on the bed")
    out = json.loads(tools_print.print_estimate({"path": str(stl)}))
    assert not out["ok"] and "does not fit" in out["error"] and "300 x 300 x 340" in out["error"]


def test_bad_arguments_and_a_missing_slicer(writable, monkeypatch):
    stl = writable / "p.stl"
    stl.write_text("x")
    FakeSlicer(monkeypatch)
    for args, word in [({"path": str(stl), "material": "wood"}, "unknown material"),
                       ({"path": str(stl), "printer": "k1"}, "unknown printer"),
                       ({"path": str(stl), "infill": 400}, "infill"), ({"path": str(stl), "layer_height": "x"}, "layer_height")]:
        out = json.loads(tools_print.print_estimate(args))
        assert not out["ok"] and word in out["error"], (args, out)


def test_a_missing_slicer_is_a_clear_failure(writable, monkeypatch):
    stl = writable / "p.stl"
    stl.write_text("x")
    monkeypatch.setattr(tc, "which", lambda *a, **k: "")
    out = json.loads(tools_print.print_estimate({"path": str(stl)}))
    assert not out["ok"] and "PrusaSlicer is not installed" in out["error"]


def test_unreadable_slicer_output_is_an_error(writable, monkeypatch):
    stl = writable / "p.stl"
    stl.write_text("x")
    FakeSlicer(monkeypatch, tail="; nothing useful\n")
    assert "could not read the estimate" in json.loads(tools_print.print_estimate({"path": str(stl)}))["error"]


@pytest.mark.skipif(not tc.which("prusa-slicer-console", extra=tools_print.SLICER_GLOBS), reason="PrusaSlicer is not installed")
def test_real_prusaslicer_estimates_a_cube(writable):
    stl = writable / "cube.stl"
    stl.write_text(cube_stl(20.0))
    out = json.loads(tools_print.print_estimate({"path": str(stl)}))
    assert out["ok"] and out["print_seconds"] > 60 and 0.5 < out["filament_g"] < 20
    stl.write_text(cube_stl(900.0))
    big = json.loads(tools_print.print_estimate({"path": str(stl)}))
    assert not big["ok"]
