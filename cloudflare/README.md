# Cloudflare Worker 定时触发（主触发）

GitHub Actions 的 `schedule` 是 best-effort：2026-11 实测 cron 到实际运行晚
**5h24m–7h03m**，而且高负载时排队任务会被**静默丢弃**（连失败记录都没有）。
所以准时性不再交给它，改由 Cloudflare Worker 的 Cron Trigger 调用
`workflow_dispatch`——dispatch 不经过 GitHub 的定时队列，实测秒级启动。

| 北京 | UTC | 动作 |
|---|---|---|
| 08:27 | `27 0 * * *` | 调 `daily.yml` → 发日报（约 08:30–08:33 到信） |
| 09:33 | `33 1 * * *` | 调 `heartbeat.yml` → 当天没发出去就发告警邮件 |

分钟刻意避开 `:00` / `:30`：这两个整点是绝大多数定时任务的落点，压缩到偏门
分钟可以减少调度高峰的竞争。

仓库里仍保留 1 档 GitHub cron 作兜底（`daily.yml` 北京 08:36、
`heartbeat.yml` 北京 03:23 排队），Worker 正常时它们只会秒退；Worker 或
PAT 失效时由它们补发（通常已是下午）。两道触发靠 `state.json` 的
`last_sent_date` 去重，同一天只会有一封。

## 1. 生成 GitHub Token

1. GitHub → 右上头像 → **Settings** → **Developer settings**
   → **Personal access tokens** → **Fine-grained tokens** → **Generate new token**
2. 填写：
   - Token name：`ai-daily-trigger`
   - Expiration：按需（最长 1 年；到期前记得换，见第 5 节）
   - Resource owner：`LongjiangLi`
   - Repository access：**Only select repositories** → `LongjiangLi/ai-daily`
   - Permissions → Repository permissions → **Actions: Read and write**
3. 生成后**立刻复制** token（只显示一次）。

> 只需要 `Actions: Read and write`，不要给 Contents 等权限。
> token 只存在 Cloudflare 的 Secret 里，不要写进仓库或 `.env`。

## 2. 部署 Worker

需要 Node.js 20+。

```bash
npm install -g wrangler
wrangler login                 # 浏览器授权 Cloudflare

cd cloudflare
wrangler secret put GITHUB_TOKEN   # 粘贴上一步的 PAT
wrangler deploy
```

`wrangler.toml` 里已经写好了 `GH_OWNER` / `GH_REPO` / `GH_REF` 和两个
cron 触发器，无需再改。部署成功后控制台会输出 Worker 的 `workers.dev` 地址，
访问它应看到：

```
ai-daily-trigger is up
27 0 * * * UTC -> daily.yml
33 1 * * * UTC -> heartbeat.yml
```

免费计划即可满足（本 Worker 只用 2 个 Cron Trigger）。Cloudflare 的 Cron Trigger
通常在计划时间后几秒到几分钟内触发；偶发更长延迟时，也远小于 GitHub 的小时级延迟。

## 3. 验证

部署后打开 Worker 地址（`wrangler deploy` 会打印
`https://ai-daily-trigger.<你的子域>.workers.dev`），或用 curl：

```bash
curl https://ai-daily-trigger.<你的子域>.workers.dev
```

应看到 cron → workflow 的映射。这个地址只做健康检查，**不会**触发 workflow。

先确认 PAT 有效（只读，不触发任何运行）：

```bash
curl -s -o /dev/null -w '%{http_code}\n' \
  -H "Authorization: Bearer <你的 PAT>" \
  https://api.github.com/repos/LongjiangLi/ai-daily/actions/workflows
# 200 = token 有效
```

端到端验证（会真的触发一次 GitHub workflow）：

- **方式 A：等下一次真实触发**（最干净）。挂上 `wrangler tail`，到 08:27 /
  09:33 时日志出现 `dispatched daily.yml (...)`，Actions 里出现
  `event=workflow_dispatch` 的 run。
- **方式 B：临时改成每 5 分钟**。把 `wrangler.toml` 的某个 cron 改成
  `*/5 * * * *` → `wrangler deploy` → `wrangler tail` → 最多等 5 分钟 →
  验证完改回 `27 0 * * *` / `33 1 * * *` 再 `wrangler deploy`。

> 注意：对 `daily.yml` 做端到端测试会真的跑完整日报并发信（如果当天还没发过）。
> 只想验证通道、不想多收一封邮件时，用 `heartbeat.yml`：当天日报已发送时它只读
> `state.json` 然后退出。
>
> `wrangler dev --test-scheduled` 是**本地**运行 Worker：生产 Secret 不会自动
> 带过去（需要另建 `.dev.vars`，已 gitignore），而且它同样会真的调用 GitHub API。

## 4. 日常

- **日志**：Cloudflare 控制台 → Workers & Pages → `ai-daily-trigger`
  → Logs（`[observability]` 已开启）；或本地 `wrangler tail`
- **触发记录**：GitHub Actions 里 run 的 `event` 显示 `workflow_dispatch`
- **暂停**：Cloudflare 控制台 → Settings → Trigger Events 删掉 cron，
  或 `wrangler delete`（GitHub 的兜底 cron 仍在，但会变成下午到信）

## 5. Token 到期

fine-grained PAT 到期后，Worker 日志会出现：

```
dispatch daily.yml failed: HTTP 401 {"message":"Bad credentials", ...}
```

处理：重新生成 token → `cd cloudflare && wrangler secret put GITHUB_TOKEN`
→ 不用重新 deploy，Secret 立即生效。到期前的这段时间，日报由 GitHub 的
兜底 cron 在下午补发，不会丢。

## 6. 改到信时间

同时改四处，保持目标时间一致：

| 文件 | 改什么 |
|---|---|
| `cloudflare/wrangler.toml` | 两个 `crons` 的 UTC 时间 |
| `.github/workflows/daily.yml` | 兜底 `cron:`（目标时间 + 2 分钟）与 `--earliest` |
| `.github/workflows/heartbeat.yml` | 兜底 `cron:` 与 `--earliest 09:30` |
| `config.yaml` | `push_time`（手动运行的窗口锚点） |

换算：**北京时间 − 8 小时 = UTC**。改完分别 `wrangler deploy` 和提交仓库。
