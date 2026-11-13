"""Read-only CardDAV / CalDAV access (issue #1, "Guarded HTTPS connection").

The app-specific password permits writes on the provider's side, so this module is the
only barrier between the server and a write to contacts or calendars. Two layers, as in
imap.py:

1. DavSource exposes narrow methods (discovery, collection listing, etag listing,
   multiget) that build their own requests. Nothing accepts a raw method, body or header.

2. GuardedDAVConnection checks every request inside the connection object itself:
   PROPFIND or REPORT only, an allowed host, a safe path, one of the request bodies this
   module builds and a header set it builds itself. request, putrequest, putheader,
   endheaders and send each refuse anything but the request just validated, before a
   byte is written to the socket.

PROPFIND (RFC 4918 section 9.1) and REPORT (RFC 3253 section 3.6) are read-only by
specification. Redirects are followed by hand, only to allowed hosts. A response over the
size cap is a failure, never a partial result. XML is parsed with defusedxml.
"""

from __future__ import annotations

import base64
import http.client
import re
import ssl
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Literal, NoReturn
from urllib.parse import unquote
from xml.etree.ElementTree import Element, ParseError

from defusedxml import DefusedXmlException
from defusedxml.ElementTree import fromstring

from . import __version__
from .imap import default_tls_context

DEFAULT_TIMEOUT = 30
MAX_RESPONSE_BYTES = 50_000_000
MAX_RESPONSE_SECONDS = 120
MAX_REDIRECTS = 5
MAX_MULTIGET_HREFS = 100
MAX_PATH_CHARS = 2048
HTTPS_PORT = 443
USER_AGENT = f"readonly-safe-mail-mcp/{__version__}"
ALLOWED_METHODS = ("PROPFIND", "REPORT")

Service = Literal["contacts", "calendars"]


class DavError(RuntimeError):
    pass


class DavAuthError(DavError):
    pass


class DavPolicyError(DavError):
    """Refused by this server's own read-only policy. Nothing was sent."""


class DavTooLarge(DavError):
    """A response over the size cap: a failure, never a partial result."""


class DavIncomplete(DavError):
    """An answer that cannot be trusted to be the whole collection."""


# --- hosts -------------------------------------------------------------------------


@dataclass(frozen=True)
class DavHosts:
    """The hosts one account may talk to. `partitions` also allows `pNN-<host>`
    (iCloud serves each account from a partition host such as p07-contacts.icloud.com)."""

    contacts: str
    calendars: str
    contacts_start: str = "/.well-known/carddav"
    calendars_start: str = "/.well-known/caldav"
    partitions: bool = False

    def allows(self, host: object) -> bool:
        return self.base(host) is not None

    def base(self, host: object) -> str | None:
        """The contacts or calendars host that `host` is, or is a partition host of; None
        when `host` is not allowed. Two hosts with the same base serve the same account."""
        if not isinstance(host, str):
            return None
        for base in (self.contacts, self.calendars):
            if host == base or (self.partitions and re.fullmatch(r"p\d{1,3}-" + re.escape(base), host)):
                return base
        return None

    def start(self, service: Service) -> DavLocation:
        if service == "contacts":
            return DavLocation(self.contacts, self.contacts_start)
        if service == "calendars":
            return DavLocation(self.calendars, self.calendars_start)
        raise DavPolicyError("unknown DAV service")


# Kept beside config.PRESETS, not in it: PRESETS is dict[str, tuple[str, int]] (issue #1,
# Blast radius). Gmail is absent on purpose: Google needs OAuth, a non-goal.
DAV_PRESETS: dict[str, DavHosts] = {
    "icloud": DavHosts(
        "contacts.icloud.com", "caldav.icloud.com", contacts_start="/", calendars_start="/", partitions=True
    ),
    "fastmail": DavHosts("carddav.fastmail.com", "caldav.fastmail.com"),
    "yahoo": DavHosts("carddav.address.yahoo.com", "caldav.calendar.yahoo.com"),
}


