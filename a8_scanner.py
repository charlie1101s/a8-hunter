#!/usr/bin/env python3
"""
A8 Hunter 每日掃描器
- 掃描範圍：台灣50（名單在 tickers_tw50.txt）＋ SPY 權重前 50 大持股（每次自動從 SSGA 抓）
- 訊號邏輯：與 Pine「A8 Hunter V4.0」的 is_A8_Setup 完全相同（日線）
- 輸出：docs/index.html 網頁（GitHub Pages 發布）；有設定 Telegram 的話另外推播

用法：
  python a8_scanner.py tw                  掃台股，更新網頁
  python a8_scanner.py us                  掃美股，更新網頁
  python a8_scanner.py all                 兩個都掃
  python a8_scanner.py tw --require-today  Telegram 只推「今天」這根K線的訊號（排程用）
  python a8_scanner.py all --no-telegram   不推播，只更新網頁
  python a8_scanner.py --history 2330.TW   列出某檔歷史上所有 A8 訊號（拿去跟 TradingView 對照）
"""
import argparse
import datetime as dt
import html
import io
import json
import os
import sys
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import requests

# ===== A8 參數（與 Pine 版預設值相同）=====
CT_LEN = 365            # EMA 長度（日線 = 365 根交易日）
CT_FACTOR_1 = 0.7       # 進場區上緣 = EMA × 0.7
CT_FACTOR_2 = 0.5       # 極限鐵底（只用於顯示，不影響訊號）
BODY_MULT = 2.0         # 實體 > 20 根平均實體 × 2
VOL_MULT = 1.5          # 量 > 20 根均量 × 1.5
RSI_LEN = 14
RSI_MAX = 60            # RSI < 60
UPPER_WICK_RATIO = 0.8  # 上影線 < 實體 × 0.8
AVG_LEN = 20
RECENT_BARS = 20        # 網頁上「近期 A8」回看幾根K線

HERE = os.path.dirname(os.path.abspath(__file__))
TW_LIST_FILE = os.path.join(HERE, "tickers_tw50.txt")
DOCS = os.path.join(HERE, "docs")
SPY_URL = ("https://www.ssga.com/us/en/intermediary/library-content/products/"
           "fund-data/etfs/us/holdings-daily-us-en-spy.xlsx")

# SSGA 抓不到時的備援名單（大致為 SPY 權重前段班，不保證即時）
SPY_FALLBACK = [
    "NVDA", "MSFT", "AAPL", "AMZN", "META", "AVGO", "GOOGL", "GOOG", "TSLA", "BRK-B",
    "JPM", "LLY", "V", "NFLX", "XOM", "MA", "COST", "WMT", "ORCL", "PG",
    "JNJ", "HD", "ABBV", "BAC", "PLTR", "KO", "AMD", "CSCO", "UNH", "GE",
    "CVX", "WFC", "IBM", "CRM", "PM", "MS", "ABT", "GS", "LIN", "MCD",
    "AXP", "INTU", "DIS", "NOW", "MRK", "T", "RTX", "CAT", "PEP", "MU",
]

MARKETS = {
    "tw": {"label": "台股50", "tz": "Asia/Taipei", "ccy": "NT$"},
    "us": {"label": "SPY 前50", "tz": "America/New_York", "ccy": "US$"},
}


# ---------------------------------------------------------------- 指標計算
def pine_rma(src: pd.Series, n: int) -> pd.Series:
    """與 Pine ta.rma 相同：先用 SMA 起算，之後 alpha = 1/n 遞迴。"""
    v = src.to_numpy(dtype=float)
    out = np.full(len(v), np.nan)
    started = False
    for i in range(len(v)):
        if not started:
            if i >= n - 1 and not np.isnan(v[i - n + 1:i + 1]).any():
                out[i] = v[i - n + 1:i + 1].mean()
                started = True
        else:
            out[i] = v[i] / n + out[i - 1] * (1 - 1 / n)
    return pd.Series(out, index=src.index)


def pine_rsi(close: pd.Series, n: int) -> pd.Series:
    ch = close.diff()
    up = pine_rma(ch.clip(lower=0), n)
    dn = pine_rma((-ch).clip(lower=0), n)
    rsi = 100 - 100 / (1 + up / dn)
    rsi = np.where(dn == 0, 100.0, np.where(up == 0, 0.0, rsi))
    rsi = pd.Series(rsi, index=close.index)
    return rsi.where(up.notna() & dn.notna())


