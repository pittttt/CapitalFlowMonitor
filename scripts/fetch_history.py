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
import re
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from westock_client import sector_kline, kline, fund_flow_batch, flow_settled, _ths_session  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SECTORS_FILE = os.path.join(ROOT, "data", "sectors.json")
HISTORY_FILE = os.path.join(ROOT, "docs", "data", "history.json")

WINDOW_DAYS = 90        # 输出窗口（交易日）
FETCH_DAYS = 95         # 拉取窗口（多取 5 天供 5 日涨幅计算）
FLOW_LOOKBACK_DAYS = 135  # 全量重建时资金流回溯自然日（约 90+ 交易日）

# 同花顺行业资金流页面（当日板块净额与涨跌幅，单位亿元/%）
THS_FLOW_URL = "https://data.10jqka.com.cn/funds/hyzjl/field/tradezdf/order/desc/page/{pg}/ajax/1/free/1/"
THS_FLOW_REFERER = "https://data.10jqka.com.cn/funds/hyzjl/"


def fetch_ths_sector_flow():
    """抓同花顺行业资金流页面当日数据。

    返回 {板块名: {"net": 净额(亿元), "chg": 涨跌幅(%)}}；失败返回 None。
    当日涨跌幅比 last.js 更新及时，用于补齐最新交易日数据。
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
                    time.sleep(5 * (attempt + 1))
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


def fetch_ths_sector_amount(sectors):
    """抓板块详情页当日成交额（q.10jqka.com.cn/thshy/detail/code/{code}/，带 cookie 绕过配额）。

    返回 {板块名: 成交额(亿元)}；详情页数据当日实时，17:00 后为最终值。
    """
    cookie = os.environ.get("THS_COOKIE", "").strip()
    if not cookie:
        return {}
    out = {}
    for i, s in enumerate(sectors):
        for attempt in range(3):
            try:
                sess = _ths_session("https://q.10jqka.com.cn/thshy/detail/code/%s/" % s["code"])
                sess.headers["Cookie"] = cookie
                r = sess.get("https://q.10jqka.com.cn/thshy/detail/code/%s/" % s["code"], timeout=25)
                if r.status_code in (401, 403):
                    raise RuntimeError("http %s" % r.status_code)
                r.raise_for_status()
                r.encoding = "gbk"
                m = re.search(r"成交额\(亿\)</dt>\s*<dd[^>]*>([^<]+)</dd>", r.text)
                if m:
                    out[s["name"]] = float(m.group(1).strip())
                break
            except (Exception, OSError):  # noqa: BLE001
                if attempt < 2:
                    time.sleep(5 * (attempt + 1))   # 限流规避：失败后拉长等待
        if (i + 1) % 20 == 0:
            print("详情页成交额 [%d/%d]" % (i + 1, len(sectors)), flush=True)
        time.sleep(1.0)   # 板块间 1 秒间隔，避免 q 域名连续请求限流
    return out


def load_json(path):
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    return None


def fetch_sector_klines(sectors):
    """拉全部板块指数日K，返回 ({code: {date: close}}, {code: {date: amount}})。"""
    out = {}
    out_amount = {}
    for i, s in enumerate(sectors):
        try:
            d = sector_kline(s["code"])
            bars = d["bars"][-FETCH_DAYS:]
            out[s["code"]] = {b["date"]: b["close"] for b in bars}
            out_amount[s["code"]] = {b["date"]: b.get("amount") for b in bars}
        except Exception as e:  # noqa: BLE001
            print("[warn] 板块指数 %s(%s) 拉取失败: %s" % (s["name"], s["code"], e), flush=True)
        if (i + 1) % 10 == 0:
            print("板块指数 [%d/%d]" % (i + 1, len(sectors)), flush=True)
    return out, out_amount


def compute_kline_series(closes_by_date, dates, day_chg=None):
    """由收盘价序列计算涨幅(%)、3日涨幅(%)和5日涨幅(%)。

    day_chg: 可选的 {date: 当日涨跌幅(%)} 覆盖（如 hyzjl 页面当日涨幅，比 last.js 更新及时）；
    3日/5日涨幅用涨跌幅序列滚动复利累计，不依赖缺失的当日收盘价。
    """
    chg = []
    prev = None
    for i, d in enumerate(dates):
        c = closes_by_date.get(d)
        if d in (day_chg or {}):
            chg.append(day_chg[d])
            prev = c if c is not None else prev
            continue
        if c is None:
            chg.append(None)
            continue
        chg.append(None if prev is None else round((c / prev - 1) * 100, 2))
        prev = c

    def roll(n):
        out = []
        for i in range(len(dates)):
            window = [chg[j] for j in range(max(0, i - n + 1), i + 1) if chg[j] is not None]
            if len(window) < n:
                out.append(None)
                continue
            acc = 1.0
            for v in window:
                acc *= 1 + v / 100
            out.append(round((acc - 1) * 100, 2))
        return out

    return chg, roll(3), roll(5)


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
    klines, kline_amounts = fetch_sector_klines(sectors)

    # 最新交易日以腾讯 K 线为准（收盘即有当日数据；10jqka last.js 当日数据大面积滞后数小时）
    try:
        probe_klines = kline(["sh600519", "sz000001"], "2026-08-01", dt.date.today().strftime("%Y-%m-%d"))
        t_dates = [b["date"] for bars in probe_klines.values() for b in bars]
        new_day = max(t_dates) if t_dates else ""
    except Exception:  # noqa: BLE001
        new_day = ""

    # 交易日序列：last.js 日期合并最新交易日（last.js 缺当日时补上）
    all_dates = sorted({d for k in klines.values() for d in k})
    if new_day and new_day not in all_dates:
        all_dates.append(new_day)
    if not all_dates:
        all_dates = old_dates
    dates = all_dates[-WINDOW_DAYS:]
    print("交易日: %d 个 (%s ~ %s)，最新交易日(腾讯K线) = %s" % (len(dates), dates[0], dates[-1], new_day))

    # 当日涨跌幅补齐：last.js 当日数据大面积滞后（19:00 时多数板块仍缺），
    # 用 hyzjl 页面当日涨跌幅覆盖最新交易日（页面数据更新及时）
    day_chg = {}
    ths_today = None
    try:
        ths_today = fetch_ths_sector_flow()
    except Exception as e:  # noqa: BLE001
        print("[warn] hyzjl 抓取异常: %s" % e, flush=True)
    if ths_today and len(ths_today) >= 80:
        for s in sectors:
            if s["name"] in ths_today:
                day_chg[s["name"]] = {dates[-1]: ths_today[s["name"]]["chg"]}
        print("当日涨跌幅已用 hyzjl 补齐 %d 个板块" % len(day_chg))
    else:
        print("[warn] hyzjl 当日数据不完整（%s），涨幅保持 last.js 数据" % (len(ths_today) if ths_today else 0))

    series_chg = {}
    series_chg3 = {}
    series_chg5 = {}
    for s in sectors:
        closes = klines.get(s["code"]) or {}
        chg, chg3, chg5 = compute_kline_series(closes, dates, day_chg.get(s["name"]))
        series_chg[s["name"]] = chg
        series_chg3[s["name"]] = chg3
        series_chg5[s["name"]] = chg5

    # 告警：哪些板块缺最新交易日数据（10jqka 单板块 last.js 偶发滞后，次日自动补齐）
    missing = [s["name"] for s in sectors if series_chg[s["name"]][-1] is None]
    if missing:
        print("[warn] 以下板块缺少 %s 的涨幅数据: %s" % (dates[-1], "、".join(missing[:10])), flush=True)

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

    # ---------- 2d. 板块上涨家数占比（全市场 kline 当日涨跌统计，历史累积） ----------
    # 与资金流同日计算：ths_last 已更新时执行；kline 批量上限 10，8 并发
    up_old = (old_series.get("up_pct") or {}) if old else {}
    up_old_by_date = {nm: dict(zip(old_dates, arr)) for nm, arr in up_old.items()}
    series_up = {s["name"]: [up_old_by_date.get(s["name"], {}).get(d) for d in dates] for s in sectors}
    if not args.no_flow and ths_last == new_day and series_up[sectors[0]["name"]][-1] is None:
        print("统计全市场当日涨跌（上涨家数占比）...")
        all_codes = sorted({c for s in sectors for c in s["constituents"]})
        tencent_codes = [("sh" if c.startswith(("60", "68")) else "sz") + c for c in all_codes]
        tc_to_code6 = {tc: c for tc, c in zip(tencent_codes, all_codes)}
        code_to_sector = {}
        for s in sectors:
            for c in s["constituents"]:
                code_to_sector[c] = s["name"]
        batches = [tencent_codes[i:i + 10] for i in range(0, len(tencent_codes), 10)]

        def fetch_k(batch):
            try:
                return kline(batch, new_day, new_day)
            except Exception:  # noqa: BLE001
                return {}

        from concurrent.futures import ThreadPoolExecutor, as_completed

        day_change = {}  # code6 -> change_pct
        with ThreadPoolExecutor(max_workers=8) as ex:
            futs = [ex.submit(fetch_k, b) for b in batches]
            done = 0
            for fut in as_completed(futs):
                for code, bars in fut.result().items():
                    # 仅取目标日数据（含 change_pct，直接算涨跌；防当日数据未出时错位）
                    for bar in bars:
                        if bar["date"] == new_day:
                            day_change[code] = bar.get("change", bar.get("close"))
                done += 1
                if done % 100 == 0:
                    print("涨跌统计 [%d/%d]" % (done, len(batches)), flush=True)
        # 板块上涨占比（day_change 键为腾讯代码 sh/sz 前缀，映射回 6 位）
        sector_stat = {}
        for tc_code, chg in day_change.items():
            nm = code_to_sector.get(tc_code[2:])
            if not nm:
                continue
            st = sector_stat.setdefault(nm, {"up": 0, "total": 0})
            st["total"] += 1
            if chg is not None and chg > 0:
                st["up"] += 1
        idx = dates.index(new_day) if new_day in dates else -1
        for nm in series_up:
            st = sector_stat.get(nm)
            if st and st["total"] > 0 and idx >= 0:
                series_up[nm][idx] = round(st["up"] / st["total"] * 100, 1)
        print("上涨家数占比已写入 %d 个板块（%s）" % (len(sector_stat), new_day))

    # ---------- 2b. 3日/5日主力净流入（滚动累计求和） ----------
    def rolling_sum(arr, n):
        out = []
        for i in range(len(arr)):
            window = arr[max(0, i - n + 1): i + 1]
            if len(window) < n or any(v is None for v in window):
                out.append(None)
            else:
                out.append(round(sum(window), 4))
        return out

    series_flow3 = {nm: rolling_sum(arr, 3) for nm, arr in series_flow.items()}
    series_flow5 = {nm: rolling_sum(arr, 5) for nm, arr in series_flow.items()}

    # ---------- 2c. 板块每日成交额（亿元，用于单板块视图柱状图） ----------
    series_amount = {}
    for s in sectors:
        amounts = kline_amounts.get(s["code"]) or {}
        series_amount[s["name"]] = [
            round(amounts.get(d, 0) / 1e8, 1) if amounts.get(d) else None for d in dates
        ]
    # 当日成交额优先用板块详情页（q.10jqka.com.cn 当日实时，与 last.js 同源官方口径）；
    # 历史成交额保留 last.js 数据；详情页失败时该板块当日缺失，last.js 更新后重跑/次日自动补齐
    # （不使用腾讯成分股求和兜底——口径有差异，宁可缺失等官方数据）
    try:
        ths_amount = fetch_ths_sector_amount(sectors)
        if ths_amount:
            filled = 0
            for nm in series_amount:
                if nm in ths_amount:
                    series_amount[nm][-1] = ths_amount[nm]
                    filled += 1
            print("当日成交额已用详情页覆盖 %d 个板块" % filled)
            missing = [s["name"] for s in sectors if series_amount[s["name"]][-1] is None]
            if missing:
                print("[warn] 仍有 %d 个板块当日成交额缺失（详情页抓取失败），last.js 更新后重跑自动补齐" % len(missing))
        else:
            print("[warn] 详情页成交额抓取失败（可能未配置 THS_COOKIE/被限流），当日缺失待 last.js 更新后补齐")
    except Exception as e:  # noqa: BLE001
        print("[warn] 详情页成交额抓取异常: %s" % e)

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
            "netinflow3": series_flow3,
            "netinflow5": series_flow5,
            "amount": series_amount,
            "up_pct": series_up,
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
