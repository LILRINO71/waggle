"""CAD files for the chat brain: convert STEP/IGES/BREP/STL/OBJ/3MF and measure a part, through the real FreeCAD
engine (freecadcmd, headless, no window).

Why not the CLI-Anything FreeCAD harness (cli-anything-freecad 1.0.0, installed 2026-11-17)? Tried 2026-11-20: it builds
models from primitives, but `import step` only records a path in its project JSON, `measure volume|bounding-box|area`
answer `deferred: null` for an imported file, and `export render ... stl|obj` reports success text without writing a
file (the macro template prints an un-substituted `{safe_path}`). So these tools drive the same backend the harness
wraps (FreeCADCmd) with a small script of their own; the harness stays installed for building primitives.

Tools: cad_convert, cad_measure (print_estimate, which slices with PrusaSlicer, is in tools_print.py). Removal: delete
this file, its test and skills/cad-files; the tools disappear from the MCP list on the next start (tools_ext skips
missing packs).
"""
import json
import tempfile
from pathlib import Path

from . import tools_common as tc
from .tools_common import ToolError, guarded

FREECAD_GLOBS = (r"%LOCALAPPDATA%\Programs\FreeCAD*\bin\freecadcmd.exe", r"C:\Program Files\FreeCAD*\bin\freecadcmd.exe",
                 r"C:\Program Files (x86)\FreeCAD*\bin\freecadcmd.exe")
SHAPE_IN = {".step", ".stp", ".iges", ".igs", ".brep", ".brp"}
MESH_IN = {".stl", ".obj", ".3mf", ".ply", ".off"}
OUT_FORMATS = {"step": ".step", "stp": ".step", "iges": ".iges", "igs": ".iges", "brep": ".brep", "stl": ".stl",
               "obj": ".obj", "3mf": ".3mf"}
MAX_MESH_TO_SOLID = 150_000  # facets; a mesh bigger than this becomes an unusable STEP anyway
DENSITY = {"pla": 1.24, "petg": 1.27, "abs": 1.04, "asa": 1.07, "tpu": 1.21, "nylon": 1.14, "pc": 1.20,
           "acrylic": 1.18, "pom": 1.41, "delrin": 1.41, "plywood": 0.68, "aluminum": 2.70, "aluminium": 2.70,
           "steel": 7.85, "stainless": 8.00, "brass": 8.50, "copper": 8.96, "titanium": 4.51}

