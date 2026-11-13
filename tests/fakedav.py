"""A small scripted CardDAV/CalDAV server for tests, plus builders for the multistatus
bodies RFC 4918 (WebDAV), RFC 6352 (CardDAV) and RFC 4791 (CalDAV) describe.

The server records every byte and every request it receives, so tests can assert what
went over the wire, and that nothing did. All host names resolve to it: `source()` hands
DavSource a factory that makes real GuardedDAVConnection objects and points their socket
at 127.0.0.1. Replies are set per test with `on()`; anything unrouted gets 404.
"""

from __future__ import annotations

import re
import socket
import ssl
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from xml.sax.saxutils import escape

import trustme

from readonly_safe_mail_mcp.dav import DavHosts, DavSource, GuardedDAVConnection

HOSTS = DavHosts(
    "contacts.dav.example", "caldav.dav.example", contacts_start="/", calendars_start="/", partitions=True
)
CONTACTS = "contacts.dav.example"
CALDAV = "caldav.dav.example"
P_CONTACTS = "p07-contacts.dav.example"
P_CALDAV = "p12-caldav.dav.example"
FOREIGN = "evil.example"
CERT_NAMES = (CONTACTS, CALDAV, P_CONTACTS, P_CALDAV, FOREIGN)
USER = "me@example.org"
PASSWORD = "abcd-efgh-ijkl-mnop"


@dataclass
class Request:
    method: str
    path: str
    headers: list[tuple[str, str]]
    body: bytes

    def header(self, name: str) -> str | None:
        return next((v for k, v in self.headers if k.lower() == name.lower()), None)

    @property
    def host(self) -> str:
        return (self.header("Host") or "").rsplit(":", 1)[0]


@dataclass
class Reply:
    status: int = 207
    body: bytes = b""
    headers: list[tuple[str, str]] = field(default_factory=list)
    chunked: bool = False
    declared_length: int | None = None  # a Content-Length that lies; the server then closes
    cut: int = 0  # drop this many bytes from the end of the rendered reply, then close


Route = Reply | Callable[[Request], Reply]


class FakeDAVServer:
    def __init__(self, ssl_context: ssl.SSLContext) -> None:
        self.routes: dict[tuple[str | None, str, str], Route] = {}
        self.requests: list[Request] = []
        self.received = bytearray()
        self.connections = 0
        self._lock = threading.Lock()
        self._ssl = ssl_context
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind(("127.0.0.1", 0))
        self._sock.listen(5)
        self.port = self._sock.getsockname()[1]
        self._stopping = False
        self._thread = threading.Thread(target=self._serve, daemon=True)

    def __enter__(self) -> FakeDAVServer:
        self._thread.start()
        return self

    def __exit__(self, *exc) -> None:
        # On Linux, close() does not wake a thread blocked in accept(); a connection does.
        self._stopping = True
        socket.create_connection(("127.0.0.1", self.port), timeout=5).close()
        self._thread.join(timeout=5)
        self._sock.close()
        assert not self._thread.is_alive(), "fake server thread did not stop"

    def on(self, method: str, path: str, reply: Route, *, host: str | None = None) -> None:
        self.routes[(host, method, path)] = reply

    def url(self, host: str, path: str) -> str:
        return f"https://{host}:{self.port}{path}"

    def _route(self, req: Request) -> Reply:
        r = self.routes.get((req.host, req.method, req.path)) or self.routes.get((None, req.method, req.path))
        if r is None:
            return Reply(404)
        return r(req) if callable(r) else r

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
            conn = self._ssl.wrap_socket(conn, server_side=True)
            with self._lock:
                self.connections += 1
            self._session(conn)
        except (OSError, ssl.SSLError, ValueError):
            pass
        finally:
            conn.close()

    def _session(self, conn) -> None:
        rf = conn.makefile("rb")
        out = conn.makefile("wb")
        while True:
            line = rf.readline(65537)
            if not line:
                return
            with self._lock:
                self.received += line
            method, path, _ = line.decode("latin-1").rstrip("\r\n").split(" ", 2)
            headers = []
            while True:
                h = rf.readline(65537)
                with self._lock:
                    self.received += h
                if h in (b"\r\n", b"\n", b""):
                    break
                k, _, v = h.decode("latin-1").rstrip("\r\n").partition(":")
                headers.append((k, v.strip()))
            length = int(next((v for k, v in headers if k.lower() == "content-length"), "0"))
            body = rf.read(length)
            with self._lock:
                self.received += body
                req = Request(method, path, headers, body)
                self.requests.append(req)
            reply = self._route(req)
            data = self._render(reply)
            out.write(data[: len(data) - reply.cut])
            out.flush()
            if reply.declared_length is not None or reply.cut:
                return

    @staticmethod
    def _render(r: Reply) -> bytes:
        head = [f"HTTP/1.1 {r.status} Fake", "Content-Type: application/xml; charset=utf-8"]
        head += [f"{k}: {v}" for k, v in r.headers]
        if r.chunked:
            head.append("Transfer-Encoding: chunked")
            step = 4096
            body = b"".join(
                f"{len(r.body[i : i + step]):X}\r\n".encode() + r.body[i : i + step] + b"\r\n"
                for i in range(0, len(r.body), step)
            )
            body += b"0\r\n\r\n"
        else:
            n = len(r.body) if r.declared_length is None else r.declared_length
            head.append(f"Content-Length: {n}")
            body = r.body
        return ("\r\n".join(head) + "\r\n\r\n").encode() + body

    # -- client side --

    def factory(self) -> Callable[..., GuardedDAVConnection]:
        """A connection factory: real guarded connections whose socket goes to this server."""

        def make(host, port, **kw) -> GuardedDAVConnection:
            conn = GuardedDAVConnection(host, port, **kw)
            conn._create_connection = self._dial
            return conn

        return make

    def _dial(self, address, timeout=None, source_address=None):
        return socket.create_connection(("127.0.0.1", self.port), timeout)

    def source(self, client_ctx: ssl.SSLContext, **kw) -> DavSource:
        kw.setdefault("connection_factory", self.factory())
        return DavSource(
            kw.pop("hosts", HOSTS), USER, PASSWORD, port=self.port, ssl_context=client_ctx, timeout=5, **kw
        )


