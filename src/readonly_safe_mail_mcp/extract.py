"""Attachment extraction worker (PLAN.md sections 7.2-7.3).

Runs as a short-lived child process inside the platform sandbox (see isolate.py):
  python -I -m readonly_safe_mail_mcp.extract  < request
Request on stdin: one JSON line {"kind", "encoding", "charset"} followed by the raw,
still transfer-encoded part bytes. Reply on stdout: one JSON object
  {"ok": true, "items": [{"text": "..."} | {"image": "<base64>", "format": "jpeg"|"png"}]}
  {"ok": false, "error": "..."}
All decoding and parsing happens here, never in the server process. The returned text
is NOT yet sanitized; the parent runs it through the sanitizer before anyone sees it.
"""

from __future__ import annotations

import base64
import binascii
import csv
import io
import json
import quopri
import re
import sys
import zipfile

from .vcal import _ical_unescape, _ics_time, _prop, _split_components, _unfold

# --- limits (enforced while decoding, not after) -------------------------------

MAX_INPUT_BYTES = 30_000_000  # encoded part
MAX_UNZIPPED_BYTES = 60_000_000  # total across members we read from an Office zip
MAX_MEMBER_BYTES = 40_000_000
MAX_PDF_PAGES = 300
MAX_SHEETS = 50
MAX_CELLS = 300_000
MAX_SLIDES = 500
MAX_OUTPUT_CHARS = 400_000
MAX_IMAGE_PIXELS = 60_000_000  # decoded; larger images are refused before decoding
IMAGE_MAX_SIDE = 1568
MAX_VCARD_PHOTOS = 3


class ExtractError(ValueError):
    pass


# --- format table ----------------------------------------------------------------

# kind -> (content types, filename extensions)
FORMATS: dict[str, tuple[tuple[str, ...], tuple[str, ...]]] = {
    "pdf": (("application/pdf",), (".pdf",)),
    "docx": (
        ("application/vnd.openxmlformats-officedocument.wordprocessingml.document",),
        (".docx",),
    ),
    "xlsx": (("application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",), (".xlsx",)),
    "pptx": (
        ("application/vnd.openxmlformats-officedocument.presentationml.presentation",),
        (".pptx",),
    ),
    "text": (
        ("text/plain", "text/csv", "text/tab-separated-values", "text/markdown", "text/x-markdown"),
        (".txt", ".csv", ".tsv", ".md", ".markdown", ".text", ".log"),
    ),
    "html": (("text/html",), (".html", ".htm")),
    "vcf": (("text/vcard", "text/x-vcard", "text/directory"), (".vcf", ".vcard")),
    "ics": (("text/calendar", "application/ics"), (".ics",)),
    "eml": (("message/rfc822", "message/global"), (".eml",)),
    "image": (
        (
            "image/jpeg",
            "image/jpg",
            "image/pjpeg",
            "image/png",
            "image/gif",
            "image/webp",
            "image/heic",
            "image/heif",
            "image/tiff",
            "image/bmp",
        ),
        (".jpg", ".jpeg", ".png", ".gif", ".webp", ".heic", ".heif", ".tif", ".tiff", ".bmp"),
    ),
}
GENERIC_TYPES = ("application/octet-stream", "application/x-download", "application/download", "")


def kind_for(content_type: str, filename: str) -> str | None:
    """Which supported format an attachment is, or None if it is refused. The worker
    still checks the bytes match (magic numbers, zip contents)."""
    ct = (content_type or "").lower().strip()
    name = (filename or "").lower().strip()
    by_ext = next((k for k, (_, exts) in FORMATS.items() if name.endswith(exts)), None)
    for k, (types, _) in FORMATS.items():
        if ct in types:
            # Mail clients often label CSV/VCF/ICS as text/plain; trust the extension within text.
            if k == "text" and by_ext in ("text", "vcf", "ics", "html"):
                return by_ext
            return k
    if ct in GENERIC_TYPES or ct.startswith("application/vnd.ms-excel") and name.endswith(".csv"):
        return by_ext
    return None


# --- transfer decoding ---------------------------------------------------------------


