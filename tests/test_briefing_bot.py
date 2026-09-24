"""Offline tests for briefing_bot (no network)."""

import os
import sys
from datetime import date, datetime

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import briefing_bot as bb  # noqa: E402
import institutional_bot as ib  # noqa: E402


@pytest.fixture
def cfg(monkeypatch):
    for name in ("UNIVERSE", "BSJP_MAX", "BRIEF_MIN_VALUE_RP"):
        monkeypatch.delenv(name, raising=False)
    return bb.BriefConfig.from_env()


def bars(n=100, end="2026-09-23", strong_last=True):
    """Flat-ish history; optionally the last bar closes strong on heavy volume."""
    idx = pd.bdate_range(end=end, periods=n)
    close = np.linspace(950, 1000, n)
    o, h, lo = close - 5, close + 10, close - 10
    vol = np.full(n, 20_000_000.0)
    if strong_last:
        close[-1] = 1030             # +3%
        o[-1], h[-1], lo[-1] = 1000, 1032, 998
        vol[-1] = 70_000_000         # 3.5x
    return pd.DataFrame({"Open": o, "High": h, "Low": lo, "Close": close, "Volume": vol}, index=idx)


def test_indonesian_number_format():
    assert bb.num(6384.73, 2) == "6.384,73"
    assert bb.pct(-0.0088, 2) == "-0,88%"
    assert bb.pct(0.0) == "+0,0%"
    assert bb.mf_short(1.23e10) == "+Rp 12,3 M"
    assert bb.long_date(date(2026, 9, 23)) == "Rabu, 23 September 2026"


def test_split_messages_respects_limit():
    text = "\n\n".join(["x" * 1500] * 5)
    parts = bb.split_messages(text, limit=4000)
    assert all(len(p) <= 4000 for p in parts) and len(parts) == 3


def test_bsjp_screen_picks_strong_close(cfg):
    feats = {"AAAA": bb.features(bars()), "BBBB": bb.features(bars(strong_last=False))}
    day = feats["AAAA"].index[-1]
    picks = bb.bsjp_screen(feats, day, cfg)
    assert [p["ticker"] for p in picks] == ["AAAA"]
    p = picks[0]
    assert p["entry"] == 1030 and p["tp"] > p["entry"] > p["sl"]
    assert p["conf"] in ("HIGH", "MEDIUM", "LOW")


def test_evaluate_flags(cfg):
    df = bars(n=101, end="2026-09-24")
    df.iloc[-2, :] = [1000, 1032, 998, 1030, 70_000_000]   # signal day
    df.iloc[-1, :] = [1045, 1060, 1000, 1010, 30_000_000]  # next day gaps up then fades
    feats = {"AAAA": bb.features(df)}
    d0, d1 = feats["AAAA"].index[-2], feats["AAAA"].index[-1]
    pick = bb.bsjp_screen(feats, d0, cfg)[0]
    res = bb.evaluate(pick, feats, d1)
    assert res["gap"] == pytest.approx(1045 / 1030 - 1)
    assert res["tp_hit"] == (1060 >= pick["tp"]) and res["tp_open"] == (1045 >= pick["tp"])
    assert res["sl_hit"] == (1000 <= pick["sl"])


def test_trading_days_ignores_sparse_dates():
    a = pd.DataFrame(index=pd.to_datetime(["2026-09-21", "2026-09-22", "2026-09-23"]))
    b = pd.DataFrame(index=pd.to_datetime(["2026-09-21", "2026-09-23"]))
    c = pd.DataFrame(index=pd.to_datetime(["2026-09-21", "2026-09-23", "2026-09-26"]))
    days = bb.trading_days({"A": a, "B": b, "C": c})
    assert [d.day for d in days] == [21, 23]


def test_morning_message_sections(cfg):
    history = {f"{t}.JK": bars(strong_last=(t == "AAAA")) for t in ("AAAA", "BBBB", "CCCC")}
    ihsg = bars()
    feats = bb.prepare(history, ("AAAA", "BBBB", "CCCC"))
    days = bb.trading_days(feats)
    msg = bb.build_morning(ihsg, feats, days, cfg, date(2026, 9, 24))
    for header in ("INSTITUTIONAL MORNING BRIEFING | Kamis, 24 September 2026",
                   "IHSG EXECUTIVE SUMMARY", "BSJP EXIT", "BSJP SCORECARD",
                   "SMART MONEY TRACKER", "BPJS WATCHLIST", "DISCLAIMER"):
        assert header in msg
    assert "<code>$AAAA</code>" in msg      # yesterday's BSJP pick is carried into the exit plan


def test_prepare_cutoff_drops_today():
    history = {"AAAA.JK": bars(end="2026-09-24")}
    feats = bb.prepare(history, ("AAAA",), cutoff=date(2026, 9, 24))
    assert feats["AAAA"].index[-1] == pd.Timestamp("2026-09-23")


def test_run_skips_holiday_when_scheduled(cfg, monkeypatch):
    monkeypatch.setattr(ib, "download_history", lambda s: pytest.fail("should not download"))
    holiday = datetime(2026, 12, 25, 7, 45, tzinfo=ib.WIB)
    assert bb.run("morning", cfg, holiday, is_scheduled=True) == []
