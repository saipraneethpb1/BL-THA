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
from dataclasses import field as dc_field
from urllib.parse import parse_qsl, urlencode, urlsplit

import httpx

from kb.progress import Progress, report

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


async def validate_sitemap(
    raw_url: str, guard: HostGuard, timeout_s: float = 15.0, progress: Progress | None = None
) -> SitemapCheck:
    """Full check for a URL someone wants to add: syntax, public host, reachable, real sitemap."""
    url = check_url_syntax(raw_url)
    host = urlsplit(url).hostname or ""
    await report(progress, "shield", f"Checking that {host} is a public site")
    await guard.check(host)
    await report(progress, "globe", "Downloading the sitemap", url)
    async with guarded_client(guard, timeout=timeout_s) as client:
        text = await fetch_sitemap(client, url)
    await report(progress, "doc", f"Reading the XML ({len(text.encode()) / 1024:,.0f} KB)")
    parsed = parse_any_sitemap(text)
    return SitemapCheck(url, parsed.kind, len(parsed.locs))


def web_article_id(url: str) -> str:
    """Stable id for a page from a custom sitemap (help-center articles keep their own ids)."""
    return "web-" + hashlib.sha1(url.encode()).hexdigest()[:16]


# Query parameters that only track where a click came from: the page is the same without them.
TRACKING_PARAMS = ("utm_", "gclid", "fbclid", "mc_cid", "mc_eid", "ref", "_ga")


def normalize_url(url: str) -> str:
    """One spelling per page, so the same page listed twice (http and https, a trailing
    slash, tracking parameters, a #fragment, upper-case host) is recognised as one."""
    parts = urlsplit(url.strip())
    host = (parts.hostname or "").lower()
    if parts.port and not (
        (parts.scheme == "http" and parts.port == 80)
        or (parts.scheme == "https" and parts.port == 443)
    ):
        host += f":{parts.port}"
    query = urlencode(
        [
            (k, v)
            for k, v in parse_qsl(parts.query, keep_blank_values=True)
            if not k.lower().startswith(TRACKING_PARAMS)
        ]
    )
    path = parts.path or "/"
    if len(path) > 1:
        path = path.rstrip("/")
    # http and https of the same page are the same page.
    return f"https://{host}{path}" + (f"?{query}" if query else "")


@dataclass
class ChildSitemap:
    url: str
    count: int = 0
    first: str | None = None
    last: str | None = None
    error: str | None = None
    depth: int = 1


@dataclass
class SitemapTree:
    """Everything a sitemap (and any sitemaps nested in it) lists."""

    url: str
    kind: str
    locs: list[tuple[str, str | None]] = dc_field(default_factory=list)
    children: list[ChildSitemap] = dc_field(default_factory=list)
    listed_twice: int = 0  # the same page listed more than once (after normalising)
    truncated: str | None = None  # why not every page is included, if so


async def walk_sitemap(
    client: httpx.AsyncClient,
    url: str,
    *,
    max_pages: int,
    max_children: int,
    max_depth: int = 3,
    progress: Progress | None = None,
) -> SitemapTree:
    """Read a sitemap and every sitemap nested in it (an index of indexes too, up to
    `max_depth` levels). Each child is counted with its first and last URL. A child that
    fails is recorded and skipped; the rest still count. Pages listed twice count once."""
    root = parse_any_sitemap(await fetch_sitemap(client, url))
    tree = SitemapTree(url, root.kind)
    seen: set[str] = set()

    def add(locs: list[tuple[str, str | None]]) -> int:
        added = 0
        for loc, lastmod in locs:
            key = normalize_url(loc)
            if key in seen:
                tree.listed_twice += 1
                continue
            if len(tree.locs) >= max_pages:
                tree.truncated = f"stopped at {max_pages:,} pages (this server's limit per sitemap)"
                break
            seen.add(key)
            tree.locs.append((loc, lastmod))
            added += 1
        return added

    if root.kind == "urlset":
        add(root.locs)
        return tree

    queue: list[tuple[str, int]] = [(loc, 1) for loc, _ in root.locs]
    visited = {normalize_url(url)}
    while queue:
        child_url, depth = queue.pop(0)
        if normalize_url(child_url) in visited:
            continue
        visited.add(normalize_url(child_url))
        if len(tree.children) >= max_children:
            tree.truncated = (
                f"read the first {max_children:,} nested sitemaps (this server's limit)"
            )
            break
        child = ChildSitemap(child_url, depth=depth)
        tree.children.append(child)
        try:
            parsed = parse_any_sitemap(await fetch_sitemap(client, child_url))
        except SitemapError as exc:
            child.error = str(exc)
            await report(progress, "warn", f"Skipped a nested sitemap: {exc}", child_url)
            continue
        if parsed.kind == "sitemapindex":
            if depth < max_depth:
                queue.extend((loc, depth + 1) for loc, _ in parsed.locs)
            child.count = 0
            await report(
                progress, "doc", f"Nested index with {len(parsed.locs)} more sitemaps", child_url
            )
            continue
        child.count = len(parsed.locs)
        child.first, child.last = parsed.locs[0][0], parsed.locs[-1][0]
        add(parsed.locs)
        await report(
            progress,
            "doc",
            f"Read sitemap {len(tree.children)} of {len(tree.children) + len(queue)}: {child.count:,} URLs",
            child_url,
        )
        if tree.truncated:
            break
    return tree
