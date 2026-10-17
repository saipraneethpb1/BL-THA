from kb.ingestion.sitemap import ManifestRow, diff_sitemap, parse_sitemap

SITEMAP = """<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url><loc>https://x.com/support/home</loc><lastmod>2026-10-11</lastmod></url>
  <url><loc>https://x.com/support/solutions/folders/12</loc></url>
  <url><loc>https://x.com/support/solutions/articles/100-old-slug</loc><lastmod>2026-01-01</lastmod></url>
  <url><loc>https://x.com/support/solutions/articles/100-new-slug</loc><lastmod>2026-02-01</lastmod></url>
  <url><loc>https://x.com/support/solutions/articles/200-b</loc><lastmod>2026-03-01</lastmod></url>
  <url><loc>https://x.com/support/solutions/articles/300-c</loc></url>
</urlset>"""


def test_parse_keeps_only_articles_and_dedupes_by_id() -> None:
    entries = parse_sitemap(SITEMAP)
    by_id = {e.article_id: e for e in entries}
    assert set(by_id) == {"100", "200", "300"}
    assert by_id["100"].url.endswith("100-new-slug")  # last one wins
    assert by_id["300"].lastmod is None


def test_diff_classifies_new_changed_unchanged_removed() -> None:
    entries = parse_sitemap(SITEMAP)
    manifest = [
        ManifestRow("100", "2026-01-01", "done"),  # lastmod moved -> changed
        ManifestRow("200", "2026-03-01", "done"),  # same -> unchanged
        ManifestRow("999", "2026-01-01", "done"),  # gone from sitemap -> removed
        ManifestRow("888", None, "removed"),  # already removed -> ignored
    ]
    diff = diff_sitemap(entries, manifest)
    assert [e.article_id for e in diff.new] == ["300"]
    assert [e.article_id for e in diff.changed] == ["100"]
    assert [e.article_id for e in diff.unchanged] == ["200"]
    assert diff.removed == ["999"]


def test_previously_removed_article_counts_as_new_again() -> None:
    entries = parse_sitemap(SITEMAP)
    diff = diff_sitemap(entries, [ManifestRow("300", None, "removed")])
    assert "300" in [e.article_id for e in diff.new]
