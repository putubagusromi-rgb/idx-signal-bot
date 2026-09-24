"""
IDX Morning Briefing + BSJP/BPJS signals — free, yfinance only, for GitHub Actions.

Modes:
  morning  (~08:00 WIB, before the open)
    • IHSG executive summary (bias, support/resistance, breadth)
    • BSJP exit plan for yesterday's afternoon picks
    • BSJP scorecard: last cycle + cumulative hit rate
    • Smart money tracker (early accumulation vs VWAP, absorption days)
    • BPJS picks (buy this morning, sell this afternoon)
  bsjp     (~15:00 WIB, before the close)
    • BSJP picks (buy near the close, sell at tomorrow's open)

Stateless: the scorecard re-runs the BSJP screen on each past day's closing
data (no lookahead) and grades it against the next day's bar, so nothing has
to be stored between runs. Live afternoon picks use the intraday bar, so they
can differ slightly from the replayed ones.

Money flow here is estimated from price/volume (CMF, OBV, signed value flow,
VWAP). It is not broker or foreign-flow data.

Usage:
  python briefing_bot.py morning [--dry-run]
  python briefing_bot.py bsjp [--dry-run]
"""

import argparse
import logging
import os
import sys
import time
from dataclasses import dataclass
from datetime import datetime

import numpy as np
import pandas as pd

import institutional_bot as ib
from scanner import IDX_HOLIDAYS_2026

logger = logging.getLogger("briefing_bot")

SEP = "──────────────────────────"
_HARI = ["Senin", "Selasa", "Rabu", "Kamis", "Jumat", "Sabtu", "Minggu"]
_BULAN = ["Januari", "Februari", "Maret", "April", "Mei", "Juni", "Juli",
          "Agustus", "September", "Oktober", "November", "Desember"]
DISCLAIMER = ("⚠️ <b>DISCLAIMER:</b> Algoritma ini dirancang menggunakan analisis kuantitatif "
              "Market Microstructure. Money flow diestimasi dari harga &amp; volume (bukan data "
              "broker/asing). Keputusan transaksi tetap sepenuhnya menjadi tanggung jawab "
              "independen pengelola dana/trader.")


# ── Config ────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class BriefConfig:
    telegram_token: str
    telegram_chat_id: str
    universe: tuple
    min_value_rp: float       # min value traded on the signal day
    bsjp_max: int
    bpjs_max: int
    tracker_max: int
    scorecard_days: int
    bsjp_min_chg: float
    bsjp_max_chg: float
    bsjp_tp_atr: float
    bsjp_sl_atr: float
    bpjs_tp_atr: float
    bpjs_sl_atr: float

    @classmethod
    def from_env(cls):
        base = ib.Config.from_env()
        f, i = ib._env_float, ib._env_int
        return cls(
            telegram_token=base.telegram_token,
            telegram_chat_id=base.telegram_chat_id,
            universe=base.universe,
            min_value_rp=f("BRIEF_MIN_VALUE_RP", 5_000_000_000),
            bsjp_max=i("BSJP_MAX", 3),
            bpjs_max=i("BPJS_MAX", 3),
            tracker_max=i("TRACKER_MAX", 5),
            scorecard_days=i("SCORECARD_DAYS", 8),
            bsjp_min_chg=f("BSJP_MIN_CHG_PCT", 1.0) / 100,
            bsjp_max_chg=f("BSJP_MAX_CHG_PCT", 7.0) / 100,
            bsjp_tp_atr=f("BSJP_TP_ATR", 0.5),
            bsjp_sl_atr=f("BSJP_SL_ATR", 0.35),
            bpjs_tp_atr=f("BPJS_TP_ATR", 0.6),
            bpjs_sl_atr=f("BPJS_SL_ATR", 0.4),
        )


# ── Formatting (Indonesian number style) ──────────────────────────────────────

def num(x, dec=0):
    s = f"{x:,.{dec}f}"
    return s.replace(",", "_").replace(".", ",").replace("_", ".")


def pct(x, dec=1):
    """Fraction -> '+1,2%'."""
    return ("+" if x >= 0 else "") + num(x * 100, dec) + "%"


def signed(x, dec=2):
    return ("+" if x >= 0 else "") + num(x, dec)


