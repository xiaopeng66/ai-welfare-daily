# LinuxSB 每日福利站

聚合 **linux.sb**、**baipiao.org**、**nodeloc.com**、**vibex.iflow.cn**、**linux.do** 五站的 AI 中转站 / 公益站 / 抽奖类福利帖，每天自动抓取、去重、打分，生成一个纯静态页面发布到 GitHub Pages —— 无后端、无数据库、无人工。

**在线访问：** https://xiaopeng66.github.io/linuxsb-daily/

[![daily-update](../../actions/workflows/daily-update.yml/badge.svg)](../../actions/workflows/daily-update.yml)

## 数据源

| 站点 | 端点 | 发布时间来源 | 备注 |
|------|------|--------------|------|
| linux.sb | `/forum/2`（福利放送）、`/forum/8`（我要推广）、`?sort=lucky`（抽奖）、`?sort=card`（发卡）、首页 | 详情页 `article:published_time` | 列表页只有「最后回复时间」，必须进详情页取真实发布时间 |
| baipiao.org | `/bbs/api/discussions`（JSON），失败回退 HTML 列表 | API `attributes.createdAt` | 回退路径的 DOM 时间不可信，标记为未验证 |
| nodeloc.com | `/latest.json`（最新）、`/c/welfare/12.json`（抽奖福利） | Discourse `created_at` | `created_at` 即发布时间，无需详情页 |
| linux.do | `/c/welfare/36.json`（福利羊毛） | Discourse `created_at` | Cloudflare 防护，走 Scrapling `StealthyFetcher` |
| vibex.iflow.cn | `/c/4.json`（iFlow 补给站） | Discourse `created_at` | 心流AI社区。**只取 /c/4**：兄弟板块是技术闲聊，按本站关键词实测 0–1/30 命中 |

## 更新管道（三条，互为兜底）

| 管道 | 入口 | 特点 |
|------|------|------|
| **Windows 计划任务**（主） | `\linuxsb-daily-08` → `run-update.ps1` | 住宅 IP，**包含 linux.do**；每 3 小时一次。LogonType=Interactive 有丢触发的坑 |
| **Hermes cron** | `~/.hermes/scripts/linuxsb-daily-update.sh` → `cmd.exe` → `run-update.ps1` | 开机但无登录会话时接替计划任务，同样每 3 小时 |
| **GitHub Actions**（兜底） | `.github/workflows/daily-update.yml` | 每天 1 次。不依赖本机开机，但**默认跳过 linux.do**（数据中心 IP 被拒 429） |

**节奏**：主抓取每 3 小时（北京时间 08/11/14/17/20/23/02/05 点），落在 Windows 任务与 Hermes cron 上——
两者共用 `run-update.ps1`，靠脚本内的 mutex 串行化，同一时刻只会有一个在跑。
CI 保留每天一次纯兜底：**这台 Windows 机器长期离线时仍有新数据**，代价是不含 linux.do。
（改频率前 CI 是每 12 小时，那等于每轮都重发一遍 Windows 侧刚抓过的四源结果，属于纯重复。）

**每 3 小时不会触发 GitHub 风控**，但要注意两条边界：
- GitHub 的 schedule 最小间隔是 5 分钟，且官方明说高负载时会延迟、"每小时整点"最堵；所以 CI 那条刻意放在 `17 3 * * *`（03:17 UTC）而不用整点。
- 真正的限额在对面的站点，不在 GitHub：linux.do 已在 Cloudflare 后且对数据中心 IP 返 429（这就是 CI 里 `LINUXDO_ENABLED=0` 的原因）。每 3 小时的 8 次/天仍是低频，但它是唯一一个**真被限过**的源；若日后它开始返 429，先降 Windows 侧的频率，不要去动 CI。
- 仓库是 PUBLIC：Actions 额度无限、不计费，8 次/天远低于任何配额。

