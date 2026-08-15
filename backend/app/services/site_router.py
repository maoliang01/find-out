"""Runtime-configurable routing for heterogeneous web sites.

The router is deliberately independent from the scraper implementation.  It
classifies every URL separately and returns an ordered strategy chain.  User
rules live in ``data/crawl_routes.json`` and are reloaded when the file mtime
changes, so adding or adjusting a site rule never requires an application
restart.
"""

from __future__ import annotations

import json
import os
import re
import threading
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlparse


ALLOWED_SITE_TYPES = {
    "static_html",
    "dynamic_js",
    "structured_api",
    "anti_bot",
    "aggregation",
    "adaptive",
}
ALLOWED_STRATEGIES = {"http", "browser", "firecrawl"}


@dataclass(frozen=True)
class RouteRule:
    name: str
    domains: tuple[str, ...]
    site_type: str = "adaptive"
    strategies: tuple[str, ...] = ("http", "browser", "firecrawl")
    path_patterns: tuple[str, ...] = ()
    render_list_if_sparse: bool = False
    min_article_links: int = 5

    def matches(self, host: str, path: str) -> bool:
        domain_match = any(
            host == domain or host.endswith(f".{domain}")
            for domain in self.domains
        )
        if not domain_match:
            return False
        if not self.path_patterns:
            return True
        return any(re.search(pattern, path, re.IGNORECASE) for pattern in self.path_patterns)


@dataclass(frozen=True)
class RouteDecision:
    site_type: str
    strategies: tuple[str, ...]
    reason: str
    rule_name: str = "automatic"
    render_list_if_sparse: bool = False
    min_article_links: int = 5

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["strategies"] = list(self.strategies)
        return data


DEFAULT_RULES = (
    RouteRule(
        name="thepaper-structured",
        domains=("thepaper.cn",),
        site_type="structured_api",
        strategies=("http", "browser", "firecrawl"),
        render_list_if_sparse=True,
    ),
    RouteRule(
        name="dynamic-news",
        domains=("jiemian.com",),
        site_type="dynamic_js",
        strategies=("browser", "http", "firecrawl"),
        render_list_if_sparse=True,
    ),
    RouteRule(
        name="dynamic-government-data",
        domains=("miit.gov.cn",),
        site_type="dynamic_js",
        strategies=("browser", "http", "firecrawl"),
        path_patterns=(r"^/gxsj/",),
        render_list_if_sparse=True,
        min_article_links=3,
    ),
    RouteRule(
        name="social-and-hotlists",
        domains=("weibo.com", "zhihu.com", "tophub.today"),
        site_type="anti_bot",
        strategies=("browser", "firecrawl", "http"),
        render_list_if_sparse=True,
        min_article_links=3,
    ),
)