def transfer_decode(raw: bytes, encoding: str) -> bytes:
    enc = (encoding or "7bit").lower()
    if enc == "base64":
        compact = re.sub(rb"\s+", b"", raw)
        compact += b"=" * (-len(compact) % 4)
        try:
            return base64.b64decode(compact, validate=True)
        except (binascii.Error, ValueError) as e:
            raise ExtractError("invalid base64") from e
    if enc == "quoted-printable":
        return quopri.decodestring(raw)
    if enc in ("7bit", "8bit", "binary"):
        return raw
    raise ExtractError("unsupported transfer encoding")


def decode_text(data: bytes, charset: str | None) -> str:
    if data.startswith(b"\xef\xbb\xbf"):
        return data[3:].decode("utf-8", "replace")
    if data[:2] in (b"\xff\xfe", b"\xfe\xff"):
        return data.decode("utf-16", "replace")
    for cs in dict.fromkeys([c for c in ((charset or "").lower(), "utf-8") if c]):
        try:
            return data.decode(cs)
        except (LookupError, UnicodeDecodeError):
            continue
    return data.decode("cp1252", "replace")


# --- Office (zip + XML) ------------------------------------------------------------


class _Zip:
    """Bounded reads from an Office zip: declared sizes can lie, so every read is capped."""

    def __init__(self, data: bytes) -> None:
        if not data.startswith(b"PK\x03\x04"):
            raise ExtractError("not a valid Office file")
        try:
            self.zf = zipfile.ZipFile(io.BytesIO(data))
        except zipfile.BadZipFile as e:
            raise ExtractError("not a valid Office file") from e
        self.names = set(self.zf.namelist())
        self.budget = MAX_UNZIPPED_BYTES

    def read(self, name: str) -> bytes | None:
        if name not in self.names:
            return None
        cap = min(MAX_MEMBER_BYTES, self.budget)
        with self.zf.open(name) as fh:
            data = fh.read(cap + 1)
        if len(data) > cap:
            raise ExtractError("document is too large when decompressed")
        self.budget -= len(data)
        return data

    def xml(self, name: str):
        from defusedxml.ElementTree import fromstring  # no entities, no external references

        data = self.read(name)
        if data is None:
            return None
        try:
            return fromstring(data)
        except Exception as e:
            raise ExtractError("document XML could not be parsed") from e


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _docx_par(p) -> str:
    out = []
    for el in p.iter():
        t = _local(el.tag)
        if t == "t" and el.text:
            out.append(el.text)
        elif t == "tab":
            out.append("\t")
        elif t in ("br", "cr"):
            out.append("\n")
    return "".join(out)


def extract_docx(data: bytes) -> str:
    z = _Zip(data)
    root = z.xml("word/document.xml")
    if root is None:
        raise ExtractError("not a Word document")
    body = next((c for c in root if _local(c.tag) == "body"), root)
    lines: list[str] = []
    for block in body:
        t = _local(block.tag)
        if t == "p":
            lines.append(_docx_par(block))
        elif t == "tbl":
            for tr in (r for r in block.iter() if _local(r.tag) == "tr"):
                cells = [
                    " ".join(_docx_par(p) for p in tc.iter() if _local(p.tag) == "p").strip()
                    for tc in tr
                    if _local(tc.tag) == "tc"
                ]
                lines.append(" | ".join(cells))
            lines.append("")
    return "\n".join(lines)


_CELL_REF_RE = re.compile(r"^([A-Z]{1,3})(\d+)$")


def _col_index(ref: str) -> int | None:
    m = _CELL_REF_RE.match(ref or "")
    if not m:
        return None
    n = 0
    for ch in m.group(1):
        n = n * 26 + ord(ch) - 64
    return n - 1


