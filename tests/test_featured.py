#!/usr/bin/env python3
"""精选规则（featured.py）的单元测试：上限值口径 / 灌水免疫 / 种类窗口 / 控制字符卫生。

2026-10-17 建立——双份 build_items 事故（三个修复打了死代码上、潜伏两天）的直接教训：
featured.py 此前没有任何直接单测，规则回归只能靠整板肉眼。
"""
import datetime
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import featured

CST = featured.CST
NOW = datetime.datetime.now(CST)
failed = 0


def check(ok, msg, extra=None):
    global failed
    if not ok:
        failed += 1
    print(("ok    " if ok else "FAIL  ") + msg + ("" if ok else f" :: {extra}"))


# ---------------------------------------------------------------- R1 上限值口径
muse = featured.amount_of("回到你的muse即可看到30亿token , 最高可🉐300亿")
check(muse[1] == "30亿", "R1 上限词不抢显示：显示确定发放的 30亿", muse)
check(muse[0] == 4.0, "R1 分数仍按上限值计（板位不掉）", muse)
check(featured.amount_of("最高可🉐300亿")[1] == "300亿", "R1 只有上限值时照抄上限")
check(featured.amount_of("注册送20刀，最高可🉐100刀")[1] == "20$", "R1 确定值优先于上限值（显示统一）")
check(featured.amount_of("免费领取20美元额度，月费99美元") == (4.0, "20$", "usd", 20.0),
      "R1 不影响既有价格语境判定（99美元 是价格、20美元 是发放）")

# ---------------------------------------------------------------- R6 额度下限 + 显示统一
check(featured.amount_of("注册送100刀，先到先得") == (5.0, "100$", "usd", 100.0),
      "R6 刀 → $，显示为 数字+符号")
check(featured.amount_of("注册送99元")[1] == "99￥", "R6 元 → ￥", featured.amount_of("注册送99元"))
check(featured.amount_of("送 25 刀 额度")[1] == "25$", "R6 挤掉空格")
check(featured.amount_of("送10美元 额度")[1] == "10$", "R6 美元 → $")
check(featured.amount_of("注册送30亿token 额度")[3] is None, "R6 token 类不折算美元（不受下限影响）")
check(featured.amount_of("注册送2刀 额度")[3] == 2.0, "R6 币种候选带美元值（供下限门槛判）")

# ---------------------------------------------------------------- R4 灌水免疫
padded = "核心工作经历：主导核心业务交易引擎的底层重构，将单体架构迁移至分布式微服务架构，系统吞吐量提升。" * 3
real = ("注册就送 claude 和 gpt 的免费额度，每天都能领，无需付费，模型随便用。"
        "另外还有图像和视频模型的免费试用名额，先到先得。")
check(featured.relevant_len(padded) < 60, "R4 无主题词的灌水长文不计字数", featured.relevant_len(padded))
check(featured.relevant_len(real) >= 60, "R4 真实福利描述行计字数", featured.relevant_len(real))


# ------------------------------------------------------- 种类由额度 ±10 字窗口定
def row(key, title, body, hours_ago=6):
    t = (NOW - datetime.timedelta(hours=hours_ago)).isoformat()
    return {"id": key, "title": title, "url": f"https://x.test/post/{key}",
            "created_at": t, "fetched_at": t, "published_verified": True,
            "tags": [], "score": 0, "source": "test",
            "offer": {"v": 8, "key": key, "site": key, "campaign": key,
                      "amount": "", "brand": "", "at": t}}


def kind_of(title, body):
    r = row("probe", title, body)
    cache = {r["url"]: {"body": body, "meta": {"links": []}}}
    items = featured.build_items([r], cache, NOW)
    return items[0]["f"]["kind"] if items else None


# docode 实测：标题带"每日无条件抽奖"，但 300刀来自「使用邀请码赠送300刀」→ 送的，不是抽的
check(kind_of("【DoCode 专属福利】注册即送300刀免费额度，每日无条件抽奖",
              "注册后在控制台创建 API Key，复制接口地址。注册充值五重好礼："
              "1、使用邀请码赠送300刀。2、使用专属agent可领额度。一定要使用邀请码才可以获得300刀额度。"
              ) == "gift",
      "种类：额度行是「赠送」→ gift，不被标题里的抽奖带偏")

# undy 实测：额度就在抽奖句里（「评论抽奖送$500」）→ 抽奖
check(kind_of("【 Undy 】评论抽奖",
              "回帖参与评论抽奖，送$500 额度，10个名额，周五开奖。模型支持 gemini 和 gpt，注册即可使用。"
              ) == "lottery",
      "种类：额度行带「抽奖」→ lottery")

# nexotoken 实测：额度在奖品表里（窗口无抽奖也无发放动词），全帖在聊抽奖 → 保守按抽奖
check(kind_of("【Nexo】每周四免费抽奖",
              "本周奖品如下。奖品 名额 有效期：$10 平台虚拟额度 2 份，到账后 14 天。"
              "每周四开奖，请关注公告。"
              ) == "lottery",
      "种类：窗口无发放动词而全帖抽奖 → 保守 lottery")

# ---------------------------------------------------------------- 控制字符卫生
for mod in (featured, sys.modules["content"] if "content" in sys.modules else None):
    if mod is None:
        continue
    check("\x08" not in open(mod.__file__, encoding="utf-8").read(),
          f"{mod.__name__}.py 无控制字符残留（concrete 词表 api 后曾混入退格符）", mod.__file__)

print(f"\n{failed} failed" if failed else "\n全部通过")
sys.exit(1 if failed else 0)
