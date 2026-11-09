from __future__ import annotations

from typing import Any

from jinja2 import Environment, FileSystemLoader, select_autoescape

from .config import PROJECT_ROOT

TEMPLATE_DIR = PROJECT_ROOT / "templates"


def render_html(report: dict[str, Any]) -> str:
    env = Environment(
        loader=FileSystemLoader(TEMPLATE_DIR),
        autoescape=select_autoescape(["html", "j2"]),
        trim_blocks=True,
        lstrip_blocks=True,
    )
    template = env.get_template("daily.html.j2")
    return template.render(**report)


def render_plain(report: dict[str, Any]) -> str:
    """纯文本兜底版本，给不支持 HTML 的邮件客户端。"""
    lines = [f"AI 日报 · {report.get('date', '')}", ""]
    for h in report.get("headline", []):
        lines.append(f"- {h}")
    lines.append("")
    for section, key in [("本地模型", "local_models"), ("前沿影响", "frontier")]:
        items = report.get(key) or []
        if items:
            lines.append(f"【{section}】")
            for it in items:
                src = f"[{it['source_label']}] " if it.get("source_label") else ""
                lines.append(f"- {src}{it.get('name', '')}: {it.get('judgement') or it.get('change', '')}")
            lines.append("")
    for section, key in [("开源项目", "github"), ("Agent", "agent"), ("资讯", "news")]:
        items = report.get(key) or []
        if items:
            lines.append(f"【{section}】")
            for it in items:
                src = f"[{it['source_label']}] " if it.get("source_label") else ""
                lines.append(f"- {src}{it.get('title', '')}: {it.get('what', '')}")
            lines.append("")
    lines.append("详情见 HTML 版本。")
    return "\n".join(lines)
