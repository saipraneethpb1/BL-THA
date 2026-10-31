"""Split an article into retrievable chunks.

Rules, in order:
1. Split at headings first, so a chunk is one topic (one "Step 2: ...").
2. Merge tiny sections into the next one, so a chunk is never just a heading.
3. Split long sections at paragraph breaks, never mid-sentence or mid-table.
4. Every chunk starts with "Title > Section" so it makes sense on its own.
5. Screenshot markers are replaced by their description (when we have one), so text
   that only exists inside an image is searchable, and the chunk remembers its images.

Options measured by the retrieval eval (EVALS.md, "Phase 2"):
- `overlap` (default 12%): a piece of a long section starts with the last sentences of
  the piece before it (a share of max_chars), so a step split across two chunks is whole
  in one. Dev: +1 case at recall@5, none broken; held-out: no change. Within noise, and
  in the 10-15% the brief asks for. `overlap=0` reproduces the original chunks exactly.
- `heading_path` (off): the prefix names the parent heading too ("Title > Setup >
  Step 2"). No difference on dev or held-out, so it stays off.
Every chunk records its parent section (the level-2 heading above it) either way.
"""

import hashlib
import re
from dataclasses import asdict, dataclass, field
from typing import Any

HEADING_RE = re.compile(r"^(#{2,4}) (.+)$", re.MULTILINE)
IMAGE_RE = re.compile(r"\[IMAGE:([^\]]+)\]")
SENTENCE_END = re.compile(r"(?<=[.!?:])\s+")


@dataclass(frozen=True)
class ChunkingConfig:
    max_chars: int = 1500
    min_chars: int = 200
    overlap: float = 0.12  # share of max_chars repeated from the previous piece of a section
    heading_path: bool = False

    def describe(self) -> dict[str, Any]:
        return asdict(self)


DEFAULT_CONFIG = ChunkingConfig()


@dataclass
class Chunk:
    position: int
    section: str
    text: str  # what gets embedded and keyword-indexed
    image_ids: list[str] = field(default_factory=list)
    parent: str | None = None  # the level-2 heading this section sits under, if any

    def key(self, article_id: str) -> str:
        """Stable id: the same article, position and text always give the same key, so a
        re-index can tell unchanged chunks (and reuse their vectors) from new ones."""
        digest = hashlib.sha256(self.text.encode()).hexdigest()[:16]
        return f"{article_id}:{self.position}:{digest}"


@dataclass
class _Section:
    heading: str
    text: str
    parent: str | None


def _sections(title: str, body: str) -> list[_Section]:
    """Sections in order. Text before the first heading belongs to the title."""
    out: list[_Section] = []
    last_end, heading, parent, h2 = 0, title, None, None
    for m in HEADING_RE.finditer(body):
        out.append(_Section(heading, body[last_end : m.start()].strip(), parent))
        level, heading, last_end = len(m.group(1)), m.group(2).strip(), m.end()
        if level == 2:
            h2, parent = heading, None
        else:
            parent = h2
    out.append(_Section(heading, body[last_end:].strip(), parent))
    return [s for s in out if s.text]


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


def _tail(text: str, max_chars: int) -> str:
    """The last whole sentences of `text`, at most `max_chars` long, without images
    (a screenshot belongs to the chunk it is in, not to the one after it)."""
    plain = IMAGE_RE.sub("", text).strip()
    out = ""
    for sentence in reversed(SENTENCE_END.split(plain)):
        candidate = f"{sentence} {out}".strip()
        if len(candidate) > max_chars:
            break
        out = candidate
    return out


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
    merged: list[_Section] = []
    for s in _sections(title, body):
        if merged and len(merged[-1].text) < config.min_chars:
            prev = merged[-1]
            merged[-1] = _Section(
                prev.heading, f"{prev.text}\n\n## {s.heading}\n\n{s.text}", prev.parent
            )
        else:
            merged.append(s)

    chunks: list[Chunk] = []
    overlap_chars = int(config.overlap * config.max_chars)
    for s in merged:
        path = [title] if s.heading == title else [title, s.heading]
        if config.heading_path and s.parent and s.parent != s.heading:
            path.insert(1, s.parent)
        prefix = " > ".join(path)
        # Split while image markers are still in place, so each piece knows its own images.
        pieces = _split_long(s.text, config.max_chars)
        for i, piece in enumerate(pieces):
            rendered, image_ids = _render_images(piece, descriptions)
            if overlap_chars and i > 0:
                carried = _tail(pieces[i - 1], overlap_chars)
                if carried:
                    rendered = f"{carried}\n\n{rendered}"
            rendered = re.sub(r"\n{3,}", "\n\n", rendered).strip()
            if not rendered:
                continue
            chunks.append(
                Chunk(
                    position=len(chunks),
                    section=s.heading,
                    text=f"{prefix}\n\n{rendered}",
                    image_ids=image_ids,
                    parent=s.parent,
                )
            )
    return chunks
