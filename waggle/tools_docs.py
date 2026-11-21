"""Documents to PDF for the chat brain: "turn my notes into a PDF", "make this Word file / spreadsheet / deck a PDF".

- Markdown / text / HTML (Obsidian notes included): rendered to HTML here and printed to PDF by Chrome (or Edge)
  headless, no window; the pip `markdown` package is used when present, a small built-in renderer otherwise.
- Word / Excel / PowerPoint files (docx doc rtf odt xlsx xls csv pptx ppt): exported by the Microsoft Office that is
  installed on this PC (COM, no window). The script starts its own Office process and quits only a process it
  started (a new pid appeared when it created the COM object); if it ever attached to one the owner had open, only the
  document it opened is closed and the owner's Office is never quit. LibreOffice (the CLI-Anything `libreoffice`
  harness) is not installed here and is a ~350 MB download, so it was not added.

Tool: doc_to_pdf. Removal: delete this file, tests/test_tools_docs.py, skills/documents-to-pdf.
"""
import html
import re
import subprocess
import tempfile
from pathlib import Path

from . import tools_common as tc
from .tools_common import ToolError, guarded

WEB_EXTS = {".md", ".markdown", ".txt", ".html", ".htm"}
OFFICE_EXTS = {".docx": "word", ".doc": "word", ".rtf": "word", ".odt": "word", ".xlsx": "excel", ".xls": "excel",
               ".csv": "excel", ".pptx": "powerpoint", ".ppt": "powerpoint"}
BROWSER_GLOBS = (r"C:\Program Files\Google\Chrome\Application\chrome.exe",
                 r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
                 r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe",
                 r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
                 r"C:\Program Files\Microsoft\Edge\Application\msedge.exe")
OFFICE_EXE = {"word": "WINWORD.EXE", "excel": "EXCEL.EXE", "powerpoint": "POWERPNT.EXE"}

CSS = """
@page { size: A4; margin: 18mm 16mm; }
body { font-family: "Segoe UI", Calibri, Arial, sans-serif; font-size: 11pt; line-height: 1.5; color: #1a1a1a; }
h1 { font-size: 22pt; border-bottom: 2px solid #ddd; padding-bottom: 4pt; margin-top: 0; }
h2 { font-size: 16pt; margin-top: 18pt; } h3 { font-size: 13pt; }
code { font-family: Consolas, monospace; background: #f3f3f3; padding: 1px 4px; border-radius: 3px; font-size: 10pt; }
pre { background: #f3f3f3; padding: 8pt; border-radius: 4px; overflow-x: auto; white-space: pre-wrap; }
pre code { background: none; padding: 0; }
blockquote { border-left: 3px solid #bbb; margin: 8pt 0; padding: 2pt 10pt; color: #444; }
table { border-collapse: collapse; margin: 8pt 0; } th, td { border: 1px solid #bbb; padding: 3pt 8pt; }
th { background: #f0f0f0; } img { max-width: 100%; } a { color: #1a5fb4; }
"""


# ---- markdown -> html ------------------------------------------------------------------------------

def clean_obsidian(text):
    """Strip what only makes sense inside Obsidian: frontmatter, [[wikilinks]], ![[embeds]], %%comments%%.
    Fenced code blocks are left exactly as written."""
    text = text.replace("\r\n", "\n")
    text = re.sub(r"\A---\n.*?\n---\n", "", text, count=1, flags=re.S)
    parts = re.split(r"(```.*?```)", text, flags=re.S)  # odd entries are code fences
    return "".join(p if i % 2 else _clean_prose(p) for i, p in enumerate(parts))


def _clean_prose(text):
    text = re.sub(r"%%.*?%%", "", text, flags=re.S)
    text = re.sub(r"!\[\[([^\]|#]+)(?:#[^\]|]*)?(?:\|[^\]]*)?\]\]", lambda m: f"*(embedded: {m.group(1)})*", text)
    text = re.sub(r"\[\[([^\]|]+)\|([^\]]+)\]\]", r"\2", text)
    text = re.sub(r"\[\[([^\]]+)\]\]", lambda m: m.group(1).split("#")[0] or m.group(1), text)
    text = re.sub(r"==([^=\n]+)==", r"<mark>\1</mark>", text)
    text = re.sub(r"^(\s*)- \[ \] ", lambda m: m.group(1) + "- \u2610 ", text, flags=re.M)
    text = re.sub(r"^(\s*)- \[[xX]\] ", lambda m: m.group(1) + "- \u2611 ", text, flags=re.M)
    text = re.sub(r"^> \[!(\w+)\][+-]?\s*(.*)$", lambda m: f"> **{m.group(2) or m.group(1).title()}**", text, flags=re.M)
    return text