def long_date(d):
    return f"{_HARI[d.weekday()]}, {d.day} {_BULAN[d.month - 1]} {d.year}"


def mf_short(v):
    sign = "+" if v >= 0 else "-"
    v = abs(v)
    if v >= 1e12:
        return f"{sign}Rp {num(v / 1e12, 2)} T"
    if v >= 1e9:
        return f"{sign}Rp {num(v / 1e9, 1)} M"
    return f"{sign}Rp {num(v / 1e6, 0)} Jt"


def split_messages(text, limit=ib.TELEGRAM_MAX_LEN):
    """Split on blank lines so no Telegram message exceeds the limit."""
    out, cur = [], ""
    for block in text.split("\n\n"):
        cand = f"{cur}\n\n{block}" if cur else block
        if len(cand) <= limit:
            cur = cand
            continue
        if cur:
            out.append(cur)
        cur = block[:limit]
    if cur:
        out.append(cur)
    return out


# ── Features ──────────────────────────────────────────────────────────────────

def features(df):
    """Per-bar indicators for one ticker, indexed by normalized date."""
    df = df.copy()
    df.index = pd.DatetimeIndex(df.index).normalize()
    c, h, lo, v = df["Close"], df["High"], df["Low"], df["Volume"]
    rng = (h - lo)
    value = c * v
    chg = c.pct_change()
    vol_ratio = v / v.shift(1).rolling(20).mean()
    close_pos = ((c - lo) / rng.replace(0, np.nan)).fillna(0.5)
    ob = ib.obv(df)
    typical = (h + lo + c) / 3
    absorb = (vol_ratio >= 1.5) & (close_pos >= 0.5) & (chg.abs() <= 0.01)
    return pd.DataFrame({
        "open": df["Open"], "high": h, "low": lo, "close": c, "volume": v,
        "chg": chg, "value": value, "vol_ratio": vol_ratio, "close_pos": close_pos,
        "ema20": ib.ema(c, 20), "ema50": ib.ema(c, 50), "atr": ib.atr(df, 14),
        "cmf": ib.cmf(df, 20), "obv5": ob - ob.shift(5), "obv10": ob - ob.shift(10),
        "mf5": (np.sign(chg.fillna(0)) * value).rolling(5).sum(),
        "vwap10": (typical * v).rolling(10).sum() / v.rolling(10).sum(),
        "absorb10": absorb.astype(int).rolling(10).sum(),
    }).iloc[50:]  # drop EMA50 warm-up


def trading_days(feats, min_share=0.5):
    """Dates with bars for at least `min_share` of tickers (Yahoo sometimes skips IHSG days)."""
    counts = pd.Series([d for f in feats.values() for d in f.index]).value_counts()
    need = max(1.0, len(feats) * min_share)
    return sorted(d for d, n in counts.items() if n >= need)


def row_at(feat, day):
    try:
        return feat.loc[day]
    except KeyError:
        return None


def confidence(bonus):
    return "HIGH" if bonus >= 4 else "MEDIUM" if bonus >= 2 else "LOW"


# ── Screens ───────────────────────────────────────────────────────────────────

def _plan(entry, atr_v, tp_mult, sl_mult):
    tick = ib.idx_tick(entry)
    tp = ib.round_tick(entry + max(tp_mult * atr_v, 2 * tick), "up")
    sl = ib.round_tick(entry - max(sl_mult * atr_v, 2 * tick), "down")
    return tp, sl


def bsjp_screen(feats, day, cfg):
    """Strong close into the bell on heavy volume and positive flow."""
    picks = []
    for t, f in feats.items():
        r = row_at(f, day)
        if r is None or r.isna()[["chg", "vol_ratio", "atr", "cmf"]].any():
            continue
        if not (r.value >= cfg.min_value_rp and cfg.bsjp_min_chg <= r.chg <= cfg.bsjp_max_chg
                and r.close_pos >= 0.7 and r.vol_ratio >= 1.5
                and r.close > r.ema20 and r.cmf > 0):
            continue
        bonus = sum([r.vol_ratio >= 2.5, r.close_pos >= 0.9, r.cmf >= 0.15,
                     r.mf5 > 0, r.close > r.ema50])
        entry = ib.round_tick(r.close)
        tp, sl = _plan(entry, r.atr, cfg.bsjp_tp_atr, cfg.bsjp_sl_atr)
        picks.append({"ticker": t, "day": day, "conf": confidence(bonus), "bonus": bonus,
                      "entry": entry, "tp": tp, "sl": sl, "row": r})
    picks.sort(key=lambda p: (p["bonus"], p["row"].vol_ratio), reverse=True)
    return picks[:cfg.bsjp_max]


