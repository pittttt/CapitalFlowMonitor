# -*- coding: utf-8 -*-
"""爬取同花顺行业板块列表和成分股，固化到 data/sectors.json。

- 板块列表：data.10jqka.com.cn/funds/hyzjl/ 翻页（90 个行业，881xxx 代码）
- 成分股：q.10jqka.com.cn/thshy/detail/code/{code}/page/{n}/（服务端渲染 HTML）
  - q.10jqka.com.cn 对无 cookie 的 IP 有约 10 页配额，超过后触发登录墙
  - 设置环境变量 THS_COOKIE（10jqka 登录 cookie）可完整绕过
- 月刷新：sectors.json 的 fetched_at 距今 < 30 天则跳过

用法：python scripts/fetch_sectors.py [--force]
"""
import argparse
import datetime as dt
import json
import os
import re
import sys
import time

import requests

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from westock_client import _ths_session  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SECTORS_FILE = os.path.join(ROOT, "data", "sectors.json")
REFRESH_DAYS = 30

# 10jqka 登录 cookie（绕过 q.10jqka.com.cn 登录墙），来源：
# 1. 环境变量 THS_COOKIE（GitHub Actions Secrets 同名字段）
# 2. 项目根目录 .ths_cookie 文件（本地开发，已 gitignore）
def load_ths_cookie():
    c = os.environ.get("THS_COOKIE", "").strip()
    if c:
        return c
    p = os.path.join(ROOT, ".ths_cookie")
    if os.path.exists(p):
        with open(p, encoding="utf-8-sig") as f:
            return f.read().strip()
    return ""

# 行业分类导航页（固定分类，一次请求拿全 90 个行业；无 cookie 时改用资金流排行榜）
NAV_URL = "https://q.10jqka.com.cn/thshy/"
NAV_REFERER = "https://q.10jqka.com.cn/"
# 板块列表接口（fallback，完整浏览器指纹头，实测无需 cookie）
LIST_URL = "https://data.10jqka.com.cn/funds/hyzjl/field/tradezdf/order/desc/page/{pg}/ajax/1/free/1/"
LIST_REFERER = "https://data.10jqka.com.cn/funds/hyzjl/"
# 成分股页面（服务端渲染）
CONS_URL = "https://q.10jqka.com.cn/thshy/detail/code/{code}/page/{pg}/"
CONS_REFERER = "https://q.10jqka.com.cn/thshy/detail/code/{code}/"


def fetch_sector_list(cookie=""):
    """获取全部行业板块，返回 [{"code","name"},...]。

    优先用行业分类导航页（固定 90 个）；无 cookie 时导航页有配额风险，
    改用资金流排行榜接口（50+40 页）。
    """
    sectors = {}

    def parse_nav(sess):
        r = sess.get(NAV_URL, timeout=30)
        r.raise_for_status()
        r.encoding = "gbk"
        for code, name in re.findall(r"detail/code/(\d+)/[^>]*>([^<]+)<", r.text):
            sectors.setdefault(code, name.strip())

    def parse_ranking(sess):
        for pg in range(1, 8):
            try:
                r = sess.get(LIST_URL.format(pg=pg), timeout=30)
                if r.status_code in (401, 403):
                    break
                r.raise_for_status()
                r.encoding = "gbk"
                found = re.findall(r"detail/code/(\d+)/[^>]*>([^<]+)<", r.text)
            except (requests.exceptions.RequestException, OSError):
                time.sleep(2)
                continue
            if not found:
                break
            new_count = 0
            for code, name in found:
                if code not in sectors:
                    sectors[code] = name.strip()
                    new_count += 1
            if new_count == 0:
                break
            time.sleep(0.4)

    sess = _ths_session(NAV_REFERER)
    if cookie:
        sess.headers["Cookie"] = cookie
    try:
        parse_nav(sess)
    except (requests.exceptions.RequestException, OSError):
        pass
    if not sectors:
        sess2 = _ths_session(LIST_REFERER)
        parse_ranking(sess2)
    return [{"code": c, "name": n} for c, n in sectors.items()]


