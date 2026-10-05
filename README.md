# AI 福利日报

聚合五个中文论坛的 **AI 中转站 / 公益站 / 额度福利**帖，每小时自动抓取、去重、打标，生成一个纯静态页面发布到 GitHub Pages —— 无后端、无数据库、无人工。

**在线访问：** https://xiaopeng66.github.io/ai-welfare-daily/

[![daily-update](../../actions/workflows/daily-update.yml/badge.svg)](../../actions/workflows/daily-update.yml)

## 数据源

| 站点 | 抓取范围 |
|------|----------|
| linux.sb | 福利放送、我要推广、抽奖、发卡、首页 |
| baipiao.org | 全站最新 |
| nodeloc.com | 全站最新、抽奖福利 |
| vibex.iflow.cn | 补给站 |
| linux.do | 福利羊毛 |

## 站点功能

- **两种分区**：近 24 小时 / 24 小时前，另有按日期分组的视图。
- **两个筛选维度**：按来源、按分类（标签由标题关键词自动打出）。
- **两种排序**：按匹配度、按时间。
- **「近 24 小时」实时计算**：用读者浏览器时钟判定，不是抓取时冻结的标记。
- **全站北京时间**：跨时区访问时日期分组与卡片时间一致。
- **匹配度**：标题关键词的机械匹配分，不代表推荐或背书。
- **纯静态单文件**：无 CDN、无框架、无外部依赖，可直接离线打开。

## 使用方式

打开 https://xiaopeng66.github.io/ai-welfare-daily/ 即可，**没有登录、没有 API、没有配置**，页面自助筛选。

## 本地运行

```bash
python3 fetch.py -o data/topics.jsonl                            # 抓取
python3 generate.py -i data/topics.jsonl -o docs/index.html      # 生成页面
```

| 环境变量 | 默认 | 用途 |
|----------|------|------|
| `LINUXDO_ENABLED` | `1` | 设 `0` 跳过 linux.do（CI 与 WSL 调试用） |
| `CLASH_PROXY_HOST` / `CLASH_PROXY_PORT` | `127.0.0.1` / `7897` | 代理地址；设 `CLASH_PROXY_PORT=0` 直连 |

## 目录结构

```
fetch.py             抓取 + 合并 + 落盘
generate.py          渲染 docs/index.html
run-update.ps1       更新入口（抓取 → 生成 → 提交）
tests/               离线测试套件、A/B 对拍及可复核语料证据
data/topics.jsonl    store：一行一个 JSON
docs/index.html      生成的站点（GitHub Pages 直接服务这个目录）
```

## 规则离线验证

`python3 tests/test_ab_rules.py` 验证 A/B 报告不会原地改写库存行，统计保留行的标签或分数变化，并逐条显示旧值与新值，且同标题不同 URL 的候选各有独立 JSONL 判定。对拍工具通过复制输入行隔离原地打标签函数；`--jsonl PATH` 对每条闸前候选及库存行用两版规则各自计算闸门、标签、分数与 URL；损坏的语料行会按行号报告并跳过，完整扫描后才写结果；`tests/evidence/rules-20261006/generate_report.py` 从 `verdicts.jsonl` 重新生成人读报告。新增规则断言还须运行 `python3 tests/test_parsers.py`（真实正反例）；正式规则评估须对同一份 `--dump-candidates` 闸前语料执行 `tests/ab_rules.py`；无标签数量不代表分类准确率。详见 [RULES.md](RULES.md)。本轮逐条差异与冻结输入见 [规则修复证据](tests/evidence/rules-20261006/REPORT.md)。

## 更新管道

- **Windows 计划任务**（主）：住宅 IP，每小时，包含 linux.do。
- **Hermes cron**（兜底）：无登录会话时接替计划任务，每小时，带新鲜度闸门。
- **GitHub Actions**（兜底）：每 6 小时，不依赖本机开机，但跳过 linux.do。

三者错峰运行，靠互斥锁与 rebase 串行化，同一时刻只会有一个在写。

## 免责声明

本站仅聚合各站点**公开可见**的帖子标题与链接，不镜像正文、不代理登录、不绕过任何访问控制。所有内容版权归原发帖人与原站点所有。

---

> 抓取规则、合并策略、失败处理、调度配合与踩过的坑，见 **[RULES.md](RULES.md)**。
