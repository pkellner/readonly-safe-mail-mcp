"""The read-only DAV layer, tested against bytes actually sent to a TLS socket."""

import http.client
import io
import time

import fakedav
import pytest
from fakedav import (
    CALDAV,
    CONTACTS,
    FOREIGN,
    HOSTS,
    P_CALDAV,
    P_CONTACTS,
    PASSWORD,
    USER,
    VCARD,
    VEVENT,
    FakeDAVServer,
    Reply,
    entry,
    multistatus,
    status_entry,
)

from readonly_safe_mail_mcp import dav
from readonly_safe_mail_mcp.dav import (
    BODY_ADDRESSBOOK_HOME,
    BODY_CALENDAR_HOME,
    BODY_COLLECTIONS,
    BODY_ETAGS,
    BODY_PRINCIPAL,
    DAV_PRESETS,
    DavAuthError,
    DavError,
    DavLocation,
    DavPolicyError,
    DavSource,
    GuardedDAVConnection,
    basic_authorization,
    check_path,
    check_request,
    multiget_body,
    parse_multistatus,
    resolve,
)

AUTH = basic_authorization(USER, PASSWORD)
WRITE_METHODS = ["PUT", "DELETE", "PROPPATCH", "POST", "MOVE", "COPY", "MKCOL", "MKCALENDAR", "LOCK", "GET"]
OTHER_METHODS = ["UNLOCK", "ACL", "OPTIONS", "HEAD", "PATCH", "SEARCH", "BIND", "TRACE", "CONNECT"]
SPELLINGS = [
    "propfind",
    "Report",
    "PROPFIND ",
    " REPORT",
    "PROPFIND\r\n",
    "REPORT / HTTP/1.1\r\nX:",
    b"PROPFIND",
    None,
]
BODIES = [BODY_PRINCIPAL, None, VCARD.encode()]
ORIGIN = DavLocation(CONTACTS, "/")


@pytest.fixture(scope="module")
def tls_pair():
    return fakedav.tls()


@pytest.fixture
def server(tls_pair):
    with FakeDAVServer(tls_pair[0]) as s:
        yield s


@pytest.fixture
def ctx(tls_pair):
    return tls_pair[1]


@pytest.fixture
def src(server, ctx):
    with server.source(ctx) as s:
        yield s


def connect(server, ctx, host=CONTACTS) -> GuardedDAVConnection:
    return server.factory()(host, server.port, hosts=HOSTS, authorization=AUTH, context=ctx, timeout=5)


def assert_nothing_sent(server):
    assert server.requests == []
    assert server.received == b""
    assert server.connections == 0


def ok_principal(href="/123456789/principal/"):
    return Reply(207, multistatus(entry("/", fakedav.principal(href))))


def wire(method: str, path: str, host: str, body: bytes, depth: str | None) -> bytes:
    return (
        f"{method} {path} HTTP/1.1\r\nHost: {host}\r\nUser-Agent: {dav.USER_AGENT}\r\n"
        f"Accept-Encoding: identity\r\nAuthorization: {AUTH}\r\n"
        + (f"Depth: {depth}\r\n" if depth else "")
        + f"Content-Type: application/xml; charset=utf-8\r\nContent-Length: {len(body)}\r\n\r\n"
    ).encode() + body


# --- write methods: refused on every path, nothing reaches the server -------------------


@pytest.mark.parametrize("body", BODIES, ids=["fixed-body", "no-body", "vcard-body"])
@pytest.mark.parametrize("method", WRITE_METHODS + OTHER_METHODS + SPELLINGS)
def test_method_refused_through_source(server, src, method, body):
    with pytest.raises(DavPolicyError):
        src._exchange(method, ORIGIN, body)
    assert_nothing_sent(server)


@pytest.mark.parametrize("body", BODIES, ids=["fixed-body", "no-body", "vcard-body"])
@pytest.mark.parametrize("method", WRITE_METHODS + OTHER_METHODS + SPELLINGS)
def test_method_refused_through_request(server, ctx, method, body):
    conn = connect(server, ctx)
    with pytest.raises(DavPolicyError):
        conn.request(method, "/", body)
    assert_nothing_sent(server)


@pytest.mark.parametrize("method", WRITE_METHODS + OTHER_METHODS + SPELLINGS + ["PROPFIND", "REPORT"])
def test_method_refused_through_putrequest(server, ctx, method):
    conn = connect(server, ctx)
    with pytest.raises(DavPolicyError):
        conn.putrequest(method, "/")
    with pytest.raises(DavPolicyError):
        conn.endheaders()
    assert_nothing_sent(server)


@pytest.mark.parametrize("method", WRITE_METHODS + OTHER_METHODS + ["PROPFIND", "REPORT"])
def test_method_refused_through_raw_send(server, ctx, method):
    conn = connect(server, ctx)
    for data in (
        f"{method} / HTTP/1.1\r\nHost: {CONTACTS}\r\nContent-Length: 0\r\n\r\n".encode(),
        wire(method, "/", f"{CONTACTS}:{server.port}", BODY_PRINCIPAL, "0"),
    ):
        with pytest.raises(DavPolicyError):
            conn.send(data)
    assert_nothing_sent(server)


@pytest.mark.parametrize("method", WRITE_METHODS)
def test_write_on_an_open_connection_refused(server, ctx, method):
    server.on("PROPFIND", "/", ok_principal())
    conn = connect(server, ctx)
    conn.request("PROPFIND", "/", BODY_PRINCIPAL)
    conn.getresponse().read()
    assert conn.sock is not None
    with pytest.raises(DavPolicyError):
        conn.send(f"{method} / HTTP/1.1\r\nHost: {CONTACTS}\r\nContent-Length: 0\r\n\r\n".encode())
    conn = connect(server, ctx)
    conn.request("PROPFIND", "/", BODY_PRINCIPAL)
    conn.getresponse().read()
    with pytest.raises(DavPolicyError):
        conn.putrequest(method, "/")
    with pytest.raises(DavPolicyError):
        conn.request(method, "/", BODY_PRINCIPAL)
    assert [(r.method, r.path) for r in server.requests] == [("PROPFIND", "/")] * 2
    assert server.received == wire("PROPFIND", "/", f"{CONTACTS}:{server.port}", BODY_PRINCIPAL, "0") * 2


@pytest.mark.parametrize("method", WRITE_METHODS + OTHER_METHODS + SPELLINGS)
def test_method_check_comes_first(method):
    with pytest.raises(DavPolicyError, match="method"):
        check_request(method, "/", BODY_PRINCIPAL)


