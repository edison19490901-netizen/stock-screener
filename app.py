"""
Dashboard Stock Screener — Backend API
Start: python app.py
Dashboard HTML can be opened standalone (file:// or http://localhost:8080)
"""
import hashlib, json, os, sys, time
from datetime import datetime, timedelta, timezone
from http.server import HTTPServer, SimpleHTTPRequestHandler
from pathlib import Path
from urllib.parse import urlparse

import pandas as pd

# Beijing timezone (UTC+8)
BJ_TZ = timezone(timedelta(hours=8))

def bj_now():
    """Return current datetime in Beijing timezone"""
    return datetime.now(BJ_TZ)

BASE_DIR = Path(__file__).resolve().parent
CACHE_DIR = BASE_DIR / 'cache'
if not CACHE_DIR.exists():
    CACHE_DIR = BASE_DIR.parent / 'cache'

# Load .env at startup so all os.getenv() calls work everywhere
try:
    from dotenv import load_dotenv
    for p in [BASE_DIR / '.env', BASE_DIR.parent / '.env']:
        if p.exists():
            load_dotenv(p)
except ImportError:
    pass

DIVIDEND_THRESHOLD = 3.0
MIN_MARKET_CAP = 500
MAX_PCT_FROM_LOW = 15       # Price within 15% of 1Y low
MAX_PCT_FROM_LOWER = 15     # Price within 15% of weekly BB lower
PRICE_CACHE_FILE = CACHE_DIR / 'price_cache.json'
DPS_CACHE_FILE = CACHE_DIR / 'dps_cache.json'

# 持仓股：无论是否满足高股息/低估值筛选，始终显示在看板（ts_code）
HOLDINGS = ['003816.SZ', '600690.SH']  # 中国广核 / 海尔智家


def load_token():
    return os.getenv('TUSHARE_TOKEN', '')


def safe_float(val, default=0.0):
    """Convert value to float, handling NaN/None."""
    try:
        return float(val) if pd.notna(val) else default
    except (ValueError, TypeError):
        return default


def safe_int(val, default=0):
    """Convert value to int, handling NaN/None."""
    try:
        return int(float(val)) if pd.notna(val) else default
    except (ValueError, TypeError):
        return default


def calc_grid(low, high, price):
    """
    Fibonacci grid between 1Y low and 1Y high.
    5 lines (0.25 / 0.382 / 0.5 / 0.618 / 0.75) → 6 zones (bottom→top: 1..6).
    Zones 1-5 each hold 2份 (2 units); zone 6 is a no-op (不操作).
    Returns line prices + current zone (1-6).
    """
    if not low or not high or high <= low:
        return {'grid_25': None, 'grid_382': None, 'grid_50': None,
                'grid_618': None, 'grid_75': None, 'grid_zone': None}
    rng = high - low
    l25 = low + 0.25 * rng
    l382 = low + 0.382 * rng
    l50 = low + 0.5 * rng
    l618 = low + 0.618 * rng
    l75 = low + 0.75 * rng
    zone = 1
    if price >= l25:
        zone = 2
    if price >= l382:
        zone = 3
    if price >= l50:
        zone = 4
    if price >= l618:
        zone = 5
    if price >= l75:
        zone = 6
    return {
        'grid_25': round(l25, 4),
        'grid_382': round(l382, 4),
        'grid_50': round(l50, 4),
        'grid_618': round(l618, 4),
        'grid_75': round(l75, 4),
        'grid_zone': zone,
    }


# ════════════════════ Data Layer ════════════════════

def screen_from_cache():
    pqts = sorted(CACHE_DIR.glob('daily_basic_*.parquet'), reverse=True)
    if not pqts:
        return None
    pqt = pqts[0]
    trade_date = pqt.stem.replace('daily_basic_', '')
    df = pd.read_parquet(pqt)

    names_pqt = CACHE_DIR / 'stock_names.parquet'
    name_map = {}
    if names_pqt.exists():
        ndf = pd.read_parquet(names_pqt)
        name_map = dict(zip(ndf['ts_code'], ndf['name']))

    df['mcap_b'] = df['total_mv'] / 1e4
    large = df[df['mcap_b'] > MIN_MARKET_CAP].copy()
    large['dv'] = pd.to_numeric(large['dv_ttm'], errors='coerce')
    # 高股息股 + 持仓股（持仓股无论股息率/估值是否达标都始终显示）
    keep = (large['dv'] > DIVIDEND_THRESHOLD) | (large['ts_code'].isin(HOLDINGS))
    high = large[keep].copy()
    if high.empty:
        return pd.DataFrame()

    results = []
    for _, r in high.iterrows():
        price = float(r['close']) if pd.notna(r['close']) else 0
        div_y = float(r['dv']) if pd.notna(r['dv']) else 0
        results.append({
            'code': r['ts_code'],
            'name': name_map.get(r['ts_code'], r['ts_code']),
            'is_holding': bool(r['ts_code'] in HOLDINGS),
            'market_cap_billion': round(float(r['mcap_b']), 2),
            'latest_price': round(price, 2),
            'dividend_yield': round(div_y, 2),
            'dividend_per_share': round(div_y / 100 * price, 4) if div_y > 0 else None,
            'min_price_1y': None,
            'max_price_1y': None,
            'grid_25': None, 'grid_382': None, 'grid_50': None,
            'grid_618': None, 'grid_75': None, 'grid_zone': None,
            'pct_from_low': None,
            'bb_upper': None, 'bb_lower': None,
            'pct_from_upper': None, 'pct_from_lower': None,
            'data_date': trade_date,
            'price_date': bj_now().strftime('%Y%m%d'),
        })
    result_df = pd.DataFrame(results)
    if not result_df.empty:
        try:
            save_dps_cache_from_df(result_df)
        except Exception:
            pass
    return result_df


