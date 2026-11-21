"""MarkItDown + markitdown-ocr for the Drop folder: scans, Office files and pictures become text, all on this PC.

dropbox_extract calls this when its own readers return (almost) nothing: a scanned PDF, a PDF with no reader installed,
an .xlsx, or a picture (its text is added next to the vision model's description). The work runs in a small separate
venv, ~/.waggle-markitdown, because markitdown pulls ~30 packages (pandas, numpy, onnxruntime, a newer Pillow) that
should not change the interpreter the live core runs on. OCR is a local vision model (setting drop_ocr_model, default local_model)
through Ollama's OpenAI-compatible endpoint; no key, no cloud, telemetry off.

Set up: py -3.14 -m venv ~/.waggle-markitdown, then
  ~/.waggle-markitdown/Scripts/python.exe -m pip install "markitdown[pdf,docx,pptx,xlsx]==0.1.9" markitdown-ocr==0.1.2 openai==3.57.0
Off switch: config `drop_ocr` false. Removal: delete this file, markitdown_runner.py, the test and the venv folder.
"""
import json
import os
import subprocess
from pathlib import Path

from .config import Settings

NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
RUNNER = Path(__file__).with_name("markitdown_runner.py")
MIN_TEXT = 40  # fewer characters than this from the normal readers counts as "nothing"
TIMEOUT_S = 900  # a long scan is one vision call per page


def venv_python():
    base = Path(os.environ.get("WAGGLE_MARKITDOWN_VENV") or Path.home() / ".waggle-markitdown")
    exe = base / "Scripts" / "python.exe" if os.name == "nt" else base / "bin" / "python"
    return exe if exe.exists() else None


def enabled(settings=None):
    s = settings or Settings.load()
    return bool(getattr(s, "drop_ocr", True)) and venv_python() is not None


def model(settings):
    """drop_ocr_model, else local_model. Not vision_model: on this PC gemma4 accepts pictures through Ollama 0.40.16
    but does not see them (2026-11-21: a page of plain text came back as "a dark abstract background"), while
    qwen3.6:35b-a3b read the same page exactly."""
    return getattr(settings, "drop_ocr_model", "") or settings.local_model


def convert(path, kind, settings=None, run=None):
    """The text of one file, or "" when OCR is off or fails (the reason is the second value): (text, why)."""
    s = settings or Settings.load()
    if not enabled(s):
        return "", "OCR is off or the markitdown venv is missing"
    cmd = [str(venv_python()), "-I", str(RUNNER), str(path), kind, model(s), s.ollama_url]
    try:
        if run:
            out = run(cmd)
        else:
            p = subprocess.run(cmd, capture_output=True, timeout=TIMEOUT_S, stdin=subprocess.DEVNULL,
                               creationflags=NO_WINDOW, env=dict(os.environ, PYTHONIOENCODING="utf-8",
                                                                 ANONYMIZED_TELEMETRY="false", DO_NOT_TRACK="1"))
            out = p.stdout.decode("utf-8", "replace")
        data = json.loads(out.strip().splitlines()[-1])
    except (OSError, subprocess.TimeoutExpired, ValueError, IndexError) as e:
        return "", f"OCR could not run ({type(e).__name__})"
    return (data.get("text") or "").strip(), data.get("error") or ""
