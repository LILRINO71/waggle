"""Real-world smoke run of the tool packs on harmless inputs: python tools/smoke_real_tools.py [--no-office] [--no-github]

Everything happens in a temp folder with a throwaway WAGGLE_HOME (nothing in ~/.waggle is read or written, nothing in
the owner's folders is touched). Prints one line per tool call: PASS/FAIL, the tool, the key facts, the seconds.
Exit code 0 only if every call that could run passed; a tool whose program is not installed is reported SKIP.
"""
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
TMP = Path(tempfile.mkdtemp(prefix="waggle-smoke-"))
os.environ["WAGGLE_HOME"] = str(TMP / "home")
os.environ["TEMP"] = os.environ["TMP"] = str(TMP)  # inside Waggle's write roots, away from the real folders
(TMP / "home").mkdir()
(TMP / "home" / "config.json").write_text(json.dumps({"jobs_dir": str(TMP / "jobs"), "vault": str(TMP / "vault")}),
                                          encoding="utf-8")

from waggle import (tools_cad, tools_common as tc, tools_docs, tools_explain, tools_github, tools_media,  # noqa: E402
                    tools_print)

OFFICE, GITHUB = "--no-office" not in sys.argv, "--no-github" not in sys.argv
failures = []


def show(name, raw, keys, started):
    text = raw
    if raw.startswith("<untrusted"):
        text = raw[raw.index("{"): raw.rindex("}") + 1]
    d = json.loads(text)
    secs = f"{time.time() - started:.1f}s"
    if d.get("ok"):
        facts = {k: d[k] for k in keys if k in d}
        print(f"PASS {name:16} {secs:>6}  {json.dumps(facts, default=str)[:230]}")
    else:
        failures.append(name)
        print(f"FAIL {name:16} {secs:>6}  {d.get('error')} | {d.get('hint', '')}")
    return d


def call(name, fn, args, keys=("output",)):
    started = time.time()
    return show(name, fn(args), keys, started)


def cube_stl(size=20.0):
    s = size
    v = [(0, 0, 0), (s, 0, 0), (s, s, 0), (0, s, 0), (0, 0, s), (s, 0, s), (s, s, s), (0, s, s)]
    body = ""
    for a, b, c, d in [(0, 3, 2, 1), (4, 5, 6, 7), (0, 1, 5, 4), (2, 3, 7, 6), (1, 2, 6, 5), (0, 4, 7, 3)]:
        for tri in ((a, b, c), (a, c, d)):
            body += " facet normal 0 0 0\n  outer loop\n" + "".join("   vertex %g %g %g\n" % v[i] for i in tri) \
                    + "  endloop\n endfacet\n"
    return "solid cube\n" + body + "endsolid cube\n"


