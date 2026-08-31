# -*- coding: utf-8 -*-
"""计算板块历史维度数据并输出 docs/data/history.json。

维度：
- 涨幅 / 5日涨幅：同花顺官方板块指数（881xxx 加权指数）日K，与同花顺页面完全一致
- 主力净流入：同花顺官方行业资金流页面（hyzjl）当日板块净额，每日累积，与同花顺页面完全一致

更新策略：
- 板块指数每次全量重拉（90 次请求，接口返回最近 140 个交易日）
- 官方净额每日追加（hyzjl 页面 2 次请求 + 涨跌幅交叉验证确认数据日期）

用法：python scripts/fetch_history.py [--limit N] [--no-flow]
"""
import argparse
import datetime as dt
import json
import os
import re
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from westock_client import sector_kline, _ths_session  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SECTORS_FILE = os.path.join(ROOT, "data", "sectors.json")
HISTORY_FILE = os.path.join(ROOT, "docs", "data", "history.json")

WINDOW_DAYS = 60        # 输出窗口（交易日）
FETCH_DAYS = 65         # 拉取窗口（多取 5 天供 5 日涨幅计算）

# 同花顺行业资金流页面（当日板块净额，单位亿元）
THS_FLOW_URL = "https://data.10jqka.com.cn/funds/hyzjl/field/tradezdf/order/desc/page/{pg}/ajax/1/free/1/"
THS_FLOW_REFERER = "https://data.10jqka.com.cn/funds/hyzjl/"


def fetch_ths_sector_flow():
    """抓同花顺行业资金流页面当日板块净额。

    返回 {板块名: {"net": 净额(亿元), "chg": 涨跌幅(%)}}；失败返回 None。
    hyzjl 页面数据为最新交易日；页面无日期字段，用涨跌幅列与板块指数交叉验证。
    """
    out = {}
    sess = _ths_session(THS_FLOW_REFERER)
    for pg in (1, 2):
        r = None
        for attempt in range(5):
            try:
                r = sess.get(THS_FLOW_URL.format(pg=pg), timeout=30)
                if r.status_code in (401, 403):
                    raise RuntimeError("http %s" % r.status_code)
                r.raise_for_status()
                r.encoding = "gbk"
                break
            except (Exception, OSError):  # noqa: BLE001
                if attempt < 4:
                    time.sleep(3 * (attempt + 1))
                else:
                    r = None
        if r is None:
            print("[warn] hyzjl 第 %d 页连续失败" % pg, flush=True)
            continue
        for row in re.findall(r"<tr[^>]*>(.*?)</tr>", r.text, re.S):
            m = re.search(r"detail/code/(\d+)/[^>]*>([^<]+)<", row)
            tds = re.findall(r"<td[^>]*>(.*?)</td>", row, re.S)
            if not m or len(tds) < 7:
                continue
            name = m.group(2).strip()
            clean = lambda s: re.sub(r"<[^>]+>", "", s).strip().replace(",", "")
            try:
                net = float(clean(tds[6]))
                chg = float(clean(tds[3]).rstrip("%"))
            except ValueError:
                continue
            out[name] = {"net": net, "chg": chg}
    return out or None


def load_json(path):
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    return None


def fetch_sector_klines(sectors):
    """拉全部板块指数日K，返回 {code: {date: close}}。"""
    out = {}
    for i, s in enumerate(sectors):
        try:
            d = sector_kline(s["code"])
            bars = d["bars"][-FETCH_DAYS:]
            out[s["code"]] = {b["date"]: b["close"] for b in bars}
        except Exception as e:  # noqa: BLE001
            print("[warn] 板块指数 %s(%s) 拉取失败: %s" % (s["name"], s["code"], e), flush=True)
        if (i + 1) % 10 == 0:
            print("板块指数 [%d/%d]" % (i + 1, len(sectors)), flush=True)
    return out


