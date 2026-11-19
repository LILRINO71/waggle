"""Desktop awareness and control on top of Hermes's computer_use (cua-driver).

Fast path first, because a Telegram user won't wait a minute:
  - "what's on my screen?"  -> list the open windows + read the text of the top one through the accessibility
    tree (~1-2 s, exact text, no model). The local vision model is only used for questions about how something
    LOOKS, or when a window exposes no text.
  - "open notepad and type hello" -> launch the app ourselves, then type into it by name.

Hard-won rules (see tools/cu_probe.py):
  - cua-driver refuses the Claude app window ("its own authorization process"), so an action aimed at
    "whatever is in front" fails whenever Claude is in front. Always name the app.
  - Normal actions are pre-approved in Hermes's command_allowlist; Waggle's guard asks first for sensitive apps.
`dispatch(tool_name, args) -> str` is Hermes's ctx.dispatch_tool (injected so this is testable).
"""
import json
import os
import re
import subprocess
import time
from pathlib import Path

from . import events

NOISE_APPS = {"cua-driver.exe", "textinputhost.exe", "nvidia overlay.exe", "applicationframehost.exe",
              "searchhost.exe", "startmenuexperiencehost.exe", "shellexperiencehost.exe", "lockapp.exe"}
NOISE_TITLES = {"program manager", "windows input experience", "nvidia geforce overlay", ""}
PROTECTED_APPS = {"claude.exe"}  # cua-driver will not act on these

ALIASES = {"notepad": "notepad.exe", "calculator": "calc.exe", "calc": "calc.exe", "paint": "mspaint.exe",
           "file explorer": "explorer.exe", "explorer": "explorer.exe", "files": "explorer.exe",
           "settings": "ms-settings:", "task manager": "taskmgr.exe", "command prompt": "cmd.exe",
           "terminal": "wt.exe", "powershell": "powershell.exe", "chrome": "chrome", "google chrome": "chrome",
           "edge": "msedge", "word": "winword", "excel": "excel", "powerpoint": "powerpnt",
           "vs code": "code", "vscode": "code", "visual studio code": "code", "obsidian": "obsidian",
           "spotify": "spotify:", "discord": "discord", "steam": "steam:", "snipping tool": "snippingtool.exe"}
TARGET_NAMES = {"notepad.exe": "Notepad", "calc.exe": "Calculator", "mspaint.exe": "Paint", "code": "Code",
                "chrome": "chrome", "msedge": "msedge", "winword": "WINWORD", "excel": "EXCEL"}

OPEN = re.compile(r"^\s*(?:please\s+|can you\s+|could you\s+)?(?:open|launch|start|run)\s+(?:up\s+)?(?:the\s+|my\s+)?"
                  r"(?P<app>[\w .+&'-]{2,40}?)(?:\s+app)?"
                  r"(?:\s*(?:,|and|then)\s*(?:type|write|enter|put)\s+(?P<text>.+?))?\s*[.!?]*\s*$", re.I)
LOOKS = re.compile(r"\b(look like|looks?|colou?rs?|image|picture|photo|design|layout|style|chart|graph|diagram|"
                   r"meme|video|draw(ing)?)\b", re.I)


def _json(out):
    try:
        return json.loads(out) if isinstance(out, str) else (out or {})
    except ValueError:
        return {"error": str(out)[:300]}


def windows(dispatch):
    """Visible, real windows, front to back: [{app, title, pid, window_id}]."""
    d = _json(dispatch("computer_use", {"action": "list_windows"}))
    out = []
    for w in sorted(d.get("windows") or [], key=lambda w: -int(w.get("z_index") or 0)):
        app, title = (w.get("app_name") or ""), (w.get("title") or "")
        if w.get("off_screen") or app.lower() in NOISE_APPS or title.strip().lower() in NOISE_TITLES:
            continue
        out.append({"app": app, "title": title, "pid": w.get("pid"), "window_id": w.get("window_id")})
    return out


def window_text(dispatch, win, limit=60):
    """Labels from a window's accessibility tree (exact on-screen text)."""
    if win["app"].lower() in PROTECTED_APPS:
        return ""
    d = _json(dispatch("computer_use", {"action": "capture", "mode": "ax", "pid": win["pid"],
                                        "window_id": win["window_id"]}))
    labels, seen = [], set()
    for e in d.get("elements") or []:
        lab = " ".join(str(e.get("label") or "").split())
        if lab and lab not in seen and len(lab) > 1:
            seen.add(lab)
            labels.append(f"[{e.get('role', '')}] {lab[:200]}")
        if len(labels) >= limit:
            break
    return "\n".join(labels)


