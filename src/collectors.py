from __future__ import annotations

import asyncio
import hashlib
import html
import os
import re
import time
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable

import feedparser
import httpx

from .config import env
from .models import Article

USER_AGENT = "ai-daily/0.1 (+https://github.com/; contact: local)"

# 国内网络访问 huggingface.co 通常不通，可设 HF_ENDPOINT=https://hf-mirror.com
HF_ENDPOINT = env("HF_ENDPOINT", "https://huggingface.co")

# Reddit 按 UA 限流：名字太"脚本化"会直接 429。
# 官方建议格式 <platform>:<app>:<version> (by /u/<username>)，改名后能显著降低触发率。
REDDIT_UA = env("REDDIT_USER_AGENT", "windows:ai-daily:v0.1 (by /u/ai_daily_bot)")


# ---------------------------------------------------------------- 工具函数

def _aid(source: str, url: str) -> str:
    return hashlib.sha1(f"{source}:{url}".encode()).hexdigest()[:16]


def _strip_html(text: str | None) -> str:
    if not text:
        return ""
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", html.unescape(text)).strip()


def _to_dt(value: str | None) -> datetime | None:
    """把 API 返回的各种时间格式统一成带时区的 UTC datetime。"""
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _struct_to_dt(parsed: time.struct_time | None) -> datetime | None:
    """feedparser 的 published_parsed 是 UTC 的 struct_time。"""
    if not parsed:
        return None
    return datetime(*parsed[:6], tzinfo=timezone.utc)


def _entry_dt(entry: Any) -> datetime | None:
    for key in ("published_parsed", "updated_parsed", "created_parsed"):
        dt = _struct_to_dt(getattr(entry, key, None))
        if dt:
            return dt
    for key in ("published", "updated", "created"):
        dt = _to_dt(getattr(entry, key, None))
        if dt:
            return dt
    return None


# ---------------------------------------------------------------- GitHub

