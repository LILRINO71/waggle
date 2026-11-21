"""How long will this STL take to print and how much filament does it use? PrusaSlicer's console slicer (installed on
this PC) run headless with a profile for the owner's printer (Creality Ender-3 Max, 300 x 300 x 340 mm bed, 0.4 nozzle,
from Creality Print's settings). The G-code is thrown away: this is an estimate to decide "is it worth printing", not
the file to print (the owner slices in Creality Print). Never starts a print.

Removal: delete this file, tests/test_tools_print.py; the cad-files skill mentions it in one line.
"""
import re
import tempfile
from pathlib import Path

from . import tools_common as tc
from .tools_common import ToolError, guarded

SLICER_GLOBS = (r"C:\Program Files\Prusa3D\PrusaSlicer*\prusa-slicer-console.exe",
                r"%LOCALAPPDATA%\Programs\PrusaSlicer*\prusa-slicer-console.exe")
MESH_EXTS = {".stl", ".obj", ".3mf", ".step", ".stp"}
PRINTERS = {"ender3max": {"name": "Creality Ender-3 Max", "bed": (300, 300), "height": 340, "accel": 500}}
FILAMENT = {"pla": 1.24, "petg": 1.27, "abs": 1.04, "asa": 1.07, "tpu": 1.21}


def config_text(printer, material, layer, infill, supports):
    p = PRINTERS[printer]
    w, d = p["bed"]
    return "\n".join([
        f"bed_shape = 0x0,{w}x0,{w}x{d},0x{d}", f"max_print_height = {p['height']}", "nozzle_diameter = 0.4",
        "filament_diameter = 1.75", f"filament_density = {FILAMENT[material]}", "gcode_flavor = marlin2",
        f"layer_height = {layer}", f"first_layer_height = {layer}", "perimeters = 3", "top_solid_layers = 5",
        "bottom_solid_layers = 4", f"fill_density = {infill}%", "fill_pattern = grid",
        f"support_material = {1 if supports else 0}", "support_material_auto = 1",
        "perimeter_speed = 50", "external_perimeter_speed = 40", "infill_speed = 60", "solid_infill_speed = 50",
        "top_solid_infill_speed = 40", "small_perimeter_speed = 30", "travel_speed = 120", "first_layer_speed = 25",
        "max_print_speed = 100", f"default_acceleration = {p['accel']}", f"perimeter_acceleration = {p['accel']}",
        f"infill_acceleration = {p['accel']}", "machine_limits_usage = time_estimate_only",
        f"machine_max_acceleration_x = {p['accel']}", f"machine_max_acceleration_y = {p['accel']}",
        f"machine_max_acceleration_extruding = {p['accel']}", f"machine_max_acceleration_travel = {p['accel']}",
        "machine_max_feedrate_x = 300", "machine_max_feedrate_y = 300", ""])


def parse_gcode_tail(text):
    """{seconds, time, filament_g, filament_mm} from the comment block PrusaSlicer writes at the end of the G-code."""
    out = {}
    m = re.search(r"estimated printing time \(normal mode\) = ([^\r\n]+)", text)
    if m:
        out["time"] = m.group(1).strip()
        secs = 0
        for n, unit in re.findall(r"(\d+)\s*([dhms])", out["time"]):
            secs += int(n) * {"d": 86400, "h": 3600, "m": 60, "s": 1}[unit]
        out["seconds"] = secs
    m = re.search(r"total filament used \[g\] = ([\d.]+)", text) or re.search(r"filament used \[g\] = ([\d.]+)", text)
    if m:
        out["filament_g"] = float(m.group(1))
    m = re.search(r"filament used \[mm\] = ([\d.]+)", text)
    if m:
        out["filament_m"] = round(float(m.group(1)) / 1000, 2)
    return out


def slicer_exe():
    exe = tc.which("prusa-slicer-console", extra=SLICER_GLOBS)
    if not exe:
        raise ToolError("PrusaSlicer is not installed (prusa-slicer-console.exe not found)",
                        "install PrusaSlicer from prusa3d.com, then retry")
    return exe


