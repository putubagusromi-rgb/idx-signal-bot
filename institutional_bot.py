"""
Institutional Grade Telegram Signal Bot — IDX Swing.

One-shot scanner designed for GitHub Actions (no VPS, no always-on process):
run it, it screens the universe, posts to Telegram, and exits.

Pipeline (cheapest gates first so Index Alpha quota is only spent on survivors):
  1. Macro gate      — IHSG (^JKSE) vs EMA50 via yfinance.
  2. Execution gate  — ADTV5 > Rp10B, RVOL >= 2x, price > EMA20 & EMA50,
                       ATR14 expanding (yfinance daily bars).
  3. Money-flow gate — Index Alpha: net foreign buy >= 3 consecutive days
                       (within last 5) and Top-3 buyer brokers >= 50% volume.
  4. Risk model      — SL = entry - 1.5*ATR14, TP1 1:1.5, TP2 1:3,
                       lot sizing at 2% portfolio risk.

Env: TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, INDEX_ALPHA_API_KEY,
     PORTFOLIO_CAPITAL (default 100000000). Optional tuning vars in Config.

Usage:
  python institutional_bot.py            # scan and send to Telegram
  python institutional_bot.py --dry-run  # scan and print, don't send
"""

import argparse
import html
import logging
import math
import os
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import pandas as pd
import requests
import yfinance as yf

from scanner import IDX80_EXTRA, LQ45, SMALL_CAPS

logger = logging.getLogger("institutional_bot")

WIB = timezone(timedelta(hours=7))
INDEX_ALPHA_URL = "https://api.indexalpha.id"
INDEX_ALPHA_BATCH_MAX = 50
# Index Alpha publishes day D at 19:00 WIB, so intraday runs only see D-1.
INDEX_ALPHA_PUBLISH_HOUR_WIB = 19
TELEGRAM_MAX_LEN = 4096
IHSG_SYMBOL = "^JKSE"


# ── Config ────────────────────────────────────────────────────────────────────

def _env_float(name, default):
    raw = (os.getenv(name) or "").strip()
    return float(raw) if raw else float(default)


def _env_int(name, default):
    return int(_env_float(name, default))


@dataclass(frozen=True)
class Config:
    telegram_token: str
    telegram_chat_id: str
    index_alpha_key: str
    capital: float
    risk_pct: float
    adtv_min: float
    rvol_min: float
    atr_period: int
    atr_sl_mult: float
    tp1_rr: float
    tp2_rr: float
    ff_lookback_days: int
    ff_min_streak: int
    broker_top_n: int
    broker_min_share: float
    max_signals: int
    bear_mode: str          # "hold" = suppress signals, "warn" = send with warning
    send_empty_report: bool
    universe: tuple

    @classmethod
    def from_env(cls):
        custom = [t.strip().upper() for t in (os.getenv("UNIVERSE") or "").split(",") if t.strip()]
        universe = custom or list(dict.fromkeys(LQ45 + IDX80_EXTRA + SMALL_CAPS))
        bear_mode = (os.getenv("BEAR_MODE") or "hold").strip().lower()
        if bear_mode not in ("hold", "warn"):
            raise ValueError("BEAR_MODE must be 'hold' or 'warn'")
        return cls(
            telegram_token=(os.getenv("TELEGRAM_BOT_TOKEN") or "").strip(),
            telegram_chat_id=(os.getenv("TELEGRAM_CHAT_ID") or "").strip(),
            index_alpha_key=(os.getenv("INDEX_ALPHA_API_KEY") or "").strip(),
            capital=_env_float("PORTFOLIO_CAPITAL", 100_000_000),
            risk_pct=_env_float("RISK_PER_TRADE_PCT", 2.0) / 100,
            adtv_min=_env_float("ADTV_MIN_RP", 10_000_000_000),
            rvol_min=_env_float("RVOL_MIN", 2.0),
            atr_period=_env_int("ATR_PERIOD", 14),
            atr_sl_mult=_env_float("ATR_SL_MULT", 1.5),
            tp1_rr=_env_float("TP1_RR", 1.5),
            tp2_rr=_env_float("TP2_RR", 3.0),
            ff_lookback_days=_env_int("FOREIGN_LOOKBACK_DAYS", 5),
            ff_min_streak=_env_int("FOREIGN_MIN_STREAK", 3),
            broker_top_n=_env_int("BROKER_TOP_N", 3),
            broker_min_share=_env_float("BROKER_MIN_SHARE_PCT", 50) / 100,
            max_signals=_env_int("MAX_SIGNALS", 5),
            bear_mode=bear_mode,
            send_empty_report=(os.getenv("SEND_EMPTY_REPORT") or "true").strip().lower() != "false",
            universe=tuple(universe),
        )