def compute_a8(df: pd.DataFrame) -> pd.DataFrame:
    o, h, c, v = df["Open"], df["High"], df["Close"], df["Volume"]
    ema = c.ewm(span=CT_LEN, adjust=False).mean()
    zone1 = ema * CT_FACTOR_1
    zone2 = ema * CT_FACTOR_2
    body = (c - o).abs()
    upper = h - np.maximum(o, c)
    avg_body = body.rolling(AVG_LEN).mean()
    avg_vol = v.rolling(AVG_LEN).mean()
    rsi = pine_rsi(c, RSI_LEN)

    sig_buy = ((c > o)
               & (body > avg_body * BODY_MULT)
               & (v > avg_vol * VOL_MULT)
               & (upper < body * UPPER_WICK_RATIO)
               & (rsi < RSI_MAX))
    a8 = (c < zone1) & sig_buy
    warm = pd.Series(np.arange(len(df)) >= CT_LEN, index=df.index)  # EMA 暖機期不出訊號
    out = df.copy()
    out["zone1"], out["zone2"], out["rsi"] = zone1, zone2, rsi
    out["body_x"] = body / avg_body
    out["vol_x"] = v / avg_vol
    out["a8"] = a8 & warm
    return out


# ---------------------------------------------------------------- 名單
def load_tw50():
    tickers, names = [], {}
    with open(TW_LIST_FILE, encoding="utf-8") as f:
        for line in f:
            line = line.split("#", 1)[0].strip()
            if not line:
                continue
            parts = line.split()
            t = parts[0] + ".TW"
            tickers.append(t)
            names[t] = parts[1] if len(parts) > 1 else ""
    return tickers, names


def load_spy_top50():
    try:
        r = requests.get(SPY_URL, headers={"User-Agent": "Mozilla/5.0"}, timeout=30)
        r.raise_for_status()
        raw = pd.read_excel(io.BytesIO(r.content), header=None)
        hdr = next(i for i in range(len(raw))
                   if "Ticker" in [str(x).strip() for x in raw.iloc[i].tolist()])
        df = raw.iloc[hdr + 1:].copy()
        df.columns = [str(x).strip() for x in raw.iloc[hdr].tolist()]
        df["Weight"] = pd.to_numeric(df["Weight"], errors="coerce")
        df["Ticker"] = df["Ticker"].astype(str).str.strip()
        df = df[df["Ticker"].str.fullmatch(r"[A-Z]{1,5}(\.[A-Z])?") & df["Weight"].notna()]
        df = df.sort_values("Weight", ascending=False).head(50)
        tickers = [t.replace(".", "-") for t in df["Ticker"]]
        names = {t.replace(".", "-"): str(n).strip().title()[:24]
                 for t, n in zip(df["Ticker"], df["Name"])}
        if len(tickers) >= 40:
            return tickers, names, None
        raise ValueError(f"只解析到 {len(tickers)} 檔")
    except Exception as e:  # noqa: BLE001
        return list(SPY_FALLBACK), {}, f"SPY 持股名單抓取失敗，改用內建備援名單（{e}）"


# ---------------------------------------------------------------- 資料
def download(tickers):
    import yfinance as yf
    data = yf.download(tickers, period="10y", interval="1d", auto_adjust=False,
                       group_by="ticker", threads=True, progress=False)
    out = {}
    for t in tickers:
        try:
            df = data[t] if len(tickers) > 1 else data
            if isinstance(df.columns, pd.MultiIndex):
                df = df.droplevel(0, axis=1)
            df = df[["Open", "High", "Low", "Close", "Volume"]].dropna(subset=["Close"])
            if len(df) > CT_LEN:
                out[t] = df
        except Exception:  # noqa: BLE001
            pass
    return out


# ---------------------------------------------------------------- 掃描
def _f(x, nd=2):
    return None if x is None or (isinstance(x, float) and np.isnan(x)) else round(float(x), nd)


