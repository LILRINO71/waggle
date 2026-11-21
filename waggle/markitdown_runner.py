"""Runs INSIDE the markitdown venv (~/.waggle-markitdown), never imported by the core.

    python -I markitdown_runner.py FILE KIND MODEL OLLAMA_URL   ->  one JSON line {"text": ..., "error": ...}

KIND is pdf, docx, pptx, xlsx or image. Pictures inside the document, scanned pages and standalone images are read by
the local vision model through Ollama's OpenAI-compatible endpoint (markitdown-ocr). Nothing leaves the PC."""
import json
import os
import sys

os.environ.setdefault("ANONYMIZED_TELEMETRY", "false")
os.environ.setdefault("DO_NOT_TRACK", "1")


def main(path, kind, model, url):
    from openai import OpenAI
    client = OpenAI(base_url=url.rstrip("/") + "/v1", api_key="ollama", max_retries=0, timeout=240)
    create = client.chat.completions.create  # thinking off: a reasoning model otherwise spends its answer thinking
    client.chat.completions.create = lambda **kw: create(reasoning_effort="none", **kw)
    if kind == "image":
        from markitdown_ocr import LLMVisionOCRService
        with open(path, "rb") as f:
            r = LLMVisionOCRService(client, model).extract_text(f)
        return {"text": r.text, "error": r.error or ""}
    from markitdown import MarkItDown
    md = MarkItDown(enable_plugins=True, llm_client=client, llm_model=model)
    return {"text": md.convert(path).text_content or "", "error": ""}


if __name__ == "__main__":
    try:
        out = main(*sys.argv[1:5])
    except Exception as e:  # noqa: BLE001
        out = {"text": "", "error": f"{type(e).__name__}: {str(e)[:200]}"}
    sys.stdout.write(json.dumps(out))