@dataclass(frozen=True)
class DavLocation:
    host: str
    path: str


# RFC 3986 pchar without "&" and "'", so a checked path needs no XML escaping.
_PATH_RE = re.compile(r"/(?:[A-Za-z0-9\-._~!$()*+,;=:@/]|%[0-9A-Fa-f]{2})*")
_URL_RE = re.compile(r"[Hh][Tt][Tt][Pp][Ss]://([A-Za-z0-9.-]{1,253})(?::(\d{1,5}))?(/.*)?", re.S)


def check_path(path: object) -> str:
    """Return `path` unchanged if it is a plain absolute path, else raise DavPolicyError."""
    if not isinstance(path, str) or len(path) > MAX_PATH_CHARS or not _PATH_RE.fullmatch(path):
        raise DavPolicyError("path refused by read-only guard")
    if "//" in path:
        raise DavPolicyError("path refused by read-only guard")
    for seg in path.split("/"):
        d = unquote(seg)
        if d in (".", "..") or any(c in "/\\" or ord(c) < 0x20 or ord(c) == 0x7F for c in d):
            raise DavPolicyError("path refused by read-only guard")
    return path


def resolve(ref: object, base_host: str, hosts: DavHosts, port: int) -> DavLocation:
    """An href or Location value as a location on an allowed host, else DavPolicyError.
    Accepts an absolute path (on `base_host`) or an https URL; nothing relative. "&" and
    "'" are legal in a path but refused by check_path (so no path needs XML escaping); they
    are percent-encoded here, which a server decodes to the same path."""
    if not isinstance(ref, str):
        raise DavPolicyError("href refused by read-only guard")
    ref = ref.strip().replace("&", "%26").replace("'", "%27")
    if ref.startswith("/"):
        return DavLocation(base_host, check_path(ref))
    m = _URL_RE.fullmatch(ref)
    if not m:
        raise DavPolicyError("href refused by read-only guard")
    host = m.group(1).lower()
    if not hosts.allows(host) or int(m.group(2) or HTTPS_PORT) != port:
        raise DavPolicyError("host refused by read-only guard")
    return DavLocation(host, check_path(m.group(3) or "/"))


def same_path(a: str, b: str) -> bool:
    return unquote(a).rstrip("/") == unquote(b).rstrip("/")


# --- request bodies ------------------------------------------------------------------
# The only bodies the guard lets through. Each fixes its method and Depth header.

_XML = b'<?xml version="1.0" encoding="utf-8"?>\n'
_NS_D = b'xmlns:d="DAV:"'
_NS_CARD = b'xmlns:card="urn:ietf:params:xml:ns:carddav"'
_NS_CAL = b'xmlns:cal="urn:ietf:params:xml:ns:caldav"'
_NS_CS = b'xmlns:cs="http://calendarserver.org/ns/"'


def _propfind(ns: bytes, props: bytes) -> bytes:
    return _XML + b"<d:propfind " + ns + b"><d:prop>" + props + b"</d:prop></d:propfind>"


BODY_PRINCIPAL = _propfind(_NS_D, b"<d:current-user-principal/>")
BODY_ADDRESSBOOK_HOME = _propfind(_NS_D + b" " + _NS_CARD, b"<card:addressbook-home-set/>")
BODY_CALENDAR_HOME = _propfind(_NS_D + b" " + _NS_CAL, b"<cal:calendar-home-set/>")
BODY_COLLECTIONS = _propfind(
    _NS_D + b" " + _NS_CS + b" " + _NS_CAL,
    b"<d:displayname/><d:resourcetype/><cs:getctag/><cal:supported-calendar-component-set/>",
)
BODY_ETAGS = _propfind(_NS_D, b"<d:getetag/>")

_PROPFIND_DEPTH: dict[bytes, str] = {
    BODY_PRINCIPAL: "0",
    BODY_ADDRESSBOOK_HOME: "0",
    BODY_CALENDAR_HOME: "0",
    BODY_COLLECTIONS: "1",
    BODY_ETAGS: "1",
}

