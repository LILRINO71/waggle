"""Screen awareness: Waggle looks at your screen with a LOCAL vision model (nothing leaves the PC).

"what's this error on my screen?" / "read my screen" / "what am I looking at" -> screenshot -> the
vision model (default gemma4, already in Ollama) answers the question about it -> the answer is handed to
whichever brain replies. Only the last few screenshots are kept, in ~/.waggle/screens.

Screen CONTROL (clicking, typing) is Hermes's own computer_use tool (cua-driver), gated by Waggle's
guardrails; this module is only the eyes.
"""
import base64
import json
import os
import re
import subprocess
import time
import urllib.request
from pathlib import Path

from . import events
from .config import Settings

LOOK = re.compile(
    r"\b(on|at|in) (my|the) (screen|monitor|display)\b|\bmy screen\b|\bwhat am i (looking at|seeing)\b|"
    r"\b(read|look at|check|see|scan) (my|the|this) (screen|window|page|error)\b|\bscreenshot\b|"
    r"\bwhat('?s| is) (this|that) (error|window|popup|pop-up|message)\b|\bcan you see\b", re.I)
KEEP = 5

PS_CAPTURE = r"""
Add-Type -AssemblyName System.Windows.Forms, System.Drawing
Add-Type -TypeDefinition 'using System.Runtime.InteropServices; public class Dpi { [DllImport("user32.dll")] public static extern bool SetProcessDPIAware(); }'
[Dpi]::SetProcessDPIAware() | Out-Null
$b = [System.Windows.Forms.SystemInformation]::VirtualScreen
$bmp = New-Object System.Drawing.Bitmap $b.Width, $b.Height
$g = [System.Drawing.Graphics]::FromImage($bmp)
$g.CopyFromScreen($b.Left, $b.Top, 0, 0, $bmp.Size)
$w = [Math]::Min({max_w}, $b.Width); $h = [int]($b.Height * $w / $b.Width)
$small = New-Object System.Drawing.Bitmap $bmp, $w, $h
$small.Save('{out}', [System.Drawing.Imaging.ImageFormat]::Png)
"""


def wants_look(text):
    return bool(LOOK.search(text or ""))


def screens_dir():
    d = Path.home() / ".waggle" / "screens"
    d.mkdir(parents=True, exist_ok=True)
    return d


def capture(max_w=1600):
    """Screenshot of all monitors, scaled to max_w wide. Returns the PNG path."""
    out = screens_dir() / f"screen-{time.strftime('%Y%m%d-%H%M%S')}.png"
    ps = PS_CAPTURE.replace("{max_w}", str(max_w)).replace("{out}", str(out).replace("'", "''"))
    subprocess.run(["powershell", "-NoProfile", "-Command", ps], capture_output=True, timeout=30,
                   creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
    if not out.exists():
        raise RuntimeError("screenshot failed")
    for old in sorted(screens_dir().glob("screen-*.png"))[:-KEEP]:
        old.unlink(missing_ok=True)
    return out


SYSTEM = ("You are the eyes of a personal assistant. Look at the user's screenshot and answer their question "
          "about it. Read any error text, code, numbers or names exactly. Say which app/window it is. "
          "Be concise; plain text, no markdown.")


def describe(image, question, settings=None, post=None):
    """Ask the local vision model about an image. Returns its answer text."""
    s = settings or Settings.load()
    body = {"model": s.vision_model, "stream": False, "keep_alive": "2m",
            "options": {"temperature": 0.2, "num_ctx": 8192},
            "messages": [{"role": "system", "content": SYSTEM},
                         {"role": "user", "content": question or "What is on my screen?",
                          "images": [base64.b64encode(Path(image).read_bytes()).decode()]}]}
    if post:
        return post(body)
    req = urllib.request.Request(f"{s.ollama_url}/api/chat", data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=180) as r:
        return json.loads(r.read())["message"]["content"].strip()


def look(question, settings=None, capturer=None, describer=None):
    t0 = time.time()
    img = (capturer or capture)()
    answer = (describer or describe)(img, question, settings)
    events.log("screen_look", seconds=round(time.time() - t0, 1), chars=len(answer), question=question[:200])
    return answer


def context(question, settings=None, **kw):
    """<live_data> block for the plugin hook."""
    try:
        seen = look(question, settings, **kw)
        return (f'<live_data source="screen">A local vision model just looked at the user\'s screen for this '
                f"question. What it saw:\n{seen}\nAnswer the user from this.</live_data>")
    except Exception as e:  # noqa: BLE001
        return (f'<live_data source="screen">Could not look at the screen ({type(e).__name__}: {e}). '
                "Tell the user briefly.</live_data>")


if __name__ == "__main__":
    import sys
    print(look(" ".join(sys.argv[1:]) or "What is on my screen?"))