# ── Price helpers ─────────────────────────────────────────────────────────────

def idx_tick(price):
    """IDX equity tick size (fraksi harga)."""
    if price < 200:
        return 1
    if price < 500:
        return 2
    if price < 2000:
        return 5
    if price < 5000:
        return 10
    return 25


def round_tick(price, mode="nearest"):
    tick = idx_tick(price)
    fn = {"down": math.floor, "up": math.ceil}.get(mode, round)
    return int(fn(price / tick) * tick)


def rp(value):
    """Rupiah with Indonesian thousands separator: 1234567 -> 1.234.567"""
    return f"{value:,.0f}".replace(",", ".")


def rp_short(value):
    sign = "-" if value < 0 else ""
    v = abs(value)
    if v >= 1e12:
        return f"{sign}Rp {v / 1e12:.2f} T"
    if v >= 1e9:
        return f"{sign}Rp {v / 1e9:.2f} M"
    if v >= 1e6:
        return f"{sign}Rp {v / 1e6:.1f} Jt"
    return f"{sign}Rp {rp(v)}"


# ── Indicators ────────────────────────────────────────────────────────────────

def ema(series, span):
    return series.ewm(span=span, adjust=False).mean()


def atr(df, period=14):
    prev_close = df["Close"].shift(1)
    tr = pd.concat([
        df["High"] - df["Low"],
        (df["High"] - prev_close).abs(),
        (df["Low"] - prev_close).abs(),
    ], axis=1).max(axis=1)
    return tr.ewm(alpha=1 / period, adjust=False).mean()  # Wilder smoothing


# ── Market data (yfinance) ────────────────────────────────────────────────────

def download_history(symbols, period="9mo", chunk=40, retries=3):
    """Return {symbol: OHLCV DataFrame} for symbols that have usable data."""
    out = {}
    for i in range(0, len(symbols), chunk):
        batch = symbols[i:i + chunk]
        data = None
        for attempt in range(retries):
            try:
                data = yf.download(
                    batch, period=period, interval="1d", group_by="ticker",
                    auto_adjust=False, progress=False, threads=True,
                )
                break
            except Exception as e:  # network / rate limit
                logger.warning("yfinance batch failed (attempt %d): %s", attempt + 1, e)
                time.sleep(3 * (attempt + 1))
        if data is None or data.empty:
            continue
        for sym in batch:
            try:
                df = data[sym] if isinstance(data.columns, pd.MultiIndex) else data
            except KeyError:
                continue
            # Multi-ticker downloads align dates, leaving NaN rows per symbol.
            df = df.dropna(subset=["Open", "High", "Low", "Close"])
            if not df.empty:
                out[sym] = df
    return out


def fetch_sector(symbol):
    try:
        return yf.Ticker(symbol).info.get("sector") or "N/A"
    except Exception:
        return "N/A"


# ── Gate 1: Macro regime ──────────────────────────────────────────────────────

def market_regime(ihsg):
    close = ihsg["Close"]
    e20, e50 = ema(close, 20), ema(close, 50)
    last, last_e20, last_e50 = float(close.iloc[-1]), float(e20.iloc[-1]), float(e50.iloc[-1])
    chg = (last / float(close.iloc[-2]) - 1) * 100 if len(close) > 1 else 0.0
    if last < last_e50:
        trend, bullish = "🔴 DOWNTREND (di bawah EMA50) — HIGH-RISK MODE", False
    elif last_e20 > last_e50:
        trend, bullish = "🟢 UPTREND (di atas EMA20 & EMA50)", True
    else:
        trend, bullish = "🟡 SIDEWAYS (di atas EMA50)", True
    status = f"{trend} | IHSG {last:,.2f} ({chg:+.2f}%) vs EMA50 {last_e50:,.2f}"
    return {"bullish": bullish, "status": status, "close": last, "ema50": last_e50}


