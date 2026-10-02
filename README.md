# LinuxSB 每日福利站

自动抓取 **linux.sb**、**baipiao.org**、**nodeloc.com**、**linux.do** 四站的 AI 中转站福利帖子，每日自动更新并发布到 GitHub Pages。

**数据来源：**
- linux.sb：福利放送 /forum/2、我要推广 /forum/8、抽奖 ?sort=lucky、发卡 ?sort=card、首页
- baipiao.org：/bbs/api/discussions
- nodeloc.com：/latest.json（最新）、/c/welfare/12.json（抽奖福利板块）
- linux.do：/c/welfare/36.json（福利羊毛板块，Cloudflare 防护，经 Scrapling StealthyFetcher 抓取）

**更新频率：** 每天 08:00 和 20:00（UTC+8）
**托管：** GitHub Pages

## 全自动流程（三重管道，全部无人工）

| 管道 | 跑什么 | 说明 |
|------|--------|------|
| **GitHub Actions**（主） | `.github/workflows/daily-update.yml`：抓取 → 生成 → push → Pages 自动部署 | 无需本机开机。linux.do 在 CI 默认跳过（`LINUXDO_ENABLED=0`，数据中心 IP 被 linux.do 拒 429） |
| **Windows 计划任务** `\linuxsb-daily-08` / `-20` | `run-update.ps1`：同一套抓取+push（ residential IP，**含 linux.do**） | LogonType=Interactive 的丢触发坑已由 Hermes cron 兜底 |
| **Hermes cron**「AI福利日报-触发更新」（08:00/20:00） | `~/.hermes/scripts/linuxsb-daily-update.sh` → cmd.exe → `run-update.ps1` | 机器开机但无登录会话时接替 Windows 任务（2026-10-01 08:00 丢失事故的修复） |

三条管道可能并发，安全：runner 先 `pull --rebase --autostash` 再 push，并发由 rebase 吸收；CI 侧另有 `concurrency` 组串行化。

**实测：CI 并不按 08:00/20:00 跑。** `0 0,12 * * *` 的 schedule 连续多日都落到 ~03:45–03:57Z 与 ~17:30–17:55Z，即北京时间 **约 11:50 与次日 01:30**，比计划晚 3h45m–5h30m（GitHub 调度队列积压）。所以站点实际每天刷新 4 次：08:00 / 20:00（Windows 任务，含 linux.do）+ 约 11:50 / 01:30（CI，无 linux.do，只重发缓存数据）。

**本地（WSL/Linux）跑 linux.do 会失败**：`fetch()` 走 Clash 代理，但 `StealthyFetcher.fetch` 不带 `proxy=`，Chromium 直连；本机直连 linux.do 超时（Windows 侧靠系统代理/TUN 才通）。且显式加 `proxy=` 反而更糟——代理出口 IP 被 Cloudflare 判 403。WSL 里验证完整流程请用 `LINUXDO_ENABLED=0`。

## 本地运行

```bash
# 抓取数据（走 Clash 代理，默认 127.0.0.1:7897）
python3 fetch.py -o data/topics.jsonl

# 生成网页
python3 generate.py -i data/topics.jsonl -o docs/index.html
```

代理可用环境变量覆盖：`CLASH_PROXY_HOST`、`CLASH_PROXY_PORT`。

## 抓取策略

- **增量合并**：按帖子 id 合并，新数据覆盖同 id 旧值，保留历史
- **上限 200 条**：超出时按发布时间淘汰最旧的（无 created_at 算最老，自然淘汰）
- **发布时间**：linux.sb 详情页取 `article:published_time`；baipiao / nodeloc / linux.do API 的 `created_at` 即发布时间
- **相关性过滤**：标题须含 中转站/公益站/鸡蛋/兑换码/额度/体验金/抽奖 之一
- **部分源失败**：缓存行保留，并写入 `data/.fetch_errors` 供 CI 标记红构建
- **linux.do**（2026-10-02 重写）：改用 Discourse JSON 端点 + `StealthyFetcher.fetch` 类方法（1.2s/页 vs 旧 HTML 浏览器渲染 90s+），原生 `created_at`，弃用的 `StealthyFetcher()` 实例化路径已移除（v0.3 兼容）。注意必须用 `page.body` 而非 `page.html_content`（后者把 JSON 包进 `<html><body>`）。已滚出前 2 页的老帖子由 rolling window 自然淘汰

## 在线访问

https://xiaopeng66.github.io/linuxsb-daily/
