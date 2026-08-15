from datetime import date

from app.services.scraper import (
    DateExtractor,
    LIST_METADATA_ONLY_ERROR,
    LIST_METADATA_PLACEHOLDER,
    ScrapedResult,
    WebScraper,
    current_local_date,
    mark_result_as_metadata_only,
)


def make_scraper() -> WebScraper:
    return WebScraper.__new__(WebScraper)


def test_date_prefilter_skips_unrelated_date_mapping():
    scraper = make_scraper()
    links = [
        "https://example.com/article/1",
        "https://example.com/article/2",
    ]

    result = scraper._prefilter_links_by_list_dates(
        links,
        {"https://example.com/hidden/9": "2026-08-01"},
        date(2026, 7, 30),
        date(2026, 8, 5),
    )

    assert result == links


def test_date_prefilter_keeps_undated_links_when_coverage_is_low():
    scraper = make_scraper()
    links = [
        "https://example.com/article/1",
        "https://example.com/article/2",
        "https://example.com/article/3",
        "https://example.com/article/4",
    ]

    result = scraper._prefilter_links_by_list_dates(
        links,
        {
            links[2]: "2026-08-03",
            links[3]: "2026-06-01",
        },
        date(2026, 7, 30),
        date(2026, 8, 5),
    )

    assert result == [links[2], links[0], links[1]]


def test_date_prefilter_is_strict_when_mapping_coverage_is_high():
    scraper = make_scraper()
    links = [
        "https://example.com/article/1",
        "https://example.com/article/2",
        "https://example.com/article/3",
        "https://example.com/article/4",
        "https://example.com/article/5",
    ]
    dates = {
        links[0]: "2026-08-01",
        links[1]: "2026-07-31",
        links[2]: "2026-06-01",
        links[3]: "2026-05-01",
    }

    result = scraper._prefilter_links_by_list_dates(
        links,
        dates,
        date(2026, 7, 30),
        date(2026, 8, 5),
    )

    assert result == links[:2]


def test_date_extractor_accepts_explicit_relative_publish_time():
    """Relative timestamps are explicit publish times on many dynamic news sites."""
    today = current_local_date().isoformat()

    assert DateExtractor.extract_from_html('<span class="time">0分钟前</span>') == today
    assert DateExtractor.extract_from_html('<time>0小时前</time>') == today


def test_article_filter_accepts_explicit_detail_paths_across_categories():
    scraper = make_scraper()
    links = [
        "https://example.com/lists/65.html",
        "https://example.com/channel/finance.html",
        "https://example.com/article/123.html",
        "https://example.com/news/456.shtml",
    ]

    result = scraper._filter_article_links(
        links,
        "https://example.com/lists/regulation.html",
    )

    assert result == [links[2], links[3]]


def test_restricted_detail_is_metadata_only_without_fake_content():
    result = ScrapedResult(
        url="https://example.com/article/1",
        status="anti_bot_blocked",
        content="blocked page",
        html="<html>blocked</html>",
        word_count=12,
    )

    mark_result_as_metadata_only(result, "栏目中可验证的标题")

    assert result.status == "metadata_only"
    assert result.title == "栏目中可验证的标题"
    assert result.content == ""
    assert result.html == ""
    assert result.word_count == 0
    assert result.error_message == LIST_METADATA_ONLY_ERROR


def test_database_guard_rejects_legacy_metadata_placeholder():
    scraper = make_scraper()
    result = ScrapedResult(
        url="https://example.com/article/1",
        status="success",
        content=f"标题\n（{LIST_METADATA_PLACEHOLDER}）",
        word_count=80,
    )

    saved, reason = scraper.save_to_database(result)

    assert saved is False
    assert reason == LIST_METADATA_ONLY_ERROR
