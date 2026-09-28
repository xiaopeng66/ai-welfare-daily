# LinuxSB 每日福利站

自动抓取 **linux.sb**、**baipiao.org**、**nodeloc.com** 等站的 AI 中转站福利帖子，每日更新。

**数据来源：** linux.sb（福利放送/我要推广/抽奖/发卡）、baipiao.org/bbs、nodeloc.com/latest  
**更新频率：** 每天 20:00（UTC+8）  
**托管：** GitHub Pages

## 本地运行

```bash
# 抓取数据
python3 fetch.py -o data/topics.jsonl

# 生成网页
python3 generate.py -i data/topics.jsonl -o docs/index.html
```

## 在线访问

https://xiaopeng66.github.io/linuxsb-daily/