_MULTIGET: dict[str, tuple[bytes, bytes]] = {
    "contacts": (
        _XML
        + b"<card:addressbook-multiget "
        + _NS_D
        + b" "
        + _NS_CARD
        + b"><d:prop><d:getetag/><card:address-data/></d:prop>",
        b"</card:addressbook-multiget>",
    ),
    "calendars": (
        _XML
        + b"<cal:calendar-multiget "
        + _NS_D
        + b" "
        + _NS_CAL
        + b"><d:prop><d:getetag/><cal:calendar-data/></d:prop>",
        b"</cal:calendar-multiget>",
    ),
}
_HREF_ITEM_RE = re.compile(rb"<d:href>([^<>]*)</d:href>")


def multiget_body(service: Service, collection: str, hrefs: Sequence[str]) -> bytes:
    """The multiget REPORT body for `hrefs`, each a direct member of `collection`."""
    if service not in _MULTIGET:
        raise DavPolicyError("unknown DAV service")
    if not collection.endswith("/") or not 1 <= len(hrefs) <= MAX_MULTIGET_HREFS:
        raise DavPolicyError("multiget refused by read-only guard")
    base = unquote(check_path(collection))
    for h in hrefs:
        d = unquote(check_path(h))
        if not d.startswith(base) or not d[len(base) :] or "/" in d[len(base) :]:
            raise DavPolicyError("multiget href outside the collection refused by read-only guard")
    head, tail = _MULTIGET[service]
    return head + b"".join(b"<d:href>" + h.encode("ascii") + b"</d:href>" for h in hrefs) + tail


def check_request(method: object, path: object, body: object) -> str | None:
    """The Depth header for (method, path, body) if this module built that request, else
    DavPolicyError. None for a multiget: RFC 6352 section 8.7 and RFC 4791 section 7.9 say
    the client should not send Depth there. Error messages never include the request."""
    if method not in ALLOWED_METHODS:
        raise DavPolicyError("DAV method refused by read-only guard")
    check_path(path)
    if type(body) is not bytes:
        raise DavPolicyError("DAV request body refused by read-only guard")
    if method == "PROPFIND" and body in _PROPFIND_DEPTH:
        return _PROPFIND_DEPTH[body]
    if method == "REPORT":
        for service, (head, tail) in _MULTIGET.items():
            if body.startswith(head) and body.endswith(tail) and len(body) > len(head) + len(tail):
                middle = body[len(head) : -len(tail)]
                hrefs, pos = [], 0
                for m in _HREF_ITEM_RE.finditer(middle):
                    if m.start() != pos:
                        break
                    hrefs.append(m.group(1).decode("ascii", "replace"))
                    pos = m.end()
                if pos == len(middle) and multiget_body(service, path, hrefs) == body:
                    return None
    raise DavPolicyError("DAV request body refused by read-only guard")


# --- layer 2: the connection guard ----------------------------------------------------

_AUTH_RE = re.compile(r"Basic [A-Za-z0-9+/]+={0,2}")


def basic_authorization(username: str, password: str) -> str:
    text = f"{username}:{password}"
    if not username or ":" in username or any(ord(c) < 0x20 or ord(c) == 0x7F for c in text):
        raise DavPolicyError("unsupported username or password")
    return "Basic " + base64.b64encode(text.encode("utf-8")).decode("ascii")


@dataclass
class _Pending:
    method: str
    path: str
    headers: list[tuple[str, str]]
    body: bytes
    wire: bytes
    started: bool = False
    headers_put: int = 0
    unsent: bytes | None = None  # set by endheaders: the bytes send() may still write


