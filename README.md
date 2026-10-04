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
- **相关性过滤：三段式判定**（`is_relevant_title()`），不是一张词表命中即收。
  - **为什么改**：单层 8 词闸门有**两个反向缺陷**，2026-10-04 在全量语料（607 抓取 / 509 唯一标题）上量过：
    ① 漏真货 194 条——圈内黑话（`注册送1刀`、`0.0001倍率`、`爽蹬$1000刀`、`国模免费用`、`放粮`、`号池`）一个都不含那 8 个词；
    ② 放进噪音 24 条，**全部**来自 `抽奖`——它只表示「在送东西」，不表示「送的是 AI 额度」，于是 VPS / esim / TG 号 / 域名 / B站大会员 / 小鸡 的抽奖全进来了（`抽奖` 单独做闸门时精度只有 38%）。
    两级闸门（自足词 / 领域词∧信号）补上了 ①，但**只问「出现了什么词」，不问「送的是什么东西」**，
    于是「送 10 刀额度」和「送点论坛积分」在结构上仍然等价。2026-10-04 又补了**标的物**与**状态**两个维度。
  - **判定顺序**（先否决，再收）：
    - **第 0 档 已结束 → 丢**：`已开奖` `已流抽` `已结束` `已赠送` `已领完` `已失效` `已完` `已无`。死信息，不区分内容一律丢（实测积了 24 条）。
    - **第 1 档 非 AI 标的 → 丢**：服务器/主机（`小鸡` `母鸡` `杜甫` `毒妇` `独服` `服务器` `挂机宝` `家宽` `探针` `节点` `机场` `宽带` `vps` `nat`）、
      域名/存储（`域名` `玉米` `备案` `图床` `网盘` `云盘`）、
      非 AI 数字商品（`tg号` `电报号` `美区号` `苹果id` `steam` `礼品卡` `充值卡` `代金券` `虚拟卡` `信用卡` `流量卡` `电话卡` `stripe` `esim`）、
      论坛头衔（`称号` `等级` `元老` `鸡腿` `活跃度`）。
      **`账号` 与 `富可敌国` 故意不在表里**：前者太宽（会误杀「账号发到群里改倍率」），后者是 linux.do 的用户等级徽章，几乎每条推广帖都带。
  - **A 级（第 2 档，自足词）**：`中转站` `公益` `鸡蛋` `体验金` `号池` `放粮`/`放糧` `兑换码`——词本身就是福利，命中即收。
    它排在语境否决**之前**，否则「10亿token鸡蛋块领，Muse轻松注册另一种方法」这种带「方法」二字的真货会被误杀。
    `额度` **故意不在这里**：它只说明在讲额度、不说明在送额度，「额度给的太少了」「go的额度也下降了」是抱怨帖，必须走 B 级。
  - **第 3 档 会员类 → 默认丢**：ChatGPT Plus / Gemini 会员是账号商品，不是中转额度。只有**价格词 ∧ AI 词**同现才放行——
    `【明天结束】CUN.AI 首充 $66 拿 Claude / GPT Max 约 3.3 折，会员返利还叠加！` 是站点的充值优惠，属于目标内容（用户 2026-10-04 决定：会员类放行）。
  - **第 4 档 教程/抱怨意图 → 丢**：`经验` `教程` `方法` `攻略` `指南` `怎么` `如何` `防封` `太少了` `用不起` `下降了` `涨价`。用户 2026-10-04 决定：教程/攻略不要。
  - **B 级（第 5 档，AND）**：`AI/额度领域词` ∧ `福利行为信号`，且**不能**是教程/抱怨意图。
    - 领域词：`api` `key` `ai` `llm` `token` `中转` `额度` `倍率` `分组` `邀请码` `余额` `充值` `赠送` `签到` `刀` `蹬` `国模` + 各家模型名（gpt/claude/deepseek/glm/gemini/grok/qwen/kimi/codex/openai/cursor…）。
    - **`积分` 不是领域词**：它在中文论坛绝大多数指论坛自己的积分体系（`发点积分` `囤积分` `积分抽奖中奖概率降低了？`），
      当领域词会让整类闲聊通过 AND 判定。实测移出后 store 掉 7 条，其中 6 条正是这类闲聊。
    - 福利信号：`免费` `送` `赠` `白嫖` `抽` `抽奖` `兑换` `邀请` `注册` `领取` `领` `福利` `试用` `优惠` `折` `限时` `羊毛` `红包` `纳新` `撸` `抢` `新用户` `获得` `发` `蹬`
      `薅` `返现` `低价` `特价` `首充` `不花钱`（后六个 2026-10-04 补，实测各自救回真货；`刷新`/`享受` 来自 linux.do 黑话，置信度低，改闸门时优先怀疑这两个）。
    - `注册` 留在信号里是必须的（去掉丢 5 条真货：`注册送10刀` / `github注册15刀`…），它的教程噪音由否决词挡掉。
    - `抽` 收单字而不是只收 `抽奖`：`先抽个奖叭`、`抽50个10¥余额` 中间隔了字，只做 `抽奖` 子串会漏（实测净增 3 条、0 噪音）。
      `蹬` 在黑话里 = 免费用额度（`爽蹬$1000刀`、`【猛蹬】claude顶级模型不花钱`），`国模` = 国产模型（`新站开业，国模免费用`，不含「模型」二字）。这三个词实测净增 11 条真货、0 条噪音。
    - **新分类「优惠渠道」**（用户 2026-10-04 要求，专门收集低价/优惠的中转渠道）：`折扣` `折` `特价` `低价` `优惠` `首充` `充值` `返利` `倍率`。
      这是 `CATEGORY_KEYWORDS` 里的**打标**分类，与闸门词是两套表，别混。
  - **模型名要用 ASCII 前后瞻，不能用 `\b`**：Python 里中文也算 `\w`，所以 `r"\bgpt\b"` 匹配不到「月gpt」（`月` 与 `g` 之间没有边界）。`_bounded()` 用 `(?<![a-z0-9])…(?![a-z])`，前者挡 `openai` 里的 `ai`，后者挡 `keyboard` 里的 `key`，但放行 `GPT6` / `GLM-5.3` 这种带版本号的写法。
  - **长模型名另用 `_prefix_bounded()`（只卡左边界）**：`deepseekv4flash` / `DeepSeekharness` 里模型名后面直接粘字母，`_bounded` 的 `(?![a-z])` 会把它们整个挡掉（实测漏 3 条真货）。
    只对足够长、不可能是别的英文单词片段的名字放开（claude/deepseek/gemini/openai/nemotron/longcat/antigravity）；`ai` `ds` `glm` `gpt` 这类短词保持严格边界。
  - **`免费` 只做福利信号、不做领域词**：放松成领域词会把 `免费苹果共享ID`、`免费领天翼云手机`、`免费节点分享`、`免费esim7天` 整类噪音带回来。同理 `机场`/`VPS` 类词一个都不收（它们现在是第 1 档的**否决**词）。
  - 各档都过不了即整条丢弃，`CATEGORY_KEYWORDS` 根本不跑——所以闸门词与分类词的差异是**有意**的，别顺手「统一」。