def test_exact_validated_request_refused_through_raw_send(server, ctx):
    conn = connect(server, ctx)
    with pytest.raises(DavPolicyError):
        conn.send(wire("PROPFIND", "/", f"{CONTACTS}:{server.port}", BODY_PRINCIPAL, "0"))
    assert_nothing_sent(server)


@pytest.mark.parametrize(
    "call",
    [
        lambda c: c.putheader("Depth", "0"),
        lambda c: c.putheader("Authorization", AUTH),
        lambda c: c.endheaders(BODY_PRINCIPAL),
        lambda c: c.endheaders(),
        lambda c: c.send(b"x"),
        lambda c: c.send(io.BytesIO(b"PUT / HTTP/1.1\r\n\r\n")),
        lambda c: c.send([b"PUT / HTTP/1.1\r\n\r\n"]),
        lambda c: c.connect(),
        lambda c: c.set_tunnel(FOREIGN, 443),
    ],
    ids=[
        "putheader",
        "putheader-auth",
        "endheaders-body",
        "endheaders",
        "send",
        "send-file",
        "send-list",
        "connect",
        "tunnel",
    ],
)
def test_primitives_refused_outside_a_validated_request(server, ctx, call):
    conn = connect(server, ctx)
    with pytest.raises(DavPolicyError):
        call(conn)
    assert conn.sock is None
    assert_nothing_sent(server)


# --- deviations inside a validated request ---------------------------------------------


def _validated(server, ctx) -> GuardedDAVConnection:
    conn = connect(server, ctx)
    conn._pending = conn._validate("PROPFIND", "/", BODY_PRINCIPAL, None, False)
    return conn


def _all_headers(conn):
    conn.putrequest("PROPFIND", "/")
    for k, v in list(conn._pending.headers):
        conn.putheader(k, v)


DEVIATIONS = {
    "other-method": lambda c: c.putrequest("PUT", "/"),
    "other-path": lambda c: c.putrequest("PROPFIND", "/other/"),
    "putrequest-twice": lambda c: (c.putrequest("PROPFIND", "/"), c.putrequest("PROPFIND", "/")),
    "header-before-line": lambda c: c.putheader("Host", CONTACTS),
    "foreign-host-header": lambda c: (c.putrequest("PROPFIND", "/"), c.putheader("Host", FOREIGN)),
    "extra-header-first": lambda c: (
        c.putrequest("PROPFIND", "/"),
        c.putheader("X-HTTP-Method-Override", "PUT"),
    ),
    "depth-infinity": lambda c: (
        c.putrequest("PROPFIND", "/"),
        [c.putheader(k, v) for k, v in c._pending.headers[:4]],
        c.putheader("Depth", "infinity"),
    ),
    "header-multi-value": lambda c: (c.putrequest("PROPFIND", "/"), c.putheader("Host", CONTACTS, "x")),
    "header-bytes": lambda c: (c.putrequest("PROPFIND", "/"), c.putheader(b"Host", c._pending.headers[0][1])),
    "extra-header-last": lambda c: (_all_headers(c), c.putheader("Destination", "/x")),
    "headers-skipped": lambda c: (c.putrequest("PROPFIND", "/"), c.endheaders(BODY_PRINCIPAL)),
    "other-body": lambda c: (_all_headers(c), c.endheaders(BODY_ETAGS)),
    "no-body": lambda c: (_all_headers(c), c.endheaders()),
    "chunked": lambda c: (_all_headers(c), c.endheaders(BODY_PRINCIPAL, encode_chunked=True)),
    "bytearray-body": lambda c: (_all_headers(c), c.endheaders(bytearray(BODY_PRINCIPAL))),
    "send-before-endheaders": lambda c: (_all_headers(c), c.send(c._pending.wire)),
}


@pytest.mark.parametrize("deviate", DEVIATIONS.values(), ids=DEVIATIONS.keys())
def test_deviation_inside_validated_request_refused(server, ctx, deviate):
    conn = _validated(server, ctx)
    with pytest.raises(DavPolicyError):
        deviate(conn)
    assert conn._pending is None and conn._buffer == []
    assert_nothing_sent(server)


@pytest.mark.parametrize("cut", [0, 10, 60])
def test_send_of_other_bytes_during_endheaders_refused(server, ctx, cut):
    conn = _validated(server, ctx)
    conn._pending.unsent = conn._pending.wire
    with pytest.raises(DavPolicyError):
        conn.send(conn._pending.wire[:cut] + b"X")
    assert_nothing_sent(server)


def test_refusal_leaves_no_stale_bytes_for_the_next_request(server, ctx):
    server.on("PROPFIND", "/", ok_principal())
    conn = _validated(server, ctx)
    conn.putrequest("PROPFIND", "/")
    with pytest.raises(DavPolicyError):
        conn.putheader("X-Evil", "1")
    conn.request("PROPFIND", "/", BODY_PRINCIPAL)
    assert conn.getresponse().status == 207
    assert server.received == wire("PROPFIND", "/", f"{CONTACTS}:{server.port}", BODY_PRINCIPAL, "0")


# --- bodies, headers, paths ------------------------------------------------------------

HEAD, TAIL = dav._MULTIGET["contacts"]
CAL_HEAD, CAL_TAIL = dav._MULTIGET["calendars"]


def _mg(*hrefs: str, head=HEAD, tail=TAIL) -> bytes:
    return head + b"".join(b"<d:href>" + h.encode() + b"</d:href>" for h in hrefs) + tail


