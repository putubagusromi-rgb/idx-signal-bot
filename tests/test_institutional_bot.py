"""Offline tests for institutional_bot (no network). Run: python -m pytest tests/"""

import dataclasses
import os
import sys
from datetime import date, datetime

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import institutional_bot as b  # noqa: E402


@pytest.fixture
def cfg(monkeypatch):
    for name in ("PORTFOLIO_CAPITAL", "RISK_PER_TRADE_PCT", "BEAR_MODE", "UNIVERSE"):
        monkeypatch.delenv(name, raising=False)
    return b.Config.from_env()


def make_df(n=80, base=1000.0, last_vol_mult=3.0, breakout=True):
    idx = pd.bdate_range(end="2026-09-23", periods=n)
    close = np.full(n, base)
    close[-10:] = base * np.linspace(1.0, 1.15 if breakout else 0.85, 10)
    rng = np.linspace(5, 5, n)
    rng[-10:] = np.linspace(10, 40, 10)  # widening ranges -> ATR expansion
    vol = np.full(n, 20_000_000.0)
    vol[-1] *= last_vol_mult
    return pd.DataFrame({
        "Open": close, "High": close + rng, "Low": close - rng, "Close": close, "Volume": vol,
    }, index=idx)


def near_high(df):
    """Shift each bar so the close sits near its high (buying pressure), keeping range width."""
    rng = df["High"] - df["Low"]
    df["High"] = df["Close"] + rng * 0.05
    df["Low"] = df["Close"] - rng * 0.95
    return df


def test_tick_rounding():
    assert b.idx_tick(199) == 1 and b.idx_tick(499) == 2 and b.idx_tick(1999) == 5
    assert b.idx_tick(4999) == 10 and b.idx_tick(5000) == 25
    assert b.round_tick(1234, "down") == 1230
    assert b.round_tick(6337, "up") == 6350


def test_rupiah_format():
    assert b.rp(1234567) == "1.234.567"
    assert b.rp_short(15e9) == "Rp 15.00 M"


def test_foreign_streak_counts_from_latest():
    assert b.foreign_streak([1, -1, 2, 3, 4]) == 3
    assert b.foreign_streak([1, 2, None]) == 0
    assert b.foreign_streak([]) == 0


def test_broker_concentration():
    rows = [{"code": c, "buy_volume": v} for c, v in [("A", 50), ("B", 30), ("C", 10), ("D", 10)]]
    share, top = b.broker_concentration(rows, 3)
    assert share == pytest.approx(0.9) and top == ["A", "B", "C"]
    assert b.broker_concentration([], 3) == (0.0, [])


def test_flow_days_exclude_unpublished_today():
    idx = pd.to_datetime(["2026-09-21", "2026-09-22", "2026-09-23", "2026-09-24"])
    noon = datetime(2026, 9, 24, 12, 5, tzinfo=b.WIB)
    evening = datetime(2026, 9, 24, 20, 0, tzinfo=b.WIB)
    assert b.flow_trading_days(idx, noon, 5)[-1] == date(2026, 9, 23)
    assert b.flow_trading_days(idx, evening, 2) == [date(2026, 9, 23), date(2026, 9, 24)]


def test_technical_gate_pass_and_fail(cfg):
    assert b.technical_gate(make_df(), cfg) is not None
    assert b.technical_gate(make_df(last_vol_mult=1.0), cfg) is None      # RVOL too low
    assert b.technical_gate(make_df(breakout=False), cfg) is None         # below EMAs
    assert b.technical_gate(make_df(base=10.0), cfg) is None              # ADTV too low


def test_risk_plan_respects_two_percent(cfg):
    tech = {"price": 1000.0, "ema20": 980.0, "atr": 20.0}
    plan = b.risk_plan(tech, cfg)
    assert plan["sl"] == 970                 # 1000 - 1.5*20
    assert plan["tp1"] == 1045 and plan["tp2"] == 1090
    risk = (1000 - plan["sl"]) * 100 * plan["lots"]
    assert risk <= cfg.capital * 0.02
    assert plan["nominal"] <= cfg.capital


def test_regime_downtrend_flag():
    idx = pd.bdate_range(end="2026-09-23", periods=80)
    down = pd.DataFrame({"Close": np.linspace(7000, 6000, 80)}, index=idx)
    up = pd.DataFrame({"Close": np.linspace(6000, 7000, 80)}, index=idx)
    assert b.market_regime(down)["bullish"] is False
    assert b.market_regime(up)["bullish"] is True


