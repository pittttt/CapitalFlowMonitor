# -*- coding: utf-8 -*-
"""westock-data 底层接口封装（requests 直连，与 npx westock-data-skillhub 同源）。

数据源：
- 主力资金流：腾讯自选股网关 proxy.finance.qq.com（route=stock_quote_history）
- 板块指数日K：同花顺行情接口 d.10jqka.com.cn（v6/line/bk_881xxx/01/last.js）
"""
import json
import re
import time

import requests

# westock-data-skillhub@1.0.5 包内硬编码的公共凭证（npx 包公开可见）
WESTOCK_TOKEN = "99359fcc033b30b5f33a5c825ad9de81fd66a6337781834040af835a2099a553"
WESTOCK_DEV_ID = "dev_9f462a90c0cc889f5a4fe47f2a0dfdaa"
WESTOCK_URL = (
    "https://proxy.finance.qq.com/cgi/cgi-bin/openai/openclaw/proxy"
    "?app=openclaw&token=%s&dev_id=%s" % (WESTOCK_TOKEN, WESTOCK_DEV_ID)
)

# 10jqka 需要完整浏览器指纹头（实测缺 sec-ch-ua / Sec-Fetch-* 会 401）
THS_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "*/*",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    "X-Requested-With": "XMLHttpRequest",
    "Sec-Fetch-Dest": "empty",
    "Sec-Fetch-Mode": "cors",
    "Sec-Fetch-Site": "same-origin",
    "sec-ch-ua": '"Not_A Brand";v="8", "Chromium";v="120", "Google Chrome";v="120"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"Windows"',
    "Connection": "keep-alive",
}


def _ths_session(referer):
    s = requests.Session()
    s.headers.update(THS_HEADERS)
    s.headers["Referer"] = referer
    return s


def _retry(fn, tries=3, backoff=1.5, timeout=30):
    """带指数退避的重试包装。"""
    last_exc = None
    for i in range(tries):
        try:
            return fn()
        except Exception as e:  # noqa: BLE001
            last_exc = e
            if i < tries - 1:
                time.sleep(backoff * (2 ** i))
    raise last_exc


def fund_flow(code, start_date, end_date, fields=None):
    """查询单只 A 股的主力资金流历史。

    code: 腾讯代码格式，如 sh600519 / sz000001
    fields: 逗号分隔字段，默认主力净流入等核心字段
    返回: [{"date": "YYYY-MM-DD", "main_net_flow": 元(float)}, ...] 按日期升序
    """
    timeout = 30
    if fields is None:
        fields = "MainNetFlow,JumboNetFlow,BlockNetFlow,MidNetFlow,SmallNetFlow,MainInFlow,MainOutFlow,RetailInFlow,RetailOutFlow,ClosePrice"

    def call():
        body = {
            "token": WESTOCK_TOKEN,
            "route": "stock_quote_history",
            "params": {
                "code": code,
                "fields": fields,
                "start_date": start_date,
                "end_date": end_date,
            },
            "skill_name": "westock-data",
            "skill_channel": "skillhub",
        }
        r = requests.post(WESTOCK_URL, json=body, timeout=timeout)
        r.raise_for_status()
        d = r.json()
        if d.get("code") != 0:
            raise RuntimeError("westock error %s: %s" % (code, d.get("msg")))
        return d.get("data", {})

    data = _retry(call)
    series = data.get("series") or []
    out = []
    for item in series:
        rec = item.get("data", {})
        date = rec.get("EndDate") or item.get("date")
        flow = rec.get("MainNetFlow")
        if date and flow is not None:
            rec_out = {"date": date, "main_net_flow": float(flow)}
            close = rec.get("ClosePrice")
            if close is not None:
                rec_out["close"] = float(close)
            out.append(rec_out)
    out.sort(key=lambda x: x["date"])
    return out


def sector_kline(code):
    """查询同花顺板块指数日K（官方加权指数）。

    code: 同花顺板块代码，如 881274
    返回: {"name": 板块名, "bars": [{"date": "YYYY-MM-DD", "open":..,"close":..}, ...]} 按日期升序
    """
    url = "https://d.10jqka.com.cn/v6/line/bk_%s/01/last.js" % code
    timeout = 30

    def call():
        s = _ths_session("https://q.10jqka.com.cn/")
        r = s.get(url, timeout=timeout)
        r.raise_for_status()
        m = re.search(r"\((.*)\)\s*;?\s*$", r.text, re.S)
        if not m:
            raise RuntimeError("sector_kline %s: 无法解析 JSONP" % code)
        return json.loads(m.group(1))

    d = _retry(call)
    name = d.get("name", "")
    bars = []
    for row in (d.get("data") or "").split(";"):
        parts = row.split(",")
        if len(parts) < 7 or not parts[0].isdigit():
            continue
        bars.append({
            "date": "%s-%s-%s" % (parts[0][:4], parts[0][4:6], parts[0][6:8]),
            "open": float(parts[1]),
            "high": float(parts[2]),
            "low": float(parts[3]),
            "close": float(parts[4]),
        })
    bars.sort(key=lambda x: x["date"])
    return {"name": name, "bars": bars}