BAD_BODIES = {
    "none": ("PROPFIND", None),
    "empty": ("PROPFIND", b""),
    "str": ("PROPFIND", BODY_PRINCIPAL.decode()),
    "bytearray": ("PROPFIND", bytearray(BODY_PRINCIPAL)),
    "memoryview": ("PROPFIND", memoryview(BODY_PRINCIPAL)),
    "file": ("PROPFIND", io.BytesIO(BODY_PRINCIPAL)),
    "iterable": ("PROPFIND", [BODY_PRINCIPAL]),
    "trailing-space": ("PROPFIND", BODY_PRINCIPAL + b" "),
    "allprop": ("PROPFIND", b'<?xml version="1.0"?><d:propfind xmlns:d="DAV:"><d:allprop/></d:propfind>'),
    "propertyupdate": (
        "PROPFIND",
        b'<d:propertyupdate xmlns:d="DAV:"><d:set><d:prop><d:displayname>x</d:displayname>'
        b"</d:prop></d:set></d:propertyupdate>",
    ),
    "fixed-body-as-report": ("REPORT", BODY_PRINCIPAL),
    "multiget-as-propfind": ("PROPFIND", _mg("/ab/1.vcf")),
    "multiget-no-hrefs": ("REPORT", _mg()),
    "multiget-outside": ("REPORT", _mg("/other/1.vcf")),
    "multiget-prefix-trick": ("REPORT", _mg("/abc/1.vcf")),
    "multiget-nested": ("REPORT", _mg("/ab/sub/1.vcf")),
    "multiget-itself": ("REPORT", _mg("/ab/")),
    "multiget-dotdot": ("REPORT", _mg("/ab/../x.vcf")),
    "multiget-encoded-dotdot": ("REPORT", _mg("/ab/%2e%2e")),
    "multiget-absolute-url": ("REPORT", _mg(f"https://{FOREIGN}/ab/1.vcf")),
    "multiget-injected-element": ("REPORT", HEAD + b"<d:href>/ab/1.vcf</d:href><d:set/>" + TAIL),
    "multiget-gap": ("REPORT", HEAD + b"<d:href>/ab/1.vcf</d:href> <d:href>/ab/2.vcf</d:href>" + TAIL),
    "multiget-entity": ("REPORT", HEAD + b"<d:href>/ab/a&amp;b.vcf</d:href>" + TAIL),
    "multiget-non-ascii": ("REPORT", HEAD + "<d:href>/ab/é.vcf</d:href>".encode() + TAIL),
    "multiget-mixed-kinds": ("REPORT", _mg("/ab/1.vcf", tail=CAL_TAIL)),
    "multiget-too-many": ("REPORT", _mg(*[f"/ab/{i}.vcf" for i in range(dav.MAX_MULTIGET_HREFS + 1)])),
    "sync-collection": (
        "REPORT",
        b'<?xml version="1.0" encoding="utf-8"?>\n<d:sync-collection xmlns:d="DAV:"><d:sync-token/>'
        b"<d:sync-level>1</d:sync-level><d:prop><d:getetag/></d:prop></d:sync-collection>",
    ),
}


@pytest.mark.parametrize("method,body", BAD_BODIES.values(), ids=BAD_BODIES.keys())
def test_unknown_request_body_refused(server, ctx, method, body):
    conn = connect(server, ctx)
    with pytest.raises(DavPolicyError):
        conn.request(method, "/ab/", body)
    assert_nothing_sent(server)


@pytest.mark.parametrize(
    "method,path,body,depth",
    [
        ("PROPFIND", "/", BODY_PRINCIPAL, "0"),
        ("PROPFIND", "/1/principal/", BODY_ADDRESSBOOK_HOME, "0"),
        ("PROPFIND", "/1/principal/", BODY_CALENDAR_HOME, "0"),
        ("PROPFIND", "/1/carddavhome/", BODY_COLLECTIONS, "1"),
        ("PROPFIND", "/1/carddavhome/card/", BODY_ETAGS, "1"),
        ("REPORT", "/ab/", _mg("/ab/1.vcf", "/ab/2.vcf"), None),
        ("REPORT", "/ab/", _mg(*[f"/ab/{i}.vcf" for i in range(dav.MAX_MULTIGET_HREFS)]), None),
        ("REPORT", "/ab%40x/", _mg("/ab@x/1.vcf"), None),
        ("REPORT", "/cal/", _mg("/cal/e.ics", head=CAL_HEAD, tail=CAL_TAIL), None),
    ],
)
def test_every_body_this_package_builds_is_accepted(method, path, body, depth):
    assert check_request(method, path, body) == depth


@pytest.mark.parametrize("headers", [{"Depth": "0"}, {"Authorization": AUTH}, {"X": "1"}, [("Depth", "0")]])
def test_caller_headers_refused(server, ctx, headers):
    conn = connect(server, ctx)
    with pytest.raises(DavPolicyError):
        conn.request("PROPFIND", "/", BODY_PRINCIPAL, headers)
    assert_nothing_sent(server)


def test_chunked_request_refused(server, ctx):
    conn = connect(server, ctx)
    with pytest.raises(DavPolicyError):
        conn.request("PROPFIND", "/", BODY_PRINCIPAL, encode_chunked=True)
    assert_nothing_sent(server)


BAD_PATHS = [
    "",
    "x",
    "x/y",
    "//evil.example/",
    "/a//b",
    "/a/../b",
    "/a/./b",
    "/..",
    "/a/%2e%2e/b",
    "/a/%2E./b",
    "/a%2fb",
    "/a%5cb",
    "/a%00b",
    "/a%0d%0aX:%20y",
    "/a%zz",
    "/a b",
    "/a\r\nX: y",
    "/a\tb",
    "/a?x=1",
    "/a#f",
    "/a&b",
    "/a'b",
    '/a"b',
    "/a<b",
    "/a\\b",
    "/é",
    "http://evil.example/",
    "/" + "a" * dav.MAX_PATH_CHARS,
    None,
    b"/",
]


@pytest.mark.parametrize("path", BAD_PATHS)
def test_unsafe_path_refused(server, ctx, path):
    with pytest.raises(DavPolicyError):
        check_path(path)
    conn = connect(server, ctx)
    with pytest.raises(DavPolicyError):
        conn.request("PROPFIND", path, BODY_PRINCIPAL)
    assert_nothing_sent(server)


@pytest.mark.parametrize(
    "path",
    ["/", "/.well-known/carddav", "/123/carddavhome/card/", "/dav/me@example.org/Default/", "/a%40b/x;y=1"],
)
def test_plain_paths_accepted(path):
    assert check_path(path) == path


# --- hosts ------------------------------------------------------------------------------

ICLOUD = DAV_PRESETS["icloud"]


@pytest.mark.parametrize(
    "host",
    [
        "contacts.icloud.com",
        "caldav.icloud.com",
        "p07-contacts.icloud.com",
        "p1-caldav.icloud.com",
        "p999-caldav.icloud.com",
    ],
)
def test_icloud_hosts_allowed(host):
    assert ICLOUD.allows(host)