三条管道可能并发：runner 侧 `git pull --rebase --autostash` 后再 push，冲突由 rebase 吸收；CI 侧另有 `concurrency` 组串行化。

冲突兜底的实际行为（两个 runner 一致）：

1. push 被拒 → `git pull --rebase --autostash`。两边各自新增同一行时 git 视为同一改动、直接合并，多数情况到此为止。
2. rebase 真冲突 → `git rebase --abort` → `git pull --no-rebase --autostash -X ours`。
   `--autostash` 不能省：工作区里有 `data`/`docs` 之外的脏改动、而 upstream 恰好也动了那个文件时，没有它这条 pull 会直接拒绝
   （`Your local changes ... would be overwritten by merge`，exit 2）→ 脚本走到 `FAIL git push` → **本轮抓取被丢弃**。实测过：加上后 exit 0、push 成功。
   （rebase 那条路径同样带 `--autostash`，否则脏工作区会让 rebase 连启动都拒。）
3. `-X ours` 的含义是**保留本轮快照**（本轮是全量抓取）。副作用：两边都改过的**同一行**，对方那版被替换 —— 若该帖仍在榜上，下一轮抓取会把它带回来；只有「对方改过、且此后掉出榜」的行才会真丢。这是有意的取舍，不是 bug。
4. 若 autostash 无法干净弹出，本地那个文件会留冲突标记，但本轮数据已经 push 成功。

> **实测：CI 不会准点跑。** 早先的 `0 0,12 * * *` 连续多日落在 ~03:45–03:57Z 与 ~17:30–17:55Z，
> 比计划晚 3h45m–5h30m（GitHub 调度队列积压）。这就是现在把主节奏挪到 Windows 侧、CI 只留兜底的原因——
> 用"每 3 小时"来换时效性时，不能建立在一个会漂移几小时的调度器上。

## 抓取与合并策略

- **增量合并**：以「站点限定 id」`host#id` 为键合并，同键新数据覆盖旧值，历史全部保留。
  - 用站点限定键是因为各站 id 都从 1 开始自增：linux.sb 已到 ~2.4 万而 nodeloc 已过 10 万，两个区间迟早相遇。裸数字 id 做键时，跨站撞号会静默丢掉一条或覆盖另一站的帖子；现在撞号只是让新来者改用 `nodeloc.com#12345` 这种键，两条都留下。同站 URL 变更（改 slug）仍按原键覆盖。
- **滚动上限 300 条**：超出时按发布时间淘汰最旧的；`created_at` 缺失的行按 `fetched_at` 计龄
  （早先缺失被当成「无限旧」，结果把最新的 linux.do 帖先淘汰掉，而 2020 年的老帖反而留着）。
  - **为什么从 200 提到 300**：闸门放宽（见下）后每轮入库量差不多翻倍（75 → 152），固定 200 会被新内容瞬间填满，
    滚动窗口从 49 天塌到 11 天；后果是**内容偏旧的一整个源被无声挤出**——vibex 最新帖是 2026-09-08，
    比新窗口的起点还早，于是整源归零（抓取本身正常，返回 90 条）。300 把窗口还原到 60~65 天，五个源都有代表。
    改这个值要连带看窗口跨度，`tests/test_parsers.py` 里有 `DEFAULT_LIMIT` 断言兜底。