def main():
    print("smoke folder:", TMP)
    # --- CAD + print
    step = TMP / "bracket.step"
    try:
        from build123d import Box, Cylinder, Pos, export_step
        export_step(Box(40, 20, 10) - Pos(5, 0, 0) * Cylinder(4, 10), str(step))
    except Exception:
        step = None
    if tc.which("freecadcmd", extra=tools_cad.FREECAD_GLOBS):
        stl = TMP / "cube.stl"
        stl.write_text(cube_stl())
        if step:
            call("cad_measure step", tools_cad.cad_measure, {"path": str(step), "material": "aluminum"},
                 ("bbox_mm", "volume_mm3", "cylinders", "mass_g"))
            call("cad_convert step>stl", tools_cad.cad_convert, {"path": str(step), "to": "stl"})
        call("cad_measure stl", tools_cad.cad_measure, {"path": str(stl), "material": "pla"},
             ("bbox_mm", "closed_solid", "volume_mm3", "mass_g"))
        call("cad_convert stl>3mf", tools_cad.cad_convert, {"path": str(stl), "to": "3mf"})
    else:
        print("SKIP cad_*            FreeCAD not installed")
    if tc.which("prusa-slicer-console", extra=tools_print.SLICER_GLOBS):
        part = TMP / "cube.stl"
        part.write_text(cube_stl(30.0))
        call("print_estimate", tools_print.print_estimate, {"path": str(part)}, ("print_time", "filament_g", "assumptions"))
    else:
        print("SKIP print_estimate   PrusaSlicer not installed")
    # --- documents
    note = TMP / "Strategy.md"
    note.write_text("---\ntags: [x]\n---\n# BIOBUZZ strategy\n\nSee [[Robot V2|the robot]].\n\n- [ ] tune shooter\n\n"
                    "| a | b |\n|---|---|\n| 1 | 2 |\n", encoding="utf-8")
    if tc.which("chrome", "msedge", extra=tools_docs.BROWSER_GLOBS):
        call("doc_to_pdf md", tools_docs.doc_to_pdf, {"path": str(note)}, ("output", "via"))
        call("doc_to_pdf text", tools_docs.doc_to_pdf, {"markdown": "# Hello\n\nFrom the smoke run.", "name": "smoke"},
             ("output", "title"))
    else:
        print("SKIP doc_to_pdf md    no Chrome/Edge")
    if OFFICE and tc.which(extra=[r"C:\Program Files\Microsoft Office\root\Office16\WINWORD.EXE"]):
        (TMP / "note.rtf").write_text(r"{\rtf1\ansi Hello from the smoke run.\par Second line.}")
        (TMP / "scores.csv").write_text("name,score\nalpha,1\nbeta,2\n")
        call("doc_to_pdf word", tools_docs.doc_to_pdf, {"path": str(TMP / "note.rtf")}, ("output", "source"))
        call("doc_to_pdf excel", tools_docs.doc_to_pdf, {"path": str(TMP / "scores.csv")}, ("output", "source"))
    else:
        print("SKIP doc_to_pdf word/excel  (Office not installed or --no-office)")
    # --- media
    try:
        ff = tools_media.ffmpeg_exe()
        clip = TMP / "clip.mp4"
        subprocess.run([ff, "-y", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc=duration=8:size=1280x720:rate=30",
                        "-f", "lavfi", "-i", "sine=frequency=440:duration=8", "-c:v", "libx264", "-pix_fmt", "yuv420p",
                        "-b:v", "3M", "-c:a", "aac", str(clip)], check=True, creationflags=tc.NO_WINDOW)
        call("media shrink", tools_media.media_convert, {"path": str(clip), "to": "mp4", "max_mb": 0.8},
             ("output", "source_size", "met_target"))
        call("media gif", tools_media.media_convert, {"path": str(clip), "to": "gif", "start": "1", "end": "3", "width": 320})
        call("media mp3", tools_media.media_convert, {"path": str(clip), "to": "mp3"})
    except tc.ToolError as e:
        print("SKIP media_convert    ", e)
    try:
        from PIL import Image
        photo = TMP / "photo.png"
        Image.new("RGBA", (2000, 1500), (200, 30, 30, 200)).save(photo)
        call("image_convert", tools_media.image_convert, {"path": str(photo), "to": "jpg", "max_side": 640},
             ("output", "size_px", "metadata"))
    except ImportError:
        print("SKIP image_convert    no Pillow")
    # --- GitHub (read-only)
    if GITHUB and tc.which("gh", extra=tools_github.GH_EXTRA):
        d = call("portfolio_audit", tools_github.portfolio_audit, {"limit": 5},
                 ("owner", "repos_checked", "average_score", "suggested_pins", "biggest_fixes"))
    else:
        print("SKIP portfolio_audit  (no gh or --no-github)")
    # --- REA
    if tc.which("rea.cmd", "rea", extra=tools_explain.REA_EXTRA):
        import sqlite3
        db = TMP / "t.db"
        con = sqlite3.connect(db)
        con.execute("create table scores(team text, pts int)")
        con.commit()
        con.close()
        call("explain exe", tools_explain.explain_file, {"path": sys.executable},
             ("kind", "identity", "verdict_hints", "imports"))
        call("explain database", tools_explain.explain_file, {"path": str(db)}, ("kind", "tables", "rows_read"))
        call("explain folder", tools_explain.explain_file, {"path": str(Path(sys.executable).parent / "Lib" / "json")},
             ("kind", "runtime_guess", "built_with"))
    else:
        print("SKIP explain_file     REA not installed")
    print("FAILURES:", failures or "none")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