@pytest.mark.parametrize(
    "host",
    [
        "p1000-contacts.icloud.com",
        "P07-contacts.icloud.com",
        "Contacts.icloud.com",
        "contacts.icloud.com.",
        "contacts.icloud.com:443",
        "p07-contacts.icloud.com.evil.example",
        "contacts.icloud.com.evil.example",
        "evil.example",
        "p07-evil.example",
        "xp07-contacts.icloud.com",
        "p07contacts.icloud.com",
        "pa-contacts.icloud.com",
        "p07-p07-contacts.icloud.com",
        "icloud.com",
        "www.icloud.com",
        "imap.mail.me.com",
        "",
        None,
        b"contacts.icloud.com",
        "p\u0663-contacts.icloud.com",  # Arabic-Indic digit three
        "p\uff10\uff17-contacts.icloud.com",  # fullwidth 07
        "p0\u0967-caldav.icloud.com",  # Devanagari digit one
    ],
)
def test_other_hosts_refused(host):
    assert not ICLOUD.allows(host)


def test_partition_hosts_only_where_the_preset_says():
    assert DAV_PRESETS["fastmail"].allows("carddav.fastmail.com")
    assert not DAV_PRESETS["fastmail"].allows("p07-carddav.fastmail.com")
    assert not DAV_PRESETS["yahoo"].allows("p07-caldav.calendar.yahoo.com")


def test_dav_presets_are_a_parallel_table():
    from readonly_safe_mail_mcp.config import PRESETS

    assert set(DAV_PRESETS) <= set(PRESETS) and "gmail" not in DAV_PRESETS
    assert all(isinstance(v, tuple) for v in PRESETS.values())


@pytest.mark.parametrize(
    "host,port",
    [(FOREIGN, 443), ("contacts.dav.example:443", 443), (CONTACTS, 0), (CONTACTS, "443"), (CONTACTS, True)],
)
def test_connection_to_foreign_host_cannot_be_built(ctx, host, port):
    with pytest.raises(DavPolicyError):
        GuardedDAVConnection(host, port, hosts=HOSTS, authorization=AUTH, context=ctx)


def test_unverified_tls_refused(server, ctx):
    import ssl

    loose = ssl.create_default_context()
    loose.check_hostname = False
    loose.verify_mode = ssl.CERT_NONE
    with pytest.raises(DavPolicyError):
        GuardedDAVConnection(CONTACTS, 443, hosts=HOSTS, authorization=AUTH, context=loose)
    with pytest.raises(DavPolicyError):
        DavSource(HOSTS, USER, PASSWORD, ssl_context=loose)


@pytest.mark.parametrize("auth", ["Bearer abc", "Basic a\r\nX: y", "Basic ", "", None])
def test_malformed_authorization_refused(ctx, auth):
    with pytest.raises(DavPolicyError):
        GuardedDAVConnection(CONTACTS, 443, hosts=HOSTS, authorization=auth, context=ctx)


@pytest.mark.parametrize("user,pw", [("me:x@example.org", "pw"), ("", "pw"), ("me@example.org", "p\r\nw")])
def test_unsupported_credentials_refused(ctx, user, pw):
    with pytest.raises(DavPolicyError):
        DavSource(HOSTS, user, pw, ssl_context=ctx)


@pytest.mark.parametrize("attr,value", [("host", FOREIGN), ("port", 443), ("_tunnel_host", FOREIGN)])
def test_connection_retargeted_after_construction_refused(server, ctx, attr, value):
    server.on("PROPFIND", "/", ok_principal())
    conn = connect(server, ctx)
    setattr(conn, attr, value)
    with pytest.raises(DavPolicyError):
        conn.request("PROPFIND", "/", BODY_PRINCIPAL)
    assert_nothing_sent(server)


class _Subclass(GuardedDAVConnection):
    """Passes the constructor's checks, then undoes every guarded primitive."""

    _base = http.client.HTTPSConnection
    putrequest, putheader, endheaders, send, connect = (
        _base.putrequest,
        _base.putheader,
        _base.endheaders,
        _base.send,
        _base.connect,
    )

    def request(self, method, url, body=None, headers=None, *, encode_chunked=False):
        self._base.request(self, method, url, body, headers or {})


UNGUARDED = {
    "plain-https": lambda host, port, **kw: http.client.HTTPSConnection(host, port, context=kw["context"]),
    "plain-http": lambda host, port, **kw: http.client.HTTPConnection(host, port),
    "guarded-subclass": lambda host, port, **kw: _Subclass(host, port, **kw),
}


@pytest.mark.parametrize("factory", UNGUARDED.values(), ids=UNGUARDED.keys())
def test_unguarded_connection_refused_by_source(server, ctx, factory):
    with server.source(ctx, connection_factory=factory) as s:
        with pytest.raises(DavPolicyError):
            s.principal("contacts")
    assert_nothing_sent(server)


def test_source_api_is_narrow():
    public = {n for n in dir(DavSource) if not n.startswith("_")}
    assert public == {"close", "principal", "home", "collections", "etags", "multiget"}


# --- redirects and foreign hosts --------------------------------------------------------


def _foreign_redirects(port):
    return {
        "foreign": f"https://{FOREIGN}:{port}/",
        "foreign-no-path": f"https://{FOREIGN}:{port}",
        "network-path": f"//{FOREIGN}:{port}/",
        "userinfo": f"https://{CONTACTS}@{FOREIGN}:{port}/",
        "suffix": f"https://{CONTACTS}.{FOREIGN}:{port}/",
        "partition-of-foreign": f"https://p07-{FOREIGN}:{port}/",
        "plain-http": f"http://{CONTACTS}:{port}/",
        "other-port": f"https://{CONTACTS}/",
        "relative": "x/y",
        "traversal": f"https://{CONTACTS}:{port}/a/../b",
        "query": f"https://{CONTACTS}:{port}/?x=1",
    }


@pytest.mark.parametrize("kind", list(_foreign_redirects(1)))
@pytest.mark.parametrize("status", [301, 302, 307, 308])
def test_foreign_redirect_refused_and_no_authorization_leaves(server, src, kind, status):
    server.on("PROPFIND", "/", Reply(status, headers=[("Location", _foreign_redirects(server.port)[kind])]))
    server.on("PROPFIND", "/", ok_principal(), host=FOREIGN)  # would answer if the guard let it through
    with pytest.raises(DavPolicyError):
        src.principal("contacts")
    assert [(r.host, r.method, r.path) for r in server.requests] == [(CONTACTS, "PROPFIND", "/")]
    assert all(HOSTS.allows(r.host) for r in server.requests)
    assert server.connections == 1  # no second connection, not even one that sends nothing


