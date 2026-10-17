"""Split an article into retrievable chunks.

Rules, in order:
1. Split at headings first, so a chunk is one topic (one "Step 2: ...").
2. Merge tiny sections into the next one, so a chunk is never just a heading.
3. Split long sections at paragraph breaks, never mid-sentence or mid-table.
4. Every chunk starts with "Title > Section" so it makes sense on its own.
5. Screenshot markers are replaced by their description (when we have one), so text
   that only exists inside an image is searchable, and the chunk remembers its images.
"""

import re
from dataclasses import dataclass, field

HEADING_RE = re.compile(r"^#{2,4} (.+)$", re.MULTILINE)
IMAGE_RE = re.compile(r"\[IMAGE:([^\]]+)\]")


@dataclass(frozen=True)
class ChunkingConfig:
    max_chars: int = 1500
    min_chars: int = 200


DEFAULT_CONFIG = ChunkingConfig()


@dataclass
class Chunk:
    position: int
    section: str
    text: str  # what gets embedded and keyword-indexed
    image_ids: list[str] = field(default_factory=list)


def _sections(title: str, body: str) -> list[tuple[str, str]]:
    """Return (heading, text) pairs. Text before the first heading belongs to the title."""
    out: list[tuple[str, str]] = []
    last_end, heading = 0, title
    for m in HEADING_RE.finditer(body):
        out.append((heading, body[last_end : m.start()].strip()))
        heading, last_end = m.group(1).strip(), m.end()
    out.append((heading, body[last_end:].strip()))
    return [(h, t) for h, t in out if t]


def _split_long(text: str, max_chars: int) -> list[str]:
    if len(text) <= max_chars:
        return [text]
    parts, current = [], ""
    for para in re.split(r"\n{2,}", text):
        if current and len(current) + len(para) + 2 > max_chars:
            parts.append(current)
            current = para
        else:
            current = f"{current}\n\n{para}" if current else para
    if current:
        parts.append(current)
    # A single paragraph longer than max_chars stays whole: splitting it would cut a
    # procedure or a table in half, which hurts answers more than a long chunk does.
    return parts


def _render_images(text: str, descriptions: dict[str, str]) -> tuple[str, list[str]]:
    ids: list[str] = []

    def repl(m: re.Match[str]) -> str:
        image_id = m.group(1)
        ids.append(image_id)
        desc = descriptions.get(image_id)
        return f"[Screenshot: {desc}]" if desc else ""

    return IMAGE_RE.sub(repl, text), ids


def chunk_article(
    title: str,
    body: str,
    image_descriptions: dict[str, str] | None = None,
    config: ChunkingConfig = DEFAULT_CONFIG,
) -> list[Chunk]:
    descriptions = image_descriptions or {}

    # Merge small sections forward so a heading always travels with real content.
    merged: list[tuple[str, str]] = []
    for heading, text in _sections(title, body):
        if merged and len(merged[-1][1]) < config.min_chars:
            prev_heading, prev_text = merged[-1]
            merged[-1] = (prev_heading, f"{prev_text}\n\n## {heading}\n\n{text}")
        else:
            merged.append((heading, text))

    chunks: list[Chunk] = []
    for heading, text in merged:
        prefix = title if heading == title else f"{title} > {heading}"
        # Split while image markers are still in place, so each piece knows its own images.
        for piece in _split_long(text, config.max_chars):
            rendered, image_ids = _render_images(piece, descriptions)
            rendered = re.sub(r"\n{3,}", "\n\n", rendered).strip()
            if not rendered:
                continue
            chunks.append(
                Chunk(
                    position=len(chunks),
                    section=heading,
                    text=f"{prefix}\n\n{rendered}",
                    image_ids=image_ids,
                )
            )
    return chunks
