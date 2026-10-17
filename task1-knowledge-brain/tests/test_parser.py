from pathlib import Path

import pytest

from kb.ingestion.parser import ParseError, parse_article

FIXTURE = Path(__file__).parent / "fixtures" / "article_5000558401.html"


@pytest.fixture(scope="module")
def article():  # type: ignore[no-untyped-def]
    return parse_article(FIXTURE.read_text(encoding="utf-8"), "5000558401", "https://x/a")


def test_title_and_breadcrumb(article) -> None:  # type: ignore[no-untyped-def]
    assert article.title == "How To Import Categories Via CSV File"
    assert article.category == "My Members"
    assert article.folder == "Member Categories"


def test_body_is_clean_text_without_page_chrome(article) -> None:  # type: ignore[no-untyped-def]
    assert "### Step 2: Import The Category Structure" in article.body
    assert "Link:" not in article.body
    assert "<" not in article.body
    assert "For additional assistance" not in article.body  # footer is outside the article


def test_images_keep_position_section_and_context(article) -> None:  # type: ignore[no-untyped-def]
    assert len(article.images) == 4
    first, third = article.images[0], article.images[2]
    assert f"[IMAGE:{first.image_id}]" in article.body
    assert first.section == "Step 1: Create The Category Structure in a CSV file"
    assert third.section == "Step 2: Import The Category Structure"
    assert "Import Categories" in third.context_before
    assert "[IMAGE:" not in third.context_before
    assert third.context_after.startswith("The button will lead")


def test_hash_is_stable_and_content_sensitive(article) -> None:  # type: ignore[no-untyped-def]
    again = parse_article(FIXTURE.read_text(encoding="utf-8"), "5000558401", "https://x/a")
    assert again.content_hash == article.content_hash
    again.body += " edited"
    assert again.content_hash != article.content_hash


def test_non_article_page_raises() -> None:
    with pytest.raises(ParseError):
        parse_article("<html><body><p>nope</p></body></html>", "1", "u")