# ── Gate 3: Technical & volatility ────────────────────────────────────────────

def technical_gate(df, cfg):
    """Return metrics dict if the stock passes every execution filter, else None."""
    if len(df) < 60:
        return None
    close, volume = df["Close"], df["Volume"]
    last = float(close.iloc[-1])

    adtv = float((close * volume).iloc[-5:].mean())
    if adtv < cfg.adtv_min:
        return None

    avg_vol20 = float(volume.iloc[-21:-1].mean())
    if avg_vol20 <= 0:
        return None
    rvol = float(volume.iloc[-1]) / avg_vol20
    if rvol < cfg.rvol_min:
        return None

    e20, e50 = float(ema(close, 20).iloc[-1]), float(ema(close, 50).iloc[-1])
    if not (last > e20 and last > e50):
        return None

    a = atr(df, cfg.atr_period)
    atr_now, atr_prev5 = float(a.iloc[-1]), float(a.iloc[-6])
    atr_base = float(a.iloc[-21:-1].mean())
    if not (atr_now > atr_prev5 and atr_now > atr_base):
        return None

    return {
        "price": last, "adtv": adtv, "rvol": rvol, "ema20": e20, "ema50": e50,
        "atr": atr_now, "atr_prev5": atr_prev5, "bar_date": df.index[-1].date(),
    }


# ── Gate 2: Smart money (Index Alpha) ─────────────────────────────────────────

class IndexAlphaError(RuntimeError):
    pass


class IndexAlphaClient:
    def __init__(self, api_key, base_url=INDEX_ALPHA_URL, timeout=30):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers.update({
            "Authorization": f"Bearer {api_key}",
            "Accept": "application/json",
        })

    def _post(self, path, body, retries=4):
        url = f"{self.base_url}{path}"
        for attempt in range(retries):
            try:
                r = self.session.post(url, json=body, timeout=self.timeout)
            except requests.RequestException as e:
                logger.warning("Index Alpha %s network error: %s", path, e)
                time.sleep(2 ** attempt)
                continue
            if r.status_code == 429 or r.status_code >= 500:
                wait = float(r.headers.get("Retry-After") or 2 ** (attempt + 1))
                logger.warning("Index Alpha %s -> %d, retrying in %.0fs", path, r.status_code, wait)
                time.sleep(wait)
                continue
            if r.status_code != 200:
                raise IndexAlphaError(f"{path} -> HTTP {r.status_code}: {r.text[:300]}")
            payload = r.json()
            if not payload.get("success", False):
                raise IndexAlphaError(f"{path} -> {payload.get('error')}")
            return payload.get("data") or {}
        raise IndexAlphaError(f"{path} failed after {retries} attempts")

    def _batched(self, path, tickers, extra):
        merged = {}
        for i in range(0, len(tickers), INDEX_ALPHA_BATCH_MAX):
            body = {"tickers": tickers[i:i + INDEX_ALPHA_BATCH_MAX], **extra}
            merged.update(self._post(path, body))
        return merged

    def foreign_flow(self, tickers, day):
        d = day.isoformat()
        return self._batched("/foreign-flow/batch", tickers, {"from": d, "to": d, "market": "RG"})

    def broker_summary(self, tickers, start, end):
        return self._batched("/stocks/broker-summary/batch", tickers, {
            "from": start.isoformat(), "to": end.isoformat(), "investor": "all", "market": "RG",
        })


def flow_trading_days(ihsg_index, now_wib, lookback):
    """Last `lookback` IDX trading days whose Index Alpha data is already published."""
    days = sorted({ts.date() for ts in ihsg_index})
    if days and days[-1] >= now_wib.date() and now_wib.hour < INDEX_ALPHA_PUBLISH_HOUR_WIB:
        days = [d for d in days if d < now_wib.date()]
    return days[-lookback:]


def foreign_streak(daily_net):
    """Consecutive net-buy days counted back from the most recent day."""
    streak = 0
    for net in reversed(daily_net):
        if net is None or net <= 0:
            break
        streak += 1
    return streak