@pytest.mark.parametrize("status", [301, 302, 307, 308])
def test_redirect_to_partition_host_followed(server, src, status):
    server.on("PROPFIND", "/", Reply(status, headers=[("Location", server.url(P_CONTACTS, "/x/"))]))
    server.on("PROPFIND", "/x/", Reply(207, multistatus(entry("/x/", fakedav.principal("/1/principal/")))))
    assert src.principal("contacts") == DavLocation(P_CONTACTS, "/1/principal/")
    assert [r.header("Host") for r in server.requests] == [
        f"{CONTACTS}:{server.port}",
        f"{P_CONTACTS}:{server.port}",
    ]
    assert [r.header("Authorization") for r in server.requests] == [AUTH, AUTH]


def test_redirect_loop_stops(server, src):
    server.on("PROPFIND", "/", Reply(301, headers=[("Location", "/")]))
    with pytest.raises(DavError, match="too many redirects"):
        src.principal("contacts")
    assert len(server.requests) == dav.MAX_REDIRECTS + 1


@pytest.mark.parametrize("status", [303, 300, 301])
def test_redirect_without_usable_location_fails(server, src, status):
    server.on("PROPFIND", "/", Reply(status))
    with pytest.raises(DavError):
        src.principal("contacts")
    assert len(server.requests) == 1


def test_principal_on_foreign_host_refused(server, src):
    server.on("PROPFIND", "/", ok_principal(server.url(FOREIGN, "/1/principal/")))
    server.on("PROPFIND", "/1/principal/", Reply(207, b""), host=FOREIGN)
    with pytest.raises(DavPolicyError):
        src.home("contacts")
    assert [r.host for r in server.requests] == [CONTACTS]
    assert server.connections == 1


def test_home_set_on_foreign_host_refused(server, src):
    server.on("PROPFIND", "/", ok_principal("/1/principal/"))
    server.on(
        "PROPFIND",
        "/1/principal/",
        Reply(
            207, multistatus(entry("/1/principal/", fakedav.addressbook_home(server.url(FOREIGN, "/1/h/"))))
        ),
    )
    with pytest.raises(DavPolicyError):
        src.home("contacts")
    assert [r.host for r in server.requests] == [CONTACTS, CONTACTS]
    assert server.connections == 1


def test_listing_entry_on_another_host_refused(server, src):
    home = DavLocation(CONTACTS, "/1/h/")
    server.on("PROPFIND", "/1/h/", Reply(207, multistatus(entry(server.url(P_CONTACTS, "/1/h/ab/")))))
    with pytest.raises(DavError):
        src.collections(home)
    assert [r.host for r in server.requests] == [CONTACTS] and server.connections == 1


def test_multiget_outside_collection_refused_at_source(server, src):
    with pytest.raises(DavPolicyError):
        src.multiget("contacts", DavLocation(CONTACTS, "/ab/"), ["/other/1.vcf"])
    with pytest.raises(DavPolicyError):
        src.multiget("contacts", DavLocation(CONTACTS, "/ab/"), [])
    with pytest.raises(DavPolicyError):
        src.multiget("todo", DavLocation(CONTACTS, "/ab/"), ["/ab/1.vcf"])
    assert_nothing_sent(server)


# --- responses: caps, statuses, XML ----------------------------------------------------

CAP = 2000


def _padded(n: int) -> bytes:
    body = multistatus(entry("/", fakedav.principal("/1/principal/")))
    return body + b" " * (n - len(body))


@pytest.mark.parametrize("chunked", [False, True], ids=["content-length", "chunked"])
def test_oversize_response_is_a_failure(server, ctx, chunked):
    server.on("PROPFIND", "/", Reply(207, _padded(CAP + 1), chunked=chunked))
    with server.source(ctx, max_response_bytes=CAP) as s:
        with pytest.raises(DavError, match="size cap"):
            s.principal("contacts")


@pytest.mark.parametrize("chunked", [False, True], ids=["content-length", "chunked"])
def test_response_at_the_cap_is_read(server, ctx, chunked):
    server.on("PROPFIND", "/", Reply(207, _padded(CAP), chunked=chunked))
    with server.source(ctx, max_response_bytes=CAP) as s:
        assert s.principal("contacts").path == "/1/principal/"


def test_declared_length_over_cap_fails_before_reading(server, ctx):
    server.on("PROPFIND", "/", Reply(207, b"<x/>", declared_length=10**12))
    with server.source(ctx, max_response_bytes=CAP) as s:
        with pytest.raises(DavError, match="size cap"):
            s.principal("contacts")


def test_truncated_response_is_a_failure(server, src):
    body = multistatus(entry("/", fakedav.principal("/1/principal/")))
    server.on("PROPFIND", "/", Reply(207, body, declared_length=len(body) + 100))
    with pytest.raises(DavError, match="truncated"):
        src.principal("contacts")


@pytest.mark.parametrize("cut", [5, 7, 200])
def test_truncated_chunked_response_is_a_failure(server, src, cut):
    body = multistatus(entry("/", fakedav.principal("/1/principal/")))
    server.on("PROPFIND", "/", Reply(207, body, chunked=True, cut=cut))
    with pytest.raises(DavError, match="request failed|well-formed"):
        src.principal("contacts")


@pytest.mark.parametrize(
    "status,exc", [(200, DavError), (404, DavError), (500, DavError), (401, DavAuthError)]
)
def test_non_multistatus_answer_fails(server, src, status, exc):
    server.on("PROPFIND", "/", Reply(status, multistatus(entry("/", fakedav.principal("/1/p/")))))
    with pytest.raises(exc):
        src.principal("contacts")


def test_compressed_response_refused(server, src):
    server.on("PROPFIND", "/", Reply(207, b"\x1f\x8b", headers=[("Content-Encoding", "gzip")]))
    with pytest.raises(DavError, match="content encoding"):
        src.principal("contacts")


def test_failed_connection_is_replaced(server, src):
    body = multistatus(entry("/", fakedav.principal("/1/principal/")))
    server.on("PROPFIND", "/", Reply(207, body, declared_length=len(body) + 1))
    with pytest.raises(DavError):
        src.principal("contacts")
    server.on("PROPFIND", "/", Reply(207, body))
    assert src.principal("contacts").path == "/1/principal/"
    assert server.connections == 2


BASE = DavLocation(CONTACTS, "/ab/")