async def collect_github(cfg: dict, client: httpx.AsyncClient, start: datetime) -> list[Article]:
    headers = {"Accept": "application/vnd.github+json"}
    token = env("GITHUB_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"

    since = start.astimezone(timezone.utc).strftime("%Y-%m-%d")
    per_page = int(cfg.get("per_page", 30))
    min_stars = int(cfg.get("min_stars", 0))
    topics: list[str] = cfg.get("topics", [])

    queries: list[str] = [f'{t} created:>={since}' for t in topics]
    if cfg.get("include_active"):
        queries += [f'{t} stars:>=200 pushed:>={since}' for t in topics]

    articles: list[Article] = []
    for q in queries:
        try:
            r = await client.get(
                "https://api.github.com/search/repositories",
                params={"q": q, "sort": "stars", "order": "desc", "per_page": per_page},
                headers=headers,
                timeout=25,
            )
            r.raise_for_status()
            items = r.json().get("items", [])
        except Exception as exc:  # 单个查询失败不影响整体
            print(f"  [github] 查询失败 ({q}): {exc}")
            continue

        for it in items:
            stars = int(it.get("stargazers_count") or 0)
            if stars < min_stars:
                continue
            created = _to_dt(it.get("created_at"))
            if created is None:
                continue
            license_info = it.get("license") or {}
            articles.append(
                Article(
                    id=_aid("github", it["html_url"]),
                    source="github",
                    title=it["full_name"],
                    url=it["html_url"],
                    published_at=created,
                    raw_summary=it.get("description") or "",
                    heat=stars,
                    tags=(it.get("topics") or [])[:8],
                    extra={
                        "stars": stars,
                        "forks": it.get("forks_count", 0),
                        "language": it.get("language"),
                        "license": license_info.get("spdx_id"),
                        "homepage": it.get("homepage"),
                        "pushed_at": it.get("pushed_at"),
                    },
                )
            )
        # 未认证时 Search API 限 10 次/分钟
        await asyncio.sleep(0.4)

    return articles


# ---------------------------------------------------------------- HuggingFace

def _params_b(detail: dict) -> float | None:
    """从 safetensors 元数据里估算参数量（十亿）。"""
    st = detail.get("safetensors") or {}
    total = st.get("total")
    if isinstance(total, (int, float)) and total > 0:
        return round(total / 1e9, 1)
    return None


def _license(detail: dict) -> str | None:
    card = detail.get("cardData") or {}
    lic = card.get("license")
    if isinstance(lic, list):
        lic = ", ".join(str(x) for x in lic)
    if lic:
        return str(lic)
    for tag in detail.get("tags", []):
        if isinstance(tag, str) and tag.startswith("license:"):
            return tag.split(":", 1)[1]
    return None


async def collect_hf_models(cfg: dict, client: httpx.AsyncClient, start: datetime) -> list[Article]:
    try:
        r = await client.get(
            f"{HF_ENDPOINT}/api/models",
            params={
                "sort": "trendingScore",
                "direction": -1,
                "limit": int(cfg.get("models_limit", 60)),
                "full": "true",
            },
            timeout=30,
        )
        r.raise_for_status()
        models = r.json()
    except Exception as exc:
        print(f"  [hf] 列表失败: {exc}")
        return []

    min_likes = int(cfg.get("min_likes", 0))
    candidates = [m for m in models if int(m.get("likes") or 0) >= min_likes]
    detail_limit = int(cfg.get("detail_limit", 15))
    readme_limit = int(cfg.get("readme_limit", 8))

    articles: list[Article] = []
    for idx, m in enumerate(candidates):
        repo_id = m.get("id") or m.get("modelId")
        if not repo_id:
            continue

        published = _to_dt(m.get("createdAt")) or start
        detail: dict[str, Any] = {}
        readme = ""
        # 只对前 N 个拉详情，控制请求数
        if idx < detail_limit:
            try:
                dr = await client.get(f"{HF_ENDPOINT}/api/models/{repo_id}", timeout=20)
                if dr.status_code == 200:
                    detail = dr.json()
            except Exception:
                pass

        # README 里通常写了显存需求、量化档位、benchmark，是本地模型卡片的信息来源
        if idx < readme_limit:
            try:
                rr = await client.get(
                    f"{HF_ENDPOINT}/{repo_id}/raw/main/README.md",
                    timeout=20,
                    follow_redirects=True,
                )
                if rr.status_code == 200:
                    readme = rr.text[:4000]
            except Exception:
                pass

        merged = {**m, **detail}
        articles.append(
            Article(
                id=_aid("hf_model", f"https://huggingface.co/{repo_id}"),
                source="hf_model",
                title=repo_id,
                url=f"https://huggingface.co/{repo_id}",
                published_at=published,
                raw_summary=" ".join(merged.get("tags") or [])[:600],
                heat=int(merged.get("likes") or 0),
                tags=[t for t in (merged.get("tags") or []) if isinstance(t, str)][:12],
                extra={
                    "params_b": _params_b(merged),
                    "license": _license(merged),
                    "downloads": merged.get("downloads"),
                    "pipeline_tag": merged.get("pipeline_tag"),
                    "created_at": merged.get("createdAt"),
                    "readme": readme,
                },
            )
        )
    return articles


async def collect_hf_papers(cfg: dict, client: httpx.AsyncClient, start: datetime) -> list[Article]:
    try:
        r = await client.get(
            f"{HF_ENDPOINT}/api/daily_papers",
            params={"limit": int(cfg.get("papers_limit", 30))},
            timeout=30,
        )
        r.raise_for_status()
        papers = r.json()
    except Exception as exc:
        print(f"  [hf_paper] 失败: {exc}")
        return []

    articles: list[Article] = []
    for p in papers:
        paper = p.get("paper") or p
        title = paper.get("title") or p.get("title") or "(untitled)"
        summary = paper.get("summary") or ""
        published = _to_dt(paper.get("publishedAt") or p.get("publishedAt")) or start
        pid = paper.get("id")
        url = f"https://huggingface.co/papers/{pid}" if pid else f"https://huggingface.co/papers"
        articles.append(
            Article(
                id=_aid("hf_paper", url),
                source="hf_paper",
                title=title,
                url=url,
                published_at=published,
                raw_summary=_strip_html(summary),
                heat=int(paper.get("upvotes") or 0),
                tags=["paper"],
            )
        )
    return articles


# ---------------------------------------------------------------- arXiv

async def collect_arxiv(cfg: dict, client: httpx.AsyncClient, start: datetime) -> list[Article]:
    cats = cfg.get("categories", ["cs.CL"])
    query = " OR ".join(f"cat:{c}" for c in cats)
    try:
        r = await client.get(
            "https://export.arxiv.org/api/query",
            params={
                "search_query": query,
                "sortBy": "submittedDate",
                "sortOrder": "descending",
                "max_results": int(cfg.get("max_results", 40)),
            },
            timeout=40,
        )
        r.raise_for_status()
        feed = feedparser.parse(r.content)
    except Exception as exc:
        print(f"  [arxiv] 失败: {exc}")
        return []

    articles: list[Article] = []
    for e in feed.entries:
        published = _entry_dt(e) or start
        url = getattr(e, "link", "") or ""
        articles.append(
            Article(
                id=_aid("arxiv", url or e.get("id", "")),
                source="arxiv",
                title=_strip_html(getattr(e, "title", "")),
                url=url,
                published_at=published,
                raw_summary=_strip_html(getattr(e, "summary", "")),
                heat=0,
                tags=[t.get("term", "") for t in getattr(e, "tags", [])][:6],
            )
        )
    return articles


# ---------------------------------------------------------------- HackerNews

async def collect_hackernews(cfg: dict, client: httpx.AsyncClient, start: datetime) -> list[Article]:
    since_ts = int(start.timestamp())
    min_points = int(cfg.get("min_points", 50))
    queries = cfg.get("queries", ["LLM"])

    articles: list[Article] = []
    for q in queries:
        try:
            r = await client.get(
                "https://hn.algolia.com/api/v1/search_by_date",
                params={
                    "query": q,
                    "tags": "story",
                    "numericFilters": f"created_at_i>{since_ts},points>{min_points}",
                    "hitsPerPage": 20,
                },
                timeout=25,
            )
            r.raise_for_status()
            hits = r.json().get("hits", [])
        except Exception as exc:
            print(f"  [hackernews] 失败 ({q}): {exc}")
            continue

        for h in hits:
            url = h.get("url") or f"https://news.ycombinator.com/item?id={h.get('objectID')}"
            published = _to_dt(h.get("created_at")) or start
            articles.append(
                Article(
                    id=_aid("hackernews", url),
                    source="hackernews",
                    title=h.get("title") or "(untitled)",
                    url=url,
                    published_at=published,
                    raw_summary=_strip_html(h.get("story_text"))[:400],
                    heat=int(h.get("points") or 0),
                    tags=["hn"],
                    extra={"comments": h.get("num_comments")},
                )
            )
    return articles


# ---------------------------------------------------------------- RSS / Reddit

async def _fetch_feed(
    client: httpx.AsyncClient,
    url: str,
    *,
    headers: dict[str, str] | None = None,
    retries: int = 2,
) -> Any | None:
    """拉取并解析 RSS。

    429 是限流而不是永久失败，退避后重试；其余错误直接放弃，不影响整体。
    """
    for attempt in range(retries + 1):
        try:
            r = await client.get(url, timeout=25, follow_redirects=True, headers=headers)
            if r.status_code == 429:
                wait = 3 * (attempt + 1)
                print(f"  [rss] 429 限流，{wait}s 后重试（第 {attempt + 1} 次）: {url}")
                await asyncio.sleep(wait)
                continue
            r.raise_for_status()
            return feedparser.parse(r.content)
        except Exception as exc:
            print(f"  [rss] 拉取失败 {url}: {exc}")
            return None
    print(f"  [rss] 多次限流后放弃: {url}")
    return None


async def collect_rss(cfg: dict, client: httpx.AsyncClient, start: datetime) -> list[Article]:
    articles: list[Article] = []
    for feed_conf in cfg.get("feeds", []):
        name = feed_conf.get("name", "rss")
        feed = await _fetch_feed(client, feed_conf["url"])
        if not feed:
            continue
        for e in feed.entries[:20]:
            published = _entry_dt(e) or start
            url = getattr(e, "link", "") or ""
            articles.append(
                Article(
                    id=_aid("rss", url or f"{name}:{getattr(e, 'title', '')}"),
                    source="rss",
                    title=_strip_html(getattr(e, "title", "")) or "(untitled)",
                    url=url,
                    published_at=published,
                    raw_summary=_strip_html(getattr(e, "summary", "")),
                    heat=0,
                    tags=[name],
                )
            )
    return articles


async def collect_reddit(cfg: dict, client: httpx.AsyncClient, start: datetime) -> list[Article]:
    articles: list[Article] = []
    delay = float(cfg.get("delay_seconds", 8))
    for sub in cfg.get("subreddits", []):
        url = f"https://www.reddit.com/r/{sub}/top/.rss?t=day"
        feed = await _fetch_feed(client, url, headers={"User-Agent": REDDIT_UA})
        # 板块之间拉开间隔，Reddit 匿名访问的额度很小
        await asyncio.sleep(delay)
        if not feed:
            continue
        for e in feed.entries[:20]:
            published = _entry_dt(e) or start
            link = getattr(e, "link", "") or ""
            articles.append(
                Article(
                    id=_aid("reddit", link),
                    source="reddit",
                    title=_strip_html(getattr(e, "title", "")) or "(untitled)",
                    url=link,
                    published_at=published,
                    raw_summary=_strip_html(getattr(e, "summary", ""))[:800],
                    heat=0,
                    tags=[f"r/{sub}"],
                )
            )
    return articles


# ---------------------------------------------------------------- 汇总

async def collect_all(cfg: dict, start: datetime) -> tuple[list[Article], dict[str, int]]:
    """并发跑所有采集器；单个源失败不影响整体。"""
    c = cfg.get("collectors", {})
    # (任务名, 配置键, 采集函数)
    jobs: list[tuple[str, str, Callable[[dict, httpx.AsyncClient, datetime], Awaitable[list[Article]]]]] = []

    if c.get("github", {}).get("enabled", True):
        jobs.append(("github", "github", collect_github))
    if c.get("huggingface", {}).get("enabled", True):
        jobs.append(("hf_models", "huggingface", collect_hf_models))
        jobs.append(("hf_papers", "huggingface", collect_hf_papers))
    if c.get("arxiv", {}).get("enabled", True):
        jobs.append(("arxiv", "arxiv", collect_arxiv))
    if c.get("hackernews", {}).get("enabled", True):
        jobs.append(("hackernews", "hackernews", collect_hackernews))
    if c.get("rss", {}).get("enabled", True):
        jobs.append(("rss", "rss", collect_rss))
    if c.get("reddit", {}).get("enabled", True):
        jobs.append(("reddit", "reddit", collect_reddit))

    async with httpx.AsyncClient(
        headers={"User-Agent": USER_AGENT}, follow_redirects=True
    ) as client:
        results = await asyncio.gather(
            *[fn(c.get(cfg_key, {}), client, start) for _, cfg_key, fn in jobs],
            return_exceptions=True,
        )

    articles: list[Article] = []
    stat: dict[str, int] = {}
    for (name, _, _), result in zip(jobs, results):
        if isinstance(result, Exception):
            print(f"  [collect] {name} 异常: {result}")
            stat[name] = 0
            continue
        stat[name] = len(result)
        articles.extend(result)
    return articles, stat