# Runs inside freecadcmd (FreeCAD's own Python). Reads the job JSON named by WAGGLE_CAD_JOB, writes the result JSON.
SCRIPT = r'''
import json, os, sys, traceback
job = json.load(open(os.environ["WAGGLE_CAD_JOB"], encoding="utf-8"))
res = {"ok": False}
try:
    import FreeCAD, Part, Mesh, MeshPart
    src = job["src"]
    ext = os.path.splitext(src)[1].lower()
    shape = mesh = None
    if ext in (".step", ".stp", ".iges", ".igs", ".brep", ".brp"):
        shape = Part.read(src)
    else:
        mesh = Mesh.Mesh(src)
    if job.get("scale") and job["scale"] != 1:
        if shape is not None:
            shape = shape.scaled(job["scale"])
        else:
            m = FreeCAD.Matrix()
            m.scale(job["scale"], job["scale"], job["scale"])
            mesh.transform(m)

    def r(x, n=3):
        return round(float(x), n)

    def bbox(b):
        return {"min": [r(b.XMin), r(b.YMin), r(b.ZMin)], "max": [r(b.XMax), r(b.YMax), r(b.ZMax)],
                "size": [r(b.XLength), r(b.YLength), r(b.ZLength)]}

    if job["mode"] == "measure":
        if shape is not None:
            solids = shape.Solids
            res["kind"] = "solid model"
            res["bbox_mm"] = bbox(shape.BoundBox)
            res["solids"] = len(solids)
            res["faces"] = len(shape.Faces)
            res["edges"] = len(shape.Edges)
            res["valid"] = bool(shape.isValid())
            res["area_mm2"] = r(shape.Area, 2)
            vol = sum(s.Volume for s in solids) if solids else 0.0
            res["volume_mm3"] = r(vol, 2)
            if solids and vol > 0:
                c = sum((s.CenterOfMass * s.Volume for s in solids), FreeCAD.Vector()) * (1.0 / vol)
                res["center_of_mass_mm"] = [r(c.x), r(c.y), r(c.z)]
            groups = {}
            for f in shape.Faces:
                try:
                    surf = f.Surface
                    if surf.__class__.__name__ != "Cylinder":
                        continue
                    u0, u1, v0, v1 = f.ParameterRange
                    pt = f.valueAt((u0 + u1) / 2, (v0 + v1) / 2)
                    nrm = f.normalAt((u0 + u1) / 2, (v0 + v1) / 2)
                    ax = surf.Axis
                    rad = pt - surf.Center
                    rad = rad - ax * rad.dot(ax)
                    kind = "hole" if nrm.dot(rad) < 0 else "boss"
                    key = (kind, r(surf.Radius * 2, 3), r(surf.Center.x, 1), r(surf.Center.y, 1), r(surf.Center.z, 1),
                           r(abs(ax.x), 2), r(abs(ax.y), 2), r(abs(ax.z), 2))
                    groups[key] = 1
                except Exception:
                    continue
            summary = {}
            for key in groups:
                summary[(key[0], key[1])] = summary.get((key[0], key[1]), 0) + 1
            res["cylinders"] = [{"kind": k[0], "diameter_mm": k[1], "count": n}
                                for k, n in sorted(summary.items(), key=lambda kv: (kv[0][0], kv[0][1]))][:30]
        else:
            res["kind"] = "mesh"
            res["bbox_mm"] = bbox(mesh.BoundBox)
            res["facets"] = mesh.CountFacets
            res["points"] = mesh.CountPoints
            res["area_mm2"] = r(mesh.Area, 2)
            res["volume_mm3"] = r(mesh.Volume, 2)
            res["closed_solid"] = bool(mesh.isSolid())
            res["components"] = int(mesh.countComponents())
            res["non_manifold_edges"] = bool(mesh.hasNonManifolds())
            res["flipped_normals"] = int(mesh.countNonUniformOrientedFacets())
            res["self_intersections"] = bool(mesh.hasSelfIntersections()) if mesh.CountFacets < 60000 else None
        res["ok"] = True
    else:
        dst = job["dst"]
        dext = os.path.splitext(dst)[1].lower()
        if dext in (".stl", ".obj", ".3mf"):
            if shape is not None:
                mesh = MeshPart.meshFromShape(Shape=shape, LinearDeflection=job["lin"], AngularDeflection=job["ang"],
                                              Relative=False)
            mesh.write(dst)
            res["facets"] = mesh.CountFacets
            res["closed_solid"] = bool(mesh.isSolid())
        else:
            if shape is None:
                if mesh.CountFacets > job["max_facets"]:
                    raise RuntimeError("mesh too large (%d facets) to turn into a STEP solid" % mesh.CountFacets)
                s = Part.Shape()
                s.makeShapeFromMesh(mesh.Topology, 0.05)
                shape = Part.Solid(Part.Shell(s.Faces)) if s.Faces else s
                res["note"] = "built from a mesh: every triangle became a flat face; fine for viewing, poor for editing"
            shape.exportStep(dst) if dext == ".step" else (shape.exportIges(dst) if dext == ".iges" else shape.exportBrep(dst))
        res["ok"] = os.path.exists(dst) and os.path.getsize(dst) > 0
        if not res["ok"]:
            res["error"] = "FreeCAD wrote no file (the format may not be available in this FreeCAD build)"
except Exception as e:
    res["error"] = "%s: %s" % (type(e).__name__, e)
    res["trace"] = traceback.format_exc()[-600:]
json.dump(res, open(job["result"], "w", encoding="utf-8"))
sys.stdout.flush()
'''


def freecad_exe():
    exe = tc.which("freecadcmd", extra=FREECAD_GLOBS)
    if not exe:
        raise ToolError("FreeCAD is not installed (freecadcmd.exe not found)",
                        "install FreeCAD 1.x from freecad.org, then retry")
    return exe


def run_job(job, timeout=180):
    """Run the embedded script in freecadcmd; returns the result dict the script wrote."""
    exe = freecad_exe()
    with tempfile.TemporaryDirectory(prefix="waggle-cad-") as tmp:
        tmp = Path(tmp)
        (tmp / "job.py").write_text(SCRIPT, encoding="utf-8")
        result = tmp / "result.json"
        (tmp / "job.json").write_text(json.dumps({**job, "result": str(result)}), encoding="utf-8")
        import os
        env = {**os.environ, "WAGGLE_CAD_JOB": str(tmp / "job.json")}
        code, out, err = tc.run([exe, str(tmp / "job.py")], timeout=timeout, env=env, cwd=str(tmp))
        if not result.exists():
            raise ToolError("FreeCAD did not finish the job", tc.tail(err or out))
        return json.loads(result.read_text(encoding="utf-8"))