def load_dps_cache():
    """Load cached DPS (dividend per share) keyed by ts_code"""
    if DPS_CACHE_FILE.exists():
        try:
            with open(DPS_CACHE_FILE, 'r', encoding='utf-8') as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def save_dps_cache_from_df(df):
    """Persist DPS from a screened DataFrame so yield can be recalculated with new prices"""
    if df is None or df.empty:
        return
    cache = load_dps_cache()
    today = bj_now().strftime('%Y%m%d')
    for _, r in df.iterrows():
        code = r.get('code', '')
        dps = r.get('dividend_per_share')
        if code and dps is not None and dps > 0:
            cache[code] = {'dps': round(float(dps), 6), 'date': today}
    try:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        with open(DPS_CACHE_FILE, 'w', encoding='utf-8') as f:
            json.dump(cache, f, ensure_ascii=False)
    except Exception as e:
        print(f'  DPS cache write error: {e}')


def recalc_yield_from_dps(df):
    """Recalculate dividend_yield = cached_dps / latest_price * 100 for each stock.
    This allows updating yields without calling Tushare (avoiding rate limit).
    Returns (df_updated, recalc_count)"""
    if df is None or df.empty:
        return df, 0
    cache = load_dps_cache()
    if not cache:
        return df, 0
    recalc = 0
    for i, (_, r) in enumerate(df.iterrows()):
        code = r.get('code', '')
        if code in cache:
            dps = cache[code]['dps']
            price = r.get('latest_price', 0)
            if price and price > 0:
                new_yield = round(dps / float(price) * 100, 2)
                old_yield = r.get('dividend_yield', 0)
                if abs(new_yield - float(old_yield)) > 0.01:
                    df.at[i, 'dividend_yield'] = new_yield
                    df.at[i, 'dividend_per_share'] = round(dps, 4)
                    recalc += 1
    if recalc:
        print(f'[{bj_now():%H:%M}] DPS recalc: {recalc} stocks yield updated from cache')
    return df, recalc


