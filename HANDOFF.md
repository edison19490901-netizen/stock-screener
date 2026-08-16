# Stock Screener — 项目交接说明

## 快速链接

- **仓库**：`git@github.com:edison19490901-netizen/stock-screener.git`（SSH）
- **Render URL**：`https://stock-screener.onrender.com`
- **本地启动**：`python app.py`（默认 http://localhost:8080）
- **推送**：`git push origin master`（SSH，勿用 HTTPS）

## 架构

| 层 | 技术 | 说明 |
|----|------|------|
| 后端 | Python 3 `http.server` | 无框架，单文件服务 |
| 数据源 | Tushare + Baostock | Tushare 股息/市值（需 IP 白名单）；Baostock 价格/K线（免费无鉴权） |
| 前端 | 单 HTML 文件 | 自包含，深色主题，Canvas 绘图，含 EMBED 兜底数据 |
| 部署 | Render Web Service | 自动 HTTPS，`render.yaml` 声明式配置 |
| PWA | manifest.json + sw.js | 手机「添加到主屏幕」安装，图标 ox_icon |

## 选股策略

- 股息率 > 3%
- 市值 > 500 亿
- 最新价距 1 年低点 < 15%
- 最新价距周线 BB 下轨 < 15%
- **持仓股（HOLDINGS）**：`app.py` 顶部 `HOLDINGS` 常量里的股票，无论是否满足上述条件都强制纳入并跳过价格过滤（当前：中国广核 003816.SZ、海尔智家 600690.SH）；前端以金色「持仓」标签标识

## 网格交易策略（6 区斐波那契）

1Y 低 ~ 1Y 高之间 5 条斐波那契线（0.25 / 0.382 / 0.5 / 0.618 / 0.75）→ 6 区：

| 区 | 区间 | 操作 | 份数 |
|----|------|------|------|
| 1 | 低 ~ 0.25 | 低估·重仓买入 | 2 份 |
| 2 | 0.25 ~ 0.382 | 偏低·加仓 | 2 份 |
| 3 | 0.382 ~ 0.5 | 偏低·加仓 | 2 份 |
| 4 | 0.5 ~ 0.618 | 偏高·持有/观望 | 2 份 |
| 5 | 0.618 ~ 0.75 | 偏高·减仓 | 2 份 |
| 6 | 0.75 ~ 高 | 不操作 | 0 份 |

1-5 区各 2 份、6 区不操作，共 10 份。后端字段 `grid_25/382/50/618/75` + `grid_zone`（`calc_grid()` 计算）。

## 数据流

```
Tushare daily_basic (股息率/市值，慢变化)   Baostock 日线 (价格/K线，每日)
        │                                              │
        │  dv_ttm × 收盘价 ÷ 100 = DPS（每股分红）      │
        │                                              │
        └──────────► 股息率 = DPS ÷ 最新价 × 100% ◄─────┘
                            （每次 Update Price 重算）
```

- **DPS** 只在公司公告分红时变动，慢变化，缓存足够
- **股息率** 每天跟随股价自动重算
- 价格/K线/网格/布林带由 Baostock 实时拉取（Render 可用，无需白名单）

## 行情解读（Market Analysis）

看板顶部「📊 行情解读」面板 + PushPlus 微信推送（两者共用同一份内容）。**由规则脚本生成（非大模型）**：确定性输出、零成本、无延迟，文字是「数字填进中文模板」，不会编造数据。

- **整体概览**：筛选股数、平均/中位/最高股息率、平均市值、网格 6 区分布、低估区（1-2 区）占比、距 1 年低点/布林下轨均值、近 5/20 日平均涨幅、站上 MA20 比例、整体方向（偏强/偏弱/震荡分化）+ 一段自然语言小结
- **重点个股**（约 16 只，每只带 1-2 句描述）：
  - `buy` 接近买入点：1-2 区（低估/偏低）前 5，按区内位置排序
  - `yield` 股息率吸引力：按股息率降序前 5（与 buy 去重），含 6% 目标价带（`DPS÷0.06`）位置
  - `holding` 持仓股：中国广核 / 海尔智家，始终包含
  - `mover` 近期异动：近 5 日涨幅前 3 + 跌幅前 3
