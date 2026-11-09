# ai-daily

> 部署、Secret 配置、定时任务与故障排查见 **[DEPLOY.md](DEPLOY.md)**。

每天早上 8:30 前把最近 24 小时的 AI 领域动态整理成一封邮件：

- **大模型**：本地可部署开源模型（显存/速度/许可证）+ 前沿模型进化的连带影响
- **开源项目 / Agent 动态 / 行业资讯**：每条给「是什么 / 为何重要 / 机会」

## 目录结构

```
pyproject.toml          依赖声明（uv / PEP 621）
uv.lock                 依赖锁文件，保证本地与 CI 完全一致
.python-version         锁定 Python 3.12
main.py                 主流程编排
config.yaml             数据源、板块配额、关键词
src/collectors.py       7 个采集器（GitHub / HF / arXiv / HN / RSS / Reddit）
src/prefilter.py        去重 → 时间窗 → 关键词粗筛（零成本，决定 LLM 花费）
src/llm.py              LLM 封装 + token 计费统计
src/extractors.py       三段式抽取（打分 / 本地模型 / 前沿影响 / 通用）
src/render.py           Jinja2 渲染
src/mailer.py           SMTP 发信
src/state.py            去重状态
prompts/                5 个 prompt 模板
templates/daily.html.j2 邮件模板
.github/workflows/      定时任务
```

## 本地运行

```bash
cd E:\pythonProject\ai-daily
uv sync

# 1) 先验证采集和渲染（不调用 LLM、不发信）
uv run main.py --dry-run

# 2) 打开 preview.html 看效果

# 3) 配置 .env 后跑完整流程（不发信）
uv run main.py --no-mail

# 4) 真正发一封测试邮件
uv run main.py
```

常用调试参数（**都不发信、不污染 state**）：

| 参数 | 作用 |
|---|---|
| `--now` | 窗口改成「最近 24h 到当前时刻」。定时任务默认就用这个模式；下午手动跑也不会被固定上界截断 |
| `--ignore-seen` | 忽略 `state.json` 里最近 3 天推送过的条目，看"全新一天"的完整候选 |

例如白天反复调试：`uv run main.py --now --no-mail --ignore-seen`。
注意：`--no-mail` / `--dry-run` 本来就不会写 state，`--ignore-seen` 额外保证即使发信也不写。

PyCharm 里把解释器指向项目根目录的 `.venv`（由 `uv sync` 生成），即可正常调试。

## 配置

```bash
cp .env.example .env
```

`.env` 里需要填：

| 变量 | 说明 |
|---|---|
| `LLM_API_KEY` | DeepSeek 的 API Key |
| `SMTP_HOST` / `SMTP_PORT` | QQ 邮箱：`smtp.qq.com` / `465` |
| `SMTP_USER` / `SMTP_PASS` | 邮箱账号 / **授权码**（不是登录密码） |
| `MAIL_TO` | 收件人，多个用英文逗号分隔 |

QQ 邮箱授权码：设置 → 账户 → 开启「POP3/SMTP 服务」→ 生成授权码。

## 部署到 GitHub Actions

1. 新建**私有**仓库，把本目录推上去（`.env` 已在 `.gitignore` 里，不会被提交）
2. 仓库 Settings → Secrets and variables → Actions，逐个添加 `.env` 里的变量
3. Actions 页面手动触发一次 `daily-digest` 验证
4. 之后每天 UTC 00:05（北京 08:05）自动运行，整条链路约 3 分钟；
   算上 Actions 的触发延迟，邮件通常 **08:10–08:25** 到达

> 为什么 cron 不写 08:30：Actions 的定时任务会延迟 5–20 分钟才启动，
> 掐点触发必然晚于 08:30。所以提前到 08:05，并用 `--now` 让窗口变成
> 「最近 24h」。换算规则：`cron` 用 UTC，北京时间减 8 小时。
> 8:05 → `5 0 * * *`。`config.yaml` 的 `push_time` 只影响手动运行的窗口锚点。

**关于 60 天休眠**：GitHub 会禁用长期无提交仓库的定时任务。本项目的 workflow 每天会把 `state.json` 提交回仓库，因此不会休眠，不需要额外的 keepalive。

## 成本

按每天约 5 万输入 token + 8 千输出 token 估算，DeepSeek 约 **¥0.1 / 天**，全年 ¥40 左右。

实际消耗每次运行结束都会打印：

```
LLM 调用 9 次 ｜ 输入 51,234 tok ｜ 输出 7,890 tok ｜ 约 ¥0.1205
```

## 每次运行都是实时数据

不做结果缓存。每次运行都会重新采集，所以同一天不同时间运行，抓到的条目数量和
内容本来就会不同（下午比早上多出一批新仓库/新模型是正常现象）。