class GuardedDAVConnection(http.client.HTTPSConnection):
    """HTTPS to one allowed DAV host. Every primitive that could write to the socket
    refuses anything but the request request() has just validated."""

    def __init__(
        self,
        host: str,
        port: int,
        *,
        hosts: DavHosts,
        authorization: str,
        context: ssl.SSLContext,
        timeout: float = DEFAULT_TIMEOUT,
    ) -> None:
        if not hosts.allows(host) or type(port) is not int or not 0 < port < 65536:
            raise DavPolicyError("host refused by read-only guard")
        if context.verify_mode != ssl.CERT_REQUIRED or not context.check_hostname:
            raise DavPolicyError("TLS certificate and hostname verification must be enabled")
        if not isinstance(authorization, str) or not _AUTH_RE.fullmatch(authorization):
            raise DavPolicyError("unsupported authorization")
        super().__init__(host, port, timeout=timeout, context=context)
        self._hosts = hosts
        self._guard_host = host
        self._guard_port = port
        self._authorization = authorization
        self._pending: _Pending | None = None

    def _refuse(self, what: str) -> NoReturn:
        self._pending = None
        del self._buffer[:]
        self.close()
        raise DavPolicyError(f"{what} refused by read-only guard")

    def _check_origin(self) -> None:
        if (
            self.host != self._guard_host
            or self.port != self._guard_port
            or not self._hosts.allows(self.host)
            or self._tunnel_host is not None
        ):
            self._refuse("DAV host")

    def _validate(self, method, url, body, headers, encode_chunked) -> _Pending:
        self._check_origin()
        if headers or encode_chunked:
            self._refuse("DAV request header")
        try:
            depth = check_request(method, url, body)
        except DavPolicyError:
            self._refuse("DAV request")
        host = self.host if self.port == HTTPS_PORT else f"{self.host}:{self.port}"
        hdrs = [
            ("Host", host),
            ("User-Agent", USER_AGENT),
            ("Accept-Encoding", "identity"),
            ("Authorization", self._authorization),
            *([("Depth", depth)] if depth is not None else []),
            ("Content-Type", "application/xml; charset=utf-8"),
            ("Content-Length", str(len(body))),
        ]
        head = f"{method} {url} HTTP/1.1\r\n" + "".join(f"{k}: {v}\r\n" for k, v in hdrs) + "\r\n"
        return _Pending(method, url, hdrs, body, head.encode("ascii") + body)

    def request(self, method, url, body=None, headers=None, *, encode_chunked=False):
        self._pending = None
        pending = self._validate(method, url, body, headers, encode_chunked)
        self._pending = pending
        try:
            self.putrequest(method, url)
            for name, value in pending.headers:
                self.putheader(name, value)
            self.endheaders(body)
        except BaseException:
            self._pending = None
            del self._buffer[:]
            self.close()
            raise

    def putrequest(self, method, url, skip_host=False, skip_accept_encoding=False):
        p = self._pending
        if p is None or p.started or method != p.method or url != p.path:
            self._refuse("DAV request line")
        p.started = True
        super().putrequest(method, url, skip_host=True, skip_accept_encoding=True)

    def putheader(self, header, *values):
        p = self._pending
        if (
            p is None
            or not p.started
            or p.unsent is not None
            or p.headers_put >= len(p.headers)
            or (header, values) != (p.headers[p.headers_put][0], (p.headers[p.headers_put][1],))
        ):
            self._refuse("DAV request header")
        p.headers_put += 1
        super().putheader(header, *values)

    def endheaders(self, message_body=None, *, encode_chunked=False):
        p = self._pending
        if (
            p is None
            or not p.started
            or p.unsent is not None
            or p.headers_put != len(p.headers)
            or encode_chunked
            or type(message_body) is not bytes
            or message_body != p.body
        ):
            self._refuse("DAV request body")
        p.unsent = p.wire
        super().endheaders(message_body)
        if p.unsent:
            self._refuse("DAV request")
        self._pending = None

    def send(self, data):
        p = self._pending
        if p is None or not p.unsent or type(data) is not bytes or not data or not p.unsent.startswith(data):
            self._refuse("DAV send")
        self._check_origin()
        if self.sock is None:
            self.connect()
        p.unsent = p.unsent[len(data) :]
        super().send(data)

    def connect(self):
        if self._pending is None or not self._pending.unsent:
            self._refuse("DAV connect")
        self._check_origin()
        super().connect()

    def set_tunnel(self, host, port=None, headers=None):
        self._refuse("DAV tunnel")