def scan_market(key, frames=None, names=None, tickers=None):
    """回傳這個市場每一檔的最新狀態（給網頁和 Telegram 共用）。"""
    cfg = MARKETS[key]
    warn = None
    if tickers is None:
        if key == "tw":
            tickers, names = load_tw50()
        else:
            tickers, names, warn = load_spy_top50()
    if frames is None:
        frames = download(tickers)
    names = names or {}

    rows = []
    for t in tickers:
        df = frames.get(t)
        if df is None:
            continue
        a = compute_a8(df)
        r = a.iloc[-1]
        recent = a.iloc[-RECENT_BARS:]
        recent_hits = [
            {"date": str(pd.Timestamp(i).date()), "close": _f(x["Close"])}
            for i, x in recent[recent["a8"]].iterrows()
        ]
        rows.append({
            "ticker": t, "sym": t.replace(".TW", ""), "name": names.get(t, ""),
            "date": str(pd.Timestamp(a.index[-1]).date()),
            "close": _f(r["Close"]), "zone1": _f(r["zone1"]), "zone2": _f(r["zone2"]),
            "ratio": _f(r["Close"] / r["zone1"], 4),
            "rsi": _f(r["rsi"], 1), "body_x": _f(r["body_x"], 1), "vol_x": _f(r["vol_x"], 1),
            "a8_today": bool(r["a8"]), "recent_a8": recent_hits,
        })

    missing = [t for t in tickers if t not in frames]
    bar_date = max((x["date"] for x in rows), default=None)
    return {
        "key": key, "label": cfg["label"], "ccy": cfg["ccy"],
        "scanned_at": dt.datetime.now(ZoneInfo("Asia/Taipei")).strftime("%Y-%m-%d %H:%M"),
        "bar_date": bar_date, "total": len(tickers), "missing": missing,
        "warn": warn, "rows": rows,
    }


# ---------------------------------------------------------------- 網頁
PAGE_CSS = """
:root{--bg:#f6f5f1;--card:#fff;--ink:#1d1d1b;--mute:#6b6a64;--line:#e4e2db;
--gold:#b8860b;--gold-bg:#fff4d6;--red:#b42318;--red-bg:#fdecea;--blue:#1f5fbf;--chip:#efeee8}
@media (prefers-color-scheme:dark){:root:not([data-theme=light]){--bg:#121211;--card:#1c1c1a;
--ink:#ecebe6;--mute:#9a988f;--line:#2e2d2a;--gold:#f0c24b;--gold-bg:#3a2f10;--red:#ff8a80;
--red-bg:#3b1715;--blue:#8ab4ff;--chip:#272724}}
*{box-sizing:border-box}html,body{margin:0}
body{background:var(--bg);color:var(--ink);font:15px/1.5 -apple-system,"PingFang TC","Noto Sans TC",
"Microsoft JhengHei",sans-serif;-webkit-font-smoothing:antialiased}
.wrap{max-width:880px;margin:0 auto;padding:20px 16px 48px}
h1{font-size:22px;margin:0 0 2px;letter-spacing:.02em}
.sub{color:var(--mute);font-size:13px;margin:0 0 18px}
.tabs{display:flex;gap:6px;margin:0 0 16px}
.tab{flex:1;border:1px solid var(--line);background:var(--card);color:var(--ink);border-radius:10px;
padding:10px 8px;font:inherit;font-weight:600;cursor:pointer}
.tab[aria-selected=true]{background:var(--ink);color:var(--bg);border-color:var(--ink)}
.tab .n{font-weight:500;opacity:.75;font-size:12px;display:block}
.meta{color:var(--mute);font-size:12.5px;margin:0 0 14px}
.warn{background:var(--red-bg);color:var(--red);border-radius:10px;padding:10px 12px;font-size:13px;margin:0 0 14px}
h2{font-size:15px;margin:22px 0 10px}
h2 small{color:var(--mute);font-weight:500;font-size:12.5px;margin-left:6px}
.cards{display:grid;gap:10px;grid-template-columns:repeat(auto-fill,minmax(250px,1fr))}
.card{background:var(--gold-bg);border:1px solid var(--gold);border-radius:12px;padding:12px 14px}
.card .t{font-weight:700;font-size:16px}.card .t span{font-weight:500;color:var(--mute);font-size:13px;margin-left:6px}
.card .px{font-size:22px;font-weight:700;margin:4px 0 2px;font-variant-numeric:tabular-nums}
.card .d{font-size:12.5px;color:var(--mute);font-variant-numeric:tabular-nums}
.empty{background:var(--card);border:1px dashed var(--line);border-radius:12px;padding:14px;color:var(--mute);font-size:13.5px}
.tbl{background:var(--card);border:1px solid var(--line);border-radius:12px;overflow:hidden}
.row{display:grid;grid-template-columns:1fr auto;gap:2px 10px;padding:10px 14px;border-top:1px solid var(--line)}
.row:first-child{border-top:0}
.row .nm{font-weight:600}.row .nm span{color:var(--mute);font-weight:400;font-size:13px;margin-left:6px}
.row .rt{text-align:right;font-variant-numeric:tabular-nums;font-weight:600}
.row .s{grid-column:1/-1;color:var(--mute);font-size:12.5px;font-variant-numeric:tabular-nums}
.bar{grid-column:1/-1;height:4px;background:var(--chip);border-radius:2px;position:relative;margin:4px 0 2px}
.bar i{position:absolute;top:0;bottom:0;left:0;border-radius:2px;background:var(--mute)}
.bar b{position:absolute;top:-3px;bottom:-3px;width:2px;background:var(--gold)}
.in .bar i{background:var(--red)}
.chip{display:inline-block;font-size:11px;font-weight:600;padding:1px 7px;border-radius:999px;margin-left:6px;vertical-align:1px}
.c-a8{background:var(--gold);color:#1d1d1b}.c-zone{background:var(--red-bg);color:var(--red)}
.c-floor{background:var(--red);color:#fff}
.dn{color:var(--red)}
details{margin-top:22px;color:var(--mute);font-size:13px}
details summary{cursor:pointer;font-weight:600;color:var(--ink)}
details li{margin:3px 0}
.foot{margin-top:28px;color:var(--mute);font-size:12px}
"""