def screen_context(question, dispatch, vision=None):
    """<live_data> block answering 'what's on my screen'. vision(question) -> str is the slow fallback."""
    t0 = time.time()
    try:
        wins = windows(dispatch)
    except Exception as e:  # noqa: BLE001
        wins, err = [], e
    else:
        err = None
    parts = []
    if wins:
        parts.append("Open windows, front to back (only titles are known unless text is given below; "
                     "never guess what a window shows):\n" + "\n".join(
                         f"{i + 1}. {w['title']} ({w['app']})"
                         + (" [contents not readable]" if w["app"].lower() in PROTECTED_APPS else "")
                         for i, w in enumerate(wins[:10])))
        top = next((w for w in wins if w["app"].lower() not in PROTECTED_APPS), None)
        text = window_text(dispatch, top) if top else ""
        if text:
            parts.append(f"Text in the top window ({top['title']}):\n{text}")
    how = "windows"
    if vision and (LOOKS.search(question or "") or not wins or err):
        try:
            parts.append("What the local vision model sees:\n" + vision(question))
            how = "vision"
        except Exception as e:  # noqa: BLE001
            parts.append(f"(vision model unavailable: {e})")
    events.log("screen_look", seconds=round(time.time() - t0, 1), how=how, windows=len(wins),
               question=(question or "")[:200])
    if not parts:
        return f'<live_data source="screen">Could not read the screen ({err}). Say so in one line.</live_data>'
    return ('<live_data source="screen">You CAN see the user\'s PC screen: this was read from it just now. '
            "Answer from it directly and briefly; do not call look_at_screen or computer_use again for this.\n"
            + "\n\n".join(parts) + "</live_data>")


def parse_open(text):
    """'open notepad and type hello' -> ('notepad', 'hello'); not an open request -> None."""
    m = OPEN.match(text or "")
    if not m:
        return None
    app = m.group("app").strip().lower()
    if app in ("the door", "up", "it", "this", "that") or len(app.split()) > 4:
        return None
    return app, (m.group("text") or "").strip().strip("\"'")


def _start_menu_match(name):
    """A Start-menu shortcut whose name contains `name` (for apps with no command alias)."""
    roots = [Path(os.environ.get("ProgramData", r"C:\ProgramData")) / r"Microsoft\Windows\Start Menu\Programs",
             Path(os.environ.get("APPDATA", "")) / r"Microsoft\Windows\Start Menu\Programs"]
    hits = []
    for r in roots:
        if r.exists():
            hits += [p for p in r.rglob("*.lnk") if name in p.stem.lower()]
    return min(hits, key=lambda p: len(p.stem)) if hits else None


def launch(name, runner=None):
    """Start an app by friendly name. Returns (ok, target_name_for_computer_use, how)."""
    import shutil
    key = name.lower().strip()
    cmd = ALIASES.get(key, key)
    run = runner or (lambda c: subprocess.Popen(["cmd", "/c", "start", "", c], shell=False,
                                                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)))
    lnk = None if key in ALIASES else _start_menu_match(key)
    if key not in ALIASES and not lnk and not shutil.which(key):
        return False, None, "no app by that name on this PC"
    try:
        run(str(lnk) if lnk else cmd)
    except OSError as e:
        return False, None, str(e)
    target = TARGET_NAMES.get(cmd, (lnk.stem if lnk else key))
    return True, target, (f"shortcut {lnk.name}" if lnk else cmd)


def open_and_type(name, text, dispatch, runner=None, wait=2.0):
    """Do the whole 'open X and type Y' request. Returns a plain-language result for the model."""
    ok, target, how = launch(name, runner)
    if not ok:
        return f"Could not open {name}: {how}."
    if not text:
        events.log("desktop", action="open", app=name, ok=True)
        return f"Opened {name}."
    time.sleep(wait)
    cap = _json(dispatch("computer_use", {"action": "capture", "mode": "ax", "app": target}))
    if cap.get("error"):
        events.log("desktop", action="open_type", app=name, ok=False, error=str(cap["error"])[:200])
        return f"Opened {name}, but couldn't find its window to type in ({cap['error']})."
    res = _json(dispatch("computer_use", {"action": "type", "text": text, "app": target}))
    good = bool(res.get("ok"))
    events.log("desktop", action="open_type", app=name, ok=good, error=str(res.get("error", ""))[:200])
    return (f"Opened {name} and typed \"{text}\"." if good
            else f"Opened {name}, but typing failed: {res.get('error') or res}.")
