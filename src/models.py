from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

# 数据来源的展示名。RSS / Reddit 会进一步细化到具体站点或板块。
SOURCE_LABELS = {
    "github": "GitHub",
    "hf_model": "HuggingFace",
    "hf_paper": "HuggingFace Papers",
    "arxiv": "arXiv",
    "hackernews": "Hacker News",
    "reddit": "Reddit",
    "rss": "RSS",
}


def source_label(article: "Article") -> str:
    """返回给人看的数据来源，例如 GitHub / arXiv / 机器之心 / r/LocalLLaMA。"""
    if article.source in ("rss", "reddit") and article.tags:
        return article.tags[0]
    return SOURCE_LABELS.get(article.source, article.source)


class Article(BaseModel):
    """所有采集器的统一输出结构（相当于 Java 里的 DTO）。

    新增数据源时只要返回 List[Article]，下游完全不用改。
    """

    id: str                      # 哈希(source + url)，去重主键
    source: str                  # github / hf_model / hf_paper / arxiv / rss / hackernews / reddit
    title: str
    url: str
    published_at: datetime       # 带时区（UTC）
    raw_summary: str = ""
    heat: int = 0                # stars / likes / points，无则为 0
    tags: list[str] = Field(default_factory=list)
    extra: dict[str, Any] = Field(default_factory=dict)

    def text_for_llm(self, limit: int = 1200) -> str:
        body = (self.raw_summary or "").strip()
        if len(body) > limit:
            body = body[:limit] + "…"
        return body

    def brief(self) -> str:
        """给 LLM 打分阶段的紧凑文本，控制 token 用量。"""
        return f"[{self.id}] ({self.source}) {self.title}\n{self.text_for_llm(300)}"
