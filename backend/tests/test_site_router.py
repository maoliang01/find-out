import json

import pytest

from app.services.site_router import SiteStrategyRouter


def test_routes_are_independent_per_url(tmp_path):
    router = SiteStrategyRouter(tmp_path / "routes.json")

    dynamic = router.classify("https://www.jiemian.com/lists/1330kb.html")
    static = router.classify("https://www.aircas.ac.cn/dtxw/kydt/")
    adaptive = router.classify("https://example.com/news/")

    assert dynamic.site_type == "dynamic_js"
    assert dynamic.strategies[0] == "browser"
    assert static.site_type == "static_html"
    assert static.strategies[0] == "http"
    assert adaptive.site_type == "adaptive"
    assert adaptive.strategies[0] == "http"


def test_user_rules_hot_reload_without_recreating_router(tmp_path):
    config_path = tmp_path / "routes.json"
    router = SiteStrategyRouter(config_path)
    assert router.classify("https://news.example.org/list").site_type == "adaptive"

    router.replace_user_rules([
        {
            "name": "example-dynamic",
            "domains": ["example.org"],
            "path_patterns": [r"^/list"],
            "site_type": "dynamic_js",
            "strategies": ["browser", "http"],
            "render_list_if_sparse": True,
            "min_article_links": 2,
        }
    ])

    decision = router.classify("https://news.example.org/list")
    assert decision.rule_name == "example-dynamic"
    assert decision.strategies == ("browser", "http")
    assert decision.render_list_if_sparse is True
    assert decision.min_article_links == 2

    payload = json.loads(config_path.read_text(encoding="utf-8"))
    assert payload["rules"][0]["name"] == "example-dynamic"


def test_invalid_runtime_rule_keeps_previous_valid_config(tmp_path):
    router = SiteStrategyRouter(tmp_path / "routes.json")
    router.replace_user_rules([
        {
            "name": "valid",
            "domains": ["example.org"],
            "site_type": "static_html",
            "strategies": ["http"],
        }
    ])

    with pytest.raises(ValueError):
        router.replace_user_rules([
            {
                "name": "invalid",
                "domains": ["example.org"],
                "site_type": "static_html",
                "strategies": ["unknown"],
            }
        ])

    assert router.classify("https://example.org/article").rule_name == "valid"


def test_html_markers_promote_unknown_site_to_dynamic(tmp_path):
    router = SiteStrategyRouter(tmp_path / "routes.json")
    decision = router.classify(
        "https://unknown.example/news",
        html='<html><body><div id="root"></div><script src="app.js"></script></body></html>',
    )

    assert decision.site_type == "dynamic_js"
    assert decision.strategies[0] == "browser"
    assert decision.render_list_if_sparse is True
