# -*- coding: utf-8 -*-
"""计算板块历史维度数据并输出 docs/data/history.json。

维度：
- 涨幅 / 5日涨幅：同花顺官方板块指数（881xxx 加权指数）日K，与同花顺页面完全一致
- 主力净流入：成分股当日主力净流入（腾讯 MainNetFlow）求和，单位亿元

更新策略：
- 板块指数每次全量重拉（90 次请求，接口返回最近 140 个交易日）
- 资金流默认增量（读 meta.last_date，只拉新交易日的个股资金流）；--full 全量重建

用法：python scripts/fetch_history.py [--full] [--limit N] [--no-flow]
"""
import argparse
import datetime as dt
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from westock_client import sector_kline, fund_flow_batch  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SECTORS_FILE = os.path.join(ROOT, "data", "sectors.json")
HISTORY_FILE = os.path.join(ROOT, "docs", "data", "history.json")

WINDOW_DAYS = 60        # 输出窗口（交易日）
FETCH_DAYS = 65         # 拉取窗口（多取 5 天供 5 日涨幅计算）
FLOW_LOOKBACK_DAYS = 95  # 全量时资金流回溯自然日（约 60+ 交易日）


def to_tencent_code(code6):
    if code6.startswith(("60", "68")):
        return "sh" + code6
    if code6.startswith(("00", "30")):
        return "sz" + code6
    return "bj" + code6  # 北交所等


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
    ap.add_argument("--full", action="store_true", help="资金流全量重建（60 日）")
    ap.add_argument("--limit", type=int, default=0, help="仅处理前 N 个板块（调试用）")
    ap.add_argument("--no-flow", action="store_true", help="跳过资金流维度")
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

    # ---------- 2. 主力净流入（成分股求和） ----------
    series_flow = {}
    if not args.no_flow:
        old_flow = old_series.get("netinflow") or {}
        last_date = old_meta.get("last_date") or ""

        if args.full or not old_dates or last_date not in old_dates or last_date not in dates:
            # 历史缺失、或 last_date 已被 60 日窗口挤出（长期未更新）时全量重建
            start = (dt.date.today() - dt.timedelta(days=FLOW_LOOKBACK_DAYS)).strftime("%Y-%m-%d")
            print("资金流全量模式，起始 %s" % start)
            new_dates = dates
        else:
            idx = old_dates.index(last_date)
            new_dates = dates[idx + 1:]
            start = last_date
            print("资金流增量模式：已有 %s，新增 %d 个交易日" % (last_date, len(new_dates)))

        if new_dates:
            new_set = set(new_dates)
            # 收集全部成分股（当前 sectors.json）
            all_codes = sorted({c for s in sectors for c in s["constituents"]})
            tencent_codes = [to_tencent_code(c) for c in all_codes]
            print("拉取 %d 只成分股资金流（%s ~ %s）..." % (len(tencent_codes), start, dt.date.today()))
            flows = fund_flow_batch(
                tencent_codes, start, dt.date.today().strftime("%Y-%m-%d"),
                workers=8,
                progress=lambda d, t: print("资金流 %d/%d" % (d, t), flush=True),
            )
            # 数据新鲜度校验：腾讯资金流结算有延迟（实测收盘后约 1 小时仍为前日数据），
            # 若返回的最新日期落后于板块指数最新交易日，本次跳过写入，次日增量会自动补齐
            flow_dates = [rec["date"] for recs in flows.values() if recs for rec in recs]
            latest_flow = max(flow_dates) if flow_dates else None
            if latest_flow != new_dates[-1]:
                print(
                    "[warn] 腾讯资金流最新日期 %s != 板块指数最新交易日 %s，数据未结算，本次跳过资金流更新（次日自动补齐）"
                    % (latest_flow, new_dates[-1]),
                    flush=True,
                )
                new_dates = []
            # code6 -> flows
            code6_to_tencent = {c: to_tencent_code(c) for c in all_codes}
            tencent_to_code6 = {v: k for k, v in code6_to_tencent.items()}

            # 板块 -> 成分股 tencent code 列表 + 股票 -> 所属板块索引
            sector_stocks = {}
            stock_owners = {}
            for s in sectors:
                tc_list = [to_tencent_code(c) for c in s["constituents"]]
                sector_stocks[s["name"]] = tc_list
                for tc in tc_list:
                    stock_owners.setdefault(tc, []).append(s["name"])

            # 计算每个板块每个新交易日的净流入（元 -> 亿元）
            day_flow_sum = {d: {} for d in new_dates}
            for tc, recs in flows.items():
                if not recs:
                    continue
                owners = stock_owners.get(tc)
                if not owners:
                    continue
                for rec in recs:
                    d = rec["date"]
                    if d not in day_flow_sum:
                        continue
                    val = rec["main_net_flow"]
                    for nm in owners:
                        day_flow_sum[d][nm] = day_flow_sum[d].get(nm, 0.0) + val

            # 序列按日期对齐：历史旧值（旧成分股口径）按日期映射，新日期用新成分股计算
            # 注意 60 日窗口滚动后旧序列与 dates 可能错位，不能按索引拼接
            old_by_date = {}
            for nm_old, arr in old_flow.items():
                old_by_date[nm_old] = dict(zip(old_dates, arr))
            for nm in [s["name"] for s in sectors]:
                hist = old_by_date.get(nm, {})
                series_flow[nm] = []
                for d in dates:
                    if d in new_set:
                        v = day_flow_sum.get(d, {}).get(nm)
                        series_flow[nm].append(None if v is None else round(v / 1e8, 4))
                    else:
                        series_flow[nm].append(hist.get(d))
        else:
            print("无新增交易日，资金流跳过")
            series_flow = {s["name"]: old_flow.get(s["name"]) for s in sectors}

    # ---------- 3. 写输出 ----------
    payload = {
        "meta": {
            "updated_at": dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "last_date": dates[-1],
            "sectors_fetched_at": sectors_data["fetched_at"],
            "trade_dates_count": len(dates),
            "source": "板块指数:同花顺(10jqka) / 主力净流入:腾讯自选股(westock) 成分股求和",
            "inflow_caliber": "非严格同花顺口径：腾讯自选股个股主力净流入(大单+超大单)按板块成分股求和",
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