def parse(body: bytes, port: int = 443):
    return parse_multistatus(body, BASE, HOSTS, port)


BAD_XML = {
    "not-xml": b"not xml",
    "truncated": multistatus(entry("/ab/1.vcf", fakedav.etag('"1"')))[:-5],
    "wrong-root": b'<d:propfind xmlns:d="DAV:"/>',
    "no-namespace": b"<multistatus><response><href>/ab/</href></response></multistatus>",
    "dtd": b'<?xml version="1.0"?><!DOCTYPE d [<!ELEMENT d ANY>]><d:multistatus xmlns:d="DAV:"/>',
    "entity-bomb": (
        b'<?xml version="1.0"?><!DOCTYPE x [<!ENTITY a "aaaaaaaaaa"><!ENTITY b "&a;&a;&a;&a;&a;">]>'
        b'<d:multistatus xmlns:d="DAV:"><d:response><d:href>&b;</d:href></d:response></d:multistatus>'
    ),
    "external-entity": (
        b'<?xml version="1.0"?><!DOCTYPE x [<!ENTITY e SYSTEM "file:///etc/passwd">]>'
        b'<d:multistatus xmlns:d="DAV:"><d:response><d:href>&e;</d:href></d:response></d:multistatus>'
    ),
    "no-href": multistatus("<d:response><d:status>HTTP/1.1 200 OK</d:status></d:response>"),
    "relative-href": multistatus(entry("1.vcf", fakedav.etag('"1"'))),
    "dotdot-href": multistatus(entry("/ab/../x", fakedav.etag('"1"'))),
    "foreign-href": multistatus(entry(f"https://{FOREIGN}/ab/1.vcf", fakedav.etag('"1"'))),
    "bad-status": multistatus(
        "<d:response><d:href>/ab/1.vcf</d:href><d:propstat><d:prop/><d:status>200</d:status></d:propstat></d:response>"
    ),
    "propstat-no-prop": multistatus(
        "<d:response><d:href>/ab/1.vcf</d:href><d:propstat><d:status>HTTP/1.1 200 OK</d:status></d:propstat></d:response>"
    ),
    "neither-status-nor-propstat": multistatus("<d:response><d:href>/ab/1.vcf</d:href></d:response>"),
    "two-hrefs-with-propstat": multistatus(
        "<d:response><d:href>/ab/1.vcf</d:href><d:href>/ab/2.vcf</d:href><d:propstat><d:prop/>"
        "<d:status>HTTP/1.1 200 OK</d:status></d:propstat></d:response>"
    ),
}


@pytest.mark.parametrize("body", BAD_XML.values(), ids=BAD_XML.keys())
def test_malformed_multistatus_raises(body):
    with pytest.raises(DavError):
        parse(body)


def test_parse_reads_plain_values():
    items = parse(
        multistatus(
            entry(
                "/ab/",
                fakedav.displayname("Home & Work"),
                fakedav.resourcetype("d:collection", "card:addressbook"),
                fakedav.ctag("c-1"),
            ),
            entry(
                "https://contacts.dav.example/ab/cal/",
                fakedav.resourcetype("d:collection", "cal:calendar", "cs:shared"),
                fakedav.components("VEVENT", "VTODO"),
                missing=("<cs:getctag/>",),
            ),
            entry("/ab/1.vcf", fakedav.etag('"e1"'), fakedav.address_data(VCARD)),
            '<d:response><d:href>/ab/2.ics</d:href><d:propstat><d:prop><d:getetag>"e2"</d:getetag>'
            "<cal:calendar-data><![CDATA[" + VEVENT + "]]></cal:calendar-data></d:prop>"
            "<d:status>HTTP/1.1 200 OK</d:status></d:propstat></d:response>",
            entry("/ab/3.vcf", missing=("<d:getetag/>",), status=403),
            status_entry("/ab/4.vcf", "/ab/5.vcf"),
            "<d:sync-token>tok</d:sync-token>",
        )
    )
    assert [(i.href, i.status) for i in items] == [
        ("/ab/", 200),
        ("/ab/cal/", 200),
        ("/ab/1.vcf", 200),
        ("/ab/2.ics", 200),
        ("/ab/3.vcf", 403),
        ("/ab/4.vcf", 404),
        ("/ab/5.vcf", 404),
    ]
    home, cal, card, event, denied, gone, _ = items
    assert home.displayname == "Home & Work" and home.ctag == "c-1"
    assert home.resource_types == ("collection", "addressbook")
    assert cal.resource_types == ("collection", "calendar", "{http://calendarserver.org/ns/}shared")
    assert cal.components == ("VEVENT", "VTODO") and cal.ctag is None
    assert card.etag == '"e1"' and card.data == VCARD.replace("\r\n", "\n")
    assert event.etag == '"e2"' and "SUMMARY:Planning" in event.data
    assert denied.etag is None and gone.etag is None and gone.host == CONTACTS


def test_parse_ignores_props_from_failed_propstats():
    (item,) = parse(
        multistatus(
            '<d:response><d:href>/ab/1.vcf</d:href><d:propstat><d:prop><d:getetag>"x"</d:getetag></d:prop>'
            "<d:status>HTTP/1.1 404 Not Found</d:status></d:propstat></d:response>"
        )
    )
    assert item.status == 404 and item.etag is None


def test_absolute_href_with_port_resolves(server):
    (item,) = parse(
        multistatus(entry(f"https://{CONTACTS}:{server.port}/ab/1.vcf", fakedav.etag('"1"'))), server.port
    )
    assert item.location == DavLocation(CONTACTS, "/ab/1.vcf")


@pytest.mark.parametrize(
    "ref,expected",
    [
        ("/x/", DavLocation(CONTACTS, "/x/")),
        (" /x/ ", DavLocation(CONTACTS, "/x/")),
        (f"https://{P_CALDAV}:443/1/calendars/", DavLocation(P_CALDAV, "/1/calendars/")),
        (f"HTTPS://{CALDAV.upper()}/", DavLocation(CALDAV, "/")),
        (f"https://{CALDAV}", DavLocation(CALDAV, "/")),
    ],
)
def test_resolve_allowed(ref, expected):
    assert resolve(ref, CONTACTS, HOSTS, 443) == expected


# --- happy paths against the fake server ------------------------------------------------


