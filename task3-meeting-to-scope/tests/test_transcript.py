import pytest

from scope.models import Turn
from scope.transcript import TranscriptError, chunk_transcript, locate_quote, parse_transcript
from tests.fakes import TRANSCRIPT


def test_parses_bracket_timestamps_and_joins_continuation_lines() -> None:
    t = parse_transcript(TRANSCRIPT, "m1", "Kickoff", "2026-10-10")
    assert len(t.turns) == 7  # the title line before the first speaker is ignored
    assert t.turns[1].speaker == "Priya (Client)"
    assert t.turns[1].timestamp == "00:00:12"
    assert t.turns[1].text.endswith("We get complaints every week.")


def test_parses_other_formats() -> None:
    text = "Ana (00:01:02): First point.\nBob: Second point.\nAna (00:02:00): Third."
    t = parse_transcript(text, "m", "x", "d")
    assert [(x.speaker, x.timestamp) for x in t.turns] == [
        ("Ana", "00:01:02"),
        ("Bob", None),
        ("Ana", "00:02:00"),
    ]


def test_rejects_text_without_speakers() -> None:
    with pytest.raises(TranscriptError):
        parse_transcript("just some notes\nwith no speakers", "m", "x", "d")


def test_chunks_cut_at_turns_with_overlap() -> None:
    t = parse_transcript(TRANSCRIPT, "m1", "Kickoff", "2026-10-10")
    chunks = chunk_transcript(t, max_chars=250, overlap_turns=1)
    assert len(chunks) > 1
    covered = {turn.index for c in chunks for turn in c.turns}
    assert covered == set(range(len(t.turns)))  # nothing lost
    for a, b in zip(chunks, chunks[1:], strict=False):
        assert a.turns[-1].index == b.turns[0].index  # one turn of overlap
    assert "[turn 1 @ 00:00:12] Priya (Client):" in chunks[0].render()


def test_one_long_turn_still_forms_a_chunk() -> None:
    t = parse_transcript("A: " + "word " * 3000 + "\nB: ok", "m", "x", "d")
    chunks = chunk_transcript(t, max_chars=500)
    assert [len(c.turns) for c in chunks][0] >= 1
    assert {x.index for c in chunks for x in c.turns} == {0, 1}


TURNS = [
    Turn(index=0, speaker="A", text="We definitely need a map on the search results, please."),
    Turn(index=1, speaker="B", text="Sure thing."),
]


@pytest.mark.parametrize(
    ("quote", "found"),
    [
        ("we DEFINITELY need a map on the search results", 0),  # case and punctuation
        ("we need a map on the search results please", 0),  # model dropped a filler word
        ("we need a booking calendar for every clinic", None),  # not said
        ("Sure", None),  # too short to trust fuzzily, but exact match works below
    ],
)
def test_locate_quote(quote: str, found: int | None) -> None:
    turn = locate_quote(quote, TURNS)
    if quote == "Sure":
        assert turn is not None and turn.index == 1
    else:
        assert (turn.index if turn else None) == found
