# Waggle's shipped skills

`python install.py` copies each `skills/<name>/SKILL.md` into Hermes (`~/.hermes/skills/waggle/`) and re-indexes. A
skill carries no scripts: it tells the chat brain when and how to use Waggle's MCP tools.

## Planning and verification skills

| skill | use it when |
|---|---|
| `objective-planning` | a long multi-step goal that should run in the background |
| `research-brief` | "look into / compare / find out", answered with sources and saved to the vault |
| `verified-browser-task` | multi-step website jobs where each step is checked |
| `verified-computer-use` | clicking through a desktop app with proof after each step |

## Real-world tool skills (branch rebuild/tools, 2026-11-20)

Each skill sits on one tool pack in `waggle/tools_*.py` (registered through `waggle/tools_ext.py`, policy rows in
`waggle/policy.py`). Results are always NEW files: nothing is overwritten.

| skill | tools | use it when | needs on this PC |
|---|---|---|---|
| `cad-files` | `cad_convert`, `cad_measure`, `print_estimate` | "STEP to STL", "how big / heavy / what holes", "how long to print" | FreeCAD 1.x, PrusaSlicer |
| `documents-to-pdf` | `doc_to_pdf` | "make this note / Word file / sheet / deck a PDF" | Chrome or Edge; Microsoft Office for Office files |
| `media-convert` | `media_convert`, `image_convert` | "shrink this video under 25 MB", "GIF of this", "resize these photos" | ffmpeg (imageio-ffmpeg), Pillow |
| `github-portfolio-audit` | `portfolio_audit` | "is my GitHub ready for college?", "what should I pin?" | `gh`, signed in |
| `explain-this-file` | `explain_file` | "what is this exe / app / model / zip / database?" | REA (`rea`), Windows PowerShell |

### Removing a tool pack

Delete its module, test and skill; `tools_ext.py` skips the missing module on the next start, and the policy rows are
harmless. From the repo root, one line per pack:

    git rm -r waggle/tools_cad.py tests/test_tools_cad.py skills/cad-files               # cad_convert, cad_measure
    git rm -r waggle/tools_print.py tests/test_tools_print.py                             # print_estimate
    git rm -r waggle/tools_docs.py tests/test_tools_docs.py skills/documents-to-pdf       # doc_to_pdf
    git rm -r waggle/tools_media.py tests/test_tools_media.py skills/media-convert        # media_convert, image_convert
    git rm -r waggle/tools_github.py tests/test_tools_github.py skills/github-portfolio-audit   # portfolio_audit
    git rm -r waggle/tools_explain.py tests/test_tools_explain.py skills/explain-this-file      # explain_file

Then delete the installed copy (`~/.hermes/skills/waggle/<skill>`) and restart Waggle. To drop all packs and the
shared plumbing: also `git rm waggle/tools_common.py waggle/tools_ext.py tests/test_tools_common.py
tools/smoke_real_tools.py`, remove `"waggle.tools_ext"` from `_load_providers` in `waggle/waggle_mcp.py`, and drop the
8 tool rows from `WAGGLE_TOOLS` in `waggle/policy.py`. Nothing was installed outside the repo (no venv, no packages).
