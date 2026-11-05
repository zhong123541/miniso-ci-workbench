#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
竞品情报工作台 · 每日数据更新脚本
流程: Google News RSS 抓取 -> DeepSeek API 结构化 -> 合并 events-latest.json -> 注入 index.html
运行: python3 update_data.py [--dry-run]
环境变量: DEEPSEEK_API_KEY (DeepSeek 平台 API key)
"""
import json
import os
import re
import sys
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone

INDEX_FILE = "index.html"
EVENTS_FILE = "events-latest.json"
MAX_EVENTS = 50

BRANDS = [
    ("miniso", "名创优品", "MINISO"),
    ("popmart", "泡泡玛特", "Pop Mart"),
    ("toptoy", "TOP TOY", "TOP TOY"),
    ("daiso", "大创", "Daiso"),
    ("fivebelow", "Five Below", "Five Below"),
    ("flyingtiger", None, "Flying Tiger Copenhagen"),
]

SYSTEM_PROMPT = """你是零售行业的竞品情报分析师,服务于名创优品(MINISO)的竞品跟踪团队。
任务:从给定新闻列表中提取"值得进入竞品周报"的事件,输出严格 JSON。

输出格式:{"events":[{"date":"YYYY-MM-DD","brand":"品牌key","type":"类型","market":"地区","impact":1,"title":"标题","note":"分析","source":"来源","url":"链接"}]}

brand 枚举(必须使用这些key):miniso=名创优品,popmart=泡泡玛特,toptoy=TOP TOY,daiso=大创,fivebelow=Five Below,flyingtiger=Flying Tiger
type 枚举:财报/开店/联名/产品/营销/供应链/人事/舆情/战略
market 示例:全球/中国/东南亚/北美/日本/韩国/欧洲/中东
impact 打分:5=财报或重大战略(并购/进入新市场),4=大规模开店或头部IP联名,3=新品发布或区域活动,2=日常营销,1=弱信号
规则:
1. 剔除:广告软文、荐股内容、招聘启事、与品牌无关的同名内容、无实质信息的二手转载
2. 同一事件的多条报道只保留信息最全的一条
3. 没有值得收录的事件时输出 {"events":[]}
4. date 从 pubDate 换算为 YYYY-MM-DD(GMT)
5. title 用中文精炼(25字内),保留关键数字
6. note 写"这对竞品跟踪者意味着什么"(25-60字),不是新闻复述
7. url 保留原始链接(可回查即可)
8. 只输出 JSON,不要 markdown 代码块"""


def fetch_rss(query, lang, gl):
    params = urllib.parse.urlencode({
        "q": '"{}" when:2d'.format(query),
        "hl": lang,
        "gl": gl,
        "ceid": "{}:{}".format(gl, "zh-Hans" if lang == "zh-CN" else "en"),
    })
    url = "https://news.google.com/rss/search?" + params
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (compatible; CI-Bot/1.0)"})
    with urllib.request.urlopen(req, timeout=30) as r:
        root = ET.fromstring(r.read().decode("utf-8", "replace"))
    out = []
    for it in root.iter("item"):
        t = (it.findtext("title") or "").strip()
        u = (it.findtext("link") or "").strip()
        p = (it.findtext("pubDate") or "").strip()
        s = (it.findtext("source") or "").strip()
        if t and u:
            out.append({"title": t, "link": u, "pub": p, "src": s})
    return out


def fetch_news():
    items = []
    for key, zh, en in BRANDS:
        for query, lang, gl in ((zh, "zh-CN", "CN"), (en, "en", "US")):
            if not query:
                continue
            try:
                items += fetch_rss(query, lang, gl)
            except Exception as e:
                print("[warn] RSS {} 失败: {}".format(query, e), file=sys.stderr)
    return items


def ask_deepseek(api_key, news):
    payload = {
        "model": "deepseek-chat",
        "temperature": 0.3,
        "response_format": {"type": "json_object"},
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": "新闻列表(JSON):\n" + json.dumps(news, ensure_ascii=False)},
        ],
    }
    req = urllib.request.Request(
        "https://api.deepseek.com/v1/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", "Authorization": "Bearer " + api_key},
    )
    with urllib.request.urlopen(req, timeout=180) as r:
        resp = json.loads(r.read().decode("utf-8"))
    content = resp["choices"][0]["message"]["content"]
    return json.loads(content)


def shanghai_today():
    return datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d")


def load_events():
    if os.path.exists(EVENTS_FILE):
        with open(EVENTS_FILE, encoding="utf-8") as f:
            return json.load(f)
    return []


def merge_events(old, new):
    seen = set()
    merged = []
    for e in list(new) + list(old):
        key = e.get("url") or e.get("title") or ""
        if key in seen:
            continue
        seen.add(key)
        merged.append(e)
    merged.sort(key=lambda e: e.get("date", ""), reverse=True)
    return merged[:MAX_EVENTS]


def inject(html, events):
    js = "const SEED_EVENTS = " + json.dumps(events, ensure_ascii=False, indent=2) + ";"
    html = re.sub(r"const SEED_EVENTS = \[.*?\n\];", js, html, count=1, flags=re.S)
    html = re.sub(
        r"const SNAPSHOT_DATE = '[^']*';",
        "const SNAPSHOT_DATE = '{}';".format(shanghai_today()),
        html,
        count=1,
    )
    return html


def main():
    dry = "--dry-run" in sys.argv
    api_key = os.environ.get("DEEPSEEK_API_KEY", "").strip()
    if not api_key:
        print("[error] 缺少 DEEPSEEK_API_KEY 环境变量", file=sys.stderr)
        sys.exit(1)

    print("[1/4] 抓取新闻 RSS ...")
    news = fetch_news()
    print("      抓到 {} 条新闻".format(len(news)))
    if not news:
        print("[warn] 无新闻可分析,退出")
        sys.exit(0)

    print("[2/4] DeepSeek 结构化 ...")
    result = ask_deepseek(api_key, news[:80])
    new_events = result.get("events", [])
    print("      提取 {} 条事件".format(len(new_events)))

    old = load_events()
    merged = merge_events(old, new_events)
    print("[3/4] 合并: 旧 {} + 新 {} -> {} 条".format(len(old), len(new_events), len(merged)))

    with open(INDEX_FILE, encoding="utf-8") as f:
        html = f.read()
    html2 = inject(html, merged)

    print("[4/4] {}".format("dry-run: 不写文件,新增事件如下" if dry else "写入 index.html 完成"))
    if dry:
        for e in new_events:
            print("  + {} {} {}".format(e.get("date"), e.get("brand"), (e.get("title") or "")[:40]))
        sys.exit(0)

    with open(EVENTS_FILE, "w", encoding="utf-8") as f:
        json.dump(merged, f, ensure_ascii=False, indent=2)
    with open(INDEX_FILE, "w", encoding="utf-8") as f:
        f.write(html2)
    print("完成, 快照日期: " + shanghai_today())


if __name__ == "__main__":
    main()