PAGE_JS = """
const tabs=[...document.querySelectorAll('.tab')];
function show(k){tabs.forEach(t=>t.setAttribute('aria-selected',t.dataset.k===k));
document.querySelectorAll('.pane').forEach(p=>p.hidden=p.dataset.k!==k);
try{localStorage.setItem('a8tab',k)}catch(e){}}
tabs.forEach(t=>t.onclick=()=>show(t.dataset.k));
let k0='tw';try{k0=localStorage.getItem('a8tab')||'tw'}catch(e){}
if(!document.querySelector('.pane[data-k="'+k0+'"]'))k0=tabs[0]?.dataset.k;show(k0);
"""


def _e(s):
    return html.escape(str(s if s is not None else ""))


def _num(x, ccy=""):
    if x is None:
        return "—"
    return f"{x:,.2f}"


def render_pane(m):
    rows = m["rows"]
    a8_today = [r for r in rows if r["a8_today"]]
    recent = [r for r in rows if r["recent_a8"] and not r["a8_today"]]
    ordered = sorted(rows, key=lambda r: r["ratio"] if r["ratio"] is not None else 9)
    in_zone = [r for r in rows if r["ratio"] is not None and r["ratio"] < 1]

    out = [f'<p class="meta">K線日期 {_e(m["bar_date"])}　·　掃描時間 {_e(m["scanned_at"])}（台北）'
           f'　·　{len(rows)}/{m["total"]} 檔有資料</p>']
    probs = []
    if m.get("warn"):
        probs.append(m["warn"])
    if m["missing"]:
        probs.append(f"抓不到資料：{', '.join(m['missing'][:20])}")
    if probs:
        out.append('<div class="warn">' + "<br>".join(_e(p) for p in probs) + "</div>")

    out.append(f'<h2>今日 A8 訊號 <small>{len(a8_today)} 檔</small></h2>')
    if a8_today:
        out.append('<div class="cards">')
        for r in a8_today:
            floor = '<span class="chip c-floor">破 0.5 鐵底</span>' if r["close"] < r["zone2"] else ""
            out.append(
                f'<div class="card"><div class="t">{_e(r["sym"])}<span>{_e(r["name"])}</span>{floor}</div>'
                f'<div class="px">{_num(r["close"])}</div>'
                f'<div class="d">進場區上緣 {_num(r["zone1"])}（低 {(1 - r["ratio"]) * 100:.1f}%）<br>'
                f'RSI {r["rsi"]}　實體 {r["body_x"]}x　量 {r["vol_x"]}x</div></div>')
        out.append("</div>")
    else:
        out.append('<div class="empty">最新一根日K沒有任何一檔出現 A8。</div>')

    out.append(f'<h2>近 {RECENT_BARS} 個交易日出現過 A8 <small>{len(recent)} 檔</small></h2>')
    if recent:
        out.append('<div class="tbl">')
        for r in recent:
            h = r["recent_a8"][-1]
            chg = (r["close"] / h["close"] - 1) * 100 if h["close"] else 0
            cls = "dn" if chg < 0 else ""
            out.append(
                f'<div class="row"><div class="nm">{_e(r["sym"])}<span>{_e(r["name"])}</span></div>'
                f'<div class="rt {cls}">{chg:+.1f}%</div>'
                f'<div class="s">訊號日 {_e(h["date"])} 收 {_num(h["close"])} → 現價 {_num(r["close"])}'
                f'{"（期間共 " + str(len(r["recent_a8"])) + " 次）" if len(r["recent_a8"]) > 1 else ""}</div></div>')
        out.append("</div>")
    else:
        out.append('<div class="empty">近期沒有 A8 訊號。</div>')

    out.append(f'<h2>距離進場區 <small>獵殺區內 {len(in_zone)} 檔</small></h2>')
    out.append('<div class="tbl">')
    for r in ordered:
        if r["ratio"] is None:
            continue
        ratio = r["ratio"]
        inside = ratio < 1
        chips = ""
        if r["a8_today"]:
            chips += '<span class="chip c-a8">A8</span>'
        if r["close"] < r["zone2"]:
            chips += '<span class="chip c-floor">破鐵底</span>'
        elif inside:
            chips += '<span class="chip c-zone">獵殺區</span>'
        dist = (ratio - 1) * 100
        label = f"低於上緣 {-dist:.1f}%" if inside else f"距上緣還要跌 {(1 - 1 / ratio) * 100:.1f}%"
        # 進度條：0 = 鐵底(0.5/0.7≈0.714)，金線 = 上緣(1.0)，右端 = 2.0
        lo, hi = CT_FACTOR_2 / CT_FACTOR_1, 2.0
        pct = max(0.0, min(1.0, (ratio - lo) / (hi - lo))) * 100
        mark = (1 - lo) / (hi - lo) * 100
        out.append(
            f'<div class="row{" in" if inside else ""}"><div class="nm">{_e(r["sym"])}<span>{_e(r["name"])}</span>{chips}</div>'
            f'<div class="rt{" dn" if inside else ""}">{label}</div>'
            f'<div class="bar"><i style="width:{pct:.1f}%"></i><b style="left:{mark:.1f}%"></b></div>'
            f'<div class="s">收 {_num(r["close"])}　上緣 {_num(r["zone1"])}　鐵底 {_num(r["zone2"])}　RSI {r["rsi"]}</div></div>')
    out.append("</div>")
    return "\n".join(out)