- **相关性过滤：两级判定**（`is_relevant_title()`），不是一张词表命中即收。
  - **为什么改成两级**：单层 8 词闸门有**两个反向缺陷**，2026-10-04 在全量语料（607 抓取 / 509 唯一标题）上量过：
    ① 漏真货 194 条——圈内黑话（`注册送1刀`、`0.0001倍率`、`爽蹬$1000刀`、`国模免费用`、`放粮`、`号池`）一个都不含那 8 个词；
    ② 放进噪音 24 条，**全部**来自 `抽奖`——它只表示「在送东西」，不表示「送的是 AI 额度」，于是 VPS / esim / TG 号 / 域名 / B站大会员 / 小鸡 的抽奖全进来了（`抽奖` 单独做闸门时精度只有 38%）。
  - **A 级（自足词）**：`中转站` `公益` `鸡蛋` `体验金` `号池` `放粮`/`放糧` `兑换码`——词本身就是福利，命中即收。
    `额度` **故意不在这里**：它只说明在讲额度、不说明在送额度，「额度给的太少了」「go的额度也下降了」是抱怨帖，必须走 B 级。
  - **B 级（AND）**：`AI/额度领域词` ∧ `福利行为信号`，且**不能**是教程/抱怨意图。
    - 领域词：`api` `key` `ai` `llm` `token` `中转` `额度` `倍率` `分组` `邀请码` `积分` `余额` `充值` `赠送` `签到` `刀` `蹬` `国模` + 各家模型名（gpt/claude/deepseek/glm/gemini/grok/qwen/kimi/codex/openai/cursor…）。
    - 福利信号：`免费` `送` `赠` `白嫖` `抽` `抽奖` `兑换` `邀请` `注册` `领取` `领` `福利` `试用` `优惠` `折` `限时` `羊毛` `红包` `纳新` `撸` `抢` `新用户` `获得` `发` `蹬`。
    - 教程/抱怨否决词：`经验` `教程` `方法` `攻略` `指南` `怎么` `如何` `防封` `太少了` `用不起` `下降了` `涨价`。
    - `注册` 留在信号里是必须的（去掉丢 5 条真货：`注册送10刀` / `github注册15刀`…），它的教程噪音由否决词挡掉。
    - `抽` 收单字而不是只收 `抽奖`：`先抽个奖叭`、`抽50个10¥余额` 中间隔了字，只做 `抽奖` 子串会漏（实测净增 3 条、0 噪音）。
      `蹬` 在黑话里 = 免费用额度（`爽蹬$1000刀`、`【猛蹬】claude顶级模型不花钱`），`国模` = 国产模型（`新站开业，国模免费用`，不含「模型」二字）。这三个词实测净增 11 条真货、0 条噪音。
  - **模型名要用 ASCII 前后瞻，不能用 `\b`**：Python 里中文也算 `\w`，所以 `r"\bgpt\b"` 匹配不到「月gpt」（`月` 与 `g` 之间没有边界）。`_bounded()` 用 `(?<![a-z0-9])…(?![a-z])`，前者挡 `openai` 里的 `ai`，后者挡 `keyboard` 里的 `key`，但放行 `GPT6` / `GLM-5.3` 这种带版本号的写法。
  - **`免费` 只做福利信号、不做领域词**：放松成领域词会把 `免费苹果共享ID`、`免费领天翼云手机`、`免费节点分享`、`免费esim7天` 整类噪音带回来。同理 `机场`/`VPS` 类词一个都不收。
  - 两层都过不了即整条丢弃，`CATEGORY_KEYWORDS` 根本不跑——所以闸门词与分类词的差异是**有意**的，别顺手「统一」。
- **合并键必须能从 store 里重新算出来**：键是 `host#id`，`host` 取自 url、url 缺失时退回 source。这一条是硬约束——
  键只在内存里，store 存的是行的字段，所以任何「运行时才知道的键」（比如撞号时临时改成另一种写法）在下一轮就丢了，
  那一行会被当成全新行、甚至被另一站同名 id 挤掉。跨站撞号因此是**结构性避免**的，不需要运行时补救。
- **过滤在打标签之前**：标题过不了上面的两级闸门，`CATEGORY_KEYWORDS` 根本不会跑。所以「闸门词」与「分类词」是两套不同的表，
  差异是有意的（`免费` 只在分类里，`抽` 只在闸门里）——详见上面的两级判定说明。