为了减少无意义的顺序漂移，做了两处确定性处理：

- `temperature: 0`，降低 LLM 采样带来的差异
- 所有排序都加了 `id` 作为同分兜底键

## 空板块

五个内容板块**始终会显示**，当天没有数据时写「今日暂无最新数据」，
不会整块消失，方便判断是"真的没新闻"还是"采集挂了"。

## 调优

| 想做什么 | 改哪里 |
|---|---|
| 减少条目（省钱） | `config.yaml` → `prefilter.max_total` |
| 板块条数 | `config.yaml` → `sections.*.max` |
| 加数据源 | `config.yaml` → `collectors.rss.feeds` |
| GitHub 只抓新建仓库 / 也抓活跃老仓库 | `collectors.github.include_active`（活跃门槛 `active_min_stars`） |
| GitHub 候选构成（成熟/高成长/Java） | `config.yaml` → `prefilter.github_mix` |

`github_mix` 把 GitHub 的 25 个候选名额分成三份：成熟项目（按 star）、
高成长新仓库（按 star/天）、Java + AI 相关项目。教程合集、面试题、awesome-list
会整体排除；某一类不足时用剩余条目按 star 补满。
| 调整"机会"的口径 | `prompts/` 下对应模板 |
| 换模型厂商 | `.env` 的 `LLM_BASE_URL` / `LLM_MODEL` |

## 本地网络说明

| 数据源 | 国内直连 | 说明 |
|---|---|---|
| GitHub / arXiv / HN / 国内 RSS | ✅ | 正常 |
| HuggingFace | ❌ | 在 `.env` 里设 `HF_ENDPOINT=https://hf-mirror.com` |
| HF Blog | ❌ | 需要代理；GitHub Actions（海外）不受影响 |

**Reddit 已默认关闭**（`config.yaml` 里 `collectors.reddit.enabled: false`）。

原因：匿名 RSS 按 IP + UA 限流很严，退避后仍可能失败；且 GitHub Actions 跑在
Azure 机房 IP 上，Reddit 对云厂商 IP 段拦截更强，大概率长期抓不到。
日报价值主要由 HF / arXiv / GitHub / RSS 提供，关掉不影响完整性。

需要时改回 `enabled: true` 即可，代码和退避重试都保留着。

## 时间窗

定时任务的窗口是「最近 24h」（`--now`，截止到实际触发时刻）；手动运行不带
`--now` 时才是「昨天 `push_time` → 今天 `push_time`」。在此基础上，各源发布有延迟，
所以再按源单独配置回看：

```yaml
collectors:
  github:      { window_hours: 24 }   # 新仓库当天就能抓到
  huggingface:
    window_hours: 72                  # 兜底
    models_window_hours: 336          # 模型榜是「近期热门」，createdAt 常是几周前
    papers_window_hours: 168          # daily papers 周末停更，3 天会漏整批
  arxiv:       { window_hours: 72 }   # 论文公告延迟 1-2 天
  rss:         { window_hours: 36 }
```

运行日志会打印粗筛漏斗（去重 → 时间窗 → 关键词 → 截断）的每一级条数；
排查「采集几百条、只剩几条」时先看是哪一级掉的。`--ignore-seen` 可以忽略
`state.json` 里最近推送过的条目，用于本地调试。

## Python 版本与环境管理

用 **uv** 管理，版本锁在 **3.12**：

- `.python-version` → `3.12`
- `pyproject.toml` → `requires-python = ">=3.12,<3.13"`
- `uv.lock` → 依赖版本锁定，本地和 CI 用同一份

`uv sync` 会自动准备 3.12 解释器并创建项目内的 `.venv`。**不需要另外装 Python 3.12。**

```bash
uv sync              # 安装/同步依赖
uv add <package>     # 新增依赖（会自动更新 pyproject + lock）
uv run python --version   # 确认版本
```

Windows 上 `zoneinfo` 需要 `tzdata`（已写进 pyproject），否则会报
`ZoneInfoNotFoundError: 'Asia/Shanghai'`。

## 已知问题

- GitHub Actions 的 cron 有 5–20 分钟延迟，这是平台特性，无法消除；调度已提前到 08:05 来吸收这部分延迟
- 滚动 24h 窗口下，如果某天触发时间比前一天早，两天窗口之间可能有几分钟重叠；已推送条目靠 `state.json` 去重，不会重复出现
- 部分 RSS 地址可能失效，运行日志里会有 `[rss] 拉取失败 <url>`，改 `config.yaml` 即可
- 显存/速度数据依赖模型 README，缺失时会显示「需实测」而不是编造
- GitHub Search API 未认证时限 10 次/分钟，本地反复调试容易 403。在 `.env` 里设 `GITHUB_TOKEN` 可提到 30 次/分钟（Actions 里已自动注入）