def supplement_baostock(df):
    import baostock as bs
    if df.empty:
        return df

    # Login to Baostock (with retry)
    login_ok = False
    for attempt in range(3):
        try:
            lg = bs.login()
            if lg.error_code == '0':
                login_ok = True
                break
            print(f'  Baostock login failed (attempt {attempt+1}/3): {lg.error_msg}')
            time.sleep(2)
        except Exception as e:
            print(f'  Baostock login error (attempt {attempt+1}/3): {e}')
            time.sleep(2)
    if not login_ok:
        print('  Baostock login failed, skipping real-time price update (using Tushare cache)')
        return df

    today = bj_now().strftime('%Y%m%d %H:%M')
    fail_count = 0
    max_fail = 20  # consecutive failures threshold

    for i, (_, row) in enumerate(df.iterrows()):
        if fail_count >= max_fail:
            print(f'  Baostock: {max_fail} consecutive failures, skipping remaining {len(df) - i} stocks')
            break

        df.at[i, 'price_date'] = today
        parts = row['code'].split('.')
        bs_code = f'{parts[1].lower()}.{parts[0]}' if len(parts) == 2 else row['code']

        # Retry up to 2 times per stock
        success = False
        for retry in range(2):
            try:
                end = bj_now()
                start = end - timedelta(days=400)
                rs = bs.query_history_k_data_plus(
                    bs_code, 'date,close',
                    start_date=start.strftime('%Y-%m-%d'),
                    end_date=end.strftime('%Y-%m-%d'),
                    frequency='d', adjustflag='2',
                )
                if rs.error_code != '0':
                    fail_count += 1
                    break
                dp = rs.get_data()
                if dp.empty:
                    fail_count += 1
                    break
                dp['close'] = pd.to_numeric(dp['close'], errors='coerce')
                dp.dropna(subset=['close'], inplace=True)
                if dp.empty:
                    fail_count += 1
                    break

                df.at[i, 'latest_price'] = round(float(dp['close'].iloc[-1]), 2)
                mn = float(dp['close'].min())
                df.at[i, 'min_price_1y'] = round(mn, 2)
                df.at[i, 'pct_from_low'] = round((df.at[i, 'latest_price'] - mn) / mn * 100, 1)

                # 1Y high + Fibonacci grid (6 zones)
                mx = float(dp['close'].max())
                df.at[i, 'max_price_1y'] = round(mx, 2)
                grid = calc_grid(mn, mx, df.at[i, 'latest_price'])
                df.at[i, 'grid_25'] = grid['grid_25']
                df.at[i, 'grid_382'] = grid['grid_382']
                df.at[i, 'grid_50'] = grid['grid_50']
                df.at[i, 'grid_618'] = grid['grid_618']
                df.at[i, 'grid_75'] = grid['grid_75']
                df.at[i, 'grid_zone'] = grid['grid_zone']

                # Dividend yield & market cap from Tushare dv_ttm, not recalculated with price
                # Bollinger Bands (weekly)
                try:
                    dp_dt = dp.copy()
                    dp_dt['trade_date'] = pd.to_datetime(dp_dt['date']) if 'date' in dp_dt.columns else pd.to_datetime(dp_dt.index)
                    dp_dt = dp_dt.set_index('trade_date').sort_index()
                    weekly = dp_dt['close'].resample('W-FRI').last().dropna()
                    if len(weekly) >= 20:
                        ma20 = weekly.rolling(20).mean()
                        std20 = weekly.rolling(20).std()
                        bb_up = float(ma20.iloc[-1] + 2 * std20.iloc[-1])
                        bb_lo = float(ma20.iloc[-1] - 2 * std20.iloc[-1])
                        df.at[i, 'bb_upper'] = round(bb_up, 2)
                        df.at[i, 'bb_lower'] = round(bb_lo, 2)
                        df.at[i, 'pct_from_upper'] = round((df.at[i, 'latest_price'] - bb_up) / bb_up * 100, 1)
                        df.at[i, 'pct_from_lower'] = round((df.at[i, 'latest_price'] - bb_lo) / bb_lo * 100, 1)
                except Exception:
                    pass

                fail_count = 0  # reset consecutive failure count on success
                success = True
                break
            except Exception:
                if retry == 0:
                    time.sleep(0.5)
                else:
                    fail_count += 1

        # Rate limit: 150ms between stocks
        time.sleep(0.15)

        # Save daily price history + daily BB for mini chart (last ~1 year)
        try:
            closes = [round(float(c), 2) for c in dp['close'].values[-260:].tolist()]
            df.at[i, 'price_history'] = json.dumps(closes)
            if len(closes) >= 20:
                import numpy as np
                s = pd.Series(closes)
                ma20_d = s.rolling(20).mean()
                std20_d = s.rolling(20).std()
                df.at[i, 'bb_daily_upper'] = json.dumps([round(float(v), 2) if pd.notna(v) else None for v in (ma20_d + 2 * std20_d).tolist()])
                df.at[i, 'bb_daily_mid'] = json.dumps([round(float(v), 2) if pd.notna(v) else None for v in ma20_d.tolist()])
                df.at[i, 'bb_daily_lower'] = json.dumps([round(float(v), 2) if pd.notna(v) else None for v in (ma20_d - 2 * std20_d).tolist()])
        except Exception:
            pass

    bs.logout()
    return df


def apply_price_filter(df):
    """Filter: price within 15% of 1Y low AND within 15% of weekly BB lower"""
    if df.empty:
        return df
    mask = df['pct_from_low'].notna() & df['pct_from_lower'].notna()
    mask &= (df['pct_from_low'] < MAX_PCT_FROM_LOW) & (df['pct_from_lower'] < MAX_PCT_FROM_LOWER)
    mask |= df['code'].isin(HOLDINGS)  # 持仓股始终保留
    return df[mask].copy()


def load_price_cache():
    """Load cached price data (fast)"""
    if PRICE_CACHE_FILE.exists():
        try:
            with open(PRICE_CACHE_FILE, 'r', encoding='utf-8') as f:
                data = json.load(f)
            return pd.DataFrame(data['stocks']), data.get('price_date', '')
        except Exception:
            pass
    return pd.DataFrame(), ''


def save_price_cache(df):
    """Save price data to cache"""
    data = {
        'stocks': json.loads(df.to_json(orient='records', force_ascii=False)),
        'price_date': bj_now().strftime('%Y%m%d'),
    }
    with open(PRICE_CACHE_FILE, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False)


def run_full_pipeline(force_refresh=False):
    """Full pipeline: Tushare screen -> Baostock prices -> price filter -> cache
    - force_refresh=False: prefer cache (fast); fallback to Tushare base data only
    - force_refresh=True: full Baostock pipeline + cache update
    """
    if not force_refresh:
        cached_df, _ = load_price_cache()
        if not cached_df.empty:
            return cached_df, True  # from_cache=True
        # No price cache — try Tushare base data (no auto-fetch)
        df = screen_from_cache()
        return (df if df is not None else pd.DataFrame()), False

    df = screen_from_cache()
    if df is None or df.empty:
        return pd.DataFrame(), False

    df = supplement_baostock(df)
    df = apply_price_filter(df)
    if not df.empty:
        save_price_cache(df)
    return df, False