def tls() -> tuple[ssl.SSLContext, ssl.SSLContext]:
    """(server context with one certificate for every test host, client context trusting it)."""
    ca = trustme.CA()
    server = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ca.issue_cert(*CERT_NAMES).configure_cert(server)
    client = ssl.create_default_context()
    ca.configure_trust(client)
    return server, client


# --- multistatus builders --------------------------------------------------------------

NS = (
    'xmlns:d="DAV:" xmlns:card="urn:ietf:params:xml:ns:carddav" '
    'xmlns:cal="urn:ietf:params:xml:ns:caldav" xmlns:cs="http://calendarserver.org/ns/"'
)


def multistatus(*entries: str) -> bytes:
    return (
        f'<?xml version="1.0" encoding="utf-8"?>\n<d:multistatus {NS}>'
        + "".join(entries)
        + "</d:multistatus>"
    ).encode()


def entry(href: str, *props: str, status: int = 200, missing: tuple[str, ...] = ()) -> str:
    """A <response> with one propstat holding `props`, plus a 404 propstat for `missing`."""
    out = f"<d:response><d:href>{escape(href)}</d:href>"
    out += (
        f"<d:propstat><d:prop>{''.join(props)}</d:prop><d:status>HTTP/1.1 {status} X</d:status></d:propstat>"
    )
    if missing:
        out += f"<d:propstat><d:prop>{''.join(missing)}</d:prop><d:status>HTTP/1.1 404 Not Found</d:status></d:propstat>"
    return out + "</d:response>"


def status_entry(*hrefs: str, status: int = 404) -> str:
    links = "".join(f"<d:href>{escape(h)}</d:href>" for h in hrefs)
    return f"<d:response>{links}<d:status>HTTP/1.1 {status} X</d:status></d:response>"


def principal(href: str) -> str:
    return f"<d:current-user-principal><d:href>{escape(href)}</d:href></d:current-user-principal>"


def addressbook_home(href: str) -> str:
    return f"<card:addressbook-home-set><d:href>{escape(href)}</d:href></card:addressbook-home-set>"


def calendar_home(href: str) -> str:
    return f"<cal:calendar-home-set><d:href>{escape(href)}</d:href></cal:calendar-home-set>"


def displayname(name: str) -> str:
    return f"<d:displayname>{escape(name)}</d:displayname>"


def resourcetype(*types: str) -> str:
    return "<d:resourcetype>" + "".join(f"<{t}/>" for t in types) + "</d:resourcetype>"


def ctag(value: str) -> str:
    return f"<cs:getctag>{escape(value)}</cs:getctag>"


def etag(value: str) -> str:
    return f"<d:getetag>{escape(value)}</d:getetag>"


def components(*names: str) -> str:
    comps = "".join(f'<cal:comp name="{n}"/>' for n in names)
    return f"<cal:supported-calendar-component-set>{comps}</cal:supported-calendar-component-set>"


def address_data(text: str) -> str:
    return f"<card:address-data>{escape(text)}</card:address-data>"


def calendar_data(text: str) -> str:
    return f"<cal:calendar-data>{escape(text)}</cal:calendar-data>"


def multiget_reply(
    objects: dict[str, tuple[str, str]], data: Callable[[str], str]
) -> Callable[[Request], Reply]:
    """Answer a multiget from `objects` ({href: (etag, text)}): each requested href that is
    present comes back with its etag and data, every other one as a 404 entry."""

    def reply(req: Request) -> Reply:
        hrefs = re.findall(r"<d:href>([^<]*)</d:href>", req.body.decode())
        out = [
            entry(h, etag(objects[h][0]), data(objects[h][1])) if h in objects else status_entry(h)
            for h in hrefs
        ]
        return Reply(207, multistatus(*out))

    return reply


VCARD = "BEGIN:VCARD\r\nVERSION:3.0\r\nFN:Pat Example\r\nTEL:+1 555 0100\r\nEND:VCARD\r\n"
VEVENT = (
    "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nBEGIN:VEVENT\r\nUID:e1@example.org\r\n"
    "DTSTART:20260303T090000Z\r\nSUMMARY:Planning\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n"
)
