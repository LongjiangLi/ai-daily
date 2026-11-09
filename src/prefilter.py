from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
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

# 共用同一个采集器配置的源，可以单独覆盖回看窗口。
# 模型榜是「近期热门」而不是「刚发布」，论文的公告延迟也比模型更久，
# 两者共用一个 72h 窗口会把绝大多数候选挡在门外。
_SOURCE_WINDOW_KEYS = {
    "hf_model": "models_window_hours",
    "hf_paper": "papers_window_hours",
}


def source_window_hours(cfg: dict[str, Any]) -> dict[str, int]:
    collectors = cfg.get("collectors", {})
    hours: dict[str, int] = {}
    for source, key in _SOURCE_COLLECTOR.items():
        conf = collectors.get(key) or {}
        override_key = _SOURCE_WINDOW_KEYS.get(source)
        value = conf.get(override_key) if override_key else None
        if value is None:
            value = conf.get("window_hours", 24)
        hours[source] = int(value)
    return hours


def _text(a: Article) -> str:
    return f"{a.title} {a.raw_summary} {' '.join(a.tags)}".lower()


def _created_at(a: Article) -> datetime | None:
    """GitHub 的建仓时间；窗口判断用的是 pushed_at，这里单独取出来算仓库年龄。"""
    value = a.extra.get("created_at")
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


# 教程/合集/面试题不是「用得上的开源项目」，即使 star 很高也直接排除
_GITHUB_NOISE = (
    "awesome", "interview", "tutorial", "course", "cheatsheet", "roadmap",
    "面试", "教程", "学习路线", "资源合集",
)

# Java 组的额外门槛：topic:agent 在 GitHub 上混了 JVM agent（如 Arthas），
# 只有明显和 AI 相关的 Java 仓库才放进候选池
_AI_TOKENS = {
    "ai", "llm", "llms", "gpt", "gpt4", "gpt5", "o1", "langchain", "langchain4j",
    "spring-ai", "springai", "rag", "mcp", "embedding", "embeddings", "vector",
    "openai", "claude", "gemini", "transformer", "transformers", "inference",
    "prompt", "prompts", "genai", "aigc",
}


def _is_github_noise(a: Article) -> bool:
    text = _text(a)
    return any(word in text for word in _GITHUB_NOISE)


def _ai_related(a: Article) -> bool:
    tokens = set(re.findall(r"[a-z0-9.\-]+", _text(a)))
    return bool(tokens & _AI_TOKENS)


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


def _select_github(
    group: list[Article],
    limit: int,
    mix: dict[str, Any],
    end: datetime,
    stats: dict[str, int] | None = None,
) -> list[Article]:
    """GitHub 候选混合排序：成熟项目 / 高成长新仓库 / Java 技术栈。

    只按 star 排序会让大厂项目占满名额；三类各取一部分，既能读到成熟架构，
    也能看到新项目的成长机会，并兼顾读者的 Java 背景。
    教程合集/面试题等噪音整体排除，Java 组只保留明确和 AI 相关的仓库。
    某一类不足时用剩余条目按 star 补满，避免候选数少于预期。
    """
    mature_n = int(mix.get("mature", 12))
    growth_n = int(mix.get("growth", 8))
    java_n = int(mix.get("java", 5))
    mature_days = int(mix.get("mature_min_age_days", 180))
    growth_min_days = int(mix.get("growth_min_age_days", 7))
    growth_days = int(mix.get("growth_max_age_days", 90))

    # 教程合集、面试题、awesome-list 先整体排除，不占用任何名额
    pool = [a for a in group if not _is_github_noise(a)]

    mature: list[Article] = []
    growth: list[tuple[Article, float]] = []
    java: list[Article] = []
    for a in pool:
        created = _created_at(a)
        age_days = (end - created).total_seconds() / 86400 if created else None
        if age_days is not None and age_days >= mature_days:
            mature.append(a)
        # 太新的仓库多半还是实验品，至少放几天再看成长性
        if age_days is not None and growth_min_days <= age_days <= growth_days:
            growth.append((a, age_days))
        if str(a.extra.get("language") or "").lower() == "java" and _ai_related(a):
            java.append(a)

    mature.sort(key=lambda x: (-x.heat, x.id))
    # 成长性 = 每天涨多少 star，避免只有「建得早所以 star 多」的项目
    growth.sort(key=lambda x: (-(x[0].heat / x[1]), -x[0].heat, x[0].id))
    java.sort(key=lambda x: (-x.heat, x.id))

    picked = mature[:mature_n] + [a for a, _ in growth[:growth_n]] + java[:java_n]
    out: list[Article] = []
    chosen: set[str] = set()
    for a in picked:
        if a.id not in chosen:
            chosen.add(a.id)
            out.append(a)

    # 某一类不够时，用剩余条目按 star 补满
    if len(out) < limit:
        for a in sorted(pool, key=lambda x: (-x.heat, x.id)):
            if a.id not in chosen:
                chosen.add(a.id)
                out.append(a)
                if len(out) >= limit:
                    break

    if stats is not None:
        stats.update(
            github_mature=len(mature),
            github_growth=len(growth),
            github_java=len(java),
        )
    return out[:limit]


def prefilter(
    articles: list[Article],
    start: datetime,
    end: datetime,
    cfg: dict[str, Any],
    seen_ids: set[str] | None = None,
    stats: dict[str, int] | None = None,
) -> list[Article]:
    """去重 -> 时间窗 -> 关键词粗筛 -> 按热度排序 -> 截断。

    这一步之后才会进入 LLM，直接决定 token 成本。
    传入 stats 可拿到每一级的剩余条数，方便排查「为什么只剩几条」。
    """
    pf = cfg.get("prefilter", {})
    keywords = cfg.get("keywords", {})
    seen = seen_ids or set()

    raw = len(articles)
    already_seen = sum(1 for a in articles if a.id in seen)
    items = dedupe(articles, seen)
    after_dedupe = len(items)

    items = within_window(items, end, source_window_hours(cfg))
    after_window = len(items)

    # 垂类源（GitHub / RSS / arXiv）天然相关，不做关键词过滤；
    # 只对泛来源（HN / Reddit / 通用模型榜）过滤，避免噪音
    filter_sources = set(pf.get("filter_sources", ["hackernews", "hf_model", "reddit"]))
    min_rel = int(pf.get("min_relevance", 0))
    items = [
        a for a in items
        if a.source not in filter_sources or relevance(a, keywords) >= min_rel
    ]
    after_keyword = len(items)

    # 每个源单独截断，避免某个源淹没其他板块
    max_per_source = int(pf.get("max_per_source", 25))
    github_mix = pf.get("github_mix") or {}
    by_source: dict[str, list[Article]] = {}
    for a in items:
        by_source.setdefault(a.source, []).append(a)

    capped: list[Article] = []
    for source, group in by_source.items():
        if source == "github" and github_mix:
            capped.extend(_select_github(group, max_per_source, github_mix, end, stats))
            continue
        # 热度相同按 id 兜底，保证每次运行顺序一致
        group.sort(key=lambda x: (-x.heat, x.id))
        capped.extend(group[:max_per_source])

    capped.sort(key=lambda x: (-x.heat, x.id))
    result = capped[: int(pf.get("max_total", 90))]

    if stats is not None:
        stats.update(
            raw=raw,
            already_seen=already_seen,
            after_dedupe=after_dedupe,
            after_window=after_window,
            after_keyword=after_keyword,
            after_cap=len(result),
        )
    return result
