"""Turn a dropped file into text, locally. Nothing here calls a model or the network.

Needs no extra libraries for text, Markdown, CSV, HTML, DOCX and PPTX (those are read with the standard library:
DOCX and PPTX are zip files of XML). PDF needs pypdf (or PyMuPDF, or pdfminer.six, whichever is installed); when
none is, extract() raises ExtractError saying which to install. Images are not read here (the local vision model
describes them, see dropbox_ingest).

extract(path) -> Extracted(kind, text, ...) or raises ExtractError(reason). The reason is written to the sidecar
note next to a file that could not be read, so it must say what to do about it."""
import csv
import io
import re
import zipfile
from dataclasses import dataclass, field
from html.parser import HTMLParser
from pathlib import Path

CAP_CHARS = 200_000  # the text kept in a note; a longer document keeps its first part and says so
MAX_FILE_BYTES = 100 * 1024 * 1024
MAX_XML_BYTES = 60 * 1024 * 1024  # one unpacked part of a DOCX/PPTX (a zip bomb stays out)

TEXT_EXT = {".txt", ".text", ".md", ".markdown", ".log", ".json", ".yaml", ".yml", ".ini", ".cfg", ".toml", ".rst",
            ".tex", ".py", ".java", ".js", ".ts", ".c", ".cpp", ".h", ".cs", ".go", ".rs", ".sh", ".ps1", ".bat",
            ".xml", ".srt", ".vtt", ".ics"}
HTML_EXT = {".html", ".htm", ".xhtml"}
CSV_EXT = {".csv", ".tsv"}
IMAGE_EXT = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"}
SUPPORTED = sorted({".pdf", ".docx", ".pptx", *TEXT_EXT, *HTML_EXT, *CSV_EXT, *IMAGE_EXT})
INSTALL_HINT = "pypdf"  # the library to install for PDFs (pip install pypdf)


class ExtractError(Exception):
    """The file cannot be turned into text; the message says why and what to do."""


@dataclass
class Extracted:
    kind: str
    text: str = ""
    pages: int = 0
    total_chars: int = 0  # length of the text before the cap
    truncated: bool = False
    notes: list = field(default_factory=list)  # things worth telling the reader ("no text layer", ...)


def kind_of(path):
    ext = Path(path).suffix.lower()
    if ext in IMAGE_EXT:
        return "image"
    if ext == ".pdf":
        return "pdf"
    if ext == ".docx":
        return "docx"
    if ext == ".pptx":
        return "pptx"
    if ext in HTML_EXT:
        return "html"
    if ext in CSV_EXT:
        return "csv"
    if ext in {".md", ".markdown"}:
        return "markdown"
    if ext in TEXT_EXT:
        return "text"
    return "unknown"


def decode(raw):
    """Bytes of a text file -> str (UTF-8, UTF-16 with a BOM, else Windows-1252)."""
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        return raw.decode("utf-16", errors="replace")
    try:
        return raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        return raw.decode("cp1252", errors="replace")


def looks_like_text(raw):
    """True when the first bytes are text (no NUL bytes, mostly printable): lets an unknown extension through."""
    head = raw[:4096]
    if not head:
        return False
    if head.startswith((b"\xff\xfe", b"\xfe\xff")):
        return True
    if b"\x00" in head:
        return False
    bad = sum(1 for b in head if b < 9 or 13 < b < 32)
    return bad / len(head) < 0.02


def _clean(text):
    text = text.replace("\r\n", "\n").replace("\r", "\n").replace("\x00", "")
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{4,}", "\n\n\n", text)
    return text.strip()


def _cap(text, notes):
    """(text kept, truncated, total chars)."""
    total = len(text)
    if total <= CAP_CHARS:
        return text, False, total
    cut = text[:CAP_CHARS]
    sp = max(cut.rfind("\n"), cut.rfind(" "))
    if sp > CAP_CHARS * 0.9:
        cut = cut[:sp]
    return cut.rstrip(), True, total


# ---- HTML ----------------------------------------------------------------------------------------

