"""Attachments: formats, limits, sandbox, approval, and the end-to-end open flow."""

import base64
import io
import json
import subprocess
import sys
import zipfile
from email.message import EmailMessage
from pathlib import Path

import pytest
from fakeimap import FakeIMAPServer
from PIL import Image
from test_sync_query import GuardedIMAP4, db_for, make_cfg, run_sync

from readonly_safe_mail_mcp import approval, attachments, extract, isolate
from readonly_safe_mail_mcp.extract import ExtractError, kind_for
from readonly_safe_mail_mcp.imap import MailSource

# --- sample files ------------------------------------------------------------------


def pdf_bytes(text: str) -> bytes:
    stream = f"BT /F1 18 Tf 72 700 Td ({text}) Tj ET".encode()
    objs = [
        b"<</Type/Catalog/Pages 2 0 R>>",
        b"<</Type/Pages/Kids[3 0 R]/Count 1>>",
        b"<</Type/Page/Parent 2 0 R/MediaBox[0 0 612 792]/Contents 4 0 R/Resources<</Font<</F1 5 0 R>>>>>>",
        b"<</Length %d>>stream\n" % len(stream) + stream + b"\nendstream",
        b"<</Type/Font/Subtype/Type1/BaseFont/Helvetica>>",
    ]
    out = io.BytesIO()
    out.write(b"%PDF-1.4\n")
    offsets = []
    for i, o in enumerate(objs, 1):
        offsets.append(out.tell())
        out.write(b"%d 0 obj" % i + o + b"endobj\n")
    xref = out.tell()
    out.write(b"xref\n0 %d\n0000000000 65535 f \n" % (len(objs) + 1))
    for off in offsets:
        out.write(b"%010d 00000 n \n" % off)
    out.write(b"trailer<</Size %d/Root 1 0 R>>\nstartxref\n%d\n%%%%EOF\n" % (len(objs) + 1, xref))
    return out.getvalue()


