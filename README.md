# LinuxSB 每日福利站

自动抓取 **linux.sb**、**baipiao.org**、**nodeloc.com** 等站的 AI 中转站福利帖子，每日更新。

**数据来源：**
- linux.sb：福利放送 /forum/2、我要推广 /forum/8、抽奖 ?sort=lucky、发卡 ?sort=card、首页
- baipiao.org：/bbs/api/discussions
- nodeloc.com：/latest.json（最新）、/c/welfare/12.json（抽奖福利板块）

**更新频率：** 每天 08:00 和 20:00（UTC+8）
**托管：** GitHub Pages

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
- **上限 200 条**：超出时按发布时间淘汰最旧的
- **发布时间**：linux.sb 详情页取 `article:published_time`；baipiao / nodeloc API 的 `created_at` 即发布时间
- **相关性过滤**：标题须含 中转站/公益站/鸡蛋/兑换码/额度/体验金/抽奖 之一
- **部分源失败**：缓存行保留，并写入 `data/.fetch_errors` 供 CI 标记红构建

## 在线访问

https://xiaopeng66.github.io/linuxsb-daily/