class FakeIA:
    def __init__(self, nets, brokers):
        self.nets, self.brokers, self.bs_calls = nets, brokers, []

    def foreign_flow(self, tickers, day):
        return {t: {"net_foreign": self.nets[t].pop(0)} for t in tickers}

    def broker_summary(self, tickers, start, end):
        self.bs_calls.append(list(tickers))
        return {t: self.brokers for t in tickers}


def test_smart_money_gate_only_queries_brokers_for_streak_passers(cfg):
    days = [date(2026, 9, d) for d in (17, 18, 21, 22, 23)]
    concentrated = [{"code": "AK", "buy_volume": 60}, {"code": "BK", "buy_volume": 40}]
    fake = FakeIA({"AAAA": [1, -1, 5, 5, 5], "BBBB": [5, 5, 5, 5, -1]}, concentrated)
    out = b.smart_money_gate(fake, ["AAAA", "BBBB"], days, cfg)
    assert list(out) == ["AAAA"] and out["AAAA"]["ff_streak"] == 3
    assert fake.bs_calls == [["AAAA"]]


def test_signal_message_renders(cfg):
    cfg = dataclasses.replace(cfg, bear_mode="warn")
    sig = {
        "ticker": "BBCA", "sector": "Financial Services",
        "tech": {"price": 6225.0, "rvol": 2.3, "adtv": 5e11},
        "flow": {"ff_streak": 4, "ff_net": 1.2e10, "broker_share": 0.62, "broker_top": ["AK", "BK", "ZP"]},
        "plan": b.risk_plan({"price": 6225.0, "ema20": 6100.0, "atr": 120.0}, cfg),
    }
    regime = {"bullish": False, "status": "🔴 DOWNTREND"}
    msg = b.format_signal(sig, regime, cfg, datetime(2026, 9, 24, 16, 15, tzinfo=b.WIB))
    assert "HIGH-RISK MODE" in msg and "<code>$BBCA</code>" in msg
    assert "Rp 6.225" in msg and "Kamis, 24 Sep 2026 16:15 WIB" in msg
    assert len(msg) < b.TELEGRAM_MAX_LEN


def test_accumulation_proxy_gate(cfg):
    up = near_high(make_df())
    down = make_df(breakout=False)
    out = b.accumulation_proxy_gate({"UPPP.JK": up, "DOWN.JK": down}, ["UPPP", "DOWN", "MISS"], cfg)
    assert list(out) == ["UPPP"]
    assert out["UPPP"]["proxy"] is True and out["UPPP"]["cmf"] >= cfg.cmf_min


def test_proxy_signal_message(cfg):
    sig = {
        "ticker": "TLKM", "sector": "Communication Services",
        "tech": {"price": 2390.0, "rvol": 2.1, "adtv": 3e11},
        "flow": {"proxy": True, "cmf": 0.21, "up_days": 4},
        "plan": b.risk_plan({"price": 2390.0, "ema20": 2350.0, "atr": 60.0}, cfg),
    }
    msg = b.format_signal(sig, {"bullish": True, "status": "🟢 UPTREND"}, cfg,
                          datetime(2026, 9, 24, 12, 5, tzinfo=b.WIB))
    assert "mode gratis" in msg and "CMF20 +0.21" in msg and "4/5" in msg


def test_index_alpha_failure_falls_back_to_proxy(cfg, monkeypatch):
    idx = pd.bdate_range(end="2026-09-23", periods=80)
    ihsg = pd.DataFrame({"Open": 1, "High": 1, "Low": 1, "Close": np.linspace(6000, 7000, 80)}, index=idx)
    stock = near_high(make_df())
    monkeypatch.setattr(b, "download_history", lambda syms: {b.IHSG_SYMBOL: ihsg, "UPPP.JK": stock})
    monkeypatch.setattr(b, "fetch_sector", lambda s: "Test")

    class Broken:
        def foreign_flow(self, *a):
            raise b.IndexAlphaError("/foreign-flow/batch -> HTTP 403: quota")

    cfg = dataclasses.replace(cfg, universe=("UPPP",))
    msgs = b.run(cfg, datetime(2026, 9, 23, 16, 15, tzinfo=b.WIB), Broken())
    assert "Index Alpha gagal" in msgs[0] and "proxy gratis" in msgs[0]
    assert len(msgs) == 2 and "mode gratis" in msgs[1]