def extract_xlsx(data: bytes) -> str:
    """Cell values only: cached results of formulas, never the formulas themselves."""
    z = _Zip(data)
    wb = z.xml("xl/workbook.xml")
    if wb is None:
        raise ExtractError("not an Excel workbook")
    shared: list[str] = []
    sst = z.xml("xl/sharedStrings.xml")
    if sst is not None:
        for si in sst:
            shared.append("".join(t.text or "" for t in si.iter() if _local(t.tag) == "t"))
    rels = {}
    rel_root = z.xml("xl/_rels/workbook.xml.rels")
    if rel_root is not None:
        for r in rel_root:
            target = r.get("Target", "")
            target = target.lstrip("/")
            rels[r.get("Id")] = target if target.startswith("xl/") else "xl/" + target
    sheets = [s for s in wb.iter() if _local(s.tag) == "sheet"][:MAX_SHEETS]
    out = io.StringIO()
    cells = 0
    for i, s in enumerate(sheets, 1):
        rid = next((v for k, v in s.attrib.items() if _local(k) == "id"), None)
        path = rels.get(rid) or f"xl/worksheets/sheet{i}.xml"
        root = z.xml(path)
        if root is None:
            continue
        out.write(f"## Sheet: {s.get('name', i)}\n")
        w = csv.writer(out, lineterminator="\n")
        for row in (r for r in root.iter() if _local(r.tag) == "row"):
            values: list[str] = []
            for c in row:
                if _local(c.tag) != "c":
                    continue
                cells += 1
                if cells > MAX_CELLS:
                    raise ExtractError("spreadsheet has too many cells")
                col = _col_index(c.get("r", ""))
                if col is not None and col > len(values):
                    if col > 16_384:
                        continue
                    values += [""] * (col - len(values))
                typ = c.get("t", "n")
                v = next((x.text or "" for x in c if _local(x.tag) == "v"), "")
                if typ == "s":
                    v = shared[int(v)] if v.isdigit() and int(v) < len(shared) else ""
                elif typ == "inlineStr":
                    v = "".join(t.text or "" for t in c.iter() if _local(t.tag) == "t")
                elif typ == "b":
                    v = "TRUE" if v == "1" else "FALSE"
                values.append(v)
            while values and values[-1] == "":
                values.pop()
            if values:
                w.writerow(values)
        out.write("\n")
    return out.getvalue()


def extract_pptx(data: bytes) -> str:
    z = _Zip(data)
    slide_re = re.compile(r"^ppt/slides/slide(\d+)\.xml$")
    slides = sorted((int(m.group(1)), n) for n in z.names if (m := slide_re.match(n)))
    if not slides and "ppt/presentation.xml" not in z.names:
        raise ExtractError("not a PowerPoint file")
    out = []
    for num, name in slides[:MAX_SLIDES]:
        root = z.xml(name)
        paras = []
        for p in (e for e in root.iter() if _local(e.tag) == "p"):
            text = "".join(t.text or "" for t in p.iter() if _local(t.tag) == "t").strip()
            if text:
                paras.append(text)
        out.append(f"## Slide {num}\n" + "\n".join(paras))
    return "\n\n".join(out)


# --- PDF ------------------------------------------------------------------------------


def extract_pdf(data: bytes) -> str:
    if b"%PDF-" not in data[:1024]:
        raise ExtractError("not a PDF")
    import pypdf  # >= 6: per-stream decompression limits are built in (75 MB by default)

    try:
        reader = pypdf.PdfReader(io.BytesIO(data))
        if reader.is_encrypted:
            try:
                reader.decrypt("")  # many PDFs are "encrypted" with an empty user password
            except Exception as e:
                raise ExtractError("PDF is password protected") from e
        out, total = [], 0
        for i, page in enumerate(reader.pages):
            if i >= MAX_PDF_PAGES:
                out.append(f"[stopped after {MAX_PDF_PAGES} pages]")
                break
            t = page.extract_text() or ""
            out.append(t)
            total += len(t)
            if total > MAX_OUTPUT_CHARS:
                break
    except ExtractError:
        raise
    except Exception as e:
        raise ExtractError("PDF could not be read") from e
    text = "\n\n".join(out)
    if not text.strip():
        return "[This PDF has no text layer (probably a scan); no text could be extracted.]"
    return text


# --- vCard / iCalendar ----------------------------------------------------------------


_VCARD_LABELS = {
    "FN": "Name",
    "ORG": "Organization",
    "TITLE": "Title",
    "TEL": "Phone",
    "EMAIL": "Email",
    "ADR": "Address",
    "BDAY": "Birthday",
    "NOTE": "Note",
    "URL": "Website",
    "NICKNAME": "Nickname",
}