def kline(codes, start_date, end_date):
    """查询日K线（含收盘价），腾讯网关 route=query_kline_data。

    codes: 代码列表（如 ["sh600519", "sz000001"]）
    返回: {code: [{"date": "YYYY-MM-DD", "close": float}, ...] 按日期升序}
    """
    timeout = 30

    def call():
        body = {
            "token": WESTOCK_TOKEN,
            "route": "query_kline_data",
            "params": {
                "codes": codes,
                "ktype": "day",
                "fqtype": "qfq",
                "start_date": start_date,
                "end_date": end_date,
            },
            "skill_name": "westock-data",
            "skill_channel": "skillhub",
        }
        r = requests.post(WESTOCK_URL, json=body, timeout=timeout)
        r.raise_for_status()
        d = r.json()
        if d.get("code") != 0:
            raise RuntimeError("westock kline error: %s" % d.get("msg"))
        return d.get("data", {})

    data = _retry(call)
    out = {}
    for i, item in enumerate(data.get("array") or []):
        # 腾讯返回的 array 项无 code 字段，按请求顺序对应
        code = item.get("code") or (codes[i] if i < len(codes) else None)
        bars = []
        for k in item.get("kline_data") or []:
            d = k.get("end_date")
            c = k.get("close_price")
            if d and c is not None:
                bars.append({"date": d, "close": float(c)})
        bars.sort(key=lambda x: x["date"])
        out[code] = bars
    return out


def flow_settled(sample_codes, date):
    """校验腾讯资金流数据是否已结算：资金流记录自带 ClosePrice，与 K 线收盘价对比。

    资金流结算晚于行情（收盘即有K线，资金流需逐笔汇总），两者日期与价格一致则已结算。
    sample_codes: 样本股代码（腾讯格式），date: 目标日期（YYYY-MM-DD）
    返回 (settled: bool, detail: str)
    """
    try:
        klines = kline(sample_codes, date, date)
        ok, total = 0, 0
        detail = []
        for code in sample_codes:
            bars = klines.get(code) or []
            if not bars:
                continue
            kc = bars[-1]
            flows = fund_flow(code, date, date, fields="MainNetFlow,ClosePrice")
            if not flows:
                continue
            fc = flows[-1]
            total += 1
            match = kc["date"] == fc["date"] and abs(kc["close"] - fc["close"]) / kc["close"] < 0.005
            if match:
                ok += 1
            detail.append("%s k线%s=%.2f 资金流%s=%.2f %s" % (
                code, kc["date"], kc["close"], fc["date"], fc["close"], "一致" if match else "不一致"))
        settled = total > 0 and ok / total >= 0.8
        return settled, "; ".join(detail)
    except Exception as e:  # noqa: BLE001
        return False, "校验失败: %s" % e


EM_HOSTS = [
    "https://push2.eastmoney.com",
    "https://push2delay.eastmoney.com",
    "https://push2his.eastmoney.com",
]
EM_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Referer": "https://data.eastmoney.com/",
}


def eastmoney_main_flow_all(progress=None):
    """抓取全市场个股当日主力净流入（东财 clist 批量接口，f62 主力净流入）。

    返回 {code6: 主力净流入(元)}；多镜像域名轮换规避限流。
    """
    out = {}
    page = 1
    host_idx = 0
    while page <= 60:
        params = {
            "pn": page, "pz": 100, "po": 1, "np": 1, "fltt": 2, "invt": 2,
            "fid": "f62", "fs": "m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23",
            "fields": "f12,f14,f62",
        }
        got = None
        for attempt in range(3):
            host = EM_HOSTS[(host_idx + attempt) % len(EM_HOSTS)]
            try:
                r = requests.get(host + "/api/qt/clist/get", params=params, timeout=15, headers=EM_HEADERS)
                d = r.json()
                diff = (d.get("data") or {}).get("diff") or []
                got = diff
                host_idx = (host_idx + attempt) % len(EM_HOSTS) + 1
                break
            except Exception:  # noqa: BLE001
                time.sleep(1.5 * (attempt + 1))
        if got is None:
            print("[warn] 东财资金流第 %d 页连续失败" % page, flush=True)
            break
        if not got:
            break
        for item in got:
            v = item.get("f62")
            if v is not None and item.get("f12"):
                try:
                    out[item["f12"]] = float(v)
                except (TypeError, ValueError):
                    continue
        if len(got) < 100:
            break
        page += 1
        if progress and page % 10 == 0:
            progress(page)
        time.sleep(0.6)
    return out


def fund_flow_batch(codes, start_date, end_date, workers=8, progress=None):
    """并发批量查询资金流，返回 {code: [{"date","main_net_flow"},...]}。"""
    from concurrent.futures import ThreadPoolExecutor, as_completed

    results = {}
    total = len(codes)
    done = 0

    def fetch(code):
        return code, fund_flow(code, start_date, end_date)

    with ThreadPoolExecutor(max_workers=workers) as ex:
        futures = {ex.submit(fetch, c): c for c in codes}
        for fut in as_completed(futures):
            code = futures[fut]
            try:
                _, data = fut.result()
                results[code] = data
            except Exception as e:  # noqa: BLE001
                results[code] = None
                print("[warn] %s 资金流拉取失败: %s" % (code, e), flush=True)
            done += 1
            if progress and done % 500 == 0:
                progress(done, total)
    return results
