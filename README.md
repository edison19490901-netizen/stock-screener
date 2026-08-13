# 高股息低估值选股看板 (Stock Screener)

A 股高股息低估值选股看板，支持 K 线（布林带）、斐波那契网格交易、股息率目标价带、Excel 导出，并可安装到手机桌面（PWA）。

**选股条件**：股息率 > 3% · 市值 > 500 亿 · 距 1 年低点 < 15% · 距周线 BB 下轨 < 15%（持仓股通过 `HOLDINGS` 常量强制纳入，见「功能」）

---

## 功能

- **实时价格 + K 线图**：近 1 年（260 交易日）前复权 K 线，叠加布林带（20 日均线 ± 2σ）、1Y 低/高线、5 条斐波那契网格线（0.25/0.382/0.5/0.618/0.75）、4 条股息率目标价线（6%/5.5%/5%/4.5%）
- **6 区斐波那契网格交易**：1Y 低 ~ 1Y 高之间分 5 线 → 6 区，1-5 区各 2 份、6 区不操作；表格按价格升序展示各网格价位与「建议投入」
- **股息率目标价带**：按每股分红 DPS 反推 6%/5.5%/5%/4.5% 股息率对应的目标价位，颜色编码（红/绿/黄/白）
- **Excel 导出**：一键导出筛选结果（含网格、目标价、BB 等全部字段）
- **PWA 安装**：手机浏览器「添加到主屏幕」，独立运行、带图标
- **密码保护**：访问前需输入密码（可选）
- **持仓股**：`app.py` 的 `HOLDINGS` 常量指定的股票（当前：中国广核 003816.SZ、海尔智家 600690.SH）无论是否满足筛选条件都强制显示，带金色「持仓」标签

---

## 技术栈

| 层 | 技术 | 用途 |
|----|------|------|
| 后端 | Python 3 + `http.server` | 无框架依赖的 HTTP 服务 |
| 数据 | Tushare（股息/市值） + Baostock（价格/K线） | 数据源 |
| 计算 | pandas + numpy | 布林带、斐波那契网格 |
| 前端 | 纯 HTML/CSS/JS（Canvas 绘图） | 自包含单页应用 |
| 部署 | Render（Web Service） | 免费托管，自动 HTTPS |
| 推送 | PushPlus | 微信日报（手动触发） |

---

## 在线地址

```
https://stock-screener.onrender.com
```

手机浏览器打开后，用浏览器菜单「添加到主屏幕」即可像 App 一样使用。

---

## 目录结构

```
stock-screener/
├── app.py                  # 后端服务器（核心：选股 + API + 静态服务）
├── dashboard.html          # 前端页面（自包含，含 EMBED 兜底数据）
├── password.html           # 密码登录页
├── manifest.json           # PWA 清单
├── sw.js                   # Service Worker（PWA 安装）
├── ox_icon*.png            # 应用图标（32/180/192/256/512）
├── render.yaml             # Render 部署配置
├── requirements.txt        # Python 依赖
├── .env.example            # 环境变量模板
├── .gitignore
├── README.md
├── HANDOFF.md              # 项目交接说明
└── cache/                  # 种子缓存（部署即带数据）
    ├── daily_basic_*.parquet   # Tushare 股息/市值原始数据
    ├── stock_names.parquet     # 股票代码-名称映射
    ├── dps_cache.json          # 每股分红 DPS 缓存
    └── price_cache.json        # 价格 + 网格 + 布林带缓存
```

---

## 部署到 Render

### 1. 准备 GitHub 仓库

代码已推送至 `https://github.com/edison19490901-netizen/stock-screener`（含种子缓存，部署后即有数据）。

### 2. 在 Render 创建 Web Service

1. 登录 [dashboard.render.com](https://dashboard.render.com)
2. **New + → Web Service**
3. 授权 GitHub 并选择仓库 `edison19490901-netizen/stock-screener`
4. Render 会自动读取 `render.yaml`，确认：
   - **Name**: `stock-screener`
   - **Build Command**: `pip install -r requirements.txt`
   - **Start Command**: `python app.py`
   - **Health Check Path**: `/api/health`
5. 点击 **Create Web Service**

### 3. 配置环境变量

在 Render → stock-screener → Environment 中设置：

| 变量 | 必填 | 说明 |
|------|------|------|
| `DASHBOARD_PASSWORD` | 建议 | 看板访问密码（留空则公开访问） |
| `TUSHARE_TOKEN` | 可选 | Tushare Token，用于「更新 DPS」（需 IP 白名单） |
| `PUSHPLUS_TOKEN` | 可选 | 微信推送 Token，用于「PushPlus」按钮 |

> **关于 Tushare IP 白名单**：Render 免费版出口 IP 动态变化，无法稳定加入 Tushare 白名单，所以「更新 DPS」在 Render 上可能失败。日常看板依赖**种子缓存 + Baostock 实时价格**（Baostock 无需白名单），股息/市值数据慢变化，缓存足够用。

### 4. 首次访问

部署完成后打开 `https://stock-screener.onrender.com`，输入密码即可进入看板。

> 免费版实例空闲约 15 分钟后会休眠，再次访问约需 30 秒冷启动，属正常现象。

---

## 数据更新

| 按钮 | 接口 | 数据源 | Render 可用性 |
|------|------|--------|---------------|
| **Update Price** | `/api/refresh_prices` | Baostock（价格/K线/网格/BB） | ✅ 可用 |
| **Update DPS** | `/api/update` | Tushare（股息/市值） | ⚠️ 需 IP 白名单 |
| **PushPlus** | `/api/pushplus` | 推送当前数据到微信 | ✅ 需 PUSHPLUS_TOKEN |
| **Export Excel** | `/api/export` | 导出当前数据为 .xlsx | ✅ 可用 |

日常使用点一次 **Update Price** 即可刷新实时价格；股息率由 DPS ÷ 最新价每日重算。

---

## API 端点

| 端点 | 方法 | 说明 |
|------|------|------|
| `/api/data` | GET | 获取筛选结果 JSON（优先价格缓存） |
| `/api/refresh_prices` | POST | 拉取 Baostock 价格 + 网格 + 布林带 |
| `/api/update` | POST | 拉取 Tushare 股息/市值数据 |
| `/api/export` | GET | 下载 Excel |
| `/api/pushplus` | POST | 推送微信日报 |
| `/api/health` | GET | 健康检查 |
| `/login` | POST | 密码登录 |

---

## 本地开发

```bash
pip install -r requirements.txt
# 复制 .env.example 为 .env 并填入 TUSHARE_TOKEN / DASHBOARD_PASSWORD
python app.py
# 打开 http://localhost:8080
```

---

## 免责声明

本项目仅供参考，不构成任何投资建议。
