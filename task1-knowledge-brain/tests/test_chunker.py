from kb.retrieval.chunker import ChunkingConfig, chunk_article

BODY = """Intro paragraph that explains what this article is about in enough words to stand alone as a chunk of its own, well over the minimum size so it is kept separate from the rest of the article content, with a little extra padding text.

### Step 1

Tiny.

### Step 2

Go to Settings >> Billing and press Save.

[IMAGE:img1]

Then wait for the confirmation."""


def test_splits_at_headings_and_prefixes_title() -> None:
    chunks = chunk_article("Billing Setup", BODY)
    assert chunks[0].text.startswith("Billing Setup\n\nIntro paragraph")
    assert all(c.text.startswith("Billing Setup") for c in chunks)


def test_tiny_section_is_merged_into_the_next() -> None:
    chunks = chunk_article("Billing Setup", BODY)
    sections = [c.section for c in chunks]
    assert sections == ["Billing Setup", "Step 1"]
    assert "## Step 2" in chunks[1].text and "Tiny." in chunks[1].text


def test_image_description_is_inlined_and_image_id_kept() -> None:
    chunks = chunk_article("Billing Setup", BODY, {"img1": "Save button at the bottom right"})
    last = chunks[-1]
    assert "[Screenshot: Save button at the bottom right]" in last.text
    assert last.image_ids == ["img1"]
    assert "[IMAGE:" not in last.text


def test_image_without_description_leaves_no_marker_but_is_tracked() -> None:
    last = chunk_article("Billing Setup", BODY)[-1]
    assert "[IMAGE:" not in last.text and "[Screenshot" not in last.text
    assert last.image_ids == ["img1"]


def test_long_section_splits_at_paragraphs() -> None:
    body = "\n\n".join(f"Paragraph {i} " + "word " * 60 for i in range(10))
    chunks = chunk_article("Long", body, config=ChunkingConfig(max_chars=800, min_chars=100))
    assert len(chunks) > 1
    assert all(len(c.text) < 800 + 50 for c in chunks)
    assert all(c.text.startswith("Long\n\nParagraph") for c in chunks)
