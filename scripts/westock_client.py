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
            out.append({"date": date, "main_net_flow": float(flow)})
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