# --- responses -----------------------------------------------------------------------

_D = "{DAV:}"
_CARD = "{urn:ietf:params:xml:ns:carddav}"
_CAL = "{urn:ietf:params:xml:ns:caldav}"
_CS = "{http://calendarserver.org/ns/}"
_SHORT_NS = (_D, _CARD, _CAL)
_STATUS_RE = re.compile(r"HTTP/\d(?:\.\d)? (\d{3})(?: .*)?", re.S)


@dataclass(frozen=True)
class DavItem:
    """One <response> entry as plain values. `status` is the entry's own status, else
    200 when any propstat was 200, else the first propstat's; `entry_status` is the
    entry's own status, None when it has propstats. Properties are read only from
    propstats with status 200: `resource_types` is None when the resourcetype property
    did not come back in one."""

    host: str
    href: str
    status: int
    entry_status: int | None = None
    etag: str | None = None
    ctag: str | None = None
    displayname: str | None = None
    resource_types: tuple[str, ...] | None = None
    components: tuple[str, ...] = ()
    data: str | None = None
    principal: DavLocation | None = None
    addressbook_home: DavLocation | None = None
    calendar_home: DavLocation | None = None

    @property
    def location(self) -> DavLocation:
        return DavLocation(self.host, self.href)


def _status(text: str | None) -> int:
    m = _STATUS_RE.fullmatch((text or "").strip())
    if not m:
        raise DavError("malformed status in DAV response")
    return int(m.group(1))


def _short(tag: str) -> str:
    for ns in _SHORT_NS:
        if tag.startswith(ns):
            return tag[len(ns) :]
    return tag


def _text(el: Element) -> str | None:
    t = (el.text or "").strip()
    return t or None


def _read_prop(el: Element, props: dict, base: DavLocation, hosts: DavHosts, port: int) -> None:
    tag = el.tag
    if tag == _D + "getetag":
        props["etag"] = _text(el)
    elif tag == _CS + "getctag":
        props["ctag"] = _text(el)
    elif tag == _D + "displayname":
        props["displayname"] = _text(el)
    elif tag == _D + "resourcetype":
        props["resource_types"] = tuple(_short(c.tag) for c in el)
    elif tag == _CAL + "supported-calendar-component-set":
        props["components"] = tuple(c.get("name", "") for c in el if c.tag == _CAL + "comp")
    elif tag in (_CARD + "address-data", _CAL + "calendar-data"):
        props["data"] = el.text or ""
    elif tag in (_D + "current-user-principal", _CARD + "addressbook-home-set", _CAL + "calendar-home-set"):
        href = el.find(_D + "href")
        if href is not None:
            key = {
                _D + "current-user-principal": "principal",
                _CARD + "addressbook-home-set": "addressbook_home",
                _CAL + "calendar-home-set": "calendar_home",
            }[tag]
            props[key] = resolve(href.text, base.host, hosts, port)


def parse_multistatus(data: bytes, base: DavLocation, hosts: DavHosts, port: int) -> list[DavItem]:
    """Parse a 207 body into DavItems. Every entry href must be on `base.host`; hrefs in
    principal and home-set properties may name any allowed host. Anything malformed
    raises; there is never a partial result."""
    try:
        root = fromstring(data, forbid_dtd=True)
    except (ParseError, DefusedXmlException, ValueError):
        raise DavError("DAV response is not well-formed XML") from None
    if root.tag != _D + "multistatus":
        raise DavError("DAV response is not a multistatus")
    items: list[DavItem] = []
    for resp in root.findall(_D + "response"):
        locs = [resolve(h.text, base.host, hosts, port) for h in resp.findall(_D + "href")]
        if not locs or any(loc.host != base.host for loc in locs):
            raise DavError("DAV response entry without an href on the requested host")
        status_el = resp.find(_D + "status")
        if status_el is not None:
            status = _status(status_el.text)
            items += [DavItem(loc.host, loc.path, status, status) for loc in locs]
            continue
        propstats = resp.findall(_D + "propstat")
        if len(locs) != 1 or not propstats:
            raise DavError("malformed DAV response entry")
        props: dict = {}
        statuses = []
        for ps in propstats:
            statuses.append(_status(ps.findtext(_D + "status")))
            prop = ps.find(_D + "prop")
            if prop is None:
                raise DavError("malformed DAV response entry")
            if statuses[-1] == 200:
                for el in prop:
                    _read_prop(el, props, base, hosts, port)
        status = 200 if 200 in statuses else statuses[0]
        items.append(DavItem(locs[0].host, locs[0].path, status, **props))
    return items