def bpjs_screen(feats, day, cfg):
    """Intraday continuation candidates for today, based on yesterday's bar."""
    picks = []
    for t, f in feats.items():
        r = row_at(f, day)
        if r is None or r.isna()[["chg", "vol_ratio", "atr", "cmf"]].any():
            continue
        atr_pct = r.atr / r.close
        if not (r.value >= cfg.min_value_rp and r.close > r.ema20 > r.ema50
                and r.close_pos >= 0.6 and r.vol_ratio >= 1.2 and r.cmf > 0.05
                and atr_pct >= 0.02 and -0.01 <= r.chg <= 0.08):
            continue
        bonus = sum([r.mf5 > 0, r.obv5 > 0, r.vol_ratio >= 2, r.close_pos >= 0.85, r.chg > 0])
        ref = ib.round_tick(r.close)
        buy_low = ib.round_tick(r.close - 0.2 * r.atr, "down")
        tp, sl = _plan(ref, r.atr, cfg.bpjs_tp_atr, cfg.bpjs_sl_atr)
        picks.append({"ticker": t, "conf": confidence(bonus), "bonus": bonus, "ref": ref,
                      "buy_low": buy_low, "tp": tp, "sl": sl, "row": r})
    picks.sort(key=lambda p: (p["bonus"], p["row"].cmf), reverse=True)
    return picks[:cfg.bpjs_max]


def smart_money_tracker(feats, day, cfg):
    """Early accumulation: positive flow and absorption while price is still near/below VWAP10."""
    out = []
    for t, f in feats.items():
        r = row_at(f, day)
        if r is None or pd.isna(r.vwap10) or pd.isna(r.cmf):
            continue
        value5 = f.loc[:day, "value"].iloc[-5:].mean()
        dev = r.close / r.vwap10 - 1
        if (value5 >= cfg.min_value_rp and r.cmf > 0.05 and r.obv10 > 0
                and -0.05 <= dev <= 0.01 and r.absorb10 >= 1):
            out.append({"ticker": t, "vwap": r.vwap10, "last": r.close, "dev": dev,
                        "absorb": int(r.absorb10), "cmf": r.cmf})
    out.sort(key=lambda x: x["cmf"], reverse=True)
    return out[:cfg.tracker_max]


def evaluate(pick, feats, next_day):
    r = row_at(feats[pick["ticker"]], next_day)
    if r is None:
        return None
    e = pick["entry"]
    return {**pick, "next_day": next_day, "o": r.open, "h": r.high, "l": r.low, "c": r.close,
            "gap": r.open / e - 1, "tp_open": r.open >= pick["tp"], "sl_open": r.open <= pick["sl"],
            "tp_hit": r.high >= pick["tp"], "sl_hit": r.low <= pick["sl"]}


def scorecard(feats, days, cfg):
    """Replay BSJP over past days. Returns (last_cycle_results, cumulative_results)."""
    cumulative, last = [], []
    start = max(0, len(days) - 1 - cfg.scorecard_days)
    for i in range(start, len(days) - 1):
        res = [x for x in (evaluate(p, feats, days[i + 1]) for p in bsjp_screen(feats, days[i], cfg)) if x]
        cumulative += res
        if i == len(days) - 2:
            last = res
    return last, cumulative