def _discovery(server, service="contacts"):
    host, home_prop, body, ppath = (
        (CONTACTS, fakedav.addressbook_home, BODY_ADDRESSBOOK_HOME, "/123/carddavhome/")
        if service == "contacts"
        else (CALDAV, fakedav.calendar_home, BODY_CALENDAR_HOME, "/123/calendars/")
    )
    part = P_CONTACTS if service == "contacts" else P_CALDAV
    server.on("PROPFIND", "/", ok_principal("/123/principal/"), host=host)
    server.on(
        "PROPFIND",
        "/123/principal/",
        Reply(207, multistatus(entry("/123/principal/", home_prop(f"https://{part}:{server.port}{ppath}")))),
        host=host,
    )
    return host, body, DavLocation(part, ppath)


@pytest.mark.parametrize("service", ["contacts", "calendars"])
def test_discovery_finds_home_on_partition_host(server, src, service):
    host, body, home = _discovery(server, service)
    assert src.home(service) == home
    h = f"{host}:{server.port}"
    assert server.received == wire("PROPFIND", "/", h, BODY_PRINCIPAL, "0") + wire(
        "PROPFIND", "/123/principal/", h, body, "0"
    )
    assert server.connections == 1


def test_discovery_without_principal_fails(server, src):
    server.on("PROPFIND", "/", Reply(207, multistatus(entry("/", missing=("<d:current-user-principal/>",)))))
    with pytest.raises(DavError, match="principal"):
        src.principal("contacts")


def test_discovery_without_home_set_fails(server, src):
    server.on("PROPFIND", "/", ok_principal("/1/p/"))
    server.on("PROPFIND", "/1/p/", Reply(207, multistatus(entry("/1/p/"))))
    with pytest.raises(DavError, match="home set"):
        src.home("calendars")


def test_discovery_with_two_entries_fails(server, src):
    server.on("PROPFIND", "/", Reply(207, multistatus(entry("/", fakedav.principal("/1/")), entry("/x/"))))
    with pytest.raises(DavError, match="exactly one"):
        src.principal("contacts")


def test_collections_listing(server, src):
    home = DavLocation(P_CALDAV, "/123/calendars/")
    server.on(
        "PROPFIND",
        "/123/calendars/",
        Reply(
            207,
            multistatus(
                entry("/123/calendars", fakedav.resourcetype("d:collection")),
                entry(
                    "/123/calendars/home/",
                    fakedav.displayname("Home"),
                    fakedav.resourcetype("d:collection", "cal:calendar"),
                    fakedav.ctag("HwoQEgwAAA"),
                    fakedav.components("VEVENT"),
                ),
                entry("/123/calendars/inbox/", fakedav.resourcetype("d:collection", "cal:schedule-inbox")),
            ),
        ),
        host=P_CALDAV,
    )
    items = src.collections(home)
    assert [(i.href, i.displayname, i.ctag, i.components) for i in items] == [
        ("/123/calendars/home/", "Home", "HwoQEgwAAA", ("VEVENT",)),
        ("/123/calendars/inbox/", None, None, ()),
    ]
    assert items[0].resource_types == ("collection", "calendar")
    (req,) = server.requests
    assert (req.method, req.header("Depth"), req.body) == ("PROPFIND", "1", BODY_COLLECTIONS)


def test_etag_listing(server, src):
    col = DavLocation(P_CONTACTS, "/123/carddavhome/card/")
    server.on(
        "PROPFIND",
        col.path,
        Reply(
            207,
            multistatus(
                entry(col.path, missing=("<d:getetag/>",)),
                entry(col.path + "a.vcf", fakedav.etag('"1"')),
                entry(col.path + "b.vcf", fakedav.etag('"2"')),
            ),
            chunked=True,
        ),
    )
    items = src.etags(col)
    assert [(i.href, i.etag, i.status) for i in items] == [
        (col.path + "a.vcf", '"1"', 200),
        (col.path + "b.vcf", '"2"', 200),
    ]
    assert server.received == wire("PROPFIND", col.path, f"{P_CONTACTS}:{server.port}", BODY_ETAGS, "1")


@pytest.mark.parametrize(
    "service,text,make",
    [("contacts", VCARD, fakedav.address_data), ("calendars", VEVENT, fakedav.calendar_data)],
)
def test_multiget(server, src, service, text, make):
    col = DavLocation(P_CONTACTS, "/123/c/")
    server.on("REPORT", col.path, fakedav.multiget_reply({"/123/c/a": ('"1"', text)}, make))
    items = src.multiget(service, col, ["/123/c/a", "/123/c/gone"])
    assert [(i.href, i.status, i.etag) for i in items] == [
        ("/123/c/a", 200, '"1"'),
        ("/123/c/gone", 404, None),
    ]
    assert items[0].data == text.replace("\r\n", "\n")
    body = multiget_body(service, col.path, ["/123/c/a", "/123/c/gone"])
    assert server.received == wire("REPORT", col.path, f"{P_CONTACTS}:{server.port}", body, None)


def test_whole_flow_reuses_one_connection_per_host(server, src):
    _, _, home = _discovery(server)
    server.on("PROPFIND", home.path, Reply(207, multistatus(entry(home.path), entry(home.path + "card/"))))
    assert src.home("contacts") == home
    assert [i.href for i in src.collections(home)] == [home.path + "card/"]
    assert server.connections == 2
    assert [r.header("Authorization") for r in server.requests] == [AUTH] * 3


# --- review fixes (issue #1, R1) ------------------------------------------------------


@pytest.mark.parametrize("method", ["etags", "collections"])
def test_listing_marked_truncated_on_its_own_entry_is_incomplete(server, src, method):
    """507 on the collection's own entry marks a truncated multistatus: the one child listed
    is not all of them, so the listing must not read as complete."""
    col = DavLocation(CONTACTS, "/ab/")
    server.on(
        "PROPFIND", col.path, Reply(207, multistatus(status_entry(col.path, status=507), entry("/ab/1.vcf")))
    )
    with pytest.raises(dav.DavIncomplete, match="incomplete"):
        getattr(src, method)(col)


def test_listing_own_entry_without_its_properties_is_complete(server, src):
    col = DavLocation(CONTACTS, "/ab/")
    server.on(
        "PROPFIND",
        col.path,
        Reply(207, multistatus(entry(col.path, status=404), entry("/ab/1.vcf", fakedav.etag('"1"')))),
    )
    assert [i.href for i in src.etags(col)] == ["/ab/1.vcf"]