def _read_capped(resp: http.client.HTTPResponse, cap: int, deadline: float) -> bytes:
    declared = resp.length
    if declared is not None and declared > cap:
        raise DavTooLarge("DAV response over the size cap")
    chunks, total = [], 0
    while True:
        if time.monotonic() > deadline:
            raise DavError("DAV response took too long")
        # read1, not read: read(n) blocks until n bytes or the end, so a server sending a
        # byte at a time would never reach the deadline check. Each read1 waits at most
        # the socket timeout.
        chunk = resp.read1(65536)
        if not chunk:
            # read1 returns b"" at an early end of stream instead of raising IncompleteRead.
            if declared is not None and total != declared:
                raise DavError("DAV response truncated")
            resp.read()  # b"": marks the response done (Python 3.11's read1 does not at its length)
            return b"".join(chunks)
        total += len(chunk)
        if total > cap:
            raise DavTooLarge("DAV response over the size cap")
        chunks.append(chunk)


# --- layer 1: narrow methods ---------------------------------------------------------

ConnectionFactory = Callable[..., http.client.HTTPConnection]


class DavSource:
    """Discovery, listings and multigets for one account. Connections come from
    `connection_factory(host, port, hosts=, authorization=, context=, timeout=)` and are
    refused unless they are exactly GuardedDAVConnection."""

    def __init__(
        self,
        hosts: DavHosts,
        username: str,
        password: str,
        *,
        port: int = HTTPS_PORT,
        ssl_context: ssl.SSLContext | None = None,
        timeout: float = DEFAULT_TIMEOUT,
        max_response_bytes: int = MAX_RESPONSE_BYTES,
        connection_factory: ConnectionFactory = GuardedDAVConnection,
    ) -> None:
        ctx = ssl_context or default_tls_context()
        if ctx.verify_mode != ssl.CERT_REQUIRED or not ctx.check_hostname:
            raise DavPolicyError("TLS certificate and hostname verification must be enabled")
        self._hosts = hosts
        self._authorization = basic_authorization(username, password)
        self._port = port
        self._ctx = ctx
        self._timeout = timeout
        self._max_bytes = max_response_bytes
        self._factory = connection_factory
        self._conns: dict[str, GuardedDAVConnection] = {}

    def close(self) -> None:
        conns, self._conns = list(self._conns.values()), {}
        for conn in conns:
            conn.close()

    def __enter__(self) -> DavSource:
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # -- transport --

    def _connection(self, host: str) -> GuardedDAVConnection:
        conn = self._conns.get(host)
        if conn is not None:
            return conn
        if not self._hosts.allows(host):
            raise DavPolicyError("host refused by read-only guard")
        conn = self._factory(
            host,
            self._port,
            hosts=self._hosts,
            authorization=self._authorization,
            context=self._ctx,
            timeout=self._timeout,
        )
        if type(conn) is not GuardedDAVConnection:
            conn.close()
            raise DavPolicyError("DAV source requires a guarded connection")
        self._conns[host] = conn
        return conn

    def _drop(self, host: str) -> None:
        conn = self._conns.pop(host, None)
        if conn is not None:
            conn.close()

    def _roundtrip(self, method: str, loc: DavLocation, body: bytes) -> tuple[int, str | None, bytes]:
        conn = self._connection(loc.host)
        try:
            conn.request(method, loc.path, body)
            resp = conn.getresponse()
            if resp.getheader("Content-Encoding", "identity").strip().lower() != "identity":
                raise DavError("DAV response with an unsupported content encoding")
            data = _read_capped(resp, self._max_bytes, time.monotonic() + MAX_RESPONSE_SECONDS)
        except DavError:
            self._drop(loc.host)
            raise
        except (OSError, http.client.HTTPException) as e:
            self._drop(loc.host)
            raise DavError(f"{loc.host}: DAV request failed ({type(e).__name__})") from None
        return resp.status, resp.getheader("Location"), data

    def _exchange(self, method: str, loc: DavLocation, body: bytes) -> tuple[DavLocation, bytes]:
        """Every request goes through here: one request, redirects followed by hand to
        allowed hosts. Returns the final location and its 207 body."""
        for _ in range(MAX_REDIRECTS + 1):
            status, location, data = self._roundtrip(method, loc, body)
            if status == 207:
                return loc, data
            if status in (301, 302, 307, 308) and location:
                loc = resolve(location, loc.host, self._hosts, self._port)
                continue
            if status == 401:
                raise DavAuthError(f"{loc.host}: login failed")
            raise DavError(f"{loc.host}: DAV request failed with HTTP {status}")
        raise DavError(f"{loc.host}: too many redirects")

    def _multistatus(self, method: str, loc: DavLocation, body: bytes) -> tuple[DavLocation, list[DavItem]]:
        final, data = self._exchange(method, loc, body)
        return final, parse_multistatus(data, final, self._hosts, self._port)

    def _propfind_one(self, loc: DavLocation, body: bytes) -> DavItem:
        _, items = self._multistatus("PROPFIND", loc, body)
        if len(items) != 1:
            raise DavError("DAV discovery answer without exactly one entry")
        return items[0]

    def _children(self, loc: DavLocation, body: bytes) -> list[DavItem]:
        """The members of a collection. The collection's own entry is left out, but an
        error status on it (507 there marks a truncated answer, as in RFC 6578)
        means the members listed are not all of them."""
        final, items = self._multistatus("PROPFIND", loc, body)
        if any(i.entry_status not in (None, 200) for i in items if same_path(i.href, final.path)):
            raise DavIncomplete("DAV listing incomplete (the server marked it so)")
        return [i for i in items if not same_path(i.href, final.path)]

    # -- discovery and listings --

    def principal(self, service: Service) -> DavLocation:
        item = self._propfind_one(self._hosts.start(service), BODY_PRINCIPAL)
        if item.principal is None:
            raise DavError("DAV server named no current-user-principal")
        return item.principal

    def home(self, service: Service) -> DavLocation:
        """The address book or calendar home set (RFC 6352 section 7.1.1, RFC 4791 section 6.2.1)."""
        principal = self.principal(service)
        body = BODY_ADDRESSBOOK_HOME if service == "contacts" else BODY_CALENDAR_HOME
        item = self._propfind_one(principal, body)
        home = item.addressbook_home if service == "contacts" else item.calendar_home
        if home is None:
            raise DavError("DAV server named no home set")
        return home

    def collections(self, home: DavLocation) -> list[DavItem]:
        """The members of a home set, with displayname, resource types, ctag and
        supported components. The caller picks the address books or calendars."""
        return self._children(home, BODY_COLLECTIONS)

    def etags(self, collection: DavLocation) -> list[DavItem]:
        """The members of a collection with their etags. Raising on a non-207 answer,
        malformed XML or the size cap is this method's job; checking that every entry has
        an etag and status 200 is the caller's."""
        return self._children(collection, BODY_ETAGS)

    def multiget(self, service: Service, collection: DavLocation, hrefs: Sequence[str]) -> list[DavItem]:
        """address-data or calendar-data for `hrefs`, members of `collection`. An entry
        with status 404 is a deletion; a requested href missing from the answer is for the
        caller to notice."""
        _, items = self._multistatus("REPORT", collection, multiget_body(service, collection.path, hrefs))
        return items
