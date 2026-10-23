"""Parse the help-center sitemap and work out what changed since the last run."""

import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass

NS = {"sm": "http://www.sitemaps.org/schemas/sitemap/0.9"}
ARTICLE_RE = re.compile(r"/support/solutions/articles/(\d+)")


@dataclass(frozen=True)
class SitemapEntry:
    article_id: str
    url: str
    lastmod: str | None
    source: str = "help-center"  # or the custom sitemap's URL
    kind: str = "helpcenter"  # which parser reads the page: helpcenter | web


@dataclass(frozen=True)
class ManifestRow:
    article_id: str
    lastmod: str | None
    status: str


@dataclass
class SitemapDiff:
    new: list[SitemapEntry]
    changed: list[SitemapEntry]
    unchanged: list[SitemapEntry]
    removed: list[str]  # article ids no longer in the sitemap (they get quarantined)


def parse_sitemap(xml_text: str) -> list[SitemapEntry]:
    """Return one entry per article. Category and folder pages are skipped:
    their names are read from each article's breadcrumb instead."""
    root = ET.fromstring(xml_text)
    entries: dict[str, SitemapEntry] = {}
    for url_el in root.findall("sm:url", NS):
        loc = (url_el.findtext("sm:loc", default="", namespaces=NS) or "").strip()
        match = ARTICLE_RE.search(loc)
        if not match:
            continue
        lastmod = url_el.findtext("sm:lastmod", default=None, namespaces=NS)
        article_id = match.group(1)
        # The id is stable even when the slug in the URL changes, so dedupe on it.
        entries[article_id] = SitemapEntry(article_id, loc, lastmod.strip() if lastmod else None)
    return list(entries.values())


def diff_sitemap(
    entries: list[SitemapEntry], manifest: list[ManifestRow], keep: frozenset[str] = frozenset()
) -> SitemapDiff:
    """`keep`: ids not to remove even though they're missing (their sitemap failed to load)."""
    known = {row.article_id: row for row in manifest if row.status != "removed"}
    seen: set[str] = set()
    diff = SitemapDiff(new=[], changed=[], unchanged=[], removed=[])
    for entry in entries:
        seen.add(entry.article_id)
        row = known.get(entry.article_id)
        if row is None:
            diff.new.append(entry)
        elif row.lastmod != entry.lastmod or row.status == "quarantined":
            # A quarantined URL that is listed again is fetched again: if it works, it's back.
            diff.changed.append(entry)
        else:
            diff.unchanged.append(entry)
    diff.removed = [
        aid
        for aid, row in known.items()
        if aid not in seen and aid not in keep and row.status != "quarantined"
    ]
    return diff
