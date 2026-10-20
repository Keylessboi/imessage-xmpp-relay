"""Outbound attachments: the owner sends a file by posting its URL (XEP-0066).

The bridge fetches the URL and hands the bytes to the server's
POST /im/attachment, which uploads them to Apple. Because the URL is chosen by
whoever controls the XMPP account, the fetch is treated as an SSRF risk:

* only http/https, only ports 80/443, no credentials in the URL;
* every address the name resolves to must be globally routable (loopback,
  RFC 1918, link-local, CGNAT, cluster/service ranges, IPv6 ULA and the like
  are refused). The check lives in the resolver aiohttp itself connects
  through, so a DNS answer cannot change between the check and the connect, and
  it applies to every redirect hop; literal IPs are checked up front (and on
  every hop) because the resolver is not consulted for them;
* at most 3 redirects, each hop re-validated before it is requested, a hard byte cap enforced while streaming (not just from
  Content-Length), and an overall time limit;
* the body goes to a temporary file, never into memory.
"""
from __future__ import annotations

import asyncio
import ipaddress
import os
import socket
import tempfile
from typing import Optional
from urllib.parse import unquote, urlsplit

import aiohttp
from aiohttp.abc import AbstractResolver
from yarl import URL

from .logsetup import get_logger

log = get_logger("relay.outbound")

ALLOWED_PORTS = (80, 443)
MAX_REDIRECTS = 3


class FetchError(Exception):
    """The URL was refused or could not be fetched (message is safe to show the owner)."""


def is_public_ip(value: str) -> bool:
    try:
        ip = ipaddress.ip_address(value)
    except ValueError:
        return False
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    # is_global already excludes private, loopback, link-local, reserved and
    # CGNAT (100.64/10); multicast is excluded explicitly.
    return ip.is_global and not ip.is_multicast


def check_url(url: str) -> tuple[str, str]:
    """Validate a URL up front; returns (url, host). Raises FetchError."""
    try:
        parts = urlsplit(url.strip())
        port = parts.port
    except ValueError:
        raise FetchError("not a valid URL") from None
    if parts.scheme not in ("http", "https"):
        raise FetchError("only http and https URLs can be sent")
    host = parts.hostname or ""
    if not host:
        raise FetchError("the URL has no host")
    if parts.username or parts.password:
        raise FetchError("URLs with credentials are refused")
    effective = port or (443 if parts.scheme == "https" else 80)
    if effective not in ALLOWED_PORTS:
        raise FetchError("only ports 80 and 443 are allowed")
    try:
        ipaddress.ip_address(host)
    except ValueError:
        pass  # a name: the resolver checks it at connect time
    else:
        if not is_public_ip(host):
            raise FetchError("that address is not publicly routable")
    return url.strip(), host


class PublicOnlyResolver(AbstractResolver):
    """Resolves like the default resolver but refuses any non-public answer."""

    async def resolve(self, host, port=0, family=socket.AF_INET):
        loop = asyncio.get_running_loop()
        infos = await loop.getaddrinfo(host, port, family=socket.AF_UNSPEC, type=socket.SOCK_STREAM)
        out = []
        for fam, _t, _p, _c, sockaddr in infos:
            addr = sockaddr[0]
            if not is_public_ip(addr):
                raise OSError("refusing to connect to a non-public address")
            out.append({
                "hostname": host, "host": addr, "port": port,
                "family": fam, "proto": 0, "flags": socket.AI_NUMERICHOST,
            })
        if not out:
            raise OSError("no addresses")
        return out

    async def close(self) -> None:
        return None


def guess_name(url: str, content_disposition: Optional[str] = None) -> str:
    """A display name from the URL's last path segment (the server sanitises it again)."""
    path = unquote(urlsplit(url).path)
    base = path.rsplit("/", 1)[-1]
    return base or "file"


async def fetch_to_tempfile(
    url: str,
    *,
    max_bytes: int,
    timeout: float,
    session: Optional[aiohttp.ClientSession] = None,
) -> tuple[str, str, int]:
    """Download `url` under the SSRF rules above.

    Returns (temp file path, content-type, size). The caller deletes the file.
    Raises FetchError.
    """
    url, _host = check_url(url)
    own = session is None
    if own:
        session = aiohttp.ClientSession(
            connector=aiohttp.TCPConnector(resolver=PublicOnlyResolver(), use_dns_cache=False),
            trust_env=False,
        )
    fd, path = tempfile.mkstemp(prefix="relay-out-")
    try:
        with os.fdopen(fd, "wb") as out:
            try:
                total, ctype = await _get(session, url, out, max_bytes, timeout)
            except FetchError:
                raise
            except (aiohttp.ClientError, OSError, asyncio.TimeoutError) as exc:
                raise FetchError("could not fetch the URL (" + type(exc).__name__ + ")") from None
        if total == 0:
            raise FetchError("the URL returned an empty file")
        return path, ctype, total
    except BaseException:
        try:
            os.unlink(path)
        except OSError:
            pass
        raise
    finally:
        if own:
            await session.close()


async def _get(session, url: str, out, max_bytes: int, timeout: float) -> tuple[int, str]:
    """GET with redirects followed by hand, so EVERY hop passes check_url
    (aiohttp would otherwise connect to a redirect to a literal IP unchecked)."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    for _hop in range(MAX_REDIRECTS + 1):
        remaining = deadline - loop.time()
        if remaining <= 0:
            raise FetchError("timed out")
        async with session.get(
            url,
            allow_redirects=False,
            timeout=aiohttp.ClientTimeout(total=remaining),
            headers={"User-Agent": "imessage-xmpp-relay"},
        ) as resp:
            if resp.status in (301, 302, 303, 307, 308):
                location = resp.headers.get("Location") or ""
                if not location:
                    raise FetchError("redirect without a location")
                url, _ = check_url(str(resp.url.join(URL(location))))
                continue
            if resp.status != 200:
                raise FetchError("the file server answered HTTP " + str(resp.status))
            declared = resp.content_length
            if declared is not None and declared > max_bytes:
                raise FetchError("the file is larger than the " + str(max_bytes) + " byte limit")
            total = 0
            async for chunk in resp.content.iter_chunked(64 * 1024):
                total += len(chunk)
                if total > max_bytes:
                    raise FetchError("the file is larger than the " + str(max_bytes) + " byte limit")
                out.write(chunk)
            return total, (resp.headers.get("Content-Type") or "").split(";")[0].strip()
    raise FetchError("too many redirects")


def _is_ip(host: str) -> bool:
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        return False