class _Html(HTMLParser):
    BLOCK = {"p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6", "section", "article", "table",
             "ul", "ol", "blockquote", "pre", "header", "footer"}
    SKIP = {"script", "style", "noscript", "template", "svg"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts, self.skip, self.title, self._in_title = [], 0, "", False

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP:
            self.skip += 1
        elif tag == "title":
            self._in_title = True
        elif tag in self.BLOCK:
            self.parts.append("\n")
        elif tag in {"td", "th"}:
            self.parts.append(" | ")

    def handle_endtag(self, tag):
        if tag in self.SKIP:
            self.skip = max(0, self.skip - 1)
        elif tag == "title":
            self._in_title = False
        elif tag in self.BLOCK:
            self.parts.append("\n")

    def handle_data(self, data):
        if self._in_title:
            self.title += data
        elif not self.skip:
            self.parts.append(data)


def html_text(raw):
    p = _Html()
    p.feed(decode(raw))
    p.close()
    body = re.sub(r"[ \t]+", " ", "".join(p.parts))
    body = re.sub(r" *\n *", "\n", body)
    title = p.title.strip()
    return (f"{title}\n\n{body}" if title and title not in body[:300] else body)


# ---- DOCX / PPTX ---------------------------------------------------------------------------------

def _xml(zf, name):
    from xml.etree import ElementTree as ET
    info = zf.getinfo(name)
    if info.file_size > MAX_XML_BYTES:
        raise ExtractError(f"{name} is unpacked larger than {MAX_XML_BYTES // 1048576} MB; not read")
    try:
        return ET.fromstring(zf.read(name))
    except ET.ParseError as e:
        raise ExtractError(f"{name} inside the file is damaged ({e})") from e


def _local(tag):
    return tag.rsplit("}", 1)[-1]


def _para_text(p):
    out = []
    for el in p.iter():
        t = _local(el.tag)
        if t == "t" and el.text:
            out.append(el.text)
        elif t in {"tab"}:
            out.append("\t")
        elif t in {"br", "cr"}:
            out.append("\n")
    return "".join(out)


def docx_text(path):
    try:
        zf = zipfile.ZipFile(path)
    except zipfile.BadZipFile as e:
        raise ExtractError(f"not a valid .docx (zip is damaged: {e})") from e
    with zf:
        if "word/document.xml" not in zf.namelist():
            raise ExtractError("not a Word .docx (no word/document.xml); an old .doc must be saved as .docx first")
        root = _xml(zf, "word/document.xml")
    body = next((e for e in root if _local(e.tag) == "body"), root)
    lines = []
    for el in body:
        tag = _local(el.tag)
        if tag == "p":
            lines.append(_para_text(el))
        elif tag == "tbl":
            for tr in el.iter():
                if _local(tr.tag) == "tr":
                    cells = [" ".join(_para_text(p) for p in tc.iter() if _local(p.tag) == "p").strip()
                             for tc in tr if _local(tc.tag) == "tc"]
                    lines.append("| " + " | ".join(cells) + " |")
            lines.append("")
    return "\n".join(lines)


def pptx_text(path):
    try:
        zf = zipfile.ZipFile(path)
    except zipfile.BadZipFile as e:
        raise ExtractError(f"not a valid .pptx (zip is damaged: {e})") from e
    with zf:
        names = zf.namelist()
        slides = sorted((n for n in names if re.fullmatch(r"ppt/slides/slide\d+\.xml", n)),
                        key=lambda n: int(re.search(r"(\d+)\.xml$", n).group(1)))
        if not slides:
            raise ExtractError("not a PowerPoint .pptx (no slides found); an old .ppt must be saved as .pptx first")
        out = []
        for i, name in enumerate(slides, 1):
            paras = [_para_text(p).strip() for p in _xml(zf, name).iter() if _local(p.tag) == "p"]
            out.append(f"### Slide {i}\n" + "\n".join(t for t in paras if t))
            note = f"ppt/notesSlides/notesSlide{re.search(r'(\d+)\.xml$', name).group(1)}.xml"
            if note in names:
                nparas = [_para_text(p).strip() for p in _xml(zf, note).iter() if _local(p.tag) == "p"]
                nparas = [t for t in nparas if t and not t.isdigit()]
                if nparas:
                    out.append("Speaker notes: " + " ".join(nparas))
        return "\n\n".join(out)


# ---- PDF -----------------------------------------------------------------------------------------

def _pdf_pypdf(path):
    from pypdf import PdfReader
    r = PdfReader(str(path))
    if r.is_encrypted:
        try:
            if not r.decrypt(""):
                raise ExtractError("the PDF is password-protected; remove the password and drop it again")
        except ExtractError:
            raise
        except Exception as e:
            raise ExtractError(f"the PDF is encrypted and could not be opened ({type(e).__name__})") from e
    out, size = [], 0
    for i, page in enumerate(r.pages):
        try:
            t = page.extract_text() or ""
        except Exception:
            t = ""
        out.append(t)
        size += len(t)
        if size > CAP_CHARS * 3:  # far past the cap: stop reading pages
            return "\n\n".join(out), len(r.pages), i + 1
    return "\n\n".join(out), len(r.pages), len(r.pages)


def _pdf_pymupdf(path):
    try:
        import pymupdf as fitz
    except ImportError:
        import fitz
    doc = fitz.open(str(path))
    if doc.needs_pass:
        raise ExtractError("the PDF is password-protected; remove the password and drop it again")
    out, size = [], 0
    for i, page in enumerate(doc):
        t = page.get_text() or ""
        out.append(t)
        size += len(t)
        if size > CAP_CHARS * 3:
            return "\n\n".join(out), doc.page_count, i + 1
    return "\n\n".join(out), doc.page_count, doc.page_count


def _pdf_pdfminer(path):
    from pdfminer.high_level import extract_text
    t = extract_text(str(path)) or ""
    return t, t.count("\f") + 1, t.count("\f") + 1


PDF_BACKENDS = (("pypdf", _pdf_pypdf), ("PyMuPDF", _pdf_pymupdf), ("pdfminer.six", _pdf_pdfminer))


def pdf_text(path):
    """(text, pages, pages read). Tries each installed backend in order."""
    missing = []
    for name, fn in PDF_BACKENDS:
        try:
            return fn(path)
        except ImportError:
            missing.append(name)
        except ExtractError:
            raise
        except Exception as e:
            raise ExtractError(f"the PDF could not be read by {name}: {type(e).__name__}: {str(e)[:150]}") from e
    raise ExtractError("no PDF reader is installed (looked for " + ", ".join(missing) + "). Install one with "
                       f"`py -3.14 -m pip install {INSTALL_HINT}`, then move this file back into the Drop folder")


# ---- entry point ---------------------------------------------------------------------------------

def extract(path):
    """Read one file's text. Images return an empty text (the vision model describes them)."""
    path = Path(path)
    size = path.stat().st_size
    if size > MAX_FILE_BYTES:
        raise ExtractError(f"larger than {MAX_FILE_BYTES // 1048576} MB; too big to file automatically")
    kind = kind_of(path)
    notes = []
    pages = 0
    fence = ""
    if kind == "image":
        return Extracted("image", "", notes=notes)
    if kind == "pdf":
        text, pages, read = pdf_text(path)
        if read < pages:
            notes.append(f"only the first {read} of {pages} pages were read")
        if not _clean(text):
            notes.append("the PDF has no text layer (a scan?); nothing could be extracted from it")
    elif kind == "docx":
        text = docx_text(path)
    elif kind == "pptx":
        text = pptx_text(path)
    else:
        raw = path.read_bytes()
        if kind == "unknown":
            if not looks_like_text(raw):
                raise ExtractError(f"unsupported file type {path.suffix or '(no extension)'}; supported: "
                                   + " ".join(SUPPORTED))
            kind = "text"
            notes.append(f"unrecognised extension {path.suffix!r}, read as plain text")
        if kind == "html":
            text = html_text(raw)
        elif kind == "csv":
            text = decode(raw)
            try:
                rows = sum(1 for _ in csv.reader(io.StringIO(text), delimiter="\t" if path.suffix.lower() == ".tsv" else ","))
                notes.append(f"{rows} rows")
            except csv.Error:
                pass
            fence = "csv"
        else:
            text = decode(raw)
    text = _clean(text)
    kept, truncated, total = _cap(text, notes)
    if fence and kept:
        kept = "```" + fence + "\n" + kept + "\n```"
    if truncated:
        notes.append(f"the document is {total:,} characters; only the first {len(kept):,} are kept in the note")
    return Extracted(kind, kept, pages=pages, total_chars=total, truncated=truncated, notes=notes)
