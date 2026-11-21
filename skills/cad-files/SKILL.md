---
name: cad-files
description: "Convert and measure CAD files, and estimate 3D prints."
version: 1.0.0
author: Waggle
platforms: [windows]
metadata:
  hermes:
    category: waggle
    tags: [cad, step, stl, 3mf, obj, iges, freecad, convert, measure, volume, weight, hole, print, 3d print, filament, slicer, robot part]
    related_skills: [documents-to-pdf, explain-this-file]
---

# CAD files

Use for "turn this STEP into an STL", "how big / how heavy is this part", "what are the hole sizes", "will this fit
my printer", "how long to print this". The tools run the real FreeCAD engine and PrusaSlicer on this PC (headless,
no window). They never overwrite: a taken name becomes "name (2).stl".

## Tools

- `cad_convert` (path, to): STEP/IGES/BREP/STL/OBJ/3MF to STEP/IGES/BREP/STL/OBJ/3MF. `quality` draft/normal/fine
  sets mesh detail for STL/OBJ/3MF; `scale` 25.4 turns an inch model into mm. A mesh turned into STEP is only a
  skin of flat triangles: say so, it is for viewing, not for editing.
- `cad_measure` (path, material): bounding box, volume, area, centre of mass, solid and face counts, `valid`, and the
  hole and boss diameters of a STEP. For an STL it says whether the mesh is closed (printable) and whether normals are
  flipped. `material` (pla, petg, abs, aluminum, steel, plywood...) adds the solid weight.
- `print_estimate` (path): print time and filament on the owner's Creality Ender-3 Max (bed 300 x 300 x 340 mm) from a
  headless slice. Rough, nothing is kept, nothing is printed. Options: material, layer_height, infill, supports.

## Steps

1. Get the full path (ask the owner if only a name was given; do not search the whole disk).
2. Measure first when the question is about size, weight, holes or printability; convert when asked for a file.
3. Report the numbers in plain units with what they mean ("40 x 20 x 10 mm, 20 g in aluminum, one 8 mm hole").
4. If the owner wants the file, give the returned path, or `send_to_me` with `file` when they asked for it on the
   phone. For printing, tell them to open the STL in Creality Print: Waggle never starts a print.

## Rules

- STEP/IGES carry their unit; STL/OBJ have none and are read as millimetres. If a part looks 25.4 times too big or
  small, suggest `scale`.
- A failed tool answers `{"ok": false, "error", "hint"}`: relay the hint, do not retry blindly.
- To remove these tools: see skills/README.md.