def office_zip(files: dict[str, str | bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for name, data in files.items():
            z.writestr(name, data)
    return buf.getvalue()


W = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'


def docx_bytes() -> bytes:
    body = (
        f"<w:document {W}><w:body>"
        "<w:p><w:r><w:t>Donor letter draft</w:t></w:r></w:p>"
        "<w:tbl><w:tr><w:tc><w:p><w:r><w:t>Name</w:t></w:r></w:p></w:tc>"
        "<w:tc><w:p><w:r><w:t>Gift</w:t></w:r></w:p></w:tc></w:tr>"
        "<w:tr><w:tc><w:p><w:r><w:t>Angela</w:t></w:r></w:p></w:tc>"
        "<w:tc><w:p><w:r><w:t>$500</w:t></w:r></w:p></w:tc></w:tr></w:tbl>"
        "</w:body></w:document>"
    )
    return office_zip({"[Content_Types].xml": "<Types/>", "word/document.xml": body})


S = 'xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"'
R = 'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"'


def xlsx_bytes() -> bytes:
    return office_zip(
        {
            "xl/workbook.xml": f'<workbook {S} {R}><sheets><sheet name="Donors" sheetId="1" r:id="rId1"/></sheets></workbook>',
            "xl/_rels/workbook.xml.rels": '<Relationships><Relationship Id="rId1" Target="worksheets/sheet1.xml"/></Relationships>',
            "xl/sharedStrings.xml": f"<sst {S}><si><t>Name</t></si><si><t>Amount</t></si><si><t>Bob Lee</t></si></sst>",
            "xl/worksheets/sheet1.xml": f"<worksheet {S}><sheetData>"
            '<row r="1"><c r="A1" t="s"><v>0</v></c><c r="B1" t="s"><v>1</v></c></row>'
            '<row r="2"><c r="A2" t="s"><v>2</v></c><c r="C2"><f>SUM(X1:X9)</f><v>250</v></c></row>'
            "</sheetData></worksheet>",
        }
    )


def pptx_bytes() -> bytes:
    a = 'xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"'
    return office_zip(
        {
            "ppt/presentation.xml": "<p/>",
            "ppt/slides/slide2.xml": f"<sld {a}><a:p><a:r><a:t>Second slide</a:t></a:r></a:p></sld>",
            "ppt/slides/slide1.xml": f"<sld {a}><a:p><a:r><a:t>Gala plan</a:t></a:r></a:p></sld>",
        }
    )


def jpeg_with_gps(size=(3000, 2000)) -> bytes:
    img = Image.new("RGB", size, (10, 120, 200))
    exif = Image.Exif()
    exif[0x010F] = "SecretCam"  # Make
    exif[0x8825] = {2: (32.0, 15.0, 0.0)}  # GPSInfo latitude
    buf = io.BytesIO()
    img.save(buf, "JPEG", exif=exif)
    return buf.getvalue()


VCF = (
    "BEGIN:VCARD\r\nVERSION:3.0\r\nFN:Angela Ruiz\r\nORG:Borrego Friends;Board\r\n"
    "TEL;TYPE=CELL:+1 760 555 0100\r\nEMAIL:angela@example.org\r\n"
    "ADR;TYPE=HOME:;;12 Palm Canyon Dr;Borrego Springs;CA;92004;USA\r\n"
    "PHOTO;ENCODING=b;TYPE=JPEG:" + base64.b64encode(jpeg_with_gps((40, 40))).decode() + "\r\nEND:VCARD\r\n"
).encode()

ICS = (
    b"BEGIN:VCALENDAR\r\nMETHOD:REQUEST\r\nBEGIN:VEVENT\r\nSUMMARY:Board meeting\r\n"
    b"DTSTART;TZID=America/Los_Angeles:20261112T170000\r\nLOCATION:Library\\, room 2\r\n"
    b'ORGANIZER;CN="Jon Penn":mailto:jon@example.org\r\n'
    b"DESCRIPTION:Agenda: budget\\nJoin at https://zoom.example/j/123\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n"
)


def eml_bytes() -> bytes:
    m = EmailMessage()
    m["From"] = "Treasurer <t@example.org>"
    m["Subject"] = "Q3 numbers"
    m.set_content("Q3 income was strong.")
    m.add_alternative(
        "<p>Q3 income was <b>strong</b>.<span style='display:none'>HIDDEN</span></p>", subtype="html"
    )
    return m.as_bytes()


def run(kind: str, data: bytes, charset=None) -> list[dict]:
    return extract.extract(kind, base64.b64encode(data), "base64", charset)


# --- formats ------------------------------------------------------------------------


def test_kind_detection():
    assert kind_for("application/pdf", "x.pdf") == "pdf"
    assert kind_for("text/plain", "donors.csv") == "text"
    assert kind_for("text/plain", "card.vcf") == "vcf"
    assert kind_for("application/octet-stream", "IMG_1.HEIC") == "image"
    assert kind_for("text/x-vcard", "c.vcf") == "vcf"
    assert kind_for("application/vnd.ms-excel", "list.csv") == "text"
    for ct, name in [
        ("application/zip", "x.zip"),
        ("application/x-msdownload", "setup.exe"),
        ("application/octet-stream", "run.sh"),
        ("application/msword", "old.doc"),
        ("application/octet-stream", "x.dmg"),
    ]:
        assert kind_for(ct, name) is None, name


def test_pdf():
    [it] = run("pdf", pdf_bytes("Budget total 450 dollars"))
    assert "Budget total 450 dollars" in it["text"]


def test_docx_paragraphs_and_tables():
    [it] = run("docx", docx_bytes())
    assert "Donor letter draft" in it["text"] and "Angela | $500" in it["text"]


def test_xlsx_values_not_formulas():
    [it] = run("xlsx", xlsx_bytes())
    t = it["text"]
    assert "## Sheet: Donors" in t and "Name,Amount" in t and "Bob Lee,,250" in t
    assert "SUM" not in t


def test_pptx_slide_order():
    [it] = run("pptx", pptx_bytes())
    assert it["text"].index("Gala plan") < it["text"].index("Second slide")


def test_text_html_csv():
    assert run("text", b"name,city\nBob,Borrego")[0]["text"].startswith("name,city")
    html = b"<p>Visible</p><div style='display:none'>secret hidden</div><script>x()</script>"
    t = run("html", html)[0]["text"]
    assert "Visible" in t and "hidden" not in t and "x()" not in t


def test_vcf_with_photo():
    items = run("vcf", VCF)
    t = items[0]["text"]
    assert "Name: Angela Ruiz" in t and "Borrego Springs" in t and "Phone (cell)" in t
    assert len(items) == 2 and items[1]["format"] == "jpeg"


def test_ics():
    [it] = run("ics", ICS)
    t = it["text"]
    assert "Title: Board meeting" in t and "2026-11-12 17:00 (America/Los_Angeles)" in t
    assert "Library, room 2" in t and "Jon Penn <jon@example.org>" in t


def test_eml_prefers_plain_and_lists_nothing_hidden():
    [it] = run("eml", eml_bytes())
    assert "Subject: Q3 numbers" in it["text"] and "Q3 income was strong." in it["text"]
    assert "HIDDEN" not in it["text"]


@pytest.mark.parametrize("fmt", ["JPEG", "PNG", "GIF", "WEBP", "HEIF"])
def test_images_reencoded_downscaled_metadata_dropped(fmt):
    if fmt == "HEIF":
        import pillow_heif

        pillow_heif.register_heif_opener()
    img = Image.new("RGB", (2400, 1200), (200, 50, 50))
    exif = Image.Exif()
    exif[0x010F] = "SecretCam"
    exif[0x8825] = {2: (32.0, 15.0, 0.0)}
    buf = io.BytesIO()
    img.save(buf, fmt, **({"exif": exif} if fmt in ("JPEG", "PNG", "WEBP", "HEIF") else {}))
    [it] = run("image", buf.getvalue())
    out = base64.b64decode(it["image"])
    back = Image.open(io.BytesIO(out))
    assert max(back.size) <= extract.IMAGE_MAX_SIDE
    assert b"SecretCam" not in out and not back.getexif()


def test_image_decompression_bomb_refused():
    buf = io.BytesIO()
    Image.new("1", (12_000, 12_000)).save(buf, "PNG")  # tiny file, 144M pixels
    with pytest.raises(ExtractError, match="image could not be decoded"):
        run("image", buf.getvalue())


def test_zip_bomb_refused(monkeypatch):
    monkeypatch.setattr(extract, "MAX_MEMBER_BYTES", 1_000_000)
    big = f"<w:document {W}><w:body>" + "<w:p/>" * 400_000 + "</w:body></w:document>"
    with pytest.raises(ExtractError, match="too large when decompressed"):
        run("docx", office_zip({"word/document.xml": big}))


def test_wrong_bytes_for_type_refused():
    with pytest.raises(ExtractError):
        run("pdf", b"MZ\x90\x00 not a pdf")
    with pytest.raises(ExtractError):
        run("docx", b"%PDF-1.4 not a zip")


def test_xml_entities_refused():
    bomb = (
        '<?xml version="1.0"?><!DOCTYPE d [<!ENTITY a "aaaaaaaaaa"><!ENTITY b "&a;&a;&a;&a;">]>'
        f"<w:document {W}><w:body><w:p><w:r><w:t>&b;</w:t></w:r></w:p></w:body></w:document>"
    )
    with pytest.raises(ExtractError):
        run("docx", office_zip({"word/document.xml": bomb}))


# --- sandbox --------------------------------------------------------------------------

macos = pytest.mark.skipif(sys.platform != "darwin", reason="macOS sandbox-exec")


@macos
def test_sandboxed_worker_extracts():
    r = isolate.find_runner("ask")
    assert r.name == "sandbox-exec"
    [it] = isolate.run_worker(r, "docx", base64.b64encode(docx_bytes()), "base64", None)
    assert "Donor letter draft" in it["text"]


@macos
def test_sandbox_blocks_network_secrets_and_writes(tmp_path):
    secret = Path.home() / ".zshrc"
    probe = f"""
import socket, json, os
res = {{}}
try:
    socket.create_connection(("1.1.1.1", 53), timeout=3); res["net"] = "open"
except Exception: res["net"] = "blocked"
for name, p in [("home", {str(secret)!r}), ("cfg", {str(isolate.paths.config_dir())!r}),
                ("data", {str(isolate.paths.data_dir())!r}), ("tmp", {str(tmp_path)!r})]:
    try:
        os.listdir(p) if os.path.isdir(p) else open(p, "rb").read(1); res[name] = "open"
    except FileNotFoundError: res[name] = "missing"
    except Exception: res[name] = "blocked"
try:
    open({str(tmp_path / "w")!r}, "w").write("x"); res["write"] = "open"
except Exception: res["write"] = "blocked"
res["env"] = sorted(os.environ)
print(json.dumps(res))
"""
    r = isolate.find_runner("ask")
    argv = list(r.argv_prefix[:-2]) + ["-c", probe]  # same profile and interpreter, probe code
    p = subprocess.run(argv, capture_output=True, text=True, env={}, cwd="/")
    res = json.loads(p.stdout)
    assert res["net"] == "blocked" and res["write"] == "blocked" and res["tmp"] == "blocked"
    for k in ("home", "cfg", "data"):
        assert res[k] in ("blocked", "missing"), (k, res)
    assert not any("PASS" in e or "KEY" in e or "TOKEN" in e for e in res["env"])


def test_no_sandbox_means_refusal(monkeypatch):
    monkeypatch.setattr(isolate.sys, "platform", "win32")
    with pytest.raises(isolate.IsolationUnavailable):
        isolate.find_runner("ask")
    assert isolate.find_runner("ask-unsandboxed").name == "unsandboxed"
    with pytest.raises(isolate.IsolationUnavailable, match="turned off"):
        isolate.find_runner("never")


def test_worker_timeout_and_garbage(monkeypatch):
    slow = isolate.Runner("t", (sys.executable, "-c", "import time; time.sleep(5)"))
    with pytest.raises(isolate.WorkerError, match="too long"):
        isolate.run_worker(slow, "text", b"x", "7bit", None, timeout=0.5)
    junk = isolate.Runner("t", (sys.executable, "-c", "print('not json')"))
    with pytest.raises(isolate.WorkerError, match="failed"):
        isolate.run_worker(junk, "text", b"x", "7bit", None)


def test_profile_never_allows_config_or_data_dirs():
    prof = isolate.macos_profile()
    for p in isolate._runtime_paths():
        for f in isolate._forbidden():
            assert not (f == p or p in f.parents)
    assert "(deny default)" in prof and "network" not in prof


# --- approval -------------------------------------------------------------------------


def test_labels_cannot_inject_extra_items():
    labs = approval.labels([approval.Candidate("evil\n2. fake item"), approval.Candidate("ok")])
    assert len(labs) == 2 and "\n" not in labs[0]
    assert approval._parse_selection("2. ok", 2) == {1}
    assert approval._parse_selection("9. nope\ngarbage", 2) == set()


def test_dialog_deny_and_timeout_approve_nothing(monkeypatch):
    monkeypatch.setattr(approval.sys, "platform", "darwin")
    monkeypatch.setattr(approval.shutil, "which", lambda _: "/usr/bin/osascript")
    seen = {}

    def deny(argv, **kw):
        seen["argv"] = argv
        return subprocess.CompletedProcess(argv, 1, "", "User canceled.")

    monkeypatch.setattr(approval.subprocess, "run", deny)
    assert approval.ask_native([approval.Candidate('x" & do shell script "rm')]) == set()
    assert 'do shell script "rm' not in seen["argv"][2]  # item text is an argument, not script source

    def hang(argv, **kw):
        raise subprocess.TimeoutExpired(argv, 1)

    monkeypatch.setattr(approval.subprocess, "run", hang)
    assert approval.ask_native([approval.Candidate("a")]) == set()


# --- end to end over fake IMAP ----------------------------------------------------------


def mail_with(name: str, ctype: str, data: bytes, *, subject="Docs", body="see attached", sender=None):
    m = EmailMessage()
    m["From"] = sender or "Angela Ruiz <angela@example.org>"
    m["To"] = "me@example.com"
    m["Subject"] = subject
    m["Date"] = "Mon, 02 Nov 2026 10:00:00 -0700"
    m.set_content(body)
    mt, st = ctype.split("/")
    m.add_attachment(data, maintype=mt, subtype=st, filename=name)
    return m.as_bytes()


@pytest.fixture
def box():
    return {
        "INBOX": {
            1: mail_with(
                "budget.pdf", "application/pdf", pdf_bytes("Total 450 dollars see https://evil.example/r?t=1")
            ),
            2: mail_with("tools.zip", "application/zip", b"PK\x03\x04junk"),
            3: mail_with("photo.jpg", "image/jpeg", jpeg_with_gps()),
            4: mail_with("reset.pdf", "application/pdf", pdf_bytes("Your password reset code")),
            5: mail_with(
                "x.pdf",
                "application/pdf",
                pdf_bytes("hi"),
                subject="Your verification code",
                body="code 123456",
            ),
        }
    }


def opener(server):
    def open_source(acct, pw):
        src = MailSource(
            GuardedIMAP4("127.0.0.1", server.port, timeout=5),
            account=acct.name,
            folders=acct.folders,
            max_part_bytes=acct.rules.max_message_bytes,
        )
        src.login(acct.username, pw)
        return src

    return open_source


def ids_by_uid(db):
    return {r["uid"]: r["id"] for r in db.execute("SELECT id, uid FROM messages")}


def test_end_to_end_open(tmp_path, box):
    cfg = make_cfg(tmp_path)
    with FakeIMAPServer(box) as server:
        run_sync(cfg, server)
        db = db_for(cfg)
        ids = ids_by_uid(db)
        assert "budget.pdf (part 2, application/pdf" in __import__(
            "readonly_safe_mail_mcp.query"
        ).query.read_message(db, ids[1])
        refs = [(ids[u], "2") for u in (1, 2, 3, 4, 5)] + [(ids[1], "1"), (999999, "2")]
        targets, refused = attachments.resolve(db, refs, 20_000_000)
        assert [t.name for t in targets] == ["budget.pdf", "photo.jpg", "reset.pdf"]
        text = "\n".join(refused)
        assert "tools.zip" in text and "not a supported type" in text
        assert "withheld messages" in text and "not an attachment" in text and "no such message" in text
        runner = (
            isolate.find_runner("ask-unsandboxed") if sys.platform != "darwin" else isolate.find_runner("ask")
        )
        opened = attachments.open_approved(
            cfg, targets, runner, open_source=opener(server), password_fn=lambda _: "pw"
        )
    pdf, img, reset = opened
    assert pdf.error is None and "Total 450 dollars" in pdf.text
    assert "evil.example" not in pdf.text and "1 link(s) removed" in pdf.notes
    assert img.images and img.images[0][1] == "jpeg"
    assert b"SecretCam" not in base64.b64decode(img.images[0][0])
    assert reset.text == attachments.WITHHELD_ATTACHMENT  # keyword classifier applies to attachments too


def test_changed_attachment_not_opened(tmp_path, box):
    cfg = make_cfg(tmp_path)
    with FakeIMAPServer(box) as server:
        run_sync(cfg, server)
    db = db_for(cfg)
    ids = ids_by_uid(db)
    [t], _ = attachments.resolve(db, [(ids[1], "2")], 20_000_000)
    box["INBOX"][1] = mail_with("other.pdf", "application/pdf", pdf_bytes("swapped"))
    with FakeIMAPServer(box) as server:
        [res] = attachments.open_approved(
            cfg,
            [t],
            isolate.Runner("unsandboxed", ()),
            open_source=opener(server),
            password_fn=lambda _: "pw",
        )
    assert res.error and "changed" in res.error and not res.text


def test_server_tool_denial_opens_nothing(tmp_path, box, monkeypatch):
    import anyio

    from readonly_safe_mail_mcp import server as srv

    cfg = make_cfg(tmp_path)
    with FakeIMAPServer(box) as server:
        run_sync(cfg, server)
    ids = ids_by_uid(db_for(cfg))
    monkeypatch.setattr(srv, "_config", lambda: cfg)
    monkeypatch.setattr(srv.approval, "channel_available", lambda: True)
    monkeypatch.setattr(srv.approval, "ask_native", lambda cands: set())
    called = []
    monkeypatch.setattr(srv.attachments, "open_approved", lambda *a, **k: called.append(1) or [])
    mcp = srv.build_server()

    async def go():
        return await mcp.call_tool("request_attachments", {"items": [{"id": ids[1], "part": "2"}]})

    out = anyio.run(go)
    text = json.dumps(out, default=str)
    assert "not approved by the user" in text and not called


def test_image_items_warn_in_dialog():
    assert "cannot be redacted" in approval.prompt_for([approval.Candidate("a.jpg", image=True)])
    assert "cannot be redacted" not in approval.prompt_for([approval.Candidate("a.pdf")])
