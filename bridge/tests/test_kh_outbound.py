"""keelhaul driver for relay/outbound.py (REQ-SSRF-*).

Runs two ways: under pytest (hypothesis properties, budget via KH_EXAMPLES) and
as a keelhaul driver (`python -m trace`; hypothesis is not defined there, a
seeded random loop of the same property functions runs instead).
"""
from __future__ import annotations

import importlib
import importlib.util
import os
import pathlib
import random
import socket
import sys
import tempfile

_TRACING = sys.gettrace() is not None
_SEED = int(os.environ.get("KH_SEED", "20261020"))
_N = int(os.environ.get("KH_EXAMPLES", "40" if _TRACING else "300"))


def _load(name):
    """relay.<name>: the staged copy next to this driver under keelhaul, else the real one."""
    here = pathlib.Path(__file__).resolve().parent
    for cand in (here, *here.parents):
        if (cand / "relay" / "__init__.py").is_file():
            if str(cand) not in sys.path:
                sys.path.insert(0, str(cand))
            break
    pkg = importlib.import_module("relay")
    staged = here / (name + ".py")
    if not staged.is_file():
        return importlib.import_module("relay." + name)
    spec = importlib.util.spec_from_file_location("relay." + name, staged)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["relay." + name] = mod
    spec.loader.exec_module(mod)
    setattr(pkg, name, mod)
    return mod


outbound = _load("outbound")

import asyncio
import ipaddress

from yarl import URL
from urllib.parse import urlsplit

_HOSTS = [
    "example.org", "EXAMPLE.org", "8.8.8.8", "1.1.1.1", "[2606:4700:4700::1111]",
    "127.0.0.1", "127.1", "0", "0.0.0.0", "2130706433", "0x7f000001", "0x7f.1", "017700000001", "0177.0.0.1",
    "10.43.0.1", "192.168.1.67", "172.16.0.1", "169.254.169.254", "100.64.0.1", "localhost", "localhost.",
    "127.0.0.1.", "[::1]", "[::]", "[::ffff:127.0.0.1]", "[::ffff:7f00:1]", "[0:0:0:0:0:ffff:7f00:1]",
    "[::127.0.0.1]", "[::7f00:1]", "[64:ff9b::7f00:1]", "[64:ff9b::a00:1]", "[64:ff9b::808:808]",
    "[2002:7f00:1::]", "[2002:a00:1::]", "[fd00::1]", "[fe80::1]", "[fe80::1%25eth0]", "[::ffff:10.0.0.1]",
    "127\u30020\u30020\u30021", "127\uff0e0\uff0e0\uff0e1", "\u2460\u2461\u2466.0.0.1", "\uff11\uff12\uff17.0.0.1",
    "\uff4c\uff4f\uff43\uff41\uff4c\uff48\uff4f\uff53\uff54", "%31%32%37.0.0.1", "127%2e0.0.1", "127.0.0.1%00.evil.com",
    "a.b%2540127.0.0.1", "evil.com%2f@127.0.0.1", "xn--", "..", ".", "a..b", "-a.org", "a_b.org", "\u00e9xample.org",
]
_PRE = ["http://", "https://", "HTTP://", "http://u@", "http://u:p@", "http:// ", "https://\t", " http://", "ftp://", "http:/", "//"]
_POST = ["", "/", "/x.png", ":80", ":443", ":8080", ":0", ":65536", "#@evil.org", "?@evil.org", "\\@evil.org", "/\\@127.0.0.1", ":80/x"]


def _random_url(rng):
    host = rng.choice(_HOSTS)
    if rng.random() < 0.3:  # mutate one character
        i = rng.randrange(len(host) + 1)
        host = host[:i] + rng.choice("0123456789.:[]@%\\ \u3002\uff0e\uff11\u2460xX-") + host[i:]
    return rng.choice(_PRE) + host + rng.choice(_POST)


def prop_check_url_sound(url):
    """Whatever check_url accepts, the client connects only to a public name or public IP,
    and the host the check judged is the host yarl/aiohttp will use."""
    try:
        got, host = outbound.check_url(url)
    except outbound.FetchError:
        return
    y = URL(got)  # exactly what aiohttp does with the string it is handed
    assert y.scheme in ("http", "https"), got
    assert y.explicit_port in (None, 80, 443), got
    assert y.user is None and y.password is None, got
    raw = y.raw_host or ""
    assert raw, got
    assert raw.lower().strip(".") == host.lower().strip("."), (got, host, raw)
    try:
        ip = ipaddress.ip_address(raw)
    except ValueError:
        ip = None
    if ip is not None:
        assert outbound.is_public_ip(raw), (got, raw)
    else:
        last = raw.rstrip(".").rsplit(".", 1)[-1].lower()
        # inet_aton-style numerics ("2130706433", "0x7f.1") resolve to IPs without being IP-shaped
        assert not (last.isdigit() or last.startswith("0x")), (got, raw)
        assert all(c.isalnum() or c in "-._" for c in raw), (got, raw)
    assert urlsplit(got).hostname is not None


