from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from .models import Article

# 采集器 source 名 -> config.yaml 里的 collectors 键
_SOURCE_COLLECTOR = {
    "github": "github",
    "hf_model": "huggingface",
    "hf_paper": "huggingface",
    "arxiv": "arxiv",
    "hackernews": "hackernews",
    "rss": "rss",
    "reddit": "reddit",
}


def source_window_hours(cfg: dict[str, Any]) -> dict[str, int]:
    collectors = cfg.get("collectors", {})
    hours: dict[str, int] = {}
    for source, key in _SOURCE_COLLECTOR.items():
        hours[source] = int((collectors.get(key) or {}).get("window_hours", 24))
    return hours


def _text(a: Article) -> str:
    return f"{a.title} {a.raw_summary} {' '.join(a.tags)}".lower()


def relevance(a: Article, keywords: dict[str, list[str]]) -> int:
    """关键词命中数（去重后计）。用于成本为零的本地粗筛。"""
    text = _text(a)
    hits = set()
    for group in keywords.values():
        for kw in group:
            k = kw.lower()
            if k and k in text:
                hits.add(k)
    return len(hits)


def classify(a: Article, keywords: dict[str, list[str]]) -> str:
    """dry-run 时用于粗略分桶；正式跑由 LLM 重新判定。"""
    if a.source == "github":
        return "github"
    text = _text(a)

    def hit(group: str) -> int:
        return sum(1 for kw in keywords.get(group, []) if kw.lower() in text)

    if a.source == "hf_model":
        # 只有 HF 上的模型权重才算「本地可部署」
        return "local_model" if (a.extra.get("params_b") or 99) <= 32 else "frontier"
    if hit("agent") >= 1 and hit("agent") >= hit("frontier"):
        return "agent"
    if hit("frontier") >= 1:
        return "frontier"
    return "news"


def dedupe(articles: list[Article], seen_ids: set[str], seen_days: int = 3) -> list[Article]:
    """按 id 和 url 去重，并排除最近 N 天已推送过的条目。"""
    out: list[Article] = []
    ids: set[str] = set()
    urls: set[str] = set()
    for a in articles:
        if a.id in ids or a.url in urls:
            continue
        if a.id in seen_ids:
            continue
        ids.add(a.id)
        urls.add(a.url)
        out.append(a)
    return out


def within_window(
    articles: list[Article],
    end: datetime,
    per_source_hours: dict[str, int] | None = None,
    default_hours: int = 24,
) -> list[Article]:
    """按源各自的回看窗口过滤。

    论文/模型发布有延迟，统一用 24h 会漏掉；每个源可在 config 里单独配。
    """
    hours = per_source_hours or {}
    out: list[Article] = []
    for a in articles:
        span = timedelta(hours=hours.get(a.source, default_hours))
        if end - span <= a.published_at <= end:
            out.append(a)
    return out


def prefilter(
    articles: list[Article],
    start: datetime,
    end: datetime,
    cfg: dict[str, Any],
    seen_ids: set[str] | None = None,
) -> list[Article]:
    """去重 -> 时间窗 -> 关键词粗筛 -> 按热度排序 -> 截断。

    这一步之后才会进入 LLM，直接决定 token 成本。
    """
    pf = cfg.get("prefilter", {})
    keywords = cfg.get("keywords", {})

    items = dedupe(articles, seen_ids or set())
    items = within_window(items, end, source_window_hours(cfg))

    # 垂类源（GitHub / RSS / arXiv）天然相关，不做关键词过滤；
    # 只对泛来源（HN / Reddit / 通用模型榜）过滤，避免噪音
    filter_sources = set(pf.get("filter_sources", ["hackernews", "hf_model", "reddit"]))
    min_rel = int(pf.get("min_relevance", 0))
    items = [
        a for a in items
        if a.source not in filter_sources or relevance(a, keywords) >= min_rel
    ]

    # 每个源单独截断，避免某个源淹没其他板块
    max_per_source = int(pf.get("max_per_source", 25))
    by_source: dict[str, list[Article]] = {}
    for a in items:
        by_source.setdefault(a.source, []).append(a)

    capped: list[Article] = []
    for group in by_source.values():
        # 热度相同按 id 兜底，保证每次运行顺序一致
        group.sort(key=lambda x: (-x.heat, x.id))
        capped.extend(group[:max_per_source])

    capped.sort(key=lambda x: (-x.heat, x.id))
    return capped[: int(pf.get("max_total", 90))]
