"""A small scripted IMAP server for tests, plus helpers that derive IMAP
BODYSTRUCTURE / part bytes from real email messages.

The server records every command line it receives (passwords redacted) so tests
can assert exactly what went over the wire.
"""

from __future__ import annotations

import email
import re
import socket
import ssl
import threading
from email import policy
from email.message import Message

from readonly_safe_mail_mcp.mime import FetchedMessage, parse_bodystructure, parse_response

# --- MIME helpers -----------------------------------------------------------


def _q(s: str) -> str:
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _compat(raw: bytes) -> Message:
    return email.message_from_bytes(raw, policy=policy.compat32)


def _payload_bytes(part: Message) -> bytes:
    if part.is_multipart():
        return b"".join(_payload_bytes(p) for p in part.get_payload())
    payload = part._payload  # raw: get_payload() would pre-decode 8bit text with replacement chars
    if isinstance(payload, list):  # message/rfc822
        return b"".join(p.as_bytes() for p in payload)
    return (payload or "").encode("ascii", "surrogateescape")


def bodystructure(part: Message) -> str:
    mt, st = part.get_content_maintype(), part.get_content_subtype()
    if mt == "multipart":
        return "(" + "".join(bodystructure(p) for p in part.get_payload()) + f" {_q(st.upper())})"
    params = [(k, v) for k, v in part.get_params(header="content-type")[1:]] if part.get_params() else []
    p = (
        "(" + " ".join(f"{_q(k)} {_q(email.utils.collapse_rfc2231_value(v))}" for k, v in params) + ")"
        if params
        else "NIL"
    )
    enc = _q(part.get("Content-Transfer-Encoding", "7bit"))
    data = _payload_bytes(part)
    disp = part.get_content_disposition()
    fn = part.get_param("filename", header="content-disposition")
    disp_s = "NIL"
    if disp:
        disp_s = (
            f"({_q(disp)} "
            + (f"({_q('filename')} {_q(email.utils.collapse_rfc2231_value(fn))})" if fn else "NIL")
            + ")"
        )
    lines = data.count(b"\n")
    if mt == "text":
        return f"({_q(mt)} {_q(st)} {p} NIL NIL {enc} {len(data)} {lines} NIL {disp_s} NIL NIL)"
    if mt == "message" and st == "rfc822":
        inner = part.get_payload(0)
        return f"({_q(mt)} {_q(st)} {p} NIL NIL {enc} {len(data)} NIL {bodystructure(inner)} {lines} NIL {disp_s} NIL NIL)"
    return f"({_q(mt)} {_q(st)} {p} NIL NIL {enc} {len(data)} NIL {disp_s} NIL NIL)"


def get_part(msg: Message, part_id: str) -> Message | None:
    if not msg.is_multipart():
        return msg if part_id == "1" else None
    node = msg
    for idx in part_id.split("."):
        if not node.is_multipart() or node.get_content_maintype() != "multipart":
            return None
        children = node.get_payload()
        i = int(idx) - 1
        if not 0 <= i < len(children):
            return None
        node = children[i]
    return node


def header_fields(raw: bytes, fields: list[str]) -> bytes:
    head = re.split(rb"\r?\n\r?\n", raw, maxsplit=1)[0]
    wanted = {f.upper() for f in fields}
    out, keep = [], False
    for line in head.splitlines():
        if line[:1] in (b" ", b"\t"):
            if keep:
                out.append(line)
            continue
        name = line.split(b":", 1)[0].decode("ascii", "replace").strip().upper()
        keep = name in wanted
        if keep:
            out.append(line)
    return b"\r\n".join(out) + b"\r\n\r\n"


def fetched(
    msg: Message | bytes, *, max_part_bytes: int = 5_000_000, structure: str | None = None
) -> FetchedMessage:
    """Build a FetchedMessage exactly as MailSource would, but without a network:
    BODYSTRUCTURE text goes through the real response parser."""
    raw = msg if isinstance(msg, bytes) else msg.as_bytes()
    cm = _compat(raw)
    bs = structure if structure is not None else bodystructure(cm)
    try:
        st = parse_bodystructure(parse_response([bs.encode()])[0])
    except Exception:
        st = None
    parts: dict[str, bytes | None] = {}
    if st is not None:
        for p in st.body_text_parts():
            node = get_part(cm, p.part_id)
            data = _payload_bytes(node) if node is not None else b""
            parts[p.part_id] = None if len(data) > max_part_bytes else data
    return FetchedMessage(
        "test",
        "INBOX",
        1,
        1,
        len(raw),
        header_fields(
            raw, ["FROM", "TO", "CC", "DATE", "SUBJECT", "MESSAGE-ID", "IN-REPLY-TO", "REFERENCES"]
        ),
        st,
        parts,
    )


# --- server -----------------------------------------------------------------