def render_page(markets):
    tabs, panes = [], []
    for k in ("tw", "us"):
        m = markets.get(k)
        if not m:
            continue
        n = sum(r["a8_today"] for r in m["rows"])
        tabs.append(f'<button class="tab" data-k="{k}">{_e(m["label"])}'
                    f'<span class="n">今日 A8：{n} 檔</span></button>')
        panes.append(f'<section class="pane" data-k="{k}">{render_pane(m)}</section>')
    return f"""<!doctype html><html lang="zh-Hant"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>A8 Hunter 掃描</title><style>{PAGE_CSS}</style></head><body><div class="wrap">
<h1>A8 Hunter 每日掃描</h1>
<p class="sub">日線 · 收盤低於 0.7×EMA365，且出現放量大實體陽線（RSI&lt;60、上影線短）</p>
<div class="tabs">{''.join(tabs)}</div>
{''.join(panes)}
<details><summary>A8 訊號條件</summary><ul>
<li>收盤 &lt; EMA({CT_LEN}) × {CT_FACTOR_1}</li>
<li>陽線，實體 &gt; 近 {AVG_LEN} 根平均實體 × {BODY_MULT}</li>
<li>成交量 &gt; 近 {AVG_LEN} 根均量 × {VOL_MULT}</li>
<li>上影線 &lt; 實體 × {UPPER_WICK_RATIO}</li>
<li>RSI({RSI_LEN}) &lt; {RSI_MAX}</li></ul>
<p>進度條：左端是 0.5 鐵底，金色線是 0.7 進場區上緣，紅色代表已在獵殺區內。</p></details>
<p class="foot">資料來源 Yahoo Finance（除權息不還原）。僅供參考，不構成投資建議。</p>
</div><script>{PAGE_JS}</script></body></html>"""


