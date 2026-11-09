# AI 日报 · 部署与运维手册

> 最后更新：2026-11-09
> 本文档记录实际部署过程与运维要点，后续复用或迁移时按此操作。

---

## 0. 基本信息

| 项目 | 值 |
|---|---|
| 本地路径 | `E:\pythonProject\ai-daily` |
| 仓库 | https://github.com/LongjiangLi/ai-daily |
| 默认分支 | `main` |
| 推送时间 | 每天 **08:30**（北京） |
| cron | `30 0 * * *`（UTC 00:30） |
| 运行时 | GitHub Actions / ubuntu-latest |
| 单次耗时 | 2~4 分钟（首次约 5 分钟） |
| 月用量 | 约 90 分钟 / 2000 分钟免费额度 |
| LLM 成本 | 约 ¥0.1/天，¥3/月 |

---

## 1. 架构速览

```
GitHub Actions (cron 30 0 * * *)
        │
        ├─① 采集   GitHub / HuggingFace / arXiv / HN / RSS（并发）
        ├─② 粗筛   去重 → 时间窗 → 关键词过滤 → 截断
        ├─③④⑤⑥ LLM 打分分桶 → 三段抽取 → 头条趋势（DeepSeek）
        ├─⑦ 渲染  Jinja2 → HTML 邮件
        └─⑧ 发信  SMTP
                │
                └─ 提交 state.json 回仓库（跨天去重 + 保活）
```

### 目录结构

```
ai-daily/
├── main.py                 主流程
├── config.yaml             数据源、配额、关键词、时间
├── pyproject.toml          依赖（uv）
├── uv.lock                 依赖锁，本地与 CI 一致
├── .python-version         锁定 3.12
├── src/
│   ├── collectors.py       采集器
│   ├── prefilter.py        去重/时间窗/粗筛
│   ├── llm.py              LLM 封装 + 计费 + JSON 修复
│   ├── extractors.py       四段抽取 + 来源标注
│   ├── render.py           Jinja2 渲染
│   ├── mailer.py           SMTP 发信
│   ├── state.py            去重状态
│   ├── models.py           数据模型 + 来源标签
│   └── config.py           配置加载 + 时间窗计算
├── prompts/                5 个 prompt 模板
├── templates/daily.html.j2 邮件模板
└── .github/workflows/daily.yml
```

---

## 2. 本地环境

使用 **uv** 管理，Python 锁在 **3.12**。

```bash
cd /e/pythonProject/ai-daily

uv sync                 # 安装依赖（首次自动准备 3.12）
uv run python --version # 应为 Python 3.12.x
```

### PyCharm 解释器

```
Settings → Project: ai-daily → Python Interpreter
→ Add Interpreter → Existing
→ E:\pythonProject\ai-daily\.venv\Scripts\python.exe
```

⚠️ 若下拉框仍显示旧版本名（如 `Python 3.13 (ai-daily)`），说明 SDK 缓存没刷新。
删掉该条目重新添加即可——实际版本以 `jdk.table.xml` 里的 `<version>` 为准。

---

## 3. 配置说明

### 3.1 config.yaml 关键项

| 配置 | 当前值 | 说明 |
|---|---|---|
| `timezone` | `Asia/Shanghai` | 时区 |
| `push_time` | `"08:30"` | **改时间要同时改 workflow 的 cron** |
| `llm.temperature` | `0` | 降低输出随机性 |
| `llm.model` | `deepseek-chat` | 批量打分/抽取 |
| `llm.reasoning_model` | `deepseek-reasoner` | 仅前沿影响分析 |
| `prefilter.max_total` | `90` | **直接决定 token 成本** |
| `collectors.github.enabled` | `true` | |
| `collectors.huggingface.enabled` | `true` | |
| `collectors.arxiv.enabled` | `true` | |
| `collectors.hackernews.enabled` | `true` | |
| `collectors.rss.enabled` | `true` | |
| `collectors.reddit.enabled` | **`false`** | 限流严重，已关闭 |

### 3.2 各源回看窗口

发布有延迟，统一 24h 会漏数据：

| 源 | window_hours | 原因 |
|---|---|---|
| github | 24 | 新仓库当天可见 |
| huggingface | 72 | 模型上架到被发现需时间 |
| arxiv | 72 | 论文公告延迟 1~2 天 |
| rss | 36 | 博客更新节奏不一 |
| hackernews | 24 | 实时 |

### 3.3 板块配额

```yaml
sections:
  llm_local:    { min: 3, max: 5 }
  llm_frontier: { min: 1, max: 3 }
  github:       { min: 3, max: 5 }
  agent:        { min: 2, max: 3 }
  news:         { min: 3, max: 4 }
```