- **历史趋势**：用价格缓存里 260 日 `price_history`，`compute_trend_stats()` 算 MA20/MA60、5/20 日涨跌、趋势分类（上升=价>MA20>MA60 / 下降=价<MA20<MA60 / 震荡）
- **报告**：`build_report_markdown()` 拼装 markdown，PushPlus 以 `template='markdown'` 推送

**接口**：
| 端点 | 方法 | 说明 |
|------|------|------|
| `/api/analysis` | GET | 返回 `{ok, analysis}`（overview + highlights + markdown report），免认证 |
| `/api/pushplus_analysis` | POST | 推送解读到微信（需 `PUSHPLUS_TOKEN`，否则 400） |

前端 `dashboard.html` 的 `AN` 对象负责加载/渲染/折叠/推送；离线打开（file://）时回退为客户端基础统计（仅总数/平均股息率/区间分布）。

## 关键代码位置

| 位置 | 说明 |
|------|------|
| `app.py:43` | HOLDINGS 持仓股常量（强制纳入 + 跳过价格过滤） |
| `app.py:47-60` | safe_float / safe_int NaN 防护 |
| `app.py:63-91` | calc_grid()（5 线 → 6 区） |
| `app.py:102-150` | screen_from_cache()（读 parquet 缓存） |
| `app.py:210-333` | supplement_baostock()（价格 + BB + 网格 + 260 日历史） |
| `app.py:367-388` | run_full_pipeline() |
| `app.py` 分析区 | compute_trend_stats / build_analysis / build_report_markdown（行情解读，`/api/analysis` + `/api/pushplus_analysis`） |
| `dashboard.html` | AN 对象（行情解读面板，自动加载 + 推送微信） |
| `app.py:391-425` | update_tushare_cache()（拉 Tushare） |
| `app.py:550-606` | Handler.do_GET / do_POST 路由 |
| `app.py:842-899` | _export_excel()（Excel 导出） |
| `dashboard.html` | 前端（drawChart / cardGrid / V.table） |

## 环境变量

| 变量 | 必填 | 说明 |
|------|------|------|
| `DASHBOARD_PASSWORD` | 建议 | 看板访问密码（留空公开） |
| `TUSHARE_TOKEN` | 可选 | Tushare Token（Update DPS 用，需 IP 白名单） |
| `PUSHPLUS_TOKEN` | 可选 | 微信推送 Token（PushPlus 按钮用） |
| `PORT` | Render 自动 | 服务端口（默认 8080） |

## 已知限制 / 踩坑

- **Tushare IP 白名单**：Render 免费版 IP 动态，无法稳定加白名单 → 「Update DPS」在 Render 上可能失败。日常依赖种子缓存 + Baostock。
- **冷启动**：免费版实例空闲约 15 分钟后休眠，再次访问约 30 秒唤醒。
- **缓存丢失**：Render 磁盘是临时的，重启/重新部署会清空运行时生成的缓存；种子缓存（`cache/`）已提交进仓库，部署即恢复。
- **数据新鲜度**：首次部署自带种子数据；点一次「Update Price」刷新实时价格。
- **持仓股可能不在价格缓存里**：若卡片/解读「持仓股状态」里没有中国广核、海尔智家，说明 `price_cache.json` 是持仓机制加入前生成的；点一次「Update Price」跑完整管线后持仓股会强制纳入。

## 更新种子缓存（本地）

本地跑一次完整管线并提交，即可更新仓库里的种子数据：

```bash
python -c "
from app import run_full_pipeline
df, _ = run_full_pipeline(force_refresh=True)
print(f'{len(df)} stocks updated')
"
git add cache/ && git commit -m 'chore: refresh seed cache' && git push
```