- **「近 24 小时」由浏览器判定**：卡片是否算「新」= 发布时间落在**读者本地时钟往前的 24 小时内**，不落进 store。
  - 为什么不写进数据：这条属性只取决于 (读页面那一刻, `created_at`)，写进 store 就意味着随时间老化都要重新提交一次；放在浏览器里则页面始终是 store 的纯函数，帖子到期自然滑出分区、零提交，而且页面被缓存或开了很久也仍然显示正确的窗口。
  - `created_at` 缺失的行（老 HTML 路径遗留）退回用 `fetched_at` 判定，与卡片显示的时间保持一致。
- **只写变化**：`fetched_at` 的语义是「这一行上次变化的时间」，不是「上次查看的时间」。每轮逐字段比对（标题 / URL / 来源 / 发布时间 / 标签 / 匹配度 / 验证标记 / 新旧标记），只有真变化的行才刷新时间戳；整个 store 没变化时**连文件都不重写**。所以「没有新帖」的那次运行不产生提交，也不触发 Pages 部署。
  - 一条新帖只带来**一次**提交（出现时）。它之后从「近 24 小时」滑出去不再产生任何提交。
  - 逐字段比对里也包含发布时间 / 标签 / 匹配度 / 验证标记，所以标题被站长改了、或某行从「未验证」升级为「已验证」时，都会如实产生一次提交。
- **原子写入**：store / 页面 / 状态文件都先写同目录临时文件再 `os.replace()`，中途被杀不会留下半截文件。

### 失败处理（都会让 CI 变红，但绝不丢数据）

| 情况 | 行为 |
|------|------|
| 单个源失败 | 保留该源缓存行，写入 `data/.fetch_errors`，CI 事后标记红构建 |
| 某源抓取成功但解析出 0 条 | 判定为选择器/端点漂移，同样报警（否则该源会静默变哑巴） |
| 全部源都 0 条且 store 非空 | **拒绝写入**（`exit 2`）：这是断网/代理故障，不是站点清空 |
| store 完全无法解析 | **拒绝写入**（`exit 3`）：不把损坏的 store 当成空库 |
| store 部分行损坏 | 保留可解析的行 + 报告，照常写入 |

## 站点功能

单页静态 HTML，无外部依赖（无 CDN、无框架）：

- 两种分区 + 一种视图：**近 24 小时** / **24 小时前**（默认按匹配度排序）、**按时间分组**（每个日期一个 `📅 YYYY-MM-DD` 区块）
  - 「近 24 小时」用浏览器时钟实时算，不是抓取时冻结的标记（`tests/test_page_js.mjs` 守着这条）
- 两个筛选维度：来源（linux.sb / baipiao.org / nodeloc.com / nodeloc 福利 / vibex.iflow.cn / linux.do）、分类（按标题关键词）
- 排序：匹配度 / 时间；卡片显示来源、匹配度、发布时间
- 顶部「更新于」取自 store 里最新的 `fetched_at`（= 数据最后一次真正变化的时间），而不是渲染时刻：
  页面因此是输入的纯函数，没有新数据时两次渲染的文件逐字节相同
- **全站统一北京时间**：日期分组（`bjDateKey`）和卡片时间（`formatTime`，显式 `timeZone:'Asia/Shanghai'`）都是北京时区。`created_at` 是 UTC，直接切字符串会把 16:00–24:00 UTC 的帖子归到前一天；卡片时间若用访客本地时区，国外访客看到的时刻会和它上方的日期分组对不上。
- 所有落进 DOM 的字段都转义；单条记录字段残缺只会让那张卡片难看，不会让整页空白

## 本地运行

```bash
python3 fetch.py -o data/topics.jsonl      # 抓取（默认走 Clash 代理 127.0.0.1:7897）
python3 generate.py -i data/topics.jsonl -o docs/index.html
```

环境变量：