def _inline(s):
    s = html.escape(s, quote=False)
    s = re.sub(r"`([^`]+)`", r"<code>\1</code>", s)
    s = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", s)
    s = re.sub(r"(?<![*\w])\*([^*\n]+)\*(?!\w)", r"<em>\1</em>", s)
    s = re.sub(r"!\[([^\]]*)\]\(([^)]+)\)", r'<img alt="\1" src="\2">', s)
    s = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r'<a href="\2">\1</a>', s)
    return s


def mini_markdown(text):
    """Fallback renderer (headings, lists, quotes, fenced code, paragraphs, bold/italic/code/links) for when the
    `markdown` package is not installed."""
    out, para, lst, code = [], [], None, None

    def flush():
        nonlocal para, lst
        if para:
            out.append("<p>" + _inline(" ".join(para)) + "</p>")
            para = []
        if lst:
            out.append(f"</{lst}>")
            lst = None

    for line in text.split("\n"):
        if code is not None:
            if line.strip().startswith("```"):
                out.append("<pre><code>" + html.escape("\n".join(code)) + "</code></pre>")
                code = None
            else:
                code.append(line)
            continue
        if line.strip().startswith("```"):
            flush()
            code = []
            continue
        m = re.match(r"^(#{1,6})\s+(.*)$", line)
        li = re.match(r"^\s*([-*+]|\d+[.)])\s+(.*)$", line)
        if m:
            flush()
            out.append(f"<h{len(m.group(1))}>{_inline(m.group(2))}</h{len(m.group(1))}>")
        elif li:
            kind = "ol" if li.group(1)[0].isdigit() else "ul"
            if lst != kind:
                flush()
                out.append(f"<{kind}>")
                lst = kind
            out.append(f"<li>{_inline(li.group(2))}</li>")
        elif line.startswith(">"):
            flush()
            out.append(f"<blockquote>{_inline(line.lstrip('> '))}</blockquote>")
        elif not line.strip():
            flush()
        else:
            if lst:
                flush()
            para.append(line.strip())
    if code is not None:
        out.append("<pre><code>" + html.escape("\n".join(code)) + "</code></pre>")
    flush()
    return "\n".join(out)


def render_markdown(text):
    text = clean_obsidian(text)
    try:
        import markdown
        return markdown.markdown(text, extensions=["tables", "fenced_code", "sane_lists"])
    except ImportError:
        return mini_markdown(text)


def build_html(body, title, base=None):
    base_tag = f'<base href="{Path(base).as_uri()}/">' if base else ""
    return (f'<!doctype html><html><head><meta charset="utf-8">{base_tag}<title>{html.escape(title)}</title>'
            f"<style>{CSS}</style></head><body>{body}</body></html>")


def first_heading(text, default):
    m = re.search(r"^#\s+(.+)$", text, flags=re.M)
    return m.group(1).strip() if m else default


# ---- html -> pdf (Chrome / Edge headless) -----------------------------------------------------------

def browser_exe():
    exe = tc.which("chrome", "msedge", extra=BROWSER_GLOBS)
    if not exe:
        raise ToolError("no Chrome or Edge found to print the page", "install Chrome, or give a Word/Office file")
    return exe


def html_to_pdf(html_file, pdf, timeout=90):
    exe = browser_exe()
    with tempfile.TemporaryDirectory(prefix="waggle-chrome-") as prof:
        code, out, err = tc.run([exe, "--headless=new", "--disable-gpu", "--no-pdf-header-footer", "--no-first-run",
                                 "--disable-extensions", f"--user-data-dir={prof}", f"--print-to-pdf={pdf}",
                                 Path(html_file).as_uri()], timeout=timeout)
    if not Path(pdf).exists() or Path(pdf).stat().st_size < 200:
        raise ToolError("the browser did not produce a PDF", tc.tail(err or out, 300))


# ---- Office (COM through PowerShell) ----------------------------------------------------------------

PS_SCRIPT = r'''
param([string]$Kind, [string]$Src, [string]$Out)
$ErrorActionPreference = 'Stop'
$exe = @{word='WINWORD'; excel='EXCEL'; powerpoint='POWERPNT'}[$Kind]
$progid = @{word='Word.Application'; excel='Excel.Application'; powerpoint='PowerPoint.Application'}[$Kind]
$before = @(Get-Process $exe -ErrorAction SilentlyContinue | ForEach-Object { $_.Id })
$app = New-Object -ComObject $progid
$ours = @(Get-Process $exe -ErrorAction SilentlyContinue | Where-Object { $before -notcontains $_.Id }).Count -gt 0
try {
  if ($ours -and $Kind -ne 'powerpoint') { $app.Visible = $false; $app.DisplayAlerts = 0 }
  if ($Kind -eq 'word') {
    $m = [Type]::Missing
    $doc = $app.Documents.Open($Src, $false, $true, $false, $m, $m, $m, $m, $m, $m, $m, $false)
    try { $doc.ExportAsFixedFormat($Out, 17) } finally { $doc.Close($false) }
  } elseif ($Kind -eq 'excel') {
    $wb = $app.Workbooks.Open($Src, 0, $true)
    try { $wb.ExportAsFixedFormat(0, $Out) } finally { $wb.Close($false) }
  } else {
    $pres = $app.Presentations.Open($Src, -1, 0, 0)
    try { $pres.SaveAs($Out, 32) } finally { $pres.Close() }
  }
} finally {
  if ($ours) { try { $app.Quit() } catch {} }
}
'''