内容不足**不凑数**，空板块显示「今日暂无最新数据」。

---

## 4. GitHub Secrets

路径：`Settings → Secrets and variables → Actions → Secrets 标签页`

### 必填（7 个）

| Name | 示例 | 说明 |
|---|---|---|
| `LLM_API_KEY` | `sk-...` | DeepSeek Key，**前后不要有空格** |
| `SMTP_HOST` | `smtp.qq.com` | |
| `SMTP_PORT` | `465` | SSL |
| `SMTP_USER` | `your@qq.com` | |
| `SMTP_PASS` | 授权码 | **不是登录密码** |
| `MAIL_FROM` | `your@qq.com` | |
| `MAIL_TO` | 收件邮箱 | 多个用英文逗号 |

### 可选（不配则用 config.yaml 默认）

`LLM_BASE_URL`、`LLM_MODEL`、`LLM_REASONING_MODEL`

### 自动注入

`GITHUB_TOKEN` 无需配置。

### ⚠️ 必须开启写权限

```
Settings → Actions → General → 底部 Workflow permissions
→ 选 "Read and write permissions"
```

workflow 最后要把 `state.json` 提交回仓库。只读权限会导致发信成功但最后一步 push 失败。

---

## 5. 首次部署流程（已执行）

```bash
# 1. 本地初始化（已完成）
cd /e/pythonProject/ai-daily
git init
git add -A
git commit -m "feat: AI 日报 —— 采集、LLM 抽取、邮件推送"
git branch -M main

# 2. 关联远程并推送
git remote add origin https://github.com/LongjiangLi/ai-daily.git
git push -u origin main

# 3. 在 GitHub 网页配置 Secrets（见第 4 节）

# 4. 手动触发验证
#    Actions → daily-digest → Run workflow
```

---

## 6. 定时任务

```yaml
on:
  schedule:
    - cron: '30 0 * * *'   # UTC 00:30 = 北京 08:30
  workflow_dispatch: {}    # 支持手动触发
```

### 关键事实

- **不是常驻进程**：到点启动临时 runner，跑完销毁，两次触发间不占资源
- **实际到达时间 8:35~8:50**：GitHub 定时任务有排队延迟，属正常
- **不会并发**：`concurrency` 已配置，上一次没跑完会排队
- **60 天无提交会被禁用**：本 workflow 每天提交 `state.json` 保活，正常不会触发
  - 若连续 60 天全失败（如 Secret 失效导致跑不到 persist 步骤），仍可能被禁用

### 改推送时间（两处必须同步改）

| 文件 | 改什么 |
|---|---|
| `config.yaml` | `push_time: "08:30"` |
| `.github/workflows/daily.yml` | `cron: '30 0 * * *'` |

换算：**北京时间 − 8 小时 = cron 的 UTC 时间**。

---

## 7. 日常运维

### 查看运行情况

```
https://github.com/LongjiangLi/ai-daily/actions
```

### 手动触发

`Actions` → `daily-digest` → 右上 `Run workflow`

### 暂停 / 恢复

`Actions` → `daily-digest` → 右上 `⋯` → `Disable workflow` / `Enable workflow`

### 正常运行的日志特征

```
统计窗口：2026-11-08 08:30 → 2026-11-09 08:30 (Asia/Shanghai)
① 采集
    原始 234 条 {'github': 11, 'hf_models': 60, ...}
② 粗筛
    送入 LLM 20 条
③ 初始化 LLM
④ 打分与分桶
⑤ 抽取内容（本地 4 / 前沿 2 / 开源 5 / Agent 3 / 资讯 4）
⑥ 生成头条与趋势
    LLM 调用 9 次 ｜ 输入 51,234 tok ｜ 输出 7,890 tok ｜ 约 ¥0.1205
⑦ 渲染
⑧ 发送邮件
  [mail] 已发送至 xxx@qq.com
完成
```

### 常用改动

| 想做什么 | 改哪里 |
|---|---|
| 减少条目省钱 | `config.yaml` → `prefilter.max_total` |
| 调整板块条数 | `config.yaml` → `sections.*.max` |
| 加 RSS 源 | `config.yaml` → `collectors.rss.feeds` |
| 调整「机会」的口径 | `prompts/` 下对应模板 |
| 换 LLM 厂商 | `.env` / Secrets 的 `LLM_BASE_URL` + `LLM_MODEL` |
| 重新启用 Reddit | `collectors.reddit.enabled: true` |

---