| 变量 | 默认 | 用途 |
|------|------|------|
| `LINUXDO_ENABLED` | `1` | 设 `0` 跳过 linux.do（CI 与 WSL 调试用） |
| `CLASH_PROXY_HOST` / `CLASH_PROXY_PORT` | `127.0.0.1` / `7897` | 代理地址；设 `CLASH_PROXY_PORT=0` 直连 |

> **WSL / Linux 下跑 linux.do 会失败**：`fetch()` 走代理，但 `StealthyFetcher.fetch` 不接 `proxy=`，Chromium 是直连的，本机直连 linux.do 超时。而显式加 `proxy=` 更糟 —— 代理出口 IP 被 Cloudflare 判 403（返回 8KB 挑战页而非 91KB 数据）。验证完整流程请用 `LINUXDO_ENABLED=0`。

## 目录结构

```
fetch.py                         抓取 + 合并 + 落盘（所有网络重试与护栏都在这里）
generate.py                      读 store，渲染 docs/index.html（含内联 CSS/JS）
run-update.ps1                   Windows / Hermes cron 侧更新脚本
tests/test_guards.py             抓取护栏：注入各类故障，断言「不丢数据」
tests/test_site.py               页面不变量：数据驱动的「更新于」、渲染幂等、转义
tests/test_page_js.mjs           页面 JS 运行时：近 24 小时分区、created_at 兜底、北京时间
tests/test_parsers.py            解析层：4 个站的 HTML/JSON 解析与正则夹具
.github/workflows/daily-update.yml   CI 主管道（先跑护栏，再抓取）
.github/workflows/tests.yml          测试：push / PR 触发
data/topics.jsonl                store：一行一个 JSON，合并后的历史
docs/index.html                  生成的站点（GitHub Pages 直接服务这个目录）
```

## 维护须知（踩过的坑）

- **别在 CI 里打开 linux.do**：runner 是数据中心 IP，linux.do 回 429，会让那条兜底构建变红。
- **改抓取频率要同时改三处**，漏一处就会出现两套节奏打架：workflow 的 `cron:`、Windows 计划任务的 `RepetitionInterval`、Hermes cron 的 `schedule`。
  Windows 任务的重建脚本在 `E:\AI\Hermes\scripts\temp\register-linuxsb-tasks.ps1`；改完**必须用 `Export-ScheduledTask` 看 XML 确认**
  `<Repetition><Interval>` —— `Get-ScheduledTask` 显示的 `duration=` 是空的，光看它分不清"永久重复"还是"只跑一次"。
- **vibex.iflow.cn 对 CI 是安全的**：它是普通 Discourse，直连（无代理）约 0.7s、无 Cloudflare 挑战，是本项目里**唯一能同时被 CI 和 Windows 任务抓到的非 linux.sb 源**。选它做新源而不是 NodeSeek / sb.sb 就是因为后两者代理下 200、直连 404（CI 拿不到）。加新源前先按这个标准验一遍：`httpx.get(url)` 不给 proxy，能 200 才算可用。
- **`page.body`，不是 `page.html_content`**：后者会把 JSON 包进 `<html><body>`，`json.loads` 直接炸。JSON 端点 + `page.body` 是 1.2s/页，比浏览器渲染 HTML（90s+）快两个数量级。
- **不要给 `StealthyFetcher.fetch` 加 `proxy=`**：见上，会让 Cloudflare 判 403。
- **别改 `.gitattributes` 的换行规则**：`run-update.ps1` 必须是 CRLF，Windows 计划任务才会正常执行。
- **`fetch.py` 的退出码有意义**：`0` 正常（可能带部分失败标记）、`2` 全源空且 store 非空、`3` store 损坏。脚本据此决定是否提交。
- **「没有变化」是正常结果**：抓完发现没有新帖就什么都不写、不提交。**三个** runner 都已处理这条路径
  （日志里的 `no changes to commit` / `store already up to date`），看到它不要当故障排查。
