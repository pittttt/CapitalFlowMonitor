# 板块资金监控（Capital Flow Monitor）

A 股同花顺行业板块历史维度监控平台，静态网页 + GitHub Pages 托管。

## 功能

- **90 个同花顺行业板块**（881xxx 代码），板块列表与成分股固化在 `data/sectors.json`，每 30 天自动刷新
- **3 个维度 tab 切换**：
  - 涨幅（%）— 同花顺官方板块指数日K，与同花顺页面完全一致
  - 5日涨幅（%）— 板块指数滚动 5 日涨跌幅
  - 主力净流入（亿元）— 成分股主力净流入求和（腾讯自选股口径，见页面 ⓘ 说明）
- **排名走势曲线图**：60 个交易日，每条曲线是一个板块的每日排名（第 1 名在顶部），默认显示 Top20，可切换全部/Top50/Top10
- **点击图例**查看单板块：排名曲线 + 具体数值柱形图（双轴）
- **底部时间窗口**可拖拽调整展示范围

## 数据更新

- GitHub Actions 每天北京时间 16:30 自动运行：
  - `fetch_sectors.py` — 板块/成分股（超 30 天自动刷新）
  - `fetch_history.py` — 增量拉取新交易日数据（首次 `--full` 全量）
  - 提交数据并部署 GitHub Pages
- 也可手动触发：仓库 Actions → Update Data & Deploy → Run workflow

## 本地运行

```bash
pip install -r scripts/requirements.txt

# 拉取板块和成分股（30 天内只刷新一次；需 THS_COOKIE，见下）
python scripts/fetch_sectors.py --force

# 计算历史数据（首次全量）
python scripts/fetch_history.py --full

# 增量更新
python scripts/fetch_history.py

# 本地预览
python -m http.server 8080 -d docs
# 浏览器打开 http://localhost:8080
```

### 10jqka 登录 cookie（THS_COOKIE）

同花顺 q.10jqka.com.cn 对无 cookie 的请求有约 10 页/IP 配额，超过后触发登录墙，因此**每月刷新成分股时需要传入登录 cookie**：

1. 浏览器登录 https://q.10jqka.com.cn ，F12 → Network → 刷新页面 → 复制任意请求的 `Cookie:` 请求头
2. 传入方式（任选其一）：
   - **本地**：环境变量 `THS_COOKIE`，或把 cookie 写入项目根目录 `.ths_cookie` 文件（已 gitignore）
   - **GitHub Actions**：仓库 Settings → Secrets and variables → Actions → New repository secret，名称 `THS_COOKIE`，值粘贴 cookie
3. cookie 会过期（约 1~2 周），过期后重新获取并更新

没有 cookie 时脚本会给出提示并跳过成分股更新（保留旧数据）；网页端也会在成分股超期未更新时显示提示。

## 数据来源

- 板块分类、成分股、板块指数：同花顺（10jqka）
- 个股主力资金流：腾讯自选股行情（westock-data 底层接口，GitHub Actions 中由 requests 直连模拟调用）

## GitHub Pages 部署

1. 推送本仓库到 GitHub
2. 仓库 Settings → Pages → **Source 选择 "GitHub Actions"**
3. 等待首次 workflow 运行完成（或手动触发），页面即可访问