def compute_kline_series(closes_by_date, dates):
    """由收盘价序列计算涨幅(%)和5日涨幅(%)。"""
    chg, chg5 = [], []
    prev = None
    for i, d in enumerate(dates):
        c = closes_by_date.get(d)
        if c is None:
            chg.append(None)
            chg5.append(None)
            continue
        chg.append(None if prev is None else round((c / prev - 1) * 100, 2))
        prev = c
    for i, d in enumerate(dates):
        c = closes_by_date.get(d)
        c5 = closes_by_date.get(dates[i - 5]) if i >= 5 else None
        chg5.append(None if (c is None or c5 is None or c5 <= 0) else round((c / c5 - 1) * 100, 2))
    return chg, chg5


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0, help="仅处理前 N 个板块（调试用）")
    ap.add_argument("--no-flow", action="store_true", help="跳过官方净额抓取")
    args = ap.parse_args()

    sectors_data = load_json(SECTORS_FILE)
    if not sectors_data:
        print("!! 缺少 %s，先运行 fetch_sectors.py" % SECTORS_FILE)
        sys.exit(1)
    sectors = sectors_data["sectors"]
    if args.limit:
        sectors = sectors[: args.limit]

    old = load_json(HISTORY_FILE)
    old_dates = old["dates"] if old else []
    old_series = (old.get("series") or {}) if old else {}
    old_meta = (old.get("meta") or {}) if old else {}

    # ---------- 1. 板块指数 → 涨幅 / 5日涨幅 ----------
    print("拉取板块指数日K（%d 个板块）..." % len(sectors))
    klines = fetch_sector_klines(sectors)

    # 交易日序列：取第一个有数据的板块，否则用旧的 dates
    all_dates = sorted({d for k in klines.values() for d in k})
    if not all_dates:
        all_dates = old_dates
    dates = all_dates[-WINDOW_DAYS:]
    print("交易日: %d 个 (%s ~ %s)" % (len(dates), dates[0], dates[-1]))

    series_chg = {}
    series_chg5 = {}
    for s in sectors:
        closes = klines.get(s["code"]) or {}
        chg, chg5 = compute_kline_series(closes, dates)
        series_chg[s["name"]] = chg
        series_chg5[s["name"]] = chg5

    # ---------- 2. 主力净流入（同花顺官方净额累积） ----------
    series_flow = {}
    ths_last = old_meta.get("ths_last_date") or ""
    new_day = dates[-1]
    old_by_date = {}
    for nm_old, arr in (old_series.get("netinflow") or {}).items():
        old_by_date[nm_old] = dict(zip(old_dates, arr))
    series_flow = {s["name"]: [old_by_date.get(s["name"], {}).get(d) for d in dates] for s in sectors}

    if not args.no_flow:
        if new_day == ths_last:
            print("官方净额已更新至 %s，跳过" % new_day)
        else:
            print("抓取同花顺官方板块净额（%s）..." % new_day)
            # 日级原子性：官方净额须覆盖绝大多数板块（>=80）才写入，避免混口径；
            # 不足时等待重试（hyzjl 页面偶发不稳定/限流）
            today = None
            for rnd in range(3):
                today = fetch_ths_sector_flow()
                if today and len(today) >= 80:
                    break
                print(
                    "[warn] 官方净额仅 %s 个板块（需要 >=80），等待 60s 重试（第 %d 轮）"
                    % (len(today) if today else 0, rnd + 1),
                    flush=True,
                )
                time.sleep(60)
            if not today or len(today) < 80:
                print("[warn] 官方净额抓取不完整，本次跳过写入（保留旧值）")
            else:
                # 交叉验证：hyzjl 涨跌幅列 vs 板块指数最新交易日涨幅，一致率高则确认数据同日
                ok_cnt, total = 0, 0
                idx = dates.index(new_day) if new_day in dates else -1
                for nm, f in today.items():
                    if idx < 0:
                        break
                    v = series_chg.get(nm, [None] * len(dates))[idx]
                    if v is not None and f["chg"] is not None:
                        total += 1
                        if abs(v - f["chg"]) < 0.05:
                            ok_cnt += 1
                if total >= 30 and ok_cnt / total < 0.8:
                    print("[warn] hyzjl 涨跌幅与板块指数不一致（%d/%d），可能日期错位，跳过官方净额写入" % (ok_cnt, total))
                else:
                    for nm in series_flow:
                        if nm in today:
                            series_flow[nm][-1] = today[nm]["net"]
                    ths_last = new_day
                    print("官方净额已写入 %d 个板块（交叉验证 %d/%d 通过）" % (len(today), ok_cnt, total))

    # ---------- 3. 写输出 ----------
    payload = {
        "meta": {
            "updated_at": dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "last_date": dates[-1],
            "ths_last_date": ths_last,
            "sectors_fetched_at": sectors_data["fetched_at"],
            "trade_dates_count": len(dates),
            "source": "板块指数:同花顺(10jqka) / 主力净流入:同花顺官方行业资金流(hyzjl)每日净额累积",
            "inflow_caliber": "同花顺官方口径：行业资金流页面当日板块净额，每日累积；累积前为 null",
        },
        "dates": dates,
        "series": {
            "chg": series_chg,
            "chg5": series_chg5,
            "netinflow": series_flow,
        },
    }
    os.makedirs(os.path.dirname(HISTORY_FILE), exist_ok=True)
    with open(HISTORY_FILE, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=1)
    # 站点内同步一份板块快照，前端用它对比成分股更新时间（徽标提示）
    with open(os.path.join(ROOT, "docs", "data", "sectors.json"), "w", encoding="utf-8") as f:
        json.dump(sectors_data, f, ensure_ascii=False, indent=1)
    print("已写入 %s（%d 个交易日，%d 个板块）" % (HISTORY_FILE, len(dates), len(sectors)))


if __name__ == "__main__":
    main()
