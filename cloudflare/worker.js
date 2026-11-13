// ai-daily 外部定时触发器
//
// Cloudflare Cron Trigger 到点后，调用 GitHub Actions 的 workflow_dispatch。
// workflow_dispatch 不经过 GitHub 的 schedule 队列（后者 2026-11 实测延迟
// 5h24m-7h03m，高负载时还会静默丢弃），所以邮件能稳定在北京 08:30 左右到达。
// 触发分钟刻意避开 :00 / :30，减少定时调度高峰的竞争。
//
// 部署步骤见同目录 README.md。
// 需要的变量：
//   Secret  GITHUB_TOKEN  fine-grained PAT，权限 Actions: Read and write
//   Vars    GH_OWNER / GH_REPO / GH_REF（在 wrangler.toml 里）

// cron 一律是 UTC；北京时间减 8 小时。
const WORKFLOWS = {
  "27 0 * * *": "daily.yml",      // 北京 08:27 触发发日报（约 08:30-08:33 到信）
  "33 1 * * *": "heartbeat.yml",  // 北京 09:33 检查日报是否漏发
};

async function dispatch(env, workflow) {
  const owner = env.GH_OWNER || "LongjiangLi";
  const repo = env.GH_REPO || "ai-daily";
  const ref = env.GH_REF || "main";
  const url =
    `https://api.github.com/repos/${owner}/${repo}` +
    `/actions/workflows/${workflow}/dispatches`;

  const res = await fetch(url, {
    method: "POST",
    headers: {
      Authorization: `Bearer ${env.GITHUB_TOKEN}`,
      Accept: "application/vnd.github+json",
      "X-GitHub-Api-Version": "2022-11-28",
      "User-Agent": "ai-daily-trigger-worker",
      "Content-Type": "application/json",
    },
    body: JSON.stringify({ ref }),
  });

  // 成功时 GitHub 返回 204 No Content
  if (res.status !== 204) {
    const body = await res.text();
    throw new Error(`dispatch ${workflow} failed: HTTP ${res.status} ${body}`);
  }
  console.log(`dispatched ${workflow} (${owner}/${repo}@${ref})`);
}

export default {
  async scheduled(controller, env) {
    const workflow = WORKFLOWS[controller.cron];
    if (!workflow) {
      console.error(`unknown cron: ${controller.cron}`);
      return;
    }
    // 直接 await：失败会让这次 cron 调用标记为失败，方便在 Cloudflare 控制台看到
    await dispatch(env, workflow);
  },

  // 公开访问时的健康检查（不触发任何 workflow）
  async fetch() {
    const lines = [
      "ai-daily-trigger is up",
      ...Object.entries(WORKFLOWS).map(([cron, wf]) => `${cron} UTC -> ${wf}`),
    ];
    return new Response(lines.join("\n") + "\n", {
      headers: { "Content-Type": "text/plain; charset=utf-8" },
    });
  },
};