def market_summary(ihsg, feats, day):
    ih = ihsg.copy()
    ih.index = pd.DatetimeIndex(ih.index).normalize()
    ih = ih.loc[:day]
    c = ih["Close"]
    last, prev = float(c.iloc[-1]), float(c.iloc[-2])
    sma20, sma50 = float(c.rolling(20).mean().iloc[-1]), float(c.rolling(50).mean().iloc[-1])
    if last > sma20 and last > sma50:
        bias = "Bullish Momentum"
    elif last < sma20 and last < sma50:
        bias = "Bearish Pressure"
    elif last < sma20:
        bias = "Pullback dalam Uptrend"
    else:
        bias = "Technical Rebound (di bawah SMA-50)"
    vol = ih["Volume"]
    avg20 = float(vol.iloc[-21:-1].mean())
    vol_ratio = float(vol.iloc[-1]) / avg20 if avg20 > 0 else float("nan")
    up = down = flat = spikes = 0
    for f in feats.values():
        r = row_at(f, day)
        if r is None or pd.isna(r.chg):
            continue
        up += r.chg > 0
        down += r.chg < 0
        flat += r.chg == 0
        spikes += bool(r.vol_ratio >= 1.5)
    return {
        "day": day, "close": last, "chg": last / prev - 1, "sma20": sma20, "sma50": sma50,
        "bias": bias, "vol_ratio": vol_ratio,
        "support": float(ih["Low"].iloc[-10:].min()), "resistance": float(ih["High"].iloc[-20:].max()),
        "up": int(up), "down": int(down), "flat": int(flat), "total": int(up + down + flat),
        "spikes": int(spikes),
    }


# ── Message builders ──────────────────────────────────────────────────────────

def _ihsg_section(m):
    rel = "di atas" if m["close"] > m["sma20"] else "di bawah"
    vol = f" dengan volume {num(m['vol_ratio'], 1)}x Avg-20" if np.isfinite(m["vol_ratio"]) else ""
    return "\n".join([
        "📊 <b>IHSG EXECUTIVE SUMMARY</b>",
        f"• <b>Macro Bias:</b> {m['bias']}",
        f"• <b>Key Level:</b> Support: {num(m['support'])} | Resistance: {num(m['resistance'])}",
        f"• <b>Market Dynamic:</b> IHSG ditutup {pct(m['chg'], 2)} di {num(m['close'], 2)}, "
        f"{rel} SMA-20 ({num(m['sma20'])}){vol}. Breadth universe: {m['up']} naik / "
        f"{m['down']} turun / {m['flat']} flat dari {m['total']} saham; {m['spikes']} saham "
        f"mencatat volume ≥1,5x Avg-20.",
    ])


def _flow_line(r):
    return (f"└ Chg {pct(r.chg)} | Close di {num(r.close_pos * 100)}% range | "
            f"Vol {num(r.vol_ratio, 1)}x | CMF {signed(r.cmf)} | MF 5D {mf_short(r.mf5)}")


def _exit_section(picks, day):
    lines = ["🎯 <b>BSJP EXIT &amp; PROFIT TAKING SCENARIO</b>",
             "<i>(Instruksi Manajemen Posisi untuk Beli-Sore Kemarin)</i>"]
    if not picks:
        lines.append(f"⚪ TIDAK ADA POSISI BSJP yang dibawa dari sesi sore sebelumnya ({day:%Y-%m-%d}). "
                     "Tidak ada instruksi exit.")
        return "\n".join(lines)
    for p in picks:
        lines.append(f"• <code>${p['ticker']}</code> ({p['conf']}) | Entry Rp {num(p['entry'])} | "
                     f"TP Rp {num(p['tp'])} ({pct(p['tp'] / p['entry'] - 1)}) | "
                     f"SL Rp {num(p['sl'])} ({pct(p['sl'] / p['entry'] - 1)})")
    lines.append("└ Pre-opening: antre jual di TP. Open ≥ TP → jual di open. Open di atas entry → "
                 "jual bertahap sebelum 09:30, trailing stop di entry. Open ≤ SL → cut loss di open.")
    return "\n".join(lines)