def test_resourcetype_only_from_a_200_propstat():
    gone, empty = parse(
        multistatus(
            entry("/ab/x/", fakedav.displayname("X"), missing=(fakedav.resourcetype("d:collection"),)),
            entry("/ab/y.vcf", fakedav.resourcetype()),
        )
    )
    assert gone.status == 200 and gone.resource_types is None and gone.entry_status is None
    assert empty.resource_types == ()


def test_dripping_response_stops_at_the_deadline(server, src, monkeypatch):
    """A server sending one byte at a time cannot hold the DAV thread past the deadline:
    read(n) would wait for n bytes or the end before the deadline is checked again."""
    monkeypatch.setattr(dav, "MAX_RESPONSE_SECONDS", 0.3)
    server.on("PROPFIND", "/", Reply(207, _padded(400), drip=0.01))
    t0 = time.monotonic()
    with pytest.raises(DavError, match="took too long"):
        src.principal("contacts")
    assert time.monotonic() - t0 < 2  # the whole body would take 4 s


@pytest.mark.parametrize("name,encoded", [("a&b.vcf", "a%26b.vcf"), ("o'neil.vcf", "o%27neil.vcf")])
def test_href_with_ampersand_or_apostrophe_is_read(server, src, name, encoded):
    """Both are legal unencoded in a path; they are percent-encoded, so the guard's path
    check and the XML-safe multiget body still hold."""
    col = DavLocation(CONTACTS, "/ab/")
    assert resolve("/ab/" + name, CONTACTS, HOSTS, 443) == DavLocation(CONTACTS, "/ab/" + encoded)
    server.on(
        "PROPFIND",
        col.path,
        Reply(207, multistatus(entry(col.path), entry("/ab/" + name, fakedav.etag('"1"')))),
    )
    [item] = src.etags(col)
    assert item.href == "/ab/" + encoded
    body = multiget_body("contacts", col.path, [item.href])
    assert check_request("REPORT", col.path, body) is None and b"&" not in body and b"'" not in body
    server.on(
        "REPORT",
        col.path,
        Reply(207, multistatus(entry("/ab/" + name, fakedav.etag('"1"'), fakedav.address_data(VCARD)))),
    )
    [got] = src.multiget("contacts", col, [item.href])
    assert (got.href, got.status) == ("/ab/" + encoded, 200)


def test_over_the_cap_is_its_own_error(server, ctx):
    server.on("PROPFIND", "/", Reply(207, _padded(CAP + 1)))
    with server.source(ctx, max_response_bytes=CAP) as s, pytest.raises(dav.DavTooLarge):
        s.principal("contacts")
    server.on("PROPFIND", "/", Reply(207, b"<x/>", declared_length=10**12))
    with server.source(ctx, max_response_bytes=CAP) as s, pytest.raises(dav.DavTooLarge):
        s.principal("contacts")


def test_hosts_base_names_the_partition_hosts_of_one_provider():
    assert HOSTS.base(P_CALDAV) == HOSTS.base(CALDAV) == CALDAV
    assert HOSTS.base(P_CONTACTS) == CONTACTS and HOSTS.base(FOREIGN) is None and HOSTS.base(None) is None


# --- review fixes (issue #1, R2): ASCII-only patterns, exact str types --------------------------


@pytest.mark.parametrize("port", ["\u0664\u0664\u0663", "\uff14\uff14\uff13"])
def test_url_port_in_other_digits_refused(port):
    with pytest.raises(DavPolicyError):
        resolve(f"https://{CONTACTS}:{port}/x/", CONTACTS, HOSTS, 443)


def test_status_in_other_digits_is_malformed():
    body = (
        b'<?xml version="1.0"?><d:multistatus xmlns:d="DAV:"><d:response><d:href>/</d:href>'
        + "<d:status>HTTP/1.1 \u0662\u0660\u0660 OK</d:status>".encode()
        + b"</d:response></d:multistatus>"
    )
    with pytest.raises(DavError, match="malformed status"):
        parse_multistatus(body, ORIGIN, HOSTS, 443)


class Sneaky(str):
    """Equal to the text it was built from, but formats and prints as a write."""

    def __str__(self):
        return "DELETE"

    def __format__(self, spec):
        return "DELETE"


class Lenient(str):
    """Equal to everything."""

    def __eq__(self, other):
        return True

    __hash__ = str.__hash__


@pytest.mark.parametrize("cls", [Sneaky, Lenient])
def test_str_subclasses_refused_by_the_checks(cls):
    with pytest.raises(DavPolicyError):
        check_request(cls("PROPFIND"), "/", BODY_PRINCIPAL)
    with pytest.raises(DavPolicyError):
        check_request("PROPFIND", cls("/"), BODY_PRINCIPAL)
    with pytest.raises(DavPolicyError):
        check_path(cls("/"))
    with pytest.raises(DavPolicyError):
        resolve(cls("/x/"), CONTACTS, HOSTS, 443)
    assert not HOSTS.allows(cls(CONTACTS))


@pytest.mark.parametrize(
    "call",
    [
        lambda c: c.request(Sneaky("PROPFIND"), "/", BODY_PRINCIPAL),
        lambda c: c.request("PROPFIND", Sneaky("/"), BODY_PRINCIPAL),
        lambda c: c.request(Lenient("DELETE"), "/", BODY_PRINCIPAL),
    ],
    ids=["method-formats-as-delete", "path-formats-as-delete", "method-equal-to-anything"],
)
def test_str_subclass_request_refused_and_nothing_sent(server, ctx, call):
    conn = connect(server, ctx)
    with pytest.raises(DavPolicyError):
        call(conn)
    assert_nothing_sent(server)


def test_str_subclass_in_a_validated_request_refused(server, ctx):
    conn = connect(server, ctx)
    conn._pending = conn._validate("PROPFIND", "/", BODY_PRINCIPAL, None, False)
    with pytest.raises(DavPolicyError):
        conn.putrequest(Sneaky("PROPFIND"), "/")
    conn._pending = conn._validate("PROPFIND", "/", BODY_PRINCIPAL, None, False)
    conn.putrequest("PROPFIND", "/")
    host = conn._pending.headers[0]
    with pytest.raises(DavPolicyError):
        conn.putheader(Sneaky(host[0]), host[1])
    assert_nothing_sent(server)


def test_str_subclass_authorization_refused(ctx):
    with pytest.raises(DavPolicyError):
        GuardedDAVConnection(CONTACTS, 443, hosts=HOSTS, authorization=Sneaky(AUTH), context=ctx)