def save_and_render(res):
    os.makedirs(DOCS, exist_ok=True)
    with open(os.path.join(DOCS, f"{res['key']}.json"), "w", encoding="utf-8") as f:
        json.dump(res, f, ensure_ascii=False)
    markets = {}
    for k in ("tw", "us"):
        p = os.path.join(DOCS, f"{k}.json")
        if os.path.exists(p):
            with open(p, encoding="utf-8") as f:
                markets[k] = json.load(f)
    with open(os.path.join(DOCS, "index.html"), "w", encoding="utf-8") as f:
        f.write(render_page(markets))


# ---------------------------------------------------------------- Telegram（選用）
def build_message(res, require_today):
    today = str(dt.datetime.now(ZoneInfo(MARKETS[res["key"]]["tz"])).date())
    hits = [r for r in res["rows"] if r["a8_today"] and (not require_today or r["date"] == today)]
    if not hits:
        return ""
    lines = [f"🟡 <b>A8 Hunter 訊號｜{res['label']}｜{hits[0]['date']}</b>"]
    for r in hits:
        floor = "⚠️跌破0.5鐵底 " if r["close"] < r["zone2"] else ""
        lines.append(f"<b>{r['sym']} {r['name']}</b>\n"
                     f"  收 {r['close']:,.2f}｜進場區上緣 {r['zone1']:,.2f}（低 {(1 - r['ratio']) * 100:.1f}%）\n"
                     f"  {floor}RSI {r['rsi']}｜實體 {r['body_x']}x｜量 {r['vol_x']}x")
    url = os.environ.get("PAGE_URL")
    if url:
        lines.append(f"\n{url}")
    return "\n".join(lines)


def send_telegram(text):
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat:
        return
    for i in range(0, len(text), 3800):
        r = requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
                          data={"chat_id": chat, "text": text[i:i + 3800],
                                "parse_mode": "HTML", "disable_web_page_preview": True},
                          timeout=30)
        r.raise_for_status()


def history(ticker):
    frames = download([ticker])
    if ticker not in frames:
        sys.exit(f"抓不到 {ticker} 的資料（或資料少於 {CT_LEN} 根）")
    df = compute_a8(frames[ticker])
    hits = df[df["a8"]]
    print(f"{ticker} 共 {len(df)} 根日K，A8 訊號 {len(hits)} 次：")
    for idx, r in hits.iterrows():
        print(f"  {pd.Timestamp(idx).date()}  收 {r['Close']:.2f}  上緣 {r['zone1']:.2f}  "
              f"RSI {r['rsi']:.1f}  實體 {r['body_x']:.1f}x  量 {r['vol_x']:.1f}x")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("market", nargs="?", default="all", choices=["tw", "us", "all"])
    ap.add_argument("--require-today", action="store_true")
    ap.add_argument("--no-telegram", action="store_true")
    ap.add_argument("--history")
    a = ap.parse_args()

    if a.history:
        history(a.history)
        return

    keys = ["tw", "us"] if a.market == "all" else [a.market]
    failed = False
    for k in keys:
        try:
            res = scan_market(k)
            if not res["rows"]:
                raise RuntimeError("所有股票都抓不到資料")
            save_and_render(res)
            hits = sum(r["a8_today"] for r in res["rows"])
            print(f"[{k}] {len(res['rows'])}/{res['total']} 檔，今日 A8 {hits} 檔")
            msg = build_message(res, a.require_today)
            if msg and not a.no_telegram:
                send_telegram(msg)
        except Exception as e:  # noqa: BLE001
            failed = True
            print(f"[{k}] 掃描失敗：{e}", file=sys.stderr)
    if failed:
        sys.exit(1)


if __name__ == "__main__":
    main()