def _scorecard_section(last, cumulative):
    lines = ["📋 <b>BSJP SCORECARD (Realisasi Siklus Terakhir)</b>"]
    if last:
        lines.append(f"Sinyal sore {last[0]['day']:%Y-%m-%d} → jual pagi {last[0]['next_day']:%Y-%m-%d}")
        for x in last:
            e = x["entry"]
            res = "✅ PROFIT" if x["o"] > e else "❌ LOSS" if x["o"] < e else "➖ FLAT"
            lines.append(
                f"• <code>${x['ticker']}</code> ({x['conf']}) | Entry Rp {num(e)} → Open Rp {num(x['o'])} "
                f"({pct(x['gap'])}) | {res} @Open")
            lines.append(
                f"└ Target Rp {num(x['tp'])} / SL Rp {num(x['sl'])} | High Rp {num(x['h'])} "
                f"({pct(x['h'] / e - 1)}){' ✔ TP' if x['tp_hit'] else ''} | Low Rp {num(x['l'])} "
                f"({pct(x['l'] / e - 1)}){' ✘ SL' if x['sl_hit'] else ''} | Close Rp {num(x['c'])} "
                f"({pct(x['c'] / e - 1)})")
        wins = sum(x["o"] > x["entry"] for x in last)
        lines.append(
            f"• Ringkasan: {wins}/{len(last)} profit @Open ({num(wins / len(last) * 100)}%) | "
            f"Avg gap {pct(np.mean([x['gap'] for x in last]), 2)} | "
            f"Target @Open {sum(x['tp_open'] for x in last)} | SL @Open {sum(x['sl_open'] for x in last)} | "
            f"Target tersentuh {sum(x['tp_hit'] for x in last)} | SL tersentuh {sum(x['sl_hit'] for x in last)}")
    else:
        lines.append("• Tidak ada sinyal BSJP pada siklus terakhir.")
    if cumulative:
        days = sorted({x["day"] for x in cumulative})
        wins = sum(x["o"] > x["entry"] for x in cumulative)
        lines.append(
            f"• Kumulatif ({len(cumulative)} sinyal / {len(days)} hari, {days[0]:%Y-%m-%d} → "
            f"{days[-1]:%Y-%m-%d}): hit rate {num(wins / len(cumulative) * 100)}% | "
            f"avg gap {pct(np.mean([x['gap'] for x in cumulative]), 2)} | "
            f"target @Open {sum(x['tp_open'] for x in cumulative)} | "
            f"SL @Open {sum(x['sl_open'] for x in cumulative)}")
    return "\n".join(lines)


def _tracker_section(items):
    lines = ["🐳 <b>SMART MONEY TRACKER (Paus Holding)</b>"]
    if not items:
        lines.append("• Tidak ada saham dalam fase Early Accumulation.")
    for x in items:
        lines.append(f"• <code>${x['ticker']}</code> — Status: Early Accumulation | Fair Value VWAP: "
                     f"Rp {num(x['vwap'])} (Last Rp {num(x['last'])}, {pct(x['dev'], 2)}) | "
                     f"Absorpsi 10D: {x['absorb']}x | CMF {signed(x['cmf'])}")
    return "\n".join(lines)


def _bpjs_section(picks, bias):
    lines = ["☀️ <b>BPJS WATCHLIST (Beli Pagi Jual Sore — hari ini)</b>"]
    if bias == "Bearish Pressure":
        lines.append("⚠️ IHSG Bearish Pressure: kurangi ukuran posisi, prioritaskan HIGH.")
    if not picks:
        lines.append("• Tidak ada kandidat BPJS yang memenuhi kriteria.")
    for p in picks:
        lines.append(f"• <code>${p['ticker']}</code> ({p['conf']}) | Buy Area Rp {num(p['buy_low'])} - "
                     f"Rp {num(p['ref'])} | TP Rp {num(p['tp'])} ({pct(p['tp'] / p['ref'] - 1)}) | "
                     f"SL Rp {num(p['sl'])} ({pct(p['sl'] / p['ref'] - 1)})")
        lines.append(_flow_line(p["row"]))
    if picks:
        lines.append("└ Jangan kejar jika open gap-up di atas buy area. Tutup semua posisi sebelum 15:50.")
    return "\n".join(lines)


def build_morning(ihsg, feats, days, cfg, today):
    last_day = days[-1]
    m = market_summary(ihsg, feats, last_day)
    carried = bsjp_screen(feats, last_day, cfg)
    last, cumulative = scorecard(feats, days, cfg)
    parts = [
        f"🌅 <b>INSTITUTIONAL MORNING BRIEFING | {long_date(today)}</b>\n{SEP}",
        _ihsg_section(m),
        _exit_section(carried, last_day),
        _scorecard_section(last, cumulative),
        _tracker_section(smart_money_tracker(feats, last_day, cfg)),
        _bpjs_section(bpjs_screen(feats, last_day, cfg), m["bias"]),
        f"{SEP}\n{DISCLAIMER}",
    ]
    return "\n\n".join(parts)