def prop_is_public_ip_total(text):
    out = outbound.is_public_ip(text)
    assert out in (True, False)
    if out:
        ip = ipaddress.ip_address(text)
        assert ip.is_global and not ip.is_multicast
        # an embedded IPv4 (mapped, compatible, NAT64) must itself be public
        for emb in _embedded_v4(ip):
            assert emb.is_global, (text, emb)


def _embedded_v4(ip):
    if not isinstance(ip, ipaddress.IPv6Address):
        return []
    n = int(ip)
    out = []
    if ip.ipv4_mapped is not None:
        out.append(ipaddress.IPv4Address(n & 0xFFFFFFFF))
    if n >> 32 == 0:  # ::/96 IPv4-compatible
        out.append(ipaddress.IPv4Address(n & 0xFFFFFFFF))
    if n >> 32 == 0x0064FF9B << 64 >> 32 >> 64 << 0 or (n >> 32) == (0x0064FF9B0000000000000000 >> 32):
        out.append(ipaddress.IPv4Address(n & 0xFFFFFFFF))  # 64:ff9b::/96 NAT64
    if n >> 112 == 0x2002:  # 6to4
        out.append(ipaddress.IPv4Address((n >> 80) & 0xFFFFFFFF))
    return out


def _random_ip(rng):
    kind = rng.randrange(6)
    if kind == 0:
        return str(ipaddress.IPv4Address(rng.getrandbits(32)))
    if kind == 1:
        return str(ipaddress.IPv6Address(rng.getrandbits(128)))
    v4 = rng.getrandbits(32)
    if rng.random() < 0.5:
        v4 = rng.choice([0x7F000001, 0x0A2B0001, 0xC0A80143, 0xA9FEA9FE, 0x64400001, 0, 0xFFFFFFFF, 0x08080808])
    prefix = {2: 0xFFFF00000000, 3: 0x0064FF9B << 96, 4: 0x20020000 << 96, 5: 0}[kind]
    if kind == 4:
        return str(ipaddress.IPv6Address((0x2002 << 112) | (v4 << 80)))
    return str(ipaddress.IPv6Address(prefix | v4))


# -- resolver, redirects, size cap with a scripted session (no network) -------


class _Resp:
    def __init__(self, status, headers=None, chunks=(), url="http://example.org/", declared=None):
        self.status, self.headers = status, headers or {}
        self._chunks, self.url = list(chunks), URL(url)
        self.content_length = declared
        self.content = self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def iter_chunked(self, n):
        for c in self._chunks:
            yield c


class _Session:
    def __init__(self, script):
        self.script, self.requested = list(script), []

    def get(self, url, **kw):
        self.requested.append(url)
        assert kw.get("allow_redirects") is False
        return self.script.pop(0) if self.script else _Resp(200, chunks=[b"x"], url=url)


def _fetch(session, max_bytes=100):
    return asyncio.run(outbound.fetch_to_tempfile("http://example.org/a", max_bytes=max_bytes, timeout=5, session=session))


def prop_redirect_to_private_is_refused(location):
    s = _Session([_Resp(302, {"Location": location}, url="http://example.org/a")])
    try:
        _fetch(s)
    except outbound.FetchError:
        pass
    else:
        raise AssertionError("followed redirect to " + location)
    assert s.requested == ["http://example.org/a"], s.requested  # the hop was never requested


_BAD_LOCATIONS = [
    "http://127.0.0.1/", "http://127.0.0.1:80/x", "http://[::1]/", "http://[::ffff:127.0.0.1]/", "http://169.254.169.254/latest",
    "http://10.43.0.1/", "http://2130706433/", "http://0x7f.1/", "http://127\u30020\u30020\u30021/", "file:///etc/passwd",
    "http://user:pw@example.org/", "http://example.org:8080/", "//127.0.0.1/x", "http://[64:ff9b::7f00:1]/", "http://[::127.0.0.1]/",
]