def broker_concentration(rows, top_n):
    rows = [r for r in rows or [] if (r.get("buy_volume") or 0) > 0]
    total = sum(r["buy_volume"] for r in rows)
    if total <= 0:
        return 0.0, []
    top = sorted(rows, key=lambda r: r["buy_volume"], reverse=True)[:top_n]
    return sum(r["buy_volume"] for r in top) / total, [r["code"] for r in top]


def smart_money_gate(client, tickers, days, cfg):
    """Return {ticker: flow metrics} for tickers passing foreign-flow and broker filters."""
    if not tickers or not days:
        return {}

    net_by_day = {t: [] for t in tickers}
    for d in days:
        data = client.foreign_flow(tickers, d)
        for t in tickers:
            row = data.get(t)
            net_by_day[t].append(None if not row else row.get("net_foreign"))

    streak_ok = {}
    for t, nets in net_by_day.items():
        s = foreign_streak(nets)
        if s >= cfg.ff_min_streak:
            streak_ok[t] = (s, sum(nets[-s:]))
    if not streak_ok:
        return {}

    summaries = client.broker_summary(list(streak_ok), days[0], days[-1])
    passed = {}
    for t, (streak, net_sum) in streak_ok.items():
        share, top_codes = broker_concentration(summaries.get(t), cfg.broker_top_n)
        if share >= cfg.broker_min_share:
            passed[t] = {
                "ff_streak": streak, "ff_net": net_sum,
                "broker_share": share, "broker_top": top_codes,
            }
    return passed


# ── Gate 4: Risk model ────────────────────────────────────────────────────────

def risk_plan(tech, cfg):
    entry = tech["price"]
    buy_high = round_tick(entry, "nearest")
    buy_low = round_tick(max(tech["ema20"], entry - 0.5 * tech["atr"]), "down")
    sl = round_tick(entry - cfg.atr_sl_mult * tech["atr"], "down")
    risk_per_share = entry - sl
    if risk_per_share <= 0 or sl <= 0:
        return None
    tp1 = round_tick(entry + cfg.tp1_rr * risk_per_share, "up")
    tp2 = round_tick(entry + cfg.tp2_rr * risk_per_share, "up")

    max_risk_rp = cfg.capital * cfg.risk_pct
    lots_by_risk = math.floor(max_risk_rp / (risk_per_share * 100))
    lots_by_cash = math.floor(cfg.capital / (buy_high * 100))
    lots = max(0, min(lots_by_risk, lots_by_cash))
    return {
        "buy_low": min(buy_low, buy_high), "buy_high": buy_high,
        "sl": sl, "sl_pct": (sl / entry - 1) * 100, "tp1": tp1, "tp2": tp2,
        "lots": lots, "nominal": lots * 100 * buy_high, "max_risk_rp": max_risk_rp,
    }


# ── Telegram formatting ───────────────────────────────────────────────────────

_HARI = ["Senin", "Selasa", "Rabu", "Kamis", "Jumat", "Sabtu", "Minggu"]
_BULAN = ["Jan", "Feb", "Mar", "Apr", "Mei", "Jun", "Jul", "Agu", "Sep", "Okt", "Nov", "Des"]


def fmt_date(dt):
    return f"{_HARI[dt.weekday()]}, {dt.day} {_BULAN[dt.month - 1]} {dt.year} {dt:%H:%M} WIB"


