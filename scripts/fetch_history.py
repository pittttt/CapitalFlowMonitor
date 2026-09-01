# -*- coding: utf-8 -*-
"""计算板块历史维度数据并输出 docs/data/history.json。

维度：
- 涨幅 / 5日涨幅：同花顺官方板块指数（881xxx 加权指数）日K，与同花顺页面完全一致
- 主力净流入：东财全市场当日主力净流入（f62，与同花顺口径一致）按同花顺成分股聚合，每日累积

更新策略：
- 板块指数每次全量重拉（90 次请求，接口返回最近 140 个交易日）
- 主力净流入每日抓取累积（东财批量接口约 55 页，多域名轮换）

用法：python scripts/fetch_history.py [--limit N] [--no-flow]
"""
import argparse
import datetime as dt
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from westock_client import sector_kline, fund_flow_batch, flow_settled  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SECTORS_FILE = os.path.join(ROOT, "data", "sectors.json")
HISTORY_FILE = os.path.join(ROOT, "docs", "data", "history.json")

WINDOW_DAYS = 90        # 输出窗口（交易日）
FETCH_DAYS = 95         # 拉取窗口（多取 5 天供 5 日涨幅计算）
FLOW_LOOKBACK_DAYS = 135  # 全量重建时资金流回溯自然日（约 90+ 交易日）


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
    """由收盘价序列计算涨幅(%)、3日涨幅(%)和5日涨幅(%)。"""
    chg, chg3, chg5 = [], [], []
    prev = None
    for i, d in enumerate(dates):
        c = closes_by_date.get(d)
        if c is None:
            chg.append(None)
            chg3.append(None)
            chg5.append(None)
            continue
        chg.append(None if prev is None else round((c / prev - 1) * 100, 2))
        prev = c
    for i, d in enumerate(dates):
        c = closes_by_date.get(d)
        c3 = closes_by_date.get(dates[i - 3]) if i >= 3 else None
        c5 = closes_by_date.get(dates[i - 5]) if i >= 5 else None
        chg3.append(None if (c is None or c3 is None or c3 <= 0) else round((c / c3 - 1) * 100, 2))
        chg5.append(None if (c is None or c5 is None or c5 <= 0) else round((c / c5 - 1) * 100, 2))
    return chg, chg3, chg5


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0, help="仅处理前 N 个板块（调试用）")
    ap.add_argument("--no-flow", action="store_true", help="跳过主力净流入")
    ap.add_argument("--full", action="store_true", help="主力净流入全量重建（拉取窗口内全部交易日）")
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
    series_chg3 = {}
    series_chg5 = {}
    for s in sectors:
        closes = klines.get(s["code"]) or {}
        chg, chg3, chg5 = compute_kline_series(closes, dates)
        series_chg[s["name"]] = chg
        series_chg3[s["name"]] = chg3
        series_chg5[s["name"]] = chg5

    # ---------- 2. 主力净流入（腾讯 MainNetFlow 按同花顺成分股聚合，结算校验后写入） ----------
    series_flow = {}
    ths_last = old_meta.get("ths_last_date") or ""
    new_day = dates[-1]
    old_by_date = {}
    for nm_old, arr in (old_series.get("netinflow") or {}).items():
        old_by_date[nm_old] = dict(zip(old_dates, arr))
    series_flow = {s["name"]: [old_by_date.get(s["name"], {}).get(d) for d in dates] for s in sectors}

    if not args.no_flow:
        if new_day == ths_last and not args.full:
            # 主力净流入已是最新（上次运行已写入），无需重复拉取全市场
            print("主力净流入已是 %s 最新数据，跳过" % new_day)
        else:
            # 时间闸门：当日数据须北京时间 17:00（UTC 09:00）后才写入（腾讯约 17:00 结算）
            # ClosePrice 校验无法区分盘中实时与收盘结算数据，须先用时间闸门排除盘中
            now_utc = dt.datetime.now(dt.timezone.utc)
            if new_day == dt.date.today().strftime("%Y-%m-%d") and now_utc.hour < 9:
                print("[warn] 当日数据 17:00（北京）结算前不写入（当前 UTC %s），本次跳过" % now_utc.strftime("%H:%M"), flush=True)
                sys.exit(3)
            # 结算校验：资金流记录自带 ClosePrice 与 K 线收盘价对比（K线收盘即更新，资金流结算慢）
            # 未结算时跳过资金流写入并 exit 3，workflow 每 30 分钟重试
            sample_codes = ["sh600519", "sz000001", "sh601318", "sz300750", "sz000858"]
            settled, detail = flow_settled(sample_codes, new_day)
            print("结算校验[%s]: %s" % (new_day, detail))
            if not settled:
                print("[warn] 资金流数据未结算（%s），本次跳过主力净流入写入" % new_day, flush=True)
                sys.exit(3)

            if args.full:
                print("全量重建主力净流入（%d 个交易日）..." % len(dates))
                start = (dt.date.today() - dt.timedelta(days=FLOW_LOOKBACK_DAYS)).strftime("%Y-%m-%d")
                target_dates = dates
            else:
                print("抓取全市场主力净流入（腾讯 MainNetFlow，聚合到 %s）..." % new_day)
                start = new_day
                target_dates = [new_day]
            print("拉取全市场主力净流入（%s ~ %s）..." % (start, dt.date.today()))
            code_to_sector = {}
            for s in sectors:
                for c in s["constituents"]:
                    code_to_sector[c] = s["name"]
            all_codes = sorted({c for s in sectors for c in s["constituents"]})
            tencent_codes = [("sh" if c.startswith(("60", "68")) else "sz") + c for c in all_codes]
            # 同一天后续运行由"已是最新跳过"逻辑拦截（ths_last_date 已更新），此处仅首次结算后执行
            flows = fund_flow_batch(
                tencent_codes, start, dt.date.today().strftime("%Y-%m-%d"),
                workers=8,
                progress=lambda d, t: print("资金流 %d/%d" % (d, t), flush=True),
            )
            # 按同花顺成分股聚合（元 -> 亿元）
            day_flow_sum = {d: {} for d in target_dates}
            for tc, recs in flows.items():
                if not recs:
                    continue
                nm = code_to_sector.get(tc[2:])
                if not nm:
                    continue
                for rec in recs:
                    if rec["date"] in day_flow_sum:
                        day_flow_sum[rec["date"]][nm] = day_flow_sum[rec["date"]].get(nm, 0.0) + rec["main_net_flow"]
            for nm in series_flow:
                for i, d in enumerate(dates):
                    if d in day_flow_sum and nm in day_flow_sum[d]:
                        series_flow[nm][i] = round(day_flow_sum[d][nm] / 1e8, 4)
            ths_last = new_day
            print("主力净流入已写入 %d 个板块 × %d 个交易日（全市场 %d 只）" % (len(code_to_sector), len(target_dates), len(all_codes)))

    # ---------- 3. 写输出 ----------
    payload = {
        "meta": {
            # GitHub runner 默认 UTC，统一用北京时间（UTC+8）生成更新时间
            "updated_at": dt.datetime.now(dt.timezone(dt.timedelta(hours=8))).strftime("%Y-%m-%d %H:%M:%S"),
            "last_date": dates[-1],
            "ths_last_date": ths_last,
            "sectors_fetched_at": sectors_data["fetched_at"],
            "trade_dates_count": len(dates),
            "source": "板块指数:同花顺(10jqka) / 主力净流入:腾讯自选股(westock)个股主力净流入按同花顺成分股求和",
            "inflow_caliber": "主力净流入(腾讯口径,大单+超大单)按同花顺板块成分股求和；与同花顺逐笔算法有差异(板块级约14%)；每日07:10结算校验后写入",
        },
        "dates": dates,
        "series": {
            "chg": series_chg,
            "chg3": series_chg3,
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