def update_tushare_cache():
    token = load_token()
    if not token:
        return False, 'TUSHARE_TOKEN not configured (add it to .env file or Render env vars)'
    import tushare as ts
    try:
        pro = ts.pro_api(token=token)
    except Exception as e:
        return False, f'Tushare init failed: {e} (check token validity)'
    trade_date = None
    last_error = ''
    # Search back up to 10 days for a trading date
    for offset in range(10):
        d = (bj_now() - timedelta(days=offset)).strftime('%Y%m%d')
        try:
            result = pro.daily(trade_date=d, fields='trade_date')
            if result is not None and not result.empty:
                trade_date = d
                break
        except Exception as e:
            last_error = str(e)
            continue
    if not trade_date:
        hint = f' (last error: {last_error})' if last_error else ''
        return False, f'Could not find recent trading date — token may need IP whitelist on Render{hint}'
    try:
        df = pro.daily_basic(trade_date=trade_date)
        if df is None or df.empty:
            return False, f'daily_basic returned empty for {trade_date}'
    except Exception as e:
        return False, f'API call failed: {e}'
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    df.to_parquet(CACHE_DIR / f'daily_basic_{trade_date}.parquet', index=False)
    names = pro.stock_basic(exchange='', list_status='L', fields='ts_code,name')
    names.to_parquet(CACHE_DIR / 'stock_names.parquet', index=False)
    return True, f'Updated {len(df)} stocks ({trade_date})'


# ════════════════════ PushPlus ════════════════════

import html as html_mod
import urllib.request

def get_dashboard_url():
    """Return dashboard URL based on deployment environment"""
    if os.getenv('RENDER'):
        return 'https://dashboard-of-high-divided-screen.onrender.com'
    if os.getenv('GITHUB_ACTIONS'):
        return 'https://edison19490901-netizen.github.io/dashboard-of-high_divided_screen/'
    # Local: open local HTML file directly
    root = os.path.dirname(os.path.abspath(__file__))
    return f'file:///{root.replace(chr(92), "/")}/dashboard.html'


def is_local_env():
    """Check if running in local environment"""
    return not os.getenv('RENDER') and not os.getenv('GITHUB_ACTIONS')


def send_pushplus(token: str, title: str, content: str, template: str = 'html') -> bool:
    url = 'http://www.pushplus.plus/send'
    data = json.dumps({'token': token, 'title': title, 'content': content, 'template': template}).encode('utf-8')
    req = urllib.request.Request(url, data=data, headers={'Content-Type': 'application/json'})
    with urllib.request.urlopen(req, timeout=15) as resp:
        result = json.loads(resp.read().decode())
        return result.get('code') == 200


def build_push_html(df) -> str:
    """Build compact HTML table for PushPlus (under 20k chars)"""
    count = len(df)
    now = bj_now().strftime('%Y-%m-%d %H:%M')
    df_sorted = df.sort_values('dividend_yield', ascending=False)

    rows = ''
    for _, r in df_sorted.iterrows():
        name = html_mod.escape(str(r.get('name', '-')))
        code = html_mod.escape(str(r.get('code', '-')))
        price = r.get('latest_price', 0)
        dps = r.get('dividend_per_share')
        t6 = f'{dps/0.06:.2f}' if dps and dps > 0 else '-'
        t55 = f'{dps/0.055:.2f}' if dps and dps > 0 else '-'
        t5 = f'{dps/0.05:.2f}' if dps and dps > 0 else '-'
        t45 = f'{dps/0.045:.2f}' if dps and dps > 0 else '-'
        rows += f'<tr><td class="nl">{name}<span>{code}</span></td><td>{price:.2f}</td><td>{t6}</td><td>{t55}</td><td>{t5}</td><td>{t45}</td></tr>'

    css = 'body{margin:0;padding:10px;font-family:-apple-system,PingFang SC,Microsoft YaHei,sans-serif;background:#fff;color:#1a1a2e;font-size:12px}' \
          'h2{font-size:16px;text-align:center;margin:0 0 4px;color:#1a1a2e}' \
          '.info{text-align:center;font-size:10px;color:#64748b;margin-bottom:10px;padding-bottom:8px;border-bottom:1px solid #e2e8f0}' \
          'table{width:100%;border-collapse:collapse}' \
          'th{background:#f1f5f9;color:#64748b;font-size:10px;padding:6px 3px;text-align:left}' \
          'td{padding:5px 3px;border-bottom:1px solid #f1f5f9}' \
          '.nl{font-weight:500}.nl span{display:block;font-size:9px;color:#8892b0}' \
          '.ft{text-align:center;font-size:9px;color:#94a3b8;padding-top:8px;border-top:1px solid #e2e8f0;margin-top:8px}'

    return f'<!DOCTYPE html><html lang="en"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1.0">' \
           f'<title>High Dividend Daily Report</title><style>{css}</style></head><body>' \
           f'<h2>High Dividend Daily Report</h2>' \
           f'<div class="info">{now} | <b style="color:#059669">{count}</b> stocks | Div&gt;3% · MktCap&gt;50B · Low&lt;15% · BB&lt;15%</div>' \
           f'<table><thead><tr><th>Name</th><th>Price</th><th>P@6%</th><th>P@5.5%</th><th>P@5%</th><th>P@4.5%</th></tr></thead>' \
           f'<tbody>{rows}</tbody></table>' \
           f'<div class="ft">Tushare + Baostock | <a href="{get_dashboard_url()}" style="color:#6366f1">Open Dashboard</a></div></body></html>'


