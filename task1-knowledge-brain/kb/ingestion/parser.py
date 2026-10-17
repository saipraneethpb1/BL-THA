"""Turn a help-center article page into clean text plus its screenshots.

Images are replaced by [IMAGE:<id>] markers so later steps know exactly where each
screenshot sat, and each image keeps the text around it and the section it was in.
"""

import hashlib
import re
from dataclasses import dataclass, field

from selectolax.lexbor import LexborHTMLParser as HTMLParser
from selectolax.lexbor import LexborNode as Node

CONTEXT_CHARS = 300
BLOCK_TAGS = {"p", "div", "section", "blockquote", "pre", "br"}
SKIP_TAGS = {"script", "style", "noscript", "iframe"}
_IMAGE_MARKER = re.compile(r"\[IMAGE:[^\]]+\]")


@dataclass
class ParsedImage:
    image_id: str
    position: int
    src: str
    section: str | None
    context_before: str = ""
    context_after: str = ""


@dataclass
class ParsedArticle:
    article_id: str
    url: str
    title: str
    category: str | None
    folder: str | None
    body: str
    images: list[ParsedImage] = field(default_factory=list)

    @property
    def content_hash(self) -> str:
        # Hash what we would index, so cosmetic HTML changes do not trigger re-work.
        payload = "\n".join([self.title, self.body, *(i.src for i in self.images)])
        return hashlib.sha256(payload.encode()).hexdigest()


class ParseError(ValueError):
    pass


class _Renderer:
    def __init__(self) -> None:
        self.parts: list[str] = []
        self.images: list[ParsedImage] = []
        self.section: str | None = None

    def text(self) -> str:
        out = "".join(self.parts)
        out = re.sub(r"[ \t ]+", " ", out)
        out = re.sub(r" *\n *", "\n", out)
        return re.sub(r"\n{3,}", "\n\n", out).strip()

    def walk(self, node: Node) -> None:
        tag = node.tag
        if tag in SKIP_TAGS:
            return
        if tag == "-text":
            self.parts.append(node.text(deep=False) or "")
            return
        if tag in {"h1", "h2", "h3", "h4", "h5", "h6"}:
            heading = node.text(strip=True)
            if heading:
                self.section = heading
                level = min(int(tag[1]) + 1, 4)
                self.parts.append(f"\n\n{'#' * level} {heading}\n\n")
            return
        if tag == "img":
            self._image(node)
            return
        if tag == "li":
            self.parts.append("\n- ")
            self._children(node)
            return
        if tag == "table":
            self._table(node)
            return
        if tag == "a":
            self._children(node)
            href = node.attributes.get("href") or ""
            if href.startswith("http") and "brilliantdirectories.com" in href:
                self.parts.append(f" ({href})")
            return
        block = tag in BLOCK_TAGS
        if block:
            self.parts.append("\n")
        self._children(node)
        if block:
            self.parts.append("\n")

    def _children(self, node: Node) -> None:
        child = node.child
        while child is not None:
            self.walk(child)
            child = child.next

    def _image(self, node: Node) -> None:
        src = node.attributes.get("src") or ""
        if not src.startswith("http"):
            return
        image_id = node.attributes.get("data-id") or hashlib.sha1(src.encode()).hexdigest()[:12]
        self.images.append(
            ParsedImage(
                image_id=image_id,
                position=len(self.images),
                src=src,
                section=self.section,
                context_before=_IMAGE_MARKER.sub("", self.text())[-CONTEXT_CHARS:].strip(),
            )
        )
        self.parts.append(f"\n[IMAGE:{image_id}]\n")

    def _table(self, node: Node) -> None:
        self.parts.append("\n")
        for row in node.css("tr"):
            cells = [c.text(strip=True).replace("|", "/") for c in row.css("th, td")]
            self.parts.append("| " + " | ".join(cells) + " |\n")
        self.parts.append("\n")


def _fill_context_after(body: str, images: list[ParsedImage]) -> None:
    for image in images:
        marker = f"[IMAGE:{image.image_id}]"
        idx = body.find(marker)
        if idx >= 0:
            after = body[idx + len(marker) :]
            image.context_after = _IMAGE_MARKER.sub("", after)[:CONTEXT_CHARS].strip()


def parse_article(html: str, article_id: str, url: str) -> ParsedArticle:
    tree = HTMLParser(html)
    body_node = tree.css_first("article#article-body")
    if body_node is None:
        raise ParseError(f"no article body found for {article_id}")

    title_node = body_node.css_first("h1.post-title")
    title = title_node.text(strip=True) if title_node else ""
    if not title:
        raise ParseError(f"no title found for {article_id}")

    # Breadcrumb: Solution home > Section: <category> > <folder> > <article>
    crumbs = [a.text(strip=True) for a in tree.css("div.breadcrumb a")]
    category = crumbs[1].removeprefix("Section:").strip() if len(crumbs) > 1 else None
    folder = crumbs[2] if len(crumbs) > 2 else None

    # Drop the title and the "Link: ..." line; they are stored separately.
    for junk in body_node.css("h1.post-title, p.the-article-link"):
        junk.decompose()

    renderer = _Renderer()
    renderer.section = title
    renderer.walk(body_node)
    body = renderer.text()
    _fill_context_after(body, renderer.images)

    return ParsedArticle(
        article_id=article_id,
        url=url,
        title=title,
        category=category,
        folder=folder,
        body=body,
        images=renderer.images,
    )
