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
| 触发时间 | 主任务 **08:17**、兜底 **08:40**（北京），邮件通常 **08:20~08:45** 到达 |
| cron | `17 0 * * *` + `40 0 * * *`（UTC 00:17 / 00:40） |
| 心跳告警 | 每天 **08:45** 检查当天是否已发送，没有则发告警邮件 |
| 统计窗口 | 最近 24h（运行时带 `--now`，截止到实际触发时刻） |
| 运行时 | GitHub Actions / ubuntu-latest |
| 单次耗时 | 约 3 分钟（首次约 5 分钟） |
| 月用量 | 约 120 分钟 / 2000 分钟免费额度（告警日心跳最多多等 10 分钟） |
| LLM 成本 | 约 ¥0.1/天，¥3/月 |

---

## 1. 架构速览

```
GitHub Actions (cron 17 0 / 40 0 * * *, 运行 main.py --now)
        │
        ├─① 采集   GitHub / HuggingFace / arXiv / HN / RSS（并发）
        ├─② 粗筛   去重 → 时间窗 → 关键词过滤 → 截断
        ├─③④ LLM 分批打分分桶（批次并发）
        ├─⑤ 抽取  本地/前沿/开源/Agent/资讯 + 头条趋势（6 次调用并发）
        ├─⑥ 渲染  Jinja2 → HTML 邮件
        └─⑦ 发信  SMTP
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
├── heartbeat.py            08:45 心跳检查（当天未发送则告警）
├── prompts/                5 个 prompt 模板
├── templates/daily.html.j2 邮件模板
├── .github/workflows/daily.yml      主任务 + 兜底任务
└── .github/workflows/heartbeat.yml  心跳告警
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
| `push_time` | `"08:17"` | 只影响**手动运行**（不带 `--now`）时的窗口锚点；定时任务用 `--now`，窗口是最近 24h |
| `llm.temperature` | `0` | 降低输出随机性 |
| `llm.model` | `deepseek-chat` | 批量打分/抽取 |
| `llm.reasoning_model` | `deepseek-reasoner` | 仅前沿影响分析 |
| `prefilter.max_total` | `90` | **直接决定 token 成本** |
| `prefilter.github_mix` | 成熟 12 / 成长 8 / Java 5 | GitHub 候选构成，不足时按 star 补满 |
| `collectors.github.enabled` | `true` | |
| `collectors.github.include_active` | `true` | 额外抓「近 24h 有推送的成熟仓库」，只抓新建仓库会全是空项目 |
| `collectors.github.active_min_stars` | `200` | 成熟仓库的 star 门槛 |
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
| huggingface（模型） | `models_window_hours: 336` | 模型榜是「近期热门」，createdAt 常是几周前 |
| huggingface（论文） | `papers_window_hours: 168` | daily papers 周末停更，短窗口会漏整批 |
| huggingface（兜底） | 72 | 上面两项未配置时使用 |
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
    - cron: '17 0 * * *'   # 主任务：UTC 00:17 = 北京 08:17
    - cron: '40 0 * * *'   # 兜底：UTC 00:40 = 北京 08:40
  workflow_dispatch:
    inputs:
      force:
        description: '今天已发过也强制重发'
        type: boolean
        default: false
```

### 关键事实

- **不是常驻进程**：到点启动临时 runner，跑完销毁，两次触发间不占资源
- **cron 是 best-effort，不是准时保证**：GitHub 官方文档明确写了，高负载时任务会
  延迟，**“每个整点”就是高负载时段**，负载过高时排队的 job 会被**直接丢弃**——
  不会产生 failed run，Actions 里也查不到任何记录。社区实测有延迟 4~14 小时、
  甚至连续多天不触发的案例
- **为什么不写 00:00/08:00**：整点是全球定时任务最拥堵的时段。主任务放在 00:17，
  避不开所有延迟，但能降低概率
- **兜底与去重**：00:40 的兜底任务会在主任务被延迟/丢弃时补发；
  `main.py` 发信成功后把 `last_sent_date` 写进 `state.json`，同一天再次触发会
  在采集前直接退出（日志：`2026-11-10 的日报已发送过…跳过`）
- **不会并发**：`concurrency` 已配置；上一次没跑完时，兜底任务会排队，
  等主任务结束并提交 state 后再启动，因此能读到最新的 `last_sent_date`