def _office_pids(kind):
    try:
        code, out, _ = tc.run(["tasklist", "/FI", f"IMAGENAME eq {OFFICE_EXE[kind]}", "/FO", "CSV", "/NH"], timeout=15)
    except Exception:
        return set()
    return {int(m) for m in re.findall(r'^"[^"]+","(\d+)"', out, flags=re.M)}


def office_to_pdf(kind, src, pdf, timeout=120):
    before = _office_pids(kind)
    with tempfile.TemporaryDirectory(prefix="waggle-office-") as tmp:
        ps = Path(tmp) / "export.ps1"
        ps.write_text(PS_SCRIPT, encoding="utf-8")
        try:
            code, out, err = tc.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                                     "-File", str(ps), kind, str(src), str(pdf)], timeout=timeout)
        except subprocess.TimeoutExpired:
            for pid in _office_pids(kind) - before:  # only an Office we started
                tc.kill_tree(pid)
            raise ToolError(f"{kind.title()} did not finish in {timeout} s (a dialog may be waiting on the PC)",
                            "open the file yourself once, close any prompt, then retry")
    if not Path(pdf).exists() or Path(pdf).stat().st_size < 200:
        raise ToolError(f"{kind.title()} could not export the file", tc.tail(err or out, 300))


# ---- the tool --------------------------------------------------------------------------------------

def _tool_output_dir():
    from .config import Settings
    return Path(Settings.load().jobs_dir) / "tool-output"


def _safe_name(name):
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "-", str(name or "").strip()).strip(" .")[:80]
    return name or "document"


@guarded
def doc_to_pdf(args):
    text = args.get("markdown")
    out_arg = args.get("out")
    if text:  # the brain wrote the content itself
        if not str(text).strip():
            raise ToolError("markdown is empty")
        title = str(args.get("title") or first_heading(text, "Document"))
        pdf = tc.free_name(_tool_output_dir() / (_safe_name(args.get("name") or title) + ".pdf"))
        if out_arg:
            pdf = tc.out_path(Path(_tool_output_dir() / "x"), ".pdf", out_arg)
        pdf.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="waggle-doc-") as tmp:
            page = Path(tmp) / "doc.html"
            page.write_text(build_html(render_markdown(str(text)), title), encoding="utf-8")
            html_to_pdf(page, pdf)
        return tc.ok(output=tc.file_info(pdf), source="text", title=title)

    src = tc.read_path(args.get("path"), WEB_EXTS | set(OFFICE_EXTS))
    pdf = tc.out_path(src, ".pdf", out_arg)
    ext = src.suffix.lower()
    if ext in OFFICE_EXTS:
        office_to_pdf(OFFICE_EXTS[ext], src, pdf)
        return tc.ok(output=tc.file_info(pdf), source=OFFICE_EXTS[ext], via="Microsoft Office")
    raw = src.read_text(encoding="utf-8", errors="replace")
    with tempfile.TemporaryDirectory(prefix="waggle-doc-") as tmp:
        if ext in (".html", ".htm"):
            page = src  # print it where it is so relative images and styles still resolve
        else:
            body = render_markdown(raw) if ext in (".md", ".markdown") else "<pre>" + html.escape(raw) + "</pre>"
            page = Path(tmp) / "doc.html"
            page.write_text(build_html(body, first_heading(raw, src.stem), src.parent), encoding="utf-8")
        html_to_pdf(page, pdf)
    return tc.ok(output=tc.file_info(pdf), source=ext.lstrip("."), via="Chrome")


MCP_TOOLS = [
    ({"name": "doc_to_pdf", "description": "Make a PDF. Give `path` of a note or document (Obsidian/Markdown .md, "
      ".txt, .html, Word .docx/.doc/.rtf, Excel .xlsx/.csv, PowerPoint .pptx) or give `markdown` text you wrote "
      "(with optional title/name). Writes a NEW .pdf (never overwrites) next to the source when Waggle may write "
      "there, else in Waggle's tool-output folder, and returns the path.",
      "parameters": {"type": "object", "properties": {
          "path": {"type": "string"}, "markdown": {"type": "string"}, "title": {"type": "string"},
          "name": {"type": "string", "description": "file name for `markdown` text"},
          "out": {"type": "string", "description": "optional output path (.pdf)"}}, "required": []}}, doc_to_pdf),
]