def build_bsjp(ihsg, feats, days, cfg, now_wib):
    day = days[-1]
    m = market_summary(ihsg, feats, day)
    picks = bsjp_screen(feats, day, cfg)
    lines = [f"🌇 <b>BSJP SIGNAL | Beli Sore Jual Pagi — {long_date(now_wib)}</b>", SEP,
             f"📊 IHSG intraday {num(m['close'], 2)} ({pct(m['chg'], 2)}) | Bias: {m['bias']}"]
    if m["bias"] == "Bearish Pressure":
        lines.append("⚠️ Risiko gap-down overnight lebih tinggi saat IHSG Bearish Pressure.")
    lines.append("")
    if not picks:
        lines.append("⚪ Tidak ada kandidat BSJP hari ini.")
    for n, p in enumerate(picks, 1):
        lines.append(f"{n}. <code>${p['ticker']}</code> ({p['conf']}) | Entry Rp {num(p['entry'])} | "
                     f"Target Rp {num(p['tp'])} ({pct(p['tp'] / p['entry'] - 1)}) | "
                     f"SL Rp {num(p['sl'])} ({pct(p['sl'] / p['entry'] - 1)})")
        lines.append(_flow_line(p["row"]))
    if picks:
        lines += ["", "🕒 Eksekusi: beli di pre-closing (15:50–16:00) dekat harga entry. "
                      "Jual besok pagi sesuai instruksi exit di Morning Briefing."]
    lines += [SEP, f"🗓️ {ib.fmt_date(now_wib)}", DISCLAIMER]
    return "\n".join(lines)


# ── Orchestration ─────────────────────────────────────────────────────────────

def prepare(history, universe, cutoff=None):
    """Build features per ticker. cutoff: drop bars on/after this date (morning: today)."""
    feats = {}
    for t in universe:
        df = history.get(f"{t}.JK")
        if df is None:
            continue
        if cutoff is not None:
            df = df[pd.DatetimeIndex(df.index).normalize() < pd.Timestamp(cutoff)]
        if len(df) >= 80:
            feats[t] = features(df)
    return feats


def run(mode, cfg, now_wib, is_scheduled=False):
    today = now_wib.date()
    if is_scheduled and not np.is_busday(today, busdaycal=IDX_HOLIDAYS_2026):
        logger.info("%s is not an IDX trading day, skipping", today)
        return []
    history = ib.download_history([ib.IHSG_SYMBOL] + [f"{t}.JK" for t in cfg.universe])
    ihsg = history.get(ib.IHSG_SYMBOL)
    if ihsg is None or len(ihsg) < 60:
        raise RuntimeError("IHSG data unavailable from yfinance")
    feats = prepare(history, cfg.universe, cutoff=today if mode == "morning" else None)
    days = trading_days(feats)
    logger.info("Mode %s | %d tickers | last trading day %s", mode, len(feats), days[-1].date())
    if mode == "morning":
        text = build_morning(ihsg, feats, days, cfg, today)
    else:
        if is_scheduled and days[-1].date() != today:
            logger.info("No bars for %s yet (holiday or data delay), skipping", today)
            return []
        text = build_bsjp(ihsg, feats, days, cfg, now_wib)
    return split_messages(text)


def main(argv=None):
    parser = argparse.ArgumentParser(description="IDX morning briefing and BSJP signals")
    parser.add_argument("mode", choices=["morning", "bsjp"])
    parser.add_argument("--dry-run", action="store_true", help="print instead of sending")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    cfg = BriefConfig.from_env()
    if not args.dry_run and not (cfg.telegram_token and cfg.telegram_chat_id):
        logger.error("Missing TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID")
        return 1
    messages = run(args.mode, cfg, datetime.now(ib.WIB), os.getenv("GITHUB_EVENT_NAME") == "schedule")
    for msg in messages:
        if args.dry_run:
            print(msg, end="\n\n" + "─" * 40 + "\n\n")
        else:
            ib.send_telegram(cfg, msg)
            time.sleep(1)
    logger.info("Done: %d message(s) %s", len(messages), "printed" if args.dry_run else "sent")
    return 0


if __name__ == "__main__":
    sys.exit(main())