def _update_html_embed(stocks):
    """Update EMBED data in dashboard.html and index.html (disk persist)"""
    import re
    embed_json = json.dumps(stocks, ensure_ascii=False)
    for fname in ['dashboard.html', 'index.html']:
        fpath = BASE_DIR / fname
        if not fpath.exists():
            continue
        html = fpath.read_text(encoding='utf-8')
        if 'var EMBED=' in html:
            html = re.sub(
                r'var EMBED=\[.*?\];',
                f'var EMBED={embed_json};',
                html
            )
        elif 'var EMBED =' in html:
            html = re.sub(
                r'var EMBED = \[.*?\];',
                f'var EMBED = {embed_json};',
                html
            )
        fpath.write_text(html, encoding='utf-8')
        print(f'[{bj_now():%H:%M}] EMBED updated in {fname} ({len(stocks)} stocks)')


# ════════════════════ Web Server ════════════════════

HTML_FILE = BASE_DIR / 'dashboard.html'
PASSWORD_FILE = BASE_DIR / 'password.html'

# Auth helpers
DASHBOARD_PASSWORD = os.getenv('DASHBOARD_PASSWORD', '')
AUTH_COOKIE_NAME = 'dash_auth'

def _make_token(password):
    return hashlib.sha256(f'dash-salt-{password}'.encode()).hexdigest()

def _parse_cookies(handler):
    cookie_header = handler.headers.get('Cookie', '')
    cookies = {}
    for item in cookie_header.split(';'):
        item = item.strip()
        if '=' in item:
            k, v = item.split('=', 1)
            cookies[k.strip()] = v.strip()
    return cookies

def _check_auth(handler):
    if not DASHBOARD_PASSWORD:
        return True  # No password set = open access
    cookies = _parse_cookies(handler)
    token = cookies.get(AUTH_COOKIE_NAME, '')
    return token == _make_token(DASHBOARD_PASSWORD)


