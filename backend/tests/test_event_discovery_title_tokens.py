from app.services.kg.event_discovery import EventDiscoveryService


def test_title_tokens_respect_chinese_word_boundaries_and_remove_source_suffix():
    title = "美股三大指数开盘涨跌不一，存储板块集体上涨｜界面新闻 · 快讯"

    tokens = EventDiscoveryService._title_tokens(title)

    assert {"美股", "指数", "存储", "板块", "上涨"}.issubset(tokens)
    assert not {"涨界", "闻快", "界面", "新闻", "快讯"}.intersection(tokens)


def test_shared_title_terms_are_normal_segmented_words():
    first = EventDiscoveryService._title_tokens(
        "美股三大指数开盘涨跌不一，存储板块集体上涨｜界面新闻 · 快讯"
    )
    second = EventDiscoveryService._title_tokens(
        "美股三大指数涨跌不一，存储芯片板块上涨｜界面新闻 · 快讯"
    )

    shared = first & second

    assert {"美股", "指数", "板块", "上涨"}.issubset(shared)
    assert "涨界" not in shared
    assert "闻快" not in shared


def test_title_tokens_drop_punctuation_only_fragments():
    assert "----" not in EventDiscoveryService._title_tokens("公告 ---- 2026")