def extract_vcf(data: bytes, charset: str | None) -> list[dict]:
    text = decode_text(data, charset)
    items: list[dict] = []
    lines_out: list[str] = []
    photos = 0
    for line in _unfold(text):
        if not line.strip():
            continue
        name, params, value = _prop(line)
        if name == "BEGIN":
            if lines_out:
                lines_out.append("")
            continue
        if name == "PHOTO":
            if photos < MAX_VCARD_PHOTOS and (params.get("ENCODING", "").upper() in ("B", "BASE64")):
                try:
                    raw = base64.b64decode(re.sub(r"\s+", "", value) + "==", validate=False)
                    items.append(_image_item(raw))
                    photos += 1
                    lines_out.append("Photo: [included as an image]")
                except Exception:
                    lines_out.append("Photo: [could not be decoded]")
            elif value.startswith("data:") and photos < MAX_VCARD_PHOTOS:
                try:
                    raw = base64.b64decode(value.split(",", 1)[1])
                    items.append(_image_item(raw))
                    photos += 1
                    lines_out.append("Photo: [included as an image]")
                except Exception:
                    lines_out.append("Photo: [could not be decoded]")
            continue
        label = _VCARD_LABELS.get(name)
        if not label:
            continue
        kind = params.get("TYPE", "").lower()
        if name in ("ADR", "ORG"):
            v = ", ".join(x for x in _split_components(value) if x.strip())
        else:
            v = _ical_unescape(value)
        lines_out.append(f"{label}{f' ({kind})' if kind else ''}: {v}")
    return [{"text": "\n".join(lines_out)}] + items


_ICS_FIELDS = {
    "SUMMARY": "Title",
    "DTSTART": "Starts",
    "DTEND": "Ends",
    "LOCATION": "Location",
    "ORGANIZER": "Organizer",
    "ATTENDEE": "Attendee",
    "DESCRIPTION": "Description",
    "RRULE": "Repeats",
    "STATUS": "Status",
}


def extract_ics(data: bytes, charset: str | None) -> str:
    out: list[str] = []
    depth_event = False
    for line in _unfold(decode_text(data, charset)):
        name, params, value = _prop(line)
        if name == "BEGIN" and value.upper() in ("VEVENT", "VTODO"):
            depth_event = True
            out.append("## " + ("Event" if value.upper() == "VEVENT" else "To-do"))
            continue
        if name == "END" and value.upper() in ("VEVENT", "VTODO"):
            depth_event = False
            out.append("")
            continue
        if name == "METHOD":
            out.append(f"Invitation type: {value}")
        if not depth_event or name not in _ICS_FIELDS:
            continue
        if name in ("ORGANIZER", "ATTENDEE"):
            cn = params.get("CN", "").strip('"')
            addr = value.split(":", 1)[-1] if value.lower().startswith("mailto:") else ""
            value = f"{cn} <{addr}>" if cn and addr else cn or addr
            if name == "ATTENDEE" and params.get("PARTSTAT"):
                value += f" ({params['PARTSTAT'].lower()})"
        elif name in ("DTSTART", "DTEND"):
            tz = params.get("TZID")
            value = _ics_time(value) + (f" ({tz})" if tz else "")
        out.append(f"{_ICS_FIELDS[name]}: {_ical_unescape(value)}")
    return "\n".join(out)


# --- forwarded email ---------------------------------------------------------------------


def extract_eml(data: bytes) -> str:
    from email import policy
    from email.parser import BytesParser

    try:
        msg = BytesParser(policy=policy.default).parsebytes(data)
    except Exception as e:
        raise ExtractError("attached email could not be parsed") from e
    head = [f"{h}: {msg[h]}" for h in ("From", "To", "Cc", "Date", "Subject") if msg[h]]
    body = ""
    try:
        part = msg.get_body(preferencelist=("plain", "html"))
        if part is not None:
            content = part.get_content()
            body = _html_text(content) if part.get_content_subtype() == "html" else content
    except Exception:
        body = "[the attached email's body could not be decoded]"
    names = [p.get_filename() for p in msg.iter_attachments() if p.get_filename()]
    tail = ["", "Its attachments (not opened): " + ", ".join(names)] if names else []
    return "\n".join(head + ["", body] + tail)


def _html_text(html: str) -> str:
    """Visible text only: hidden elements (display:none, tiny fonts, ...) are dropped."""
    from .sanitize import html_to_text, strip_regions
    from .textnorm import HIDDEN_CLOSE, HIDDEN_OPEN, strip_sentinels

    return strip_sentinels(strip_regions(html_to_text(html), HIDDEN_OPEN, HIDDEN_CLOSE))


# --- images -----------------------------------------------------------------------------