def _scale(args):
    try:
        f = float(args.get("scale") or 1)
    except (TypeError, ValueError):
        raise ToolError("scale must be a number (25.4 turns inches into mm)")
    if not 0.0001 <= f <= 10000:
        raise ToolError("scale is out of range")
    return f


@guarded
def cad_convert(args):
    src = tc.read_path(args.get("path"), SHAPE_IN | MESH_IN)
    to = str(args.get("to") or "").lower().lstrip(".")
    if to not in OUT_FORMATS:
        raise ToolError(f"cannot write '{to}'", "to is one of: " + ", ".join(sorted(set(OUT_FORMATS))))
    ext = OUT_FORMATS[to]
    if src.suffix.lower() == ext or (src.suffix.lower() == ".stp" and ext == ".step"):
        raise ToolError("the file is already in that format")
    dst = tc.out_path(src, ext, args.get("out"))
    quality = str(args.get("quality") or "normal").lower()
    lin, ang = {"draft": (0.2, 0.6), "normal": (0.05, 0.35), "fine": (0.01, 0.15)}.get(quality, (0.05, 0.35))
    res = run_job({"mode": "convert", "src": str(src), "dst": str(dst), "lin": lin, "ang": ang,
                   "scale": _scale(args), "max_facets": MAX_MESH_TO_SOLID})
    if not res.get("ok"):
        dst.unlink(missing_ok=True)
        raise ToolError(res.get("error") or "conversion failed", tc.tail(res.get("trace", ""), 300))
    out = {"output": tc.file_info(dst), "from": src.suffix.lower().lstrip("."), "to": to}
    for k in ("facets", "closed_solid", "note"):
        if k in res:
            out[k] = res[k]
    if to in ("stl", "obj", "3mf"):
        out["quality"] = quality
    return tc.ok(**out)


def mass_g(volume_mm3, material):
    key = str(material or "").lower().strip()
    for name, dens in DENSITY.items():
        if name == key or (key and name in key):
            return round(volume_mm3 / 1000.0 * dens, 1), name, dens
    return None


@guarded
def cad_measure(args):
    src = tc.read_path(args.get("path"), SHAPE_IN | MESH_IN)
    res = run_job({"mode": "measure", "src": str(src), "scale": _scale(args)})
    if not res.get("ok"):
        raise ToolError(res.get("error") or "measuring failed", tc.tail(res.get("trace", ""), 300))
    res.pop("ok")
    res["file"] = src.name
    res["units"] = "mm (STEP and IGES files carry their own unit; STL/OBJ have none and are taken as mm)"
    mat = args.get("material")
    if mat and res.get("volume_mm3"):
        m = mass_g(res["volume_mm3"], mat)
        if m:
            res["mass_g"] = {"material": m[1], "density_g_cm3": m[2], "mass": m[0],
                             "note": "solid fill; a 3D print at 20% infill weighs far less"}
        else:
            res["mass_g"] = {"error": f"unknown material '{mat}'", "known": sorted(DENSITY)}
    return tc.ok(**res)


MCP_TOOLS = [
    ({"name": "cad_convert", "description": "Convert a CAD file with FreeCAD: STEP/IGES/BREP/STL/OBJ/3MF to "
      "STEP/IGES/BREP/STL/OBJ/3MF (e.g. a robot part .step to .stl for printing). Writes a NEW file next to the "
      "source (never overwrites) and returns its path. Optional scale (25.4 = inches to mm) and mesh quality.",
      "parameters": {"type": "object", "properties": {
          "path": {"type": "string", "description": "full path of the input CAD file"},
          "to": {"type": "string", "enum": sorted(set(OUT_FORMATS))},
          "out": {"type": "string", "description": "optional output path"},
          "quality": {"type": "string", "enum": ["draft", "normal", "fine"], "description": "mesh detail for STL/OBJ/3MF"},
          "scale": {"type": "number"}}, "required": ["path", "to"]}}, cad_convert),
    ({"name": "cad_measure", "description": "Measure a CAD file with FreeCAD: bounding box, volume, surface area, "
      "center of mass, solid/face counts, validity, and the hole and boss diameters of a STEP; mesh checks (closed, "
      "flipped normals) for an STL. Pass material (pla, petg, aluminum, steel, plywood...) to get the weight.",
      "parameters": {"type": "object", "properties": {
          "path": {"type": "string"}, "material": {"type": "string"}, "scale": {"type": "number"}},
          "required": ["path"]}}, cad_measure),
]