- **已结束 / 已被源站删除的帖子不留到下一轮**（用户 2026-10-04 决定）：
  - **已结束**：标题带第 0 档标记，判定是纯函数，每轮都清，不花任何请求。
  - **源站删除**：这一轮源站看得成、这行却已不在列表里，且 `probe_deleted()` 探到 **404/410** 才移除。
    分页漂移会让大量活帖同样「不在列表里」，所以**不能**把缺席当删除，必须回源头确认状态码。
  - **不对称是刻意的**：`403`（WAF）/ `429`（限速）/ `5xx` / 超时 / DNS 失败全部**保留**。删错不可恢复——
    行一离开 store，就再没有哪一轮会去重抓它；多留一轮则毫无代价。这条有专门的 guard 测试盯着。
  - **源站没看成的轮次绝不删它的行**：那时「不在列表里」只说明我们没看到，不说明帖子没了。
    判断靠 `unhealthy_hosts()`（该源这轮一个 topic 都没解析出来，或报过抓取错），粒度取粗：某源一个板块失败就算整源不健康。
  - **覆盖靠轮转**：候选按最新在前排序后，用「UTC 小时数 × 窗口大小 mod 候选数」取一段窗口，不落任何状态文件，每轮换一批，
    约 `候选数 / --probe-missing` 小时整库扫完一遍。只取前 N 条的话，排在后面的候选一辈子查不到——而沉底老帖恰恰最可能被源站删。
  - **闸门对 store 里的老行同样生效**：只判「新抓到的行」会漏掉一整类——用旧规则放进来的噪音没有任何一轮会重判它，
    会一直留到被 cap 淘汰（实测 store 里积了 28 条，正是用户抱怨的那批）。判据就是 `is_relevant()`，
    所以「已结束 / 非 AI 标的 / 教程 / 会员无价格」都会连带把老行一起清掉。
  - **但有一次删太多的保险**：若一次要删掉 **>30% 且库存 ≥20 行**，判定为「闸门写错了」而不是「噪音多」，
    **一条都不删**，并记进 `FETCH_ERRORS`（CI 变红、家宽侧打 WARN）。删掉的行若已沉出列表就再也抓不回来，
    宁可这轮留着噪音。两边的边界都有 guard 测试（40% 拒绝 / 20% 照删）。
  - 探测默认 15 条/轮，关掉用 `--probe-missing 0`。它确实加时间（本机 15 条约 100 秒，家宽侧更快），换的是「源站删帖不会在页面上挂到过期」。