def test_redirects_every_hop_is_checked():
    for loc in _BAD_LOCATIONS:
        prop_redirect_to_private_is_refused(loc)
    s = _Session([_Resp(302, {"Location": "/b"}, url="http://example.org/a"), _Resp(200, chunks=[b"hi"], url="http://example.org/b")])
    path, ctype, size = _fetch(s)
    assert size == 2 and s.requested == ["http://example.org/a", "http://example.org/b"]
    os.unlink(path)
    loop = [_Resp(302, {"Location": "/n%d" % i}, url="http://example.org/") for i in range(10)]
    s = _Session(loop)
    try:
        _fetch(s)
    except outbound.FetchError:
        pass
    else:
        raise AssertionError("redirect loop not cut")
    assert len(s.requested) == outbound.MAX_REDIRECTS + 1
    for bad in (_Resp(302, {}, url="http://example.org/"), _Resp(404), _Resp(200, chunks=[])):
        try:
            _fetch(_Session([bad]))
        except outbound.FetchError:
            continue
        raise AssertionError("accepted a bad response")


def test_byte_cap_holds_while_streaming():
    big = _Resp(200, chunks=[b"x" * 60, b"x" * 60], declared=10)  # Content-Length lies
    tmp_before = set(os.listdir(tempfile.gettempdir()))
    try:
        _fetch(_Session([big]), max_bytes=100)
    except outbound.FetchError:
        pass
    else:
        raise AssertionError("cap not enforced")
    assert not [f for f in set(os.listdir(tempfile.gettempdir())) - tmp_before if f.startswith("relay-out-")]
    try:
        _fetch(_Session([_Resp(200, chunks=[b"x"], declared=101)]), max_bytes=100)
    except outbound.FetchError:
        pass
    else:
        raise AssertionError("declared length not enforced")
    path, _c, size = _fetch(_Session([_Resp(200, chunks=[b"x" * 100])]), max_bytes=100)
    assert size == 100
    os.unlink(path)


def test_resolver_refuses_any_non_public_answer(monkeypatch=None):
    """DNS rebinding / multi-A: one private answer among public ones refuses the connect."""
    async def run(answers):
        loop = asyncio.get_running_loop()
        async def fake(host, port, **kw):
            return [(socket.AF_INET6 if ":" in a else socket.AF_INET, socket.SOCK_STREAM, 6, "", (a, port)) for a in answers]
        orig = loop.getaddrinfo
        loop.getaddrinfo = fake
        try:
            return await outbound.PublicOnlyResolver().resolve("rebind.example", 80)
        finally:
            loop.getaddrinfo = orig
    out = asyncio.run(run(["8.8.8.8", "1.1.1.1"]))
    assert [r["host"] for r in out] == ["8.8.8.8", "1.1.1.1"]
    for answers in (["8.8.8.8", "127.0.0.1"], ["127.0.0.1", "8.8.8.8"], ["::ffff:10.0.0.1"], ["64:ff9b::7f00:1"], ["::7f00:1"], ["fd00::1", "8.8.8.8"], []):
        try:
            asyncio.run(run(answers))
        except OSError:
            continue
        raise AssertionError("resolver accepted " + repr(answers))


def test_regression_unicode_dot_ip_literal_reaches_loopback():
    """yarl turns U+3002/fullwidth digits into 127.0.0.1 and aiohttp skips the resolver for literals."""
    for host in ("127\u30020\u30020\u30021", "\u2460\u2461\u2466.0.0.1", "\uff11\uff12\uff17.0.0.1", "127\uff0e0\uff0e0\uff0e1"):
        for url in ("http://" + host + "/x", "https://" + host + ":443/", "http://" + host + ":80/"):
            try:
                outbound.check_url(url)
            except outbound.FetchError:
                continue
            raise AssertionError("accepted " + url)


def test_regression_numeric_host_forms_refused_up_front():
    for host in ("2130706433", "0x7f000001", "0x7f.1", "127.1", "017700000001", "0177.0.0.1", "0", "127.0.0.1."):
        try:
            outbound.check_url("http://" + host + "/")
        except outbound.FetchError:
            continue
        raise AssertionError("accepted " + host)


def test_regression_embedded_ipv4_in_ipv6_is_judged_by_the_ipv4():
    for bad in ("::127.0.0.1", "::7f00:1", "::a00:1", "64:ff9b::7f00:1", "64:ff9b::a00:1", "64:ff9b::a9fe:a9fe", "2002:7f00:1::",
                "::ffff:10.0.0.1", "::ffff:127.0.0.1"):
        assert not outbound.is_public_ip(bad), bad
    assert outbound.is_public_ip("64:ff9b::808:808")  # NAT64 of a public address stays reachable
    assert outbound.is_public_ip("2606:4700:4700::1111")