def format_signal(sig, regime, cfg, now_wib):
    e = html.escape
    t, flow, plan = sig["ticker"], sig["flow"], sig["plan"]
    lines = [
        "🏛️ <b>INSTITUTIONAL SIGNAL | IDX SWING</b> 🏛️",
        "",
        f"📌 <b>Ticker:</b> <code>${e(t)}</code> | <b>Sector:</b> {e(sig['sector'])}",
        f"💰 <b>Current Price:</b> Rp {rp(sig['tech']['price'])}",
        "",
        "🌐 <b>MACRO &amp; MARKET REGIME:</b>",
        f"• IHSG Status: {e(regime['status'])}",
        "",
        "📊 <b>SMART MONEY &amp; ORDER FLOW INSIGHT:</b>",
        f"• <b>Foreign Flow:</b> Net Buy {flow['ff_streak']} hari berturut-turut "
        f"(Σ {rp_short(flow['ff_net'])})",
        f"• <b>Broker Concentration:</b> Top {len(flow['broker_top'])} Buyer "
        f"({e(', '.join(flow['broker_top']))}) = {flow['broker_share'] * 100:.1f}% volume "
        f"— Big Accumulation",
        f"• <b>RVOL (Volume Spike):</b> {sig['tech']['rvol']:.2f}x (High Liquidity, "
        f"ADTV5 {rp_short(sig['tech']['adtv'])})",
        "",
        "🎯 <b>EXECUTION &amp; RISK SETUP:</b>",
        f"• <b>Buy Area:</b> Rp {rp(plan['buy_low'])} - Rp {rp(plan['buy_high'])}",
        f"• <b>Stop Loss ({cfg.atr_sl_mult:g}x ATR):</b> Rp {rp(plan['sl'])} ({plan['sl_pct']:.2f}%)",
        f"• <b>Target Profit 1 (1:{cfg.tp1_rr:g}):</b> Rp {rp(plan['tp1'])}",
        f"• <b>Target Profit 2 (1:{cfg.tp2_rr:g}):</b> Rp {rp(plan['tp2'])}",
        "",
        "⚖️ <b>PORTFOLIO RISK &amp; POSITION SIZING:</b>",
        f"• <b>Max Risk per Trade:</b> {cfg.risk_pct * 100:g}% Modal "
        f"(Rp {rp(plan['max_risk_rp'])} dari Rp {rp(cfg.capital)})",
        f"• <b>Recommended Max Position:</b> {rp(plan['lots'])} Lot (~Rp {rp(plan['nominal'])})",
        "",
        f"🗓️ <b>Date:</b> {fmt_date(now_wib)}",
        "⚠️ <i>Institutional Disclaimer: Signal ini di-generate oleh Quantitative Model berbasis "
        "Smart Money Flow, ATR Volatility, dan Liquidity Threshold. Manage your risk accordingly.</i>",
    ]
    if not regime["bullish"]:
        lines[:0] = ["⚠️ <b>HIGH-RISK MODE: IHSG di bawah EMA50 — kurangi ukuran posisi.</b>", ""]
    return "\n".join(lines)


def format_summary(regime, stats, now_wib, note=None):
    e = html.escape
    lines = [
        "🏛️ <b>INSTITUTIONAL SCAN REPORT | IDX</b>",
        "",
        f"🌐 IHSG Status: {e(regime['status'])}",
        f"🔎 Universe: {stats['universe']} | Data OK: {stats['with_data']}",
        f"📈 Lolos teknikal &amp; likuiditas: {stats['technical']}",
        f"🏦 Lolos smart money flow: {stats['flow']}",
        f"🎯 Signal dikirim: {stats['signals']}",
    ]
    if stats.get("flow_days"):
        lines.append(f"🗂️ Data flow: {stats['flow_days'][0]:%d/%m} – {stats['flow_days'][-1]:%d/%m}")
    if note:
        lines += ["", e(note)]
    lines += ["", f"🗓️ {fmt_date(now_wib)}"]
    return "\n".join(lines)


def send_telegram(cfg, text):
    chat_id, _, topic = cfg.telegram_chat_id.partition(":")  # supports "-100123:topic"
    body = {
        "chat_id": chat_id, "text": text[:TELEGRAM_MAX_LEN],
        "parse_mode": "HTML", "disable_web_page_preview": True,
    }
    if topic:
        body["message_thread_id"] = int(topic)
    url = f"https://api.telegram.org/bot{cfg.telegram_token}/sendMessage"
    for _ in range(3):
        r = requests.post(url, json=body, timeout=30)
        if r.status_code == 429:
            time.sleep(r.json().get("parameters", {}).get("retry_after", 3))
            continue
        if r.ok:
            return
        raise RuntimeError(f"Telegram sendMessage failed: HTTP {r.status_code} {r.text[:300]}")
    raise RuntimeError("Telegram sendMessage rate-limited repeatedly")


# ── Orchestration ─────────────────────────────────────────────────────────────