def _image_item(data: bytes) -> dict:
    from PIL import Image, ImageOps

    Image.MAX_IMAGE_PIXELS = MAX_IMAGE_PIXELS  # larger raises DecompressionBombError before decoding
    try:
        import pillow_heif

        pillow_heif.register_heif_opener()
    except Exception:
        pass
    formats = ["JPEG", "PNG", "GIF", "WEBP", "TIFF", "BMP", "HEIF"]
    try:
        img = Image.open(io.BytesIO(data), formats=formats)
        if img.format == "JPEG":
            img.draft("RGB", (IMAGE_MAX_SIDE, IMAGE_MAX_SIDE))  # decode at reduced size
        img.seek(0)  # first frame only (animated GIF/WebP)
        img = ImageOps.exif_transpose(img)  # apply camera rotation before EXIF is dropped
        has_alpha = img.mode in ("RGBA", "LA", "PA") or (img.mode == "P" and "transparency" in img.info)
        img = img.convert("RGBA" if has_alpha else "RGB")
        img.thumbnail((IMAGE_MAX_SIDE, IMAGE_MAX_SIDE))
        buf = io.BytesIO()
        # A new image object carries no EXIF, GPS, XMP, ICC or thumbnails: only pixels.
        clean = Image.new(img.mode, img.size)
        clean.paste(img)
        if has_alpha:
            clean.save(buf, "PNG", optimize=True)
            fmt = "png"
        else:
            clean.save(buf, "JPEG", quality=85)
            fmt = "jpeg"
    except ExtractError:
        raise
    except Exception as e:
        raise ExtractError("image could not be decoded") from e
    return {"image": base64.b64encode(buf.getvalue()).decode("ascii"), "format": fmt}


# --- dispatch ---------------------------------------------------------------------------


def extract(kind: str, raw: bytes, encoding: str, charset: str | None) -> list[dict]:
    if len(raw) > MAX_INPUT_BYTES:
        raise ExtractError("attachment is too large")
    data = transfer_decode(raw, encoding)
    if kind == "image":
        return [_image_item(data)]
    if kind == "vcf":
        items = extract_vcf(data, charset)
    elif kind == "pdf":
        items = [{"text": extract_pdf(data)}]
    elif kind == "docx":
        items = [{"text": extract_docx(data)}]
    elif kind == "xlsx":
        items = [{"text": extract_xlsx(data)}]
    elif kind == "pptx":
        items = [{"text": extract_pptx(data)}]
    elif kind == "ics":
        items = [{"text": extract_ics(data, charset)}]
    elif kind == "eml":
        items = [{"text": extract_eml(data)}]
    elif kind == "html":
        items = [{"text": _html_text(decode_text(data, charset))}]
    elif kind == "text":
        items = [{"text": decode_text(data, charset)}]
    else:
        raise ExtractError("unsupported attachment type")
    for it in items:
        if "text" in it and len(it["text"]) > MAX_OUTPUT_CHARS:
            it["text"] = it["text"][:MAX_OUTPUT_CHARS] + "\n[truncated]"
    return items


def _limit_resources() -> None:
    try:
        import resource
    except ImportError:
        return
    for lim, val in (
        (resource.RLIMIT_CPU, 30),
        (resource.RLIMIT_FSIZE, 0),  # may not create or grow files
        (resource.RLIMIT_NOFILE, 64),
        (resource.RLIMIT_AS, 2_000_000_000),  # enforced on Linux; macOS ignores it
    ):
        try:
            resource.setrlimit(lim, (val, val))
        except (ValueError, OSError):
            pass


def main() -> int:
    _limit_resources()
    out = sys.stdout
    try:
        stdin = sys.stdin.buffer
        header = json.loads(stdin.readline(4096) or b"{}")
        raw = stdin.read(MAX_INPUT_BYTES + 1)
        items = extract(str(header.get("kind")), raw, str(header.get("encoding")), header.get("charset"))
        json.dump({"ok": True, "items": items}, out)
    except ExtractError as e:
        json.dump({"ok": False, "error": str(e)}, out)
    except MemoryError:
        json.dump({"ok": False, "error": "attachment needs too much memory to open"}, out)
    except Exception as e:  # never echo content; only the error type
        json.dump({"ok": False, "error": f"attachment could not be read ({type(e).__name__})"}, out)
    out.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