- **解析层是最脆的一环，夹具在 `tests/test_parsers.py`**：站点改一行 markup 就会静默解析错。两处真实踩过的坑——
  linux.sb 列表页原来用「一个正则配整页」，`re.S` + `.*?` 让丢了 `<span>` 的行去偷下一行的时间戳、并吞掉那一行
  （实测每页少 8 条）；baipiao 的 HTML 回退路径把 url 拼成 `https://baipiao.org777`，于是同一个帖子在 API 路径和
  回退路径下变成两个 host 命名空间、永久重复。改解析务必让 `test_parsers.py` 陪着改。
- **`created_at` 只会被更好地覆盖，不会被清空**：某个源这一轮没给出时间不允许把已存的抹掉（否则该行会掉到淘汰队列最前面）。
- **改完先跑测试再推**：`python3 tests/test_guards.py && python3 tests/test_parsers.py && python3 tests/test_site.py && node tests/test_page_js.mjs`
- **测试必须能咬人**：每次改动后把被守护的行为改回去跑一遍，确认测试真的红。本轮 9 处修复逐条做了变异验证（含上面 4 类），
  全部至少让一项断言失败；没有这步，「测试通过」只说明测试没跑错。
  它们守的都是「失败了也不会报警」的逻辑 —— 静默丢源、跨站覆盖、假装有变化。`daily-update.yml` 在抓取前先跑护栏套件：宁可这一天不抓，也不让会毁 store 的运行真的写下去。
- **页面现在是输入的纯函数**：`_write_if_changed()` 让没有新数据的那次运行连文件都不重写，日志会打 `(unchanged, left alone)`。要是哪天「更新于」又变回渲染时刻，`tests/test_site.py` 会红（它用一个 2020 年的 store 断言徽章必须显示 2020）。
- **改了 `generate.py` 的内联 JS 要重新生成页面**：仓库里的 `docs/index.html` 是产物，下次 CI 会覆盖，但本地看到的不一致会误导排查。`tests/test_site.py` 最后一项会直接比对「committed 页面 vs committed store 重新渲染的结果」，不一致就红。

## 验证方式

改动后按这个顺序验：

```bash
python3 tests/test_guards.py    # 抓取护栏（不联网，注入所有源）
python3 tests/test_parsers.py   # 解析层夹具（不联网）
python3 tests/test_site.py      # 页面不变量
node tests/test_page_js.mjs     # 页面 JS 运行时（改内联 JS 后必跑）
python3 -c "import ast;ast.parse(open('fetch.py').read())"   # 语法
ruff check .                                                  # lint
LINUXDO_ENABLED=0 python3 fetch.py -o /tmp/topics.jsonl       # 真跑一次抓取
python3 generate.py -i /tmp/topics.jsonl -o /tmp/index.html   # 生成
python3 - <<'EOF'                                             # 抽出内联 JS
import re;h=open('/tmp/index.html',encoding='utf-8').read()
open('/tmp/site.js','w').write(re.search(r'<script>(.*?)</script>',h,re.S).group(1))
EOF
node --check /tmp/site.js                                     # JS 语法
```

`tests/` 现在有四个套件：`test_guards`（故障注入与增量语义）、`test_parsers`（解析夹具）、`test_site`（产物不变量）、
`test_page_js`（页面 JS 运行时）。页面 JS 的**运行时**行为（不只是语法）另用一个 DOM shim 跑真实数据来验：把 `const cards = [...]` 换成构造好的 payload，用 `new Function` 执行整段脚本，断言渲染出的卡片数、日期分组、转义结果。字段残缺、XSS、时区边界这些用例都在这里覆盖。

## 免责声明

本站仅聚合各站点**公开可见**的帖子标题与链接，不镜像正文、不代理登录、不绕过任何访问控制。所有内容版权归原发帖人与原站点所有，匹配度分数只是标题关键词的机械匹配，不代表推荐或背书。