class Handler(SimpleHTTPRequestHandler):
    def do_GET(self):
        path = urlparse(self.path).path
        if path == '/api/data':
            self._api_data()
        elif path == '/api/export':
            self._export_excel()
        elif path == '/api/health':
            self._json({'status': 'ok', 'cache': CACHE_DIR.exists()})
        elif path in ('/', '/password.html'):
            if DASHBOARD_PASSWORD and _check_auth(self):
                self._redirect('/dashboard')
            else:
                self._serve_password()
        elif path in ('/dashboard', '/dashboard.html', '/index.html'):
            if _check_auth(self):
                self._serve_dashboard()
            else:
                self._redirect('/')
        elif path in ('/manifest.json', '/sw.js', '/ox_icon.png', '/ox_icon-32.png', '/ox_icon-180.png', '/ox_icon-192.png', '/ox_icon-512.png'):
            self._send_static(path)
        else:
            self.send_error(404)

    def _send_static(self, path):
        file_path = BASE_DIR / path.lstrip('/')
        if not file_path.exists():
            self.send_error(404)
            return
        content_types = {
            '.json': 'application/json',
            '.js': 'application/javascript',
            '.png': 'image/png',
        }
        ext = file_path.suffix
        content = file_path.read_bytes()
        self.send_response(200)
        self.send_header('Content-Type', content_types.get(ext, 'application/octet-stream'))
        self.send_header('Content-Length', str(len(content)))
        self.send_header('Cache-Control', 'public, max-age=86400')
        self.end_headers()
        self.wfile.write(content)

    def do_POST(self):
        path = urlparse(self.path).path
        if path == '/login':
            self._handle_login()
        elif path == '/api/update':
            self._api_update()
        elif path == '/api/update_all':
            self._api_update_all()
        elif path == '/api/refresh_prices':
            self._api_refresh_prices()
        elif path == '/api/pushplus':
            self._api_pushplus()
        else:
            self.send_error(404)

    def _api_refresh_prices(self):
        try:
            self.__api_refresh_prices()
        except Exception as e:
            import traceback
            traceback.print_exc()
            self._json({'ok': False, 'error': f'Unexpected error: {e}'}, 500)

    def __api_refresh_prices(self):
        # Step 1: Screen from cache (dividend + market cap)
        df = screen_from_cache()
        if df is None or df.empty:
            # Auto-populate cache if missing (e.g. after Render restart)
            print(f'[{bj_now():%H:%M}] No cache found — fetching from Tushare first...')
            ok, msg = update_tushare_cache()
            if not ok:
                self._json({'ok': False, 'error': f'Cache empty & Tushare failed: {msg}'}, 500)
                return
            df = screen_from_cache()
            if df is None or df.empty:
                self._json({'ok': False, 'error': 'Tushare fetched but no stocks matched criteria'}, 500)
                return

        # Step 2: Supplement with Baostock real-time prices
        df_full = supplement_baostock(df.copy())
        today = bj_now().strftime('%Y%m%d %H:%M')

        # Step 2b: Recalculate dividend yield from cached DPS ÷ latest price
        df_full, recalc_n = recalc_yield_from_dps(df_full)
        if recalc_n > 0:
            df_full['dv_check'] = pd.to_numeric(df_full['dividend_yield'], errors='coerce')
            df_full = df_full[df_full['dv_check'] > DIVIDEND_THRESHOLD].copy()
            df_full.drop(columns=['dv_check'], inplace=True, errors='ignore')
            print(f'[{bj_now():%H:%M}] DPS recalc: {len(df_full)} stocks after re-filter (>3% yield)')

        # Step 3: Apply price filter (skip if Baostock failed)
        baostock_ok = df_full['pct_from_low'].notna().any()
        if not baostock_ok:
            print(f'[{bj_now():%H:%M}] Baostock failed — using cached prices, skipping filter')
            df_filtered = df_full
        else:
            df_filtered = apply_price_filter(df_full)
        if not df_filtered.empty:
            save_price_cache(df_filtered)
        data = json.loads(df_filtered.to_json(orient='records', force_ascii=False))

        # Step 4: PushPlus — push filtered stocks
        token = os.getenv('PUSHPLUS_TOKEN', '')
        if token and not df_filtered.empty:
            try:
                html_content = build_push_html(df_filtered)
                ok = send_pushplus(token, f'High Dividend Daily Report ({len(data)} stocks)', html_content, 'html')
                print(f'[{bj_now():%H:%M}] PushPlus: {"OK" if ok else "FAIL"} (size={len(html_content)} chars)')
            except Exception as e:
                print(f'[{bj_now():%H:%M}] PushPlus error: {e}')

        # Step 5: Persist EMBED to HTML files on disk
        try:
            _update_html_embed(data)
        except Exception as e:
            print(f'[{bj_now():%H:%M}] EMBED update failed: {e}')

        self._json({'ok': True, 'stocks': data, 'count': len(data), 'price_date': today})

    def _api_update_all(self):
        """Unified update: Tushare → screen → Baostock prices → filter → cache → EMBED"""
        try:
            # Step 1: Update Tushare cache (with fallback to existing cache)
            print(f'[{bj_now():%H:%M}] Update All: fetching Tushare...')
            ok, msg = update_tushare_cache()
            tushare_ok = ok
            cache_date = None
            if not ok:
                # Fall back to existing cache if available (same pattern as _api_update)
                pqts = sorted(CACHE_DIR.glob('daily_basic_*.parquet'), reverse=True)
                if pqts:
                    cache_date = pqts[0].stem.replace('daily_basic_', '')
                    print(f'[{bj_now():%H:%M}] Tushare failed ({msg}), using cached data ({cache_date})')
                else:
                    self._json({'ok': False, 'error': f'Tushare failed and no cache exists: {msg}'}, 500)
                    return

            # Step 2: Screen from cache
            df = screen_from_cache()
            if df is None or df.empty:
                if not tushare_ok:
                    self._json({'ok': False, 'error': f'Existing cache has no matching stocks (cache date: {cache_date})'}, 500)
                else:
                    self._json({'ok': False, 'error': 'No stocks matched criteria after Tushare fetch'}, 500)
                return

            # Step 3: Supplement with Baostock prices
            df_full = supplement_baostock(df.copy())
            today = bj_now().strftime('%Y%m%d %H:%M')

            # Step 3b: Recalculate dividend yield from cached DPS ÷ latest price
            # This keeps yields current even when Tushare is rate-limited
            df_full, recalc_n = recalc_yield_from_dps(df_full)

            # Step 3c: Re-apply dividend filter after DPS recalculation
            if recalc_n > 0:
                df_full['dv_check'] = pd.to_numeric(df_full['dividend_yield'], errors='coerce')
                df_full = df_full[df_full['dv_check'] > DIVIDEND_THRESHOLD].copy()
                df_full.drop(columns=['dv_check'], inplace=True, errors='ignore')
                print(f'[{bj_now():%H:%M}] DPS recalc: {len(df_full)} stocks after re-filter (>3% yield)')

            # Step 4: Apply price filter (skip if Baostock failed)
            baostock_ok = df_full['pct_from_low'].notna().any()
            if not baostock_ok:
                print(f'[{bj_now():%H:%M}] Baostock failed — using base data, skipping filter')
                df_filtered = df_full
            else:
                df_filtered = apply_price_filter(df_full)

            # Step 5: Save price cache
            if not df_filtered.empty:
                save_price_cache(df_filtered)
            data = json.loads(df_filtered.to_json(orient='records', force_ascii=False))

            # Step 6: Update EMBED in HTML files
            try:
                _update_html_embed(data)
            except Exception as e:
                print(f'[{bj_now():%H:%M}] EMBED update failed: {e}')

            resp = {'ok': True, 'stocks': data, 'count': len(data), 'price_date': today}
            if not tushare_ok:
                resp['warning'] = f'Tushare unavailable, using cached data ({cache_date}) — {msg}'
            self._json(resp)
        except Exception as e:
            import traceback
            traceback.print_exc()
            self._json({'ok': False, 'error': f'Unexpected error: {e}'}, 500)

    def _api_pushplus(self):
        """Manual PushPlus push notification with current data"""
        token = os.getenv('PUSHPLUS_TOKEN', '')
        if not token:
            self._json({'ok': False, 'error': 'PUSHPLUS_TOKEN not configured'}, 400)
            return

        # Get current data: prefer price cache, fallback to parquet cache
        cached_df, _ = load_price_cache()
        if cached_df.empty:
            df = screen_from_cache()
            if df is None or df.empty:
                self._json({'ok': False, 'error': 'No data available to push'}, 400)
                return
            cached_df = df

        try:
            data = json.loads(cached_df.to_json(orient='records', force_ascii=False))
            html_content = build_push_html(cached_df)
            ok = send_pushplus(token, f'High Dividend Daily Report ({len(data)} stocks)', html_content, 'html')
            print(f'[{bj_now():%H:%M}] PushPlus manual: {"OK" if ok else "FAIL"}')
            self._json({'ok': ok, 'count': len(data), 'message': 'Push sent' if ok else 'PushPlus send failed'})
        except Exception as e:
            self._json({'ok': False, 'error': f'PushPlus error: {e}'}, 500)

    def _json(self, data, status=200):
        body = json.dumps(data, ensure_ascii=False).encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _serve_password(self):
        if PASSWORD_FILE.exists():
            content = PASSWORD_FILE.read_bytes()
        else:
            content = b'<h1>password.html not found</h1>'
        self.send_response(200)
        self.send_header('Content-Type', 'text/html; charset=utf-8')
        self.send_header('Content-Length', str(len(content)))
        self.send_header('Cache-Control', 'no-cache, no-store, must-revalidate')
        self.send_header('Pragma', 'no-cache')
        self.send_header('Expires', '0')
        self.end_headers()
        self.wfile.write(content)

    def _serve_dashboard(self):
        if HTML_FILE.exists():
            content = HTML_FILE.read_bytes()
        else:
            content = b'<h1>dashboard.html not found</h1>'
        self.send_response(200)
        self.send_header('Content-Type', 'text/html; charset=utf-8')
        self.send_header('Content-Length', str(len(content)))
        self.send_header('Cache-Control', 'no-cache, no-store, must-revalidate')
        self.send_header('Pragma', 'no-cache')
        self.send_header('Expires', '0')
        self.end_headers()
        self.wfile.write(content)

    def _redirect(self, location):
        self.send_response(302)
        self.send_header('Location', location)
        self.end_headers()

    def _handle_login(self):
        content_length = int(self.headers.get('Content-Length', 0))
        body = self.rfile.read(content_length).decode('utf-8')
        from urllib.parse import parse_qs
        params = parse_qs(body)
        password = params.get('password', [''])[0]

        if not DASHBOARD_PASSWORD:
            # No password configured — allow access
            token = _make_token('')
            self.send_response(302)
            self.send_header('Set-Cookie', f'{AUTH_COOKIE_NAME}={token}; Path=/; HttpOnly; Max-Age=86400; SameSite=Lax')
            self.send_header('Location', '/dashboard')
            self.end_headers()
        elif password == DASHBOARD_PASSWORD:
            token = _make_token(password)
            self.send_response(302)
            self.send_header('Set-Cookie', f'{AUTH_COOKIE_NAME}={token}; Path=/; HttpOnly; Max-Age=86400; SameSite=Lax')
            self.send_header('Location', '/dashboard')
            self.end_headers()
        else:
            self.send_response(302)
            self.send_header('Location', '/?err=1')
            self.end_headers()

    def _api_data(self):
        df, from_cache = run_full_pipeline(force_refresh=False)
        if not df.empty:
            data = json.loads(df.to_json(orient='records', force_ascii=False))
        else:
            data = []
        self._json({'stocks': data, 'count': len(data)})

    def _export_excel(self):
        """Generate and serve an Excel (.xlsx) file of screened stocks."""
        try:
            from io import BytesIO
            df, _ = run_full_pipeline(force_refresh=False)
            if df.empty:
                self._json({'error': 'No data available'})
                return
            rows = []
            for _, r in df.iterrows():
                dps = safe_float(r.get('dividend_per_share'), 0)
                rows.append({
                    '代码': r.get('code', ''),
                    '名称': r.get('name', ''),
                    '最新价': safe_float(r.get('latest_price'), 0),
                    '股息率(%)': safe_float(r.get('dividend_yield'), 0),
                    '每股分红DPS': dps if dps > 0 else None,
                    '目标价6%': round(dps / 0.06, 2) if dps > 0 else None,
                    '目标价5.5%': round(dps / 0.055, 2) if dps > 0 else None,
                    '目标价5%': round(dps / 0.05, 2) if dps > 0 else None,
                    '目标价4.5%': round(dps / 0.045, 2) if dps > 0 else None,
                    '市值(亿)': safe_float(r.get('market_cap_billion'), 0),
                    '1Y最低': r.get('min_price_1y'),
                    '1Y最高': r.get('max_price_1y'),
                    '网格0.25': r.get('grid_25'),
                    '网格0.382': r.get('grid_382'),
                    '网格0.5': r.get('grid_50'),
                    '网格0.618': r.get('grid_618'),
                    '网格0.75': r.get('grid_75'),
                    '网格区域': r.get('grid_zone'),
                    '距1Y低点(%)': r.get('pct_from_low'),
                    'BB上轨': r.get('bb_upper'),
                    'BB下轨': r.get('bb_lower'),
                    '距BB下轨(%)': r.get('pct_from_lower'),
                })
            export_df = pd.DataFrame(rows)
            output = BytesIO()
            with pd.ExcelWriter(output, engine='openpyxl') as writer:
                export_df.to_excel(writer, sheet_name='Stocks', index=False)
                ws = writer.sheets['Stocks']
                for col_idx, col_name in enumerate(export_df.columns, 1):
                    max_len = max(
                        export_df[col_name].astype(str).str.len().max(),
                        len(str(col_name))
                    )
                    ws.column_dimensions[ws.cell(row=1, column=col_idx).column_letter].width = min(max_len + 4, 40)
            body = output.getvalue()
            filename = f'dividend_stocks_{bj_now().strftime("%Y%m%d_%H%M")}.xlsx'
            self.send_response(200)
            self.send_header('Content-Type', 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
            self.send_header('Content-Disposition', f'attachment; filename="{filename}"')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except Exception as e:
            import traceback
            traceback.print_exc()
            self._json({'error': f'Export failed: {e}'}, 500)

    def _api_update(self):
        ok, msg = update_tushare_cache()
        if ok:
            # Refresh EMBED in HTML files with latest cache data
            try:
                df = screen_from_cache()
                if df is not None and not df.empty:
                    data = json.loads(df.to_json(orient='records', force_ascii=False))
                    _update_html_embed(data)
            except Exception as e:
                print(f'  EMBED update failed: {e}')
            self._json({'ok': True, 'message': msg})
            return
        # Tushare fetch failed — fall back to existing cache if available
        pqts = sorted(CACHE_DIR.glob('daily_basic_*.parquet'), reverse=True)
        if pqts:
            cache_date = pqts[0].stem.replace('daily_basic_', '')
            try:
                df = screen_from_cache()
                if df is not None and not df.empty:
                    data = json.loads(df.to_json(orient='records', force_ascii=False))
                    _update_html_embed(data)
            except Exception:
                pass
            self._json({'ok': True, 'message': f'Using cached data ({cache_date}) — Tushare: {msg}'})
        else:
            self._json({'ok': False, 'error': msg}, 500)

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'GET,POST,OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type')
        self.end_headers()

    def log_message(self, format, *args):
        print(f'[{bj_now().strftime("%H:%M:%S")}] {args[0]}')


def main():
    port = int(os.getenv('PORT', '8080'))

    # Startup: check cache status
    pqts = sorted(CACHE_DIR.glob('daily_basic_*.parquet'), reverse=True)
    if pqts:
        latest_date = pqts[0].stem.replace('daily_basic_', '')
        print(f'Cache found: {latest_date} ({len(pqts)} files)')
    else:
        print('No cache found — attempting initial Tushare fetch...')
        ok, msg = update_tushare_cache()
        print(f'  Init cache: {msg}')
        if not ok and os.getenv('RENDER'):
            print('  WARNING: On Render, set TUSHARE_TOKEN env var and ensure IP is whitelisted at tushare.pro')

    server = HTTPServer(('0.0.0.0', port), Handler)
    print(f'API Server: http://localhost:{port}')
    print(f'Data endpoint: http://localhost:{port}/api/data')
    print(f'Update endpoint: POST http://localhost:{port}/api/update')
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print('\nStopped')
        server.server_close()


if __name__ == '__main__':
    main()