- **筛选按钮从数据生成，绝不写死**（`generate.py:build_filters`）：写死曾同时造成两个上线后才发现的 bug ——
  新加的源（vibex）有 46 张卡却没有来源按钮，新加的分类（优惠渠道）有卡片却没有分类按钮。
  按 `SOURCE_LABELS` / `CATEGORY_ORDER` 定顺序，表外的值（新源、新分类）**排在后面且不丢** —— 静默丢弃正是这两个 bug 的成因。
  JS 侧的来源过滤也必须是通用前缀匹配（`c.source.indexOf(sourceFilter)===0`），否则按钮出现了却点不动。
- **打标签和闸门一样，是标题的纯函数，每轮对整库重算**：只在抓取时打标会让旧行的标签永久冻结
  （实测：按当时词表「优惠渠道」6 条，按当前词表该有 32 条 —— 用户看到的分类筛选结果是旧的）。
- **分类词必须是「这个分类特有的标的词」，不能是通用动作词**：`额度` 靠裸 `送` 给 18 条不含额度/刀的帖打标；
  `兑换码` 靠裸 `code` 命中 `zcode`（臭鸡蛋）和 `codex`（claude code 公益站）。通用词（送/免费/抽）只能进闸门。
- **闸门词要用前瞻排除反义搭配**：裸 `发` 会命中 开发/发现/发布（`报! 发现1不错GPT中转` 靠它混进来），
  但它又必须留（`又发1亿token`、`发鸡蛋啦`、`发点999刀CDK` 共 8 条真货），所以写成 `发(?!现|布|表|出|明|射|展)`。
- **合并键必须能从 store 里重新算出来**：键是 `host#id`，`host` 取自 url、url 缺失时退回 source。这一条是硬约束——
  键只在内存里，store 存的是行的字段，所以任何「运行时才知道的键」（比如撞号时临时改成另一种写法）在下一轮就丢了，
  那一行会被当成全新行、甚至被另一站同名 id 挤掉。跨站撞号因此是**结构性避免**的，不需要运行时补救。
- **过滤在打标签之前**：标题过不了上面的三段式闸门，`CATEGORY_KEYWORDS` 根本不会跑。所以「闸门词」与「分类词」是两套不同的表，
  差异是有意的（`免费` 只在分类里，`抽` 只在闸门里）——详见上面的三段式判定说明。
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