def run(cfg, now_wib, client, is_scheduled=False):
    """Run the full pipeline. Returns list of Telegram messages (HTML)."""
    symbols = [f"{t}.JK" for t in cfg.universe]
    history = download_history([IHSG_SYMBOL] + symbols)
    ihsg = history.get(IHSG_SYMBOL)
    if ihsg is None or len(ihsg) < 60:
        raise RuntimeError("IHSG data unavailable from yfinance — aborting")

    if is_scheduled and ihsg.index[-1].date() != now_wib.date():
        logger.info("No IHSG bar for %s — IDX holiday, skipping run", now_wib.date())
        return []

    regime = market_regime(ihsg)
    logger.info("Regime: %s", regime["status"])
    stats = {"universe": len(cfg.universe), "with_data": len(history) - 1,
             "technical": 0, "flow": 0, "signals": 0}

    if not regime["bullish"] and cfg.bear_mode == "hold":
        note = ("Signal DITAHAN: IHSG di bawah EMA50 (downtrend). "
                "Set BEAR_MODE=warn untuk tetap menerima signal dengan label high-risk.")
        return [format_summary(regime, stats, now_wib, note)]

    technical = {}
    for t in cfg.universe:
        df = history.get(f"{t}.JK")
        if df is None:
            continue
        m = technical_gate(df, cfg)
        if m:
            technical[t] = m
    stats["technical"] = len(technical)
    logger.info("Technical survivors (%d): %s", len(technical), ", ".join(technical))

    days = flow_trading_days(ihsg.index, now_wib, cfg.ff_lookback_days)
    stats["flow_days"] = days
    flows = smart_money_gate(client, list(technical), days, cfg)
    stats["flow"] = len(flows)
    logger.info("Smart-money survivors (%d): %s", len(flows), ", ".join(flows))

    signals = []
    for t, flow in flows.items():
        plan = risk_plan(technical[t], cfg)
        if plan is None or plan["lots"] < 1:
            logger.info("%s skipped: position size < 1 lot", t)
            continue
        signals.append({"ticker": t, "tech": technical[t], "flow": flow, "plan": plan})
    signals.sort(key=lambda s: s["tech"]["rvol"], reverse=True)
    signals = signals[:cfg.max_signals]
    stats["signals"] = len(signals)

    messages = []
    for sig in signals:
        sig["sector"] = fetch_sector(f"{sig['ticker']}.JK")
        messages.append(format_signal(sig, regime, cfg, now_wib))
    if signals or cfg.send_empty_report:
        note = None if signals else "Tidak ada saham yang lolos seluruh filter institusional hari ini."
        messages.insert(0, format_summary(regime, stats, now_wib, note))
    return messages


def main(argv=None):
    parser = argparse.ArgumentParser(description="Institutional Grade Telegram Signal Bot — IDX")
    parser.add_argument("--dry-run", action="store_true", help="print messages instead of sending")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    cfg = Config.from_env()
    required = [("INDEX_ALPHA_API_KEY", cfg.index_alpha_key)]
    if not args.dry_run:
        required += [("TELEGRAM_BOT_TOKEN", cfg.telegram_token),
                     ("TELEGRAM_CHAT_ID", cfg.telegram_chat_id)]
    missing = [name for name, value in required if not value]
    if missing:
        logger.error("Missing required env vars: %s", ", ".join(missing))
        return 1

    now_wib = datetime.now(WIB)
    is_scheduled = os.getenv("GITHUB_EVENT_NAME") == "schedule"
    client = IndexAlphaClient(cfg.index_alpha_key)
    try:
        messages = run(cfg, now_wib, client, is_scheduled)
    except IndexAlphaError as e:
        logger.error("Index Alpha error: %s", e)
        if not args.dry_run:
            send_telegram(cfg, f"⚠️ <b>Institutional bot error</b>\nIndex Alpha: {html.escape(str(e))}")
        return 1

    for msg in messages:
        if args.dry_run:
            print(msg, end="\n\n" + "─" * 40 + "\n\n")
        else:
            send_telegram(cfg, msg)
            time.sleep(1)
    logger.info("Done: %d message(s) %s", len(messages), "printed" if args.dry_run else "sent")
    return 0


if __name__ == "__main__":
    sys.exit(main())