class SiteStrategyRouter:
    """Classify URLs and hot-reload optional user routing rules."""

    def __init__(self, config_path: str | Path | None = None):
        backend_root = Path(__file__).resolve().parents[2]
        configured = config_path or os.getenv("CRAWL_ROUTE_CONFIG")
        self.config_path = Path(configured) if configured else backend_root / "data" / "crawl_routes.json"
        self._lock = threading.RLock()
        self._mtime_ns: int | None = None
        self._user_rules: tuple[RouteRule, ...] = ()
        self._last_config_error: str | None = None
        self.reload(force=True)

    @staticmethod
    def _normalize_rule(raw: dict[str, Any]) -> RouteRule:
        name = str(raw.get("name") or "custom-rule").strip()
        domains = tuple(
            str(item).strip().lower().lstrip(".")
            for item in raw.get("domains", [])
            if str(item).strip()
        )
        if not domains:
            raise ValueError(f"路由规则 {name!r} 至少需要一个 domains 项")

        site_type = str(raw.get("site_type") or "adaptive").strip()
        if site_type not in ALLOWED_SITE_TYPES:
            raise ValueError(f"路由规则 {name!r} 的 site_type 无效: {site_type}")

        strategies = tuple(str(item).strip() for item in raw.get("strategies", []))
        if not strategies:
            strategies = ("http", "browser", "firecrawl")
        invalid = [item for item in strategies if item not in ALLOWED_STRATEGIES]
        if invalid:
            raise ValueError(f"路由规则 {name!r} 包含未知策略: {invalid}")

        path_patterns = tuple(str(item) for item in raw.get("path_patterns", []) if str(item))
        for pattern in path_patterns:
            re.compile(pattern)

        return RouteRule(
            name=name,
            domains=domains,
            site_type=site_type,
            strategies=tuple(dict.fromkeys(strategies)),
            path_patterns=path_patterns,
            render_list_if_sparse=bool(raw.get("render_list_if_sparse", False)),
            min_article_links=max(1, min(int(raw.get("min_article_links", 5)), 100)),
        )

    def reload(self, force: bool = False) -> bool:
        """Reload user rules if their file changed; keep the last valid config on errors."""
        with self._lock:
            try:
                mtime_ns = self.config_path.stat().st_mtime_ns if self.config_path.exists() else None
                if not force and mtime_ns == self._mtime_ns:
                    return False
                if mtime_ns is None:
                    self._user_rules = ()
                else:
                    payload = json.loads(self.config_path.read_text(encoding="utf-8"))
                    raw_rules = payload.get("rules", []) if isinstance(payload, dict) else []
                    self._user_rules = tuple(self._normalize_rule(item) for item in raw_rules)
                self._mtime_ns = mtime_ns
                self._last_config_error = None
                return True
            except Exception as exc:
                self._last_config_error = str(exc)
                return False

    def classify(self, url: str, html: str = "", cookies: str | None = None) -> RouteDecision:
        self.reload()
        parsed = urlparse(url or "")
        host = (parsed.hostname or "").lower()
        path = parsed.path or "/"

        with self._lock:
            rules = self._user_rules + DEFAULT_RULES
        for rule in rules:
            if rule.matches(host, path):
                strategies = rule.strategies
                if cookies and "browser" in strategies:
                    strategies = ("browser",) + tuple(item for item in strategies if item != "browser")
                return RouteDecision(
                    site_type=rule.site_type,
                    strategies=strategies,
                    reason=f"matched rule: {rule.name}",
                    rule_name=rule.name,
                    render_list_if_sparse=rule.render_list_if_sparse,
                    min_article_links=rule.min_article_links,
                )

        if path.lower().endswith((".json", ".xml", ".rss", ".atom")) or "/api/" in path.lower():
            return RouteDecision(
                site_type="structured_api",
                strategies=("http", "browser", "firecrawl"),
                reason="structured URL pattern",
            )

        html_lower = (html or "").lower()
        dynamic_markers = (
            "__next_data__",
            "webpackjsonp",
            "window.__initial_state__",
            "id=\"__nuxt\"",
            "id=\"root\"",
            "id=\"app\"",
        )
        if html and any(marker in html_lower for marker in dynamic_markers):
            return RouteDecision(
                site_type="dynamic_js",
                strategies=("browser", "http", "firecrawl"),
                reason="JavaScript application markers found",
                render_list_if_sparse=True,
            )

        static_suffixes = (".gov.cn", ".ac.cn", ".edu.cn")
        if host.endswith(static_suffixes):
            return RouteDecision(
                site_type="static_html",
                strategies=("http", "browser", "firecrawl"),
                reason="institutional static-site domain",
                render_list_if_sparse=True,
            )

        return RouteDecision(
            site_type="adaptive",
            strategies=("http", "browser", "firecrawl"),
            reason="no explicit rule; use adaptive fallback",
            render_list_if_sparse=True,
        )

    def get_config(self) -> dict[str, Any]:
        self.reload()
        with self._lock:
            return {
                "config_path": str(self.config_path),
                "hot_reload": True,
                "last_error": self._last_config_error,
                "rules": [asdict(rule) for rule in self._user_rules],
                "built_in_rules": [asdict(rule) for rule in DEFAULT_RULES],
            }

    def replace_user_rules(self, raw_rules: list[dict[str, Any]]) -> dict[str, Any]:
        rules = [self._normalize_rule(item) for item in raw_rules]
        payload = {
            "version": 1,
            "rules": [
                {
                    **asdict(rule),
                    "domains": list(rule.domains),
                    "strategies": list(rule.strategies),
                    "path_patterns": list(rule.path_patterns),
                }
                for rule in rules
            ],
        }
        self.config_path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = self.config_path.with_suffix(f"{self.config_path.suffix}.tmp")
        temp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        temp_path.replace(self.config_path)
        self.reload(force=True)
        return self.get_config()


_router: SiteStrategyRouter | None = None
_router_lock = threading.Lock()


def get_site_strategy_router() -> SiteStrategyRouter:
    global _router
    if _router is None:
        with _router_lock:
            if _router is None:
                _router = SiteStrategyRouter()
    return _router

