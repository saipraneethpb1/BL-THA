"""Custom sitemaps: checking a URL people type in, and reading any sitemap safely.

The server fetches URLs that come from users, so every request (including each redirect)
is checked against private and internal addresses first. Without that, a "sitemap" at
http://169.254.169.254/ would let anyone read the cloud server's own metadata.
"""

import asyncio
import hashlib
import ipaddress
import socket
import xml.etree.ElementTree as ET
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from urllib.parse import urlsplit

import httpx

MAX_URL_LENGTH = 2048
MAX_SITEMAP_BYTES = 5_000_000

Resolver = Callable[[str], Awaitable[list[str]]]


class SitemapError(ValueError):
    """A sitemap URL that can't be used, with a message meant for the person who typed it."""


async def system_resolver(host: str) -> list[str]:
    infos = await asyncio.get_running_loop().getaddrinfo(host, None, type=socket.SOCK_STREAM)
    return sorted({str(info[4][0]) for info in infos})


def check_url_syntax(raw: str) -> str:
    """Instant checks, before any network request. Returns the cleaned URL."""
    url = raw.strip()
    if not url:
        raise SitemapError("Enter a sitemap URL.")
    if len(url) > MAX_URL_LENGTH:
        raise SitemapError("That URL is too long.")
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https"):
        raise SitemapError("The URL must start with http:// or https://.")
    if not parts.hostname or "." not in parts.hostname:
        raise SitemapError("The URL needs a full domain name, such as example.com.")
    if parts.username or parts.password:
        raise SitemapError("URLs with a username or password aren't allowed.")
    return parts._replace(fragment="").geturl()


class HostGuard:
    """Refuses hosts that resolve to private, loopback, link-local or other non-public
    addresses. Results are cached per host for the life of the guard."""

    def __init__(self, resolver: Resolver = system_resolver) -> None:
        self.resolver = resolver
        self._ok: dict[str, bool] = {}

    async def check(self, host: str) -> None:
        if host not in self._ok:
            try:
                addresses = await self.resolver(host)
            except OSError as exc:
                raise SitemapError(f"The domain {host} couldn't be found.") from exc
            self._ok[host] = bool(addresses) and all(
                ipaddress.ip_address(a.split("%", 1)[0]).is_global for a in addresses
            )
        if not self._ok[host]:
            raise SitemapError(
                f"{host} points to a private or internal address, so it can't be used."
            )

    async def on_request(self, request: httpx.Request) -> None:
        """httpx event hook: runs for the first request and for every redirect."""
        await self.check(request.url.host)


def guarded_client(guard: HostGuard, **kwargs: object) -> httpx.AsyncClient:
    return httpx.AsyncClient(event_hooks={"request": [guard.on_request]}, **kwargs)  # type: ignore[arg-type]


async def fetch_sitemap(client: httpx.AsyncClient, url: str) -> str:
    """Download a sitemap with a size cap. The client must be a guarded_client."""
    try:
        async with client.stream("GET", url, follow_redirects=True) as resp:
            if resp.status_code != 200:
                raise SitemapError(f"The server answered HTTP {resp.status_code}, not a sitemap.")
            chunks: list[bytes] = []
            size = 0
            async for chunk in resp.aiter_bytes():
                size += len(chunk)
                if size > MAX_SITEMAP_BYTES:
                    raise SitemapError("The sitemap is larger than 5 MB.")
                chunks.append(chunk)
            return b"".join(chunks).decode(resp.encoding or "utf-8", errors="replace")
    except httpx.TooManyRedirects as exc:
        raise SitemapError("The URL redirects too many times.") from exc
    except httpx.TimeoutException as exc:
        raise SitemapError("The server took too long to answer.") from exc
    except httpx.TransportError as exc:
        raise SitemapError("The server couldn't be reached.") from exc


@dataclass(frozen=True)
class ParsedSitemap:
    kind: str  # "urlset" (pages) or "sitemapindex" (more sitemaps)
    locs: list[tuple[str, str | None]]  # (url, lastmod)


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def parse_any_sitemap(xml_text: str) -> ParsedSitemap:
    """Read a standard sitemap or sitemap index, with or without the usual namespace."""
    try:
        root = ET.fromstring(xml_text.lstrip("﻿ \r\n\t"))
    except ET.ParseError as exc:
        raise SitemapError("That address doesn't return XML, so it isn't a sitemap.") from exc
    kind = _local(root.tag)
    if kind not in ("urlset", "sitemapindex"):
        raise SitemapError("That XML isn't a sitemap (expected <urlset> or <sitemapindex>).")
    child = "url" if kind == "urlset" else "sitemap"
    locs: list[tuple[str, str | None]] = []
    for item in root:
        if _local(item.tag) != child:
            continue
        loc = lastmod = None
        for field in item:
            name = _local(field.tag)
            if name == "loc":
                loc = (field.text or "").strip()
            elif name == "lastmod":
                lastmod = (field.text or "").strip() or None
        if loc and loc.startswith(("http://", "https://")):
            locs.append((loc, lastmod))
    if not locs:
        raise SitemapError("The sitemap is empty: it lists no pages.")
    return ParsedSitemap(kind, locs)


@dataclass(frozen=True)
class SitemapCheck:
    url: str
    kind: str
    url_count: int


async def validate_sitemap(raw_url: str, guard: HostGuard, timeout_s: float = 15.0) -> SitemapCheck:
    """Full check for a URL someone wants to add: syntax, public host, reachable, real sitemap."""
    url = check_url_syntax(raw_url)
    await guard.check(urlsplit(url).hostname or "")
    async with guarded_client(guard, timeout=timeout_s) as client:
        parsed = parse_any_sitemap(await fetch_sitemap(client, url))
    return SitemapCheck(url, parsed.kind, len(parsed.locs))


def web_article_id(url: str) -> str:
    """Stable id for a page from a custom sitemap (help-center articles keep their own ids)."""
    return "web-" + hashlib.sha1(url.encode()).hexdigest()[:16]
