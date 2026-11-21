"""Drop folder OCR fallback (waggle/dropbox_markitdown.py): used only when the normal readers return nothing."""
import pytest

from waggle import dropbox_extract as dx
from waggle import dropbox_markitdown as mk


@pytest.fixture
def ocr(monkeypatch, tmp_path):
    """Pretend the venv exists and record what the runner is asked; the answer is canned."""
    calls = []
    monkeypatch.setattr(mk, "venv_python", lambda: tmp_path / "python.exe")

    def fake_convert(path, kind, settings=None, run=None):
        calls.append((str(path), kind))
        return ocr.reply, ocr.why
    monkeypatch.setattr(mk, "convert", fake_convert)
    ocr.reply, ocr.why, ocr.calls = "SCANNED WORKSHEET TEXT", "", calls
    return ocr


def test_scanned_pdf_falls_back_to_ocr(ocr, monkeypatch, tmp_path):
    f = tmp_path / "scan.pdf"
    f.write_bytes(b"%PDF-1.4")
    monkeypatch.setattr(dx, "pdf_text", lambda p: ("  \n", 2, 2))
    ex = dx.extract(f)
    assert ex.text == "SCANNED WORKSHEET TEXT" and ocr.calls == [(str(f), "pdf")]
    assert any("local OCR" in n for n in ex.notes)


def test_pdf_with_text_never_calls_ocr(ocr, monkeypatch, tmp_path):
    f = tmp_path / "t.pdf"
    f.write_bytes(b"%PDF-1.4")
    monkeypatch.setattr(dx, "pdf_text", lambda p: ("A real page of text. " * 10, 1, 1))
    assert dx.extract(f).text.startswith("A real page") and ocr.calls == []


def test_no_pdf_reader_uses_ocr_else_still_raises(ocr, monkeypatch, tmp_path):
    f = tmp_path / "t.pdf"
    f.write_bytes(b"%PDF-1.4")

    def none(p):
        raise dx.ExtractError("no PDF reader is installed (looked for x)")
    monkeypatch.setattr(dx, "pdf_text", none)
    assert dx.extract(f).text == "SCANNED WORKSHEET TEXT"
    ocr.reply = ""
    with pytest.raises(dx.ExtractError, match="no PDF reader"):
        dx.extract(f)


def test_password_pdf_error_is_not_swallowed(ocr, monkeypatch, tmp_path):
    f = tmp_path / "t.pdf"
    f.write_bytes(b"%PDF-1.4")

    def locked(p):
        raise dx.ExtractError("the PDF is password-protected; remove it")
    monkeypatch.setattr(dx, "pdf_text", locked)
    with pytest.raises(dx.ExtractError, match="password"):
        dx.extract(f)
    assert ocr.calls == []


def test_image_gets_ocr_text_and_xlsx_needs_it(ocr, tmp_path):
    img = tmp_path / "a.png"
    img.write_bytes(b"\x89PNG")
    ex = dx.extract(img)
    assert ex.kind == "image" and ex.text == "SCANNED WORKSHEET TEXT"
    x = tmp_path / "b.xlsx"
    x.write_bytes(b"PK")
    assert dx.extract(x).kind == "xlsx"
    ocr.reply = ""
    with pytest.raises(dx.ExtractError, match="markitdown"):
        dx.extract(x)


def test_off_switch_and_missing_venv(monkeypatch, tmp_path):
    from waggle.config import Settings
    assert mk.enabled(Settings()) is False  # conftest hides the real venv
    monkeypatch.setattr(mk, "venv_python", lambda: tmp_path / "p.exe")
    s = Settings()
    assert mk.enabled(s) is True
    s.drop_ocr = False
    assert mk.enabled(s) is False
    f = tmp_path / "a.png"
    f.write_bytes(b"x")
    assert dx.extract(f).text == ""


def test_convert_parses_runner_output(monkeypatch, tmp_path):
    from waggle.config import Settings
    monkeypatch.setattr(mk, "venv_python", lambda: tmp_path / "p.exe")
    seen = {}

    def run(cmd):
        seen["cmd"] = cmd
        return 'noise\n{"text": " hi ", "error": ""}'
    assert mk.convert(tmp_path / "x.pdf", "pdf", Settings(), run=run) == ("hi", "")
    assert seen["cmd"][-2] == Settings().local_model and seen["cmd"][-1].startswith("http://127.0.0.1")
    assert mk.convert(tmp_path / "x.pdf", "pdf", Settings(), run=lambda c: "garbage")[1].startswith("OCR could not")
