"""Read a meeting transcript and cut it into chunks a model can handle reliably.

Accepted line formats (anything else is treated as a continuation of the last turn):
    [00:12:31] Priya: We need filters on the search page.
    Priya (00:12:31): We need filters on the search page.
    Priya: We need filters on the search page.

Why chunk at all: a one-hour meeting is 10-15k words. Models skim long inputs and drop
details from the middle, and a single call has a single point of failure. Chunks are
cut at speaker turns (never mid-sentence), with a small overlap so a requirement that
spans the boundary is seen whole in at least one chunk.
"""

import re
from dataclasses import dataclass

from scope.models import Transcript, Turn

TS = r"(\d{1,2}:\d{2}(?::\d{2})?)"
LINE_FORMS = [
    re.compile(rf"^\s*\[{TS}\]\s*([^:\[\]]{{1,40}}?)\s*:\s*(.*)$"),
    re.compile(rf"^\s*([^:\[\]()]{{1,40}}?)\s*\({TS}\)\s*:\s*(.*)$"),
    re.compile(r"^\s*([A-Z][\w .'\-]{0,39}?)\s*:\s*(.*)$"),
]


class TranscriptError(ValueError):
    pass


def parse_transcript(text: str, meeting_id: str, title: str, date: str) -> Transcript:
    turns: list[Turn] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        speaker = ts = said = None
        if m := LINE_FORMS[0].match(line):
            ts, speaker, said = m.group(1), m.group(2), m.group(3)
        elif m := LINE_FORMS[1].match(line):
            speaker, ts, said = m.group(1), m.group(2), m.group(3)
        elif (m := LINE_FORMS[2].match(line)) and not line.lower().startswith(("http", "note")):
            speaker, said = m.group(1), m.group(2)
        if speaker is not None and said is not None:
            turns.append(
                Turn(index=len(turns), speaker=speaker.strip(), timestamp=ts, text=said.strip())
            )
        elif turns:
            turns[-1].text += " " + line
        # text before the first speaker line (titles, notes) is ignored on purpose
    if len(turns) < 2:
        raise TranscriptError("could not find speaker turns; expected lines like 'Name: text'")
    return Transcript(meeting_id=meeting_id, title=title, date=date, turns=turns)


@dataclass(frozen=True)
class Chunk:
    number: int
    turns: list[Turn]

    def render(self) -> str:
        return "\n".join(
            f"[turn {t.index}{' @ ' + t.timestamp if t.timestamp else ''}] {t.speaker}: {t.text}"
            for t in self.turns
        )


def chunk_transcript(t: Transcript, max_chars: int = 8000, overlap_turns: int = 2) -> list[Chunk]:
    chunks: list[Chunk] = []
    current: list[Turn] = []
    size = 0
    for turn in t.turns:
        length = len(turn.text) + len(turn.speaker) + 20
        if current and size + length > max_chars:
            chunks.append(Chunk(len(chunks) + 1, current))
            current = current[-overlap_turns:] if overlap_turns else []
            size = sum(len(x.text) + len(x.speaker) + 20 for x in current)
        current.append(turn)
        size += length
    if current:
        chunks.append(Chunk(len(chunks) + 1, current))
    return chunks


def _norm(text: str) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", text.lower()))


def locate_quote(quote: str, turns: list[Turn]) -> Turn | None:
    """Find the turn a quote came from. Exact (after normalising case and punctuation)
    first; otherwise the turn containing at least 80% of the quote's words in order-free
    form, which tolerates the model dropping a filler word. None means unverified."""
    q = _norm(quote)
    if not q:
        return None
    for turn in turns:
        if q in _norm(turn.text):
            return turn
    words = q.split()
    if len(words) < 4:
        return None
    best, best_share = None, 0.0
    for turn in turns:
        have = set(_norm(turn.text).split())
        share = sum(w in have for w in words) / len(words)
        if share > best_share:
            best, best_share = turn, share
    return best if best_share >= 0.8 else None