class FakeIMAPServer:
    def __init__(
        self,
        folders: dict[str, dict[int, bytes]],
        *,
        flags: dict[str, tuple[str, ...]] | None = None,
        uidvalidity: dict[str, int] | None = None,
        structure_override: dict[tuple[str, int], bytes] | None = None,
        ssl_context: ssl.SSLContext | None = None,
    ) -> None:
        self.folders = folders
        self.flags = flags or {}
        self.uidvalidity = uidvalidity or {}
        self.structure_override = structure_override or {}
        self.commands: list[str] = []
        self._ssl = ssl_context
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind(("127.0.0.1", 0))
        self._sock.listen(5)
        self.port = self._sock.getsockname()[1]
        self._stopping = False
        self._thread = threading.Thread(target=self._serve, daemon=True)

    def __enter__(self) -> FakeIMAPServer:
        self._thread.start()
        return self

    def __exit__(self, *exc) -> None:
        # On Linux, close() does not wake a thread blocked in accept(); a connection does.
        self._stopping = True
        socket.create_connection(("127.0.0.1", self.port), timeout=5).close()
        self._thread.join(timeout=5)
        self._sock.close()
        assert not self._thread.is_alive(), "fake server thread did not stop"

    @property
    def logins(self) -> int:
        return sum(1 for c in self.commands if c.startswith("LOGIN"))

    def _serve(self) -> None:
        while True:
            try:
                conn, _ = self._sock.accept()
            except OSError:
                return
            if self._stopping:
                conn.close()
                return
            threading.Thread(target=self._handle, args=(conn,), daemon=True).start()

    def _handle(self, conn) -> None:
        try:
            conn.settimeout(10)
            if self._ssl:
                conn = self._ssl.wrap_socket(conn, server_side=True)
            self._session(conn)
        except (OSError, ssl.SSLError):
            pass
        finally:
            conn.close()

    def _session(self, conn) -> None:
        rf = conn.makefile("rb")
        out = conn.makefile("wb")

        def send(data: bytes) -> None:
            out.write(data)
            out.flush()

        send(b"* OK [CAPABILITY IMAP4rev1] fake ready\r\n")
        selected: str | None = None
        while True:
            line = rf.readline()
            if not line:
                return
            text = line.rstrip(b"\r\n").decode("utf-8", "replace")
            tag, _, rest = text.partition(" ")
            cmd = rest.split(" ", 1)[0].upper()
            if cmd == "LOGIN":
                self.commands.append("LOGIN " + rest.split(" ")[1] + " ***")
            else:
                self.commands.append(rest)
            t = tag.encode()

            if cmd == "CAPABILITY":
                send(b"* CAPABILITY IMAP4rev1\r\n" + t + b" OK done\r\n")
            elif cmd == "LOGIN":
                send(t + b" OK logged in\r\n")
            elif cmd == "LIST":
                for name in self.folders:
                    fl = " ".join(self.flags.get(name, ("\\HasNoChildren",)))
                    send(f'* LIST ({fl}) "/" {_q(name)}\r\n'.encode())
                send(t + b" OK done\r\n")
            elif cmd == "EXAMINE":
                m = re.match(r'EXAMINE "((?:[^"\\]|\\.)*)"$', rest)
                name = re.sub(r"\\(.)", r"\1", m.group(1)) if m else None
                if name not in self.folders:
                    send(t + b" NO no such folder\r\n")
                    continue
                selected = name
                msgs = self.folders[name]
                uv = self.uidvalidity.get(name, 42)
                nxt = max(msgs, default=0) + 1
                send(
                    f"* FLAGS (\\Seen)\r\n* {len(msgs)} EXISTS\r\n* OK [UIDVALIDITY {uv}] ok\r\n"
                    f"* OK [UIDNEXT {nxt}] ok\r\n".encode()
                    + t
                    + b" OK [READ-ONLY] done\r\n"
                )
            elif cmd == "UID" and selected is not None:
                self._uid(rest, t, selected, send)
            elif cmd == "LOGOUT":
                send(b"* BYE bye\r\n" + t + b" OK done\r\n")
                return
            else:
                send(t + b" BAD unsupported\r\n")

    def _uid(self, rest: str, t: bytes, folder: str, send) -> None:
        msgs = self.folders[folder]
        uids = sorted(msgs)
        m = re.match(r"UID SEARCH (.*)$", rest, re.I)
        if m:
            args = m.group(1).split()
            result = uids
            if "UID" in args:
                lo = int(args[args.index("UID") + 1].split(":")[0])
                result = [u for u in uids if u >= lo] or uids[-1:]  # IMAP quirk: n:* includes the max
            send(("* SEARCH " + " ".join(map(str, result))).rstrip().encode() + b"\r\n" + t + b" OK done\r\n")
            return
        m = re.match(r"UID FETCH (\S+) (.*)$", rest, re.I)
        if not m:
            send(t + b" BAD unsupported\r\n")
            return
        uidset, items = m.group(1), m.group(2)
        for u in (int(x) for x in uidset.split(",")):
            if u not in msgs:
                continue
            raw = msgs[u]
            cm = _compat(raw)
            resp = f"* {uids.index(u) + 1} FETCH (UID {u}".encode()
            if "RFC822.SIZE" in items:
                resp += f" RFC822.SIZE {len(raw)}".encode()
            hm = re.search(r"BODY\.PEEK\[HEADER\.FIELDS \(([^)]*)\)\](?:<0\.(\d+)>)?", items)
            if hm:
                data = header_fields(raw, hm.group(1).split())
                if hm.group(2):
                    data = data[: int(hm.group(2))]
                key = f"BODY[HEADER.FIELDS ({hm.group(1)})]" + ("<0>" if hm.group(2) else "")
                resp += f" {key} {{{len(data)}}}\r\n".encode() + data
            if "BODYSTRUCTURE" in items:
                bs = self.structure_override.get((folder, u)) or bodystructure(cm).encode()
                resp += b" BODYSTRUCTURE " + bs
            pm = re.search(r"BODY\.PEEK\[([\d.]+)\](?:<0\.(\d+)>)?", items)
            if pm:
                node = get_part(cm, pm.group(1))
                data = _payload_bytes(node) if node is not None else b""
                if pm.group(2):
                    data = data[: int(pm.group(2))]
                key = f"BODY[{pm.group(1)}]" + ("<0>" if pm.group(2) else "")
                resp += f" {key} {{{len(data)}}}\r\n".encode() + data
            send(resp + b")\r\n")
        send(t + b" OK done\r\n")