- **心跳告警**：`heartbeat.yml` 每天 08:45 检查 `last_sent_date`；
  若仍不是今天，则发一封 `⚠️ AI 日报可能未发出` 的告警邮件。
  它会先轮询 10 分钟，给仍在延迟中的主/兜底任务留缓冲
- **60 天无提交会被禁用**：本 workflow 每天提交 `state.json` 保活，正常不会触发
  - 若连续 60 天全失败（如 Secret 失效导致跑不到 persist 步骤），仍可能被禁用

### 改到达时间

| 文件 | 改什么 |
|---|---|
| `.github/workflows/daily.yml` | 两条 `cron:`（真正决定几点跑；主/兜底） |
| `.github/workflows/heartbeat.yml` | 心跳 `cron:`（默认比兜底晚 5 分钟） |
| `config.yaml` | `push_time`（只影响手动运行，建议与主 cron 保持一致便于对照） |

换算：**北京时间 − 8 小时 = cron 的 UTC 时间**。不要卡整点，也不要把时间写死在
08:30 之后：延迟无法消除，只能靠兜底 cron + 去重降低影响。
运行命令必须带 `--now`，否则窗口会锚定到 `push_time`，提前跑会退回到前一天。
要**严格准时**，只能改用外部定时器在 08:17 调用 `workflow_dispatch` API
（需要 fine-grained PAT，权限 `Actions: write`）。

---

## 7. 日常运维

### 查看运行情况

```
https://github.com/LongjiangLi/ai-daily/actions
```

### 手动触发

`Actions` → `daily-digest` → 右上 `Run workflow`

- 默认会先检查 `last_sent_date`：今天已发过就直接退出，不会重复发信
- 需要强制重发时，勾选 `force` 再运行
- 只想确认心跳逻辑：`Actions` → `digest-heartbeat` → `Run workflow`

### 暂停 / 恢复

`Actions` → `daily-digest` → 右上 `⋯` → `Disable workflow` / `Enable workflow`

### 正常运行的日志特征

```
统计窗口：2026-11-08 08:07 → 2026-11-09 08:07 (Asia/Shanghai)
① 采集
  [collect] hf_papers   30 条    0.8s
  [collect] arxiv       40 条    0.9s
  [github] 3/16 +30 条（topic:agent created:>=2026-11-08 fork:false archived:false）
  ...
    原始 234 条 {'github': 66, 'hf_models': 60, ...}（58.2s）
② 粗筛
    去重后 190/234 条（最近已推送 44 条）
    时间窗内 85 条
    关键词过滤后 78 条
    GitHub 候选池：成熟 102 / 高成长 5 / Java 8
    截断后送入 LLM 78 条
③ 初始化 LLM
④ 打分与分桶
    打分完成（34.6s）
⑤ 抽取内容与头条（本地 4 / 前沿 2 / 开源 5 / Agent 3 / 资讯 4）
    抽取与摘要完成（71.3s）
    LLM 调用 9 次 ｜ 输入 51,234 tok ｜ 输出 7,890 tok ｜ 约 ¥0.1205
⑥ 渲染
⑦ 发送邮件
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
| 到了 08:40 仍没收到邮件，Actions 里**连 scheduled run 都没有** | GitHub 的 cron 被延迟或静默丢弃（best-effort，整点最堵） | 这是平台行为，不是配置错误；等兜底任务或手动 `Run workflow`。08:45 心跳会发告警 |
| 日志出现 `…已发送过…跳过` | 当天已经发过，兜底/手动触发了第二次 | 正常现象；确需重发时勾选 `force` |

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
| 10 | 配好定时任务后第二天没收到邮件，Actions 里没有 scheduled run | GitHub 的 `schedule` 是 best-effort；UTC 00:05 紧贴整点拥堵时段，任务被延迟或**静默丢弃**（无失败记录） | 主任务挪到 00:17 + 00:40 兜底 + `last_sent_date` 去重 + 08:45 心跳告警；严格准时需外部定时器调 `workflow_dispatch` |

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
uv run main.py --now          # 窗口 = 最近 24h；定时任务默认用这个模式
uv run main.py --ignore-seen  # 忽略最近 3 天推送过的条目，不写 state
uv run main.py --force        # 今天已发过也强制重发
uv run heartbeat.py           # 手动跑心跳检查（当天未发送则发告警邮件）

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