def test_regression_connect_to_loopback_literal_never_happens():
    """End to end: a real aiohttp server on 127.0.0.1 must not be reachable through a unicode-dot URL."""
    import aiohttp
    from aiohttp import web

    async def scenario():
        hits = []
        app = web.Application()
        async def h(request):
            hits.append(request.path)
            return web.Response(body=b"secret")
        app.router.add_get("/{tail:.*}", h)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        old = outbound.ALLOWED_PORTS
        outbound.ALLOWED_PORTS = (port,)
        try:
            for host in ("127\u30020\u30020\u30021", "\uff11\uff12\uff17.0.0.1", "0x7f.1", "2130706433", "localhost"):
                try:
                    await outbound.fetch_to_tempfile("http://%s:%d/x" % (host, port), max_bytes=100, timeout=3)
                except outbound.FetchError:
                    continue
                raise AssertionError("fetched loopback through " + host)
        finally:
            outbound.ALLOWED_PORTS = old
            await runner.cleanup()
        assert hits == [], hits

    asyncio.run(scenario())


def test_invalid_location_and_zero_timeout_are_fetch_errors():
    for loc in ("http://[bad/", "http://\u0000/", "http://%ff.example/"):
        s = _Session([_Resp(302, {"Location": loc}, url="http://example.org/a")])
        try:
            _fetch(s)
        except outbound.FetchError:
            pass
        else:
            raise AssertionError("followed " + repr(loc))
        assert s.requested == ["http://example.org/a"]
    try:
        asyncio.run(outbound.fetch_to_tempfile("http://example.org/a", max_bytes=10, timeout=-1, session=_Session([])))
    except outbound.FetchError as exc:
        assert "timed out" in str(exc)
    else:
        raise AssertionError("deadline ignored")


class _ErrSession:
    def get(self, url, **kw):
        raise aiohttp_error()


def aiohttp_error():
    import aiohttp
    return aiohttp.ClientConnectionError("boom")


def test_transport_errors_become_fetch_errors_and_leave_no_file():
    before = set(os.listdir(tempfile.gettempdir()))
    try:
        _fetch(_ErrSession())
    except outbound.FetchError as exc:
        assert "ClientConnectionError" in str(exc)
    else:
        raise AssertionError("transport error escaped")
    assert not [f for f in set(os.listdir(tempfile.gettempdir())) - before if f.startswith("relay-out-")]



def test_guess_name_and_basics():
    assert outbound.guess_name("https://x.org/a/b/cat%20pic.jpg?x=1") == "cat pic.jpg"
    assert outbound.guess_name("https://x.org/") == "file"
    for ok in ("8.8.8.8", "2606:4700:4700::1111"):
        assert outbound.is_public_ip(ok)
    for bad in ("nope", "", "127.0.0.1", "::1", "224.0.0.1", "ff02::1", "100.64.0.1"):
        assert not outbound.is_public_ip(bad)


def test_seeded_properties():
    rng = random.Random(_SEED)
    for _ in range(_N * 5):
        prop_check_url_sound(_random_url(rng))
    for _ in range(_N * 5):
        prop_is_public_ip_total(_random_ip(rng))
    for h in _HOSTS:
        for pre in ("http://", "https://"):
            prop_check_url_sound(pre + h + "/x")
    assert outbound._is_ip("1.2.3.4") and not outbound._is_ip("a")


if not _TRACING:
    try:
        from hypothesis import given, settings
        from hypothesis import strategies as st
    except ImportError:  # pragma: no cover
        given = None
    if given is not None:
        _S = settings(max_examples=_N * 5, deadline=None)

        @_S
        @given(st.randoms(use_true_random=False))
        def test_hyp_check_url(rng):
            prop_check_url_sound(_random_url(rng))

        @_S
        @given(st.text(max_size=40))
        def test_hyp_check_url_text(text):
            prop_check_url_sound("http://" + text)
            prop_check_url_sound(text)

        @_S
        @given(st.randoms(use_true_random=False))
        def test_hyp_ip(rng):
            prop_is_public_ip_total(_random_ip(rng))

        @_S
        @given(st.ip_addresses(v=6) | st.ip_addresses(v=4))
        def test_hyp_ip_any(ip):
            prop_is_public_ip_total(str(ip))



def test_existing_suite_runs_against_this_unit():
    """Keelhaul driver only: replay the repo's own tests against the (staged) unit so coverage counts them.
    Under pytest they already run as part of the suite, so this is a no-op there."""
    if "_pytest" in sys.modules:
        return
    import pytest

    root = next(c for c in pathlib.Path(__file__).resolve().parents if (c / "relay" / "__init__.py").is_file())
    files = [str(root / "tests" / f) for f in ['test_outbound.py']]
    rc = pytest.main(["-q", "-x", "-p", "no:cacheprovider", "--rootdir", str(root), "-c", str(root / "pyproject.toml"), *files])
    assert rc == 0, "existing suite failed under the driver"

if __name__ == "__main__":
    for _name, _fn in sorted(globals().items()):
        if _name.startswith("test_") and callable(_fn):
            _fn()
    print("ok")