def _num(args, key, default, lo, hi):
    try:
        v = float(args.get(key, default) if args.get(key) not in (None, "") else default)
    except (TypeError, ValueError):
        raise ToolError(f"{key} must be a number")
    if not lo <= v <= hi:
        raise ToolError(f"{key} must be between {lo} and {hi}")
    return v


@guarded
def print_estimate(args):
    src = tc.read_path(args.get("path"), MESH_EXTS)
    printer = str(args.get("printer") or "ender3max").lower()
    if printer not in PRINTERS:
        raise ToolError(f"unknown printer '{printer}'", "known: " + ", ".join(PRINTERS))
    material = str(args.get("material") or "pla").lower()
    if material not in FILAMENT:
        raise ToolError(f"unknown material '{material}'", "known: " + ", ".join(FILAMENT))
    layer = _num(args, "layer_height", 0.2, 0.08, 0.32)
    infill = int(_num(args, "infill", 20, 0, 100))
    supports = bool(args.get("supports"))
    path = src
    with tempfile.TemporaryDirectory(prefix="waggle-print-") as tmp:
        tmp = Path(tmp)
        if src.suffix.lower() in (".step", ".stp"):  # the slicer reads meshes; STEP goes through FreeCAD first
            from . import tools_cad
            mesh = tmp / (src.stem + ".stl")
            res = tools_cad.run_job({"mode": "convert", "src": str(src), "dst": str(mesh), "lin": 0.05, "ang": 0.35,
                                     "scale": 1, "max_facets": 0})
            if not res.get("ok"):
                raise ToolError(res.get("error") or "could not turn the STEP into a mesh")
            path = mesh
        cfg, gcode = tmp / "printer.ini", tmp / "out.gcode"
        cfg.write_text(config_text(printer, material, layer, infill, supports), encoding="utf-8")
        code, out, err = tc.run([slicer_exe(), "--load", str(cfg), "--export-gcode", "--output", str(gcode), str(path)],
                                timeout=240, cwd=str(tmp))
        text = (out + "\n" + err)
        if not gcode.exists():
            low = text.lower()
            if "fit" in low and "bed" in low:
                w, d = PRINTERS[printer]["bed"]
                raise ToolError(f"the part does not fit the {PRINTERS[printer]['name']} bed ({w} x {d} x "
                                f"{PRINTERS[printer]['height']} mm)", "scale it down or split it (cad_measure shows its size)")
            raise ToolError("the slicer produced no G-code", tc.tail(text, 300))
        facts = parse_gcode_tail(gcode.read_text(encoding="utf-8", errors="replace")[-20000:])
    if "seconds" not in facts:
        raise ToolError("could not read the estimate from the slicer's output", tc.tail(text, 300))
    return tc.ok(file=src.name, printer=PRINTERS[printer]["name"], print_time=facts["time"],
                 print_seconds=facts["seconds"], filament_g=facts.get("filament_g"), filament_m=facts.get("filament_m"),
                 assumptions=f"{material.upper()} 1.75 mm, {layer} mm layers, {infill}% infill, 3 walls, "
                             f"{'with' if supports else 'no'} supports, stock-speed Ender-3 Max profile",
                 note="a rough estimate (usually within ~15% of Creality Print); no file was kept")


MCP_TOOLS = [
    ({"name": "print_estimate", "description": "Estimate print time and filament for a 3D model (STL/3MF/OBJ/STEP) on "
      "the user's Creality Ender-3 Max by slicing it headlessly with PrusaSlicer. Rough numbers only: nothing is "
      "kept and nothing is printed. Also says if the part does not fit the bed.",
      "parameters": {"type": "object", "properties": {
          "path": {"type": "string"}, "material": {"type": "string", "enum": sorted(FILAMENT)},
          "layer_height": {"type": "number"}, "infill": {"type": "integer", "description": "percent"},
          "supports": {"type": "boolean"}}, "required": ["path"]}}, print_estimate),
]