## 8. 故障排查

### 8.1 如何正确读错误日志

⚠️ 页面右上角的 **Annotations** 面板只有 warning / notice，**不含错误堆栈**。

正确路径：

```
Actions → 点进失败的 run → 左侧 job "run"
→ 步骤列表里找打红叉 ✗ 的那一步
→ 点开，拉到最底部，红色文字就是原因
```

或 `⋯` → `Download log archive`，搜索 `Error` / `Traceback`。

### 8.2 错误对照表

| 错误信息 | 原因 | 处理 |
|---|---|---|
| `LLMError: 缺少 LLM_API_KEY` | Secret 没配 / 名字错 / 建在 Variables 页 | 见第 4 节，名字必须完全一致 |
| `RuntimeError: 邮件配置缺失: xxx` | 某个 SMTP Secret 缺失 | 补齐 7 个必填项 |
| `lockfile ... needs to be updated` | uv 版本不一致 | workflow 已固定 `version: "0.12.18"` |
| `remote: Permission ... denied` | Workflow 权限只读 | 改成 Read and write |
| `smtplib.SMTPAuthenticationError` | 用了登录密码而非授权码 | 重新生成授权码 |
| `[rss] 拉取失败 <url>` | 该 RSS 地址失效 | 改 `config.yaml` 的 feeds |
| `无法解析 JSON` | 输出被 max_tokens 截断 | 已内置自动重试 + 修复；仍频繁则调大 `llm.max_tokens` |
| `[github] 403 rate limit` | 未认证限 10 次/分 | 本地设 `GITHUB_TOKEN`；Actions 已自动注入 |

### 8.3 无害的提示（不用处理）

| 提示 | 说明 |
|---|---|
| `Node.js 20 is deprecated` | action 内部实现，GitHub 自动兼容，不影响运行 |
| `ubuntu-latest will migrate to Ubuntu 26` | 平台迁移预告 |
| `[hf] 列表失败: All connection attempts failed` | 国内网络本地调试时出现，Actions 上是海外网络不受影响 |

---

## 9. 本次部署踩坑记录

| # | 问题 | 根因 | 解决 |
|---|---|---|---|
| 1 | `ZoneInfoNotFoundError: 'Asia/Shanghai'` | Windows 不自带 IANA 时区库 | 依赖加 `tzdata` |
| 2 | arXiv 报 301 | 用了 `http://` 且未跟随重定向 | 改 `https://` + `follow_redirects=True` |
| 3 | HF 配置不生效 | 采集器 key `hf_models` 与配置 key `huggingface` 对不上 | 拆分任务名与配置键 |
| 4 | arXiv 一条都抓不到 | 严格 24h 窗口，论文有 1~2 天延迟 | 按源配置 `window_hours` |
| 5 | `无法解析 JSON` | `max_tokens` 截断，`finish_reason=length` | 检测截断后加大预算重试 + JSON 修复 |
| 6 | Reddit 429 | 匿名 RSS 按 IP+UA 限流，CI 机房 IP 被拦 | 默认关闭 |
| 7 | PyCharm 显示 Python 3.13 | SDK 条目缓存了旧名字 | 删除后重新添加 `.venv` 解释器 |
| 8 | Actions 报 `缺少 LLM_API_KEY` | Secret 未配置 | 在仓库 Secrets 中添加 |
| 9 | 推送上去了但 Actions 用旧版 | 本地提交未 push | `git status -sb` 确认 `ahead N` 后推送 |

---

## 10. 命令速查

```bash
# ---- 本地 ----
cd /e/pythonProject/ai-daily

uv sync                       # 同步依赖
uv run python --version       # 确认 3.12
uv run main.py --dry-run      # 只采集+渲染，不调 LLM、不发信
uv run main.py --no-mail      # 跑完整流程，不发信
uv run main.py                # 完整运行并发信
uv run main.py --out x.html   # 指定 HTML 输出路径

# ---- Git ----
git status -sb                # 看是否 ahead（未推送）
git log --oneline -5
git push

# ---- 排查 ----
uv run main.py --dry-run 2>&1 | grep -E "原始|送入|失败"
```

---

## 11. 迁移到新机器

```bash
git clone https://github.com/LongjiangLi/ai-daily.git
cd ai-daily
cp .env.example .env          # 填 LLM 和 SMTP
uv sync
uv run main.py --dry-run      # 验证采集与渲染
uv run main.py                # 完整验证

# PyCharm 里指向 .venv/Scripts/python.exe
```

GitHub 侧的 Secrets 不需要重新配置（跟仓库走），除非换仓库。