def fetch_constituents(code, cookie=""):
    """翻页获取板块成分股（6 位股票代码列表）。

    cookie: 10jqka 登录 cookie；为空时 q 域名仅有约 10 页配额，大板块会截断
    """
    stocks = []
    sess = _ths_session(CONS_REFERER.format(code=code))
    if cookie:
        sess.headers["Cookie"] = cookie
    quota_hit = False
    for pg in range(1, 50):
        found = False
        codes = []
        for attempt in range(3):
            try:
                r = sess.get(CONS_URL.format(code=code, pg=pg), timeout=30)
                if r.status_code in (401, 403):
                    time.sleep(2.5 * (attempt + 1))
                    continue
                r.raise_for_status()
                r.encoding = "gbk"
                # 登录墙页面：重定向到 upass / Nginx forbidden（无 cookie 时约 10 页触发）
                if "upass" in r.text[:300] or "forbidden" in r.text[:300].lower():
                    quota_hit = True
                    if not cookie:
                        break
                    time.sleep(3 * (attempt + 1))
                    continue
                codes = re.findall(r"stockpage\.10jqka\.com\.cn/(\d{6})/", r.text)
                found = True
                break
            except (requests.exceptions.RequestException, OSError):
                time.sleep(1.5 * (attempt + 1))
        if quota_hit:
            print("  [warn] %s 第 %d 页触发登录墙，提前结束（建议配置 THS_COOKIE）" % (code, pg), flush=True)
            break
        if not found:
            print("  [warn] %s 第 %d 页连续失败，视为页末" % (code, pg), flush=True)
            break
        if not codes:
            break
        before = len(stocks)
        for c in codes:
            if c not in stocks:
                stocks.append(c)
        if len(stocks) == before:
            break
        if len(codes) < 20:
            break
        time.sleep(0.3)
    return stocks


def load_existing():
    if os.path.exists(SECTORS_FILE):
        with open(SECTORS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--force", action="store_true", help="忽略 30 天缓存强制刷新")
    args = ap.parse_args()

    existing = load_existing()
    if existing and not args.force:
        fetched = dt.datetime.fromisoformat(existing["fetched_at"])
        age = (dt.datetime.now() - fetched).days
        if age < REFRESH_DAYS:
            print("板块数据 %s 天内已刷新（%s 天前），跳过。用 --force 强制刷新" % (REFRESH_DAYS, age))
            return

    print("拉取同花顺行业板块列表...")
    sectors = fetch_sector_list(load_ths_cookie())
    print("板块数: %d" % len(sectors))

    if not sectors:
        print("!! 板块列表为空，退出")
        sys.exit(1)

    cookie = load_ths_cookie()
    if not cookie:
        print("""
!! 需要 10jqka 登录 cookie 才能完整抓取板块成分股（q.10jqka.com.cn 对无 cookie 请求有约 10 页/IP 配额限制，超过后触发登录墙）。

获取方式：浏览器登录 https://q.10jqka.com.cn 后，F12 → Network → 刷新 → 复制任意请求的 Cookie 请求头。
传入方式（任选其一）：
  1. 环境变量：set THS_COOKIE=<cookie>  然后重跑本脚本（GitHub Actions 在仓库 Secrets 中配置 THS_COOKIE）
  2. 文件：在项目根目录创建 .ths_cookie 文件写入 cookie（已 gitignore，不会上传）

本次跳过成分股更新（保留现有 sectors.json）。
""")
        sys.exit(0)

    for i, s in enumerate(sectors):
        cons = fetch_constituents(s["code"], cookie)
        s["constituents"] = cons
        print("[%d/%d] %s (%s): %d 只成分股" % (i + 1, len(sectors), s["name"], s["code"], len(cons)), flush=True)
        time.sleep(0.3)

    os.makedirs(os.path.dirname(SECTORS_FILE), exist_ok=True)
    payload = {
        "fetched_at": dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "source": "10jqka(thshy)",
        "sector_count": len(sectors),
        "sectors": sectors,
    }
    with open(SECTORS_FILE, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=1)
    total = sum(len(s["constituents"]) for s in sectors)
    print("已写入 %s，共 %d 只成分股（去重后约 %d）" % (SECTORS_FILE, total, len({c for s in sectors for c in s["constituents"]})))


if __name__ == "__main__":
    main()
