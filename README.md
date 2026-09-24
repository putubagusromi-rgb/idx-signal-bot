# IDX Signal Bot

A Telegram bot that scans **101 stocks** on the Indonesia Stock Exchange (IDX) every morning, scores them with a **3-category technical system** (Reversal / Breakout / Momentum), and sends buy signals to your Telegram group. Built for short-term swing trading (3-day hold, +5% take-profit target).

The bot automatically detects the market regime (bull vs bear via IHSG vs its own 20-day MA) and raises the signal threshold in downtrends — so weak setups get filtered exactly when the market is dangerous.

**Backtested result:** (old single-mode system) Score >= 9 signals, 3-day exit, +5% target -> **+5.8% annual return**, **54.9% win rate** (May 2025 to May 2026). The 3-category system has not been backtested yet.

---

## How It Works

Every weekday at **08:15 AM WIB** (Western Indonesia Time), the bot posts two messages to your Telegram group:

1. **Portfolio check** — current price for each open position, unrealised P&L, and whether to hold, take profit, or exit
2. **Buy signals** — top 5 stocks scoring >= 8/10, ranked by score, with tier labels and category tags

You act on the signals manually through your IDX broker (Stockbit or any other).

### Exit Strategy

Every trade ends exactly one of two ways:

- **Take profit** — set a Stockbit auto-sell at the +5% target when you buy
- **Day 3** — sell at market price on the 3rd trading day, no exceptions, skip weekends and IDX holidays

No percentage-based stop-loss. No panic selling. Hold until one triggers. (The bot's -5% alert is informational only.)

---

## Scoring System

Each stock is scored across **3 independent categories** simultaneously. The best match wins. Priority tiebreaker: Reversal > Breakout > Momentum (reversal is most time-sensitive).

Every signal displays a category tag: **🔄 Reversal**, **🚀 Breakout**, or **🏄 Momentum**.

### 🔄 Reversal (Oversold bounce / support bounce)

Best for bear markets, overreactions, and capitulation events.

| Signal | Points | Logic |
|---|---|---|
| RSI < 30 | +3 | Extremely oversold — sellers exhausted |
| RSI 30-40 | +2 | Oversold |
| RSI 40-50 | +1 | Neutral-leaning oversold |
| Volume >= 1.5x + down day < -2% | +3 | Capitulation — high-volume selloff, best reversal signal |
| Volume >= 1.2x + down day < -2% | +2 | Elevated selling pressure |
| Volume >= 2x + up day > +1% | +2 | Strong accumulation bounce |
| Volume >= 1.5x + up day > +1% | +1 | Mild accumulation bounce |
| 5-day pullback -2% to -8% | +2 | Healthy dip within normal range |
| 5-day drop > -8% | +1 | Bigger drop, higher risk/reward |
| Price at or near 20-day support | +2 | Sitting on the nearest floor |
| Price at or near 50-day support | +1 | Also at longer-term support |
| Hammer candle | +2 | Buyers aggressively defended the low |
| Outperformed IHSG (5d) | +1 | Relative strength vs the market |
| MACD histogram positive | +1 | Bullish momentum |
| MACD bullish crossover | +1 | Fresh momentum trigger |

**Max: 10 points | Stop:** ATR below 20-day support (max -8%)

### 🚀 Breakout (Range breakout / consolidation end)

Best for sideways-to-bull transitions and stocks emerging from consolidation.

| Signal | Points | Logic |
|---|---|---|
| Near 20-day high + vol >= 1.2x + up day | +2 | Volume-confirmed breakout from range |
| Near 20-day high only | +1 | Breaking out, volume confirmation pending |
| Volume >= 1.5x + up day | +2 | Strong accumulation on up day |
| Volume >= 1.2x + up day | +1 | Mild accumulation on up day |
| Above MA20 | +1 | Short-term uptrend |
| Above MA50 | +1 | Medium-term uptrend |
| RSI 50-65 | +1 | Breakout zone — not exhausted |
| 5-day return +2% to +10% | +2 | Breaking out of consolidation |
| 5-day return > +10% | +1 | Already running, late to breakout |
| ADX > 30 | +2 | Confirmed trend |
| ADX 25-30 | +1 | Trending, but not extreme |
| MACD bullish crossover | +1 | Fresh momentum trigger |

**Max: 10 points | Stop:** ATR below MA20 (max -6%)

### 🏄 Momentum (Trending continuation)

Best for established bull markets and strong ADX trends.

| Signal | Points | Logic |
|---|---|---|
| Price > MA20 **and** MA50 | +2 | Bull structure confirmed |
| Price > MA20 only | +1 | Early uptrend |
| ADX > 30 | +2 | Strong trend confirmed |
| ADX 25-30 | +1 | Trending, but not extreme |
| Volume >= 1.5x + up day > +1% | +2 | Strong accumulation |
| Volume >= 1.2x + up day > +1% | +1 | Mild accumulation |
| RSI 50-70 | +1 | Healthy momentum zone |
| MACD histogram positive | +1 | Bullish momentum |
| MACD bullish crossover | +1 | Fresh momentum trigger |
| Near 20-day high | +1 | Price confirming trend |
| 5-day return +2% to +10% | +1 | Healthy trending momentum |

**Max: 10 points | Stop:** ATR below MA20 (max -6%)

### Additional Filters

| **Minimum score to appear:** 8 for all tiers
| **When IHSG is in a downtrend:** threshold raised to 9 — only the strongest setups qualify
- **Average daily volume:** must be >= 500,000 shares — illiquid stocks are always excluded

### Signal Context

Every signal includes:
- RSI, volume ratio, 1-d and 5-d returns, trend direction (MA20)
- Support, resistance, MA20, MA50 levels
- ADX (trend strength), MACD histogram
- Suggested lots based on budget allocation

---

## Stock Universe

101 tickers across three tiers:

| Tier | Count | Source |
|---|---|---|
| 🔵 Blue Chip | 47 | LQ45 (rebalances Feb/May/Aug/Nov) |
| 🟡 Mid-cap | 38 | IDX80 extras beyond LQ45 |
| 🔴 Small Cap | 16 | Liquid stocks priced <= Rp 1,000 |

LQ45 tickers (May to July 2026):
AALI, ADRO, AKRA, AMMN, AMRT, ANTM, ARTO, ASII, BBCA, BBNI, BBRI, BBTN, BMRI, BRPT, BUKA, CPIN, CUAN, DEWA, EMTK, ESSA, EXCL, GOTO, HEAL, HRUM, HRTA, ICBP, INCO, INDF, INTP, ISAT, ITMG, KLBF, MAPA, MBMA, MDKA, MEDC, MIKA, MNCN, PGAS, PTBA, SMGR, TBIG, TLKM, TOWR, UNTR, UNVR, WIFI

Update `scanner.py` with the latest LQ45 list every rebalancing period.

---

## Data Source

**Live scans use the TradingView Scanner API** — free, no API key, one request for the entire universe (~1 second), and it works from any IP including cloud VPSes that Yahoo Finance blocks. It returns pre-computed indicators (RSI, ADX, ATR, MACD, SMA20/50, 1M/3M high-low, weekly performance) which the scanner scores directly.

Approximations vs raw candle math (documented in `scanner.py`):
- Average volume: 30-day (scanner field) instead of 20-day ex-today
- 5-day momentum: weekly performance (`Perf.W`)
- 20-day support/resistance: 1-month low/high
- MACD crossover: 1-bar lookback instead of 2

**Backtests still use `yfinance`** (1 year of daily candles, no-lookahead scoring). Note: Yahoo Finance blocks most cloud VPS IPs — run backtests from a residential IP (your laptop).

---

## Telegram Bot

### Commands

| Command | Description |
|---|---|
| `/signals` | Run a full universe scan now (~2 seconds) |
| `/portfolio` | Check all open positions with current prices and P&L |
| `/bought TICKER LOTS PRICE` | Log a buy, e.g. `/bought BKSL 9 102` |
| `/sold TICKER PRICE` | Log a sell and see realised P&L, e.g. `/sold BKSL 120` |
| `/summary` | Total realised P&L and win rate across all trades |
| `/help` | Show command list |

### Automatic Reports

- **Morning report:** every weekday at 08:15 WIB — portfolio check + buy signals
- **Price alerts:** every 5 minutes during IDX market hours (09:00-15:30 WIB) — alerts when an open position hits the +5% target or the -5% danger zone

### Authorization

The bot has two separate settings:
- `GROUP_CHAT_ID` — the group/channel that receives all reports and alerts
- `AUTHORIZED_UID` — your personal Telegram user ID, the only account allowed to run commands

This means anyone in the group can read signals, but only you can log trades or run `/signals`. For Telegram topics, append the topic ID: `-1001234567890:200`.

---

## Setup

### Requirements

- Python 3.10+
- A Telegram bot token (create via [@BotFather](https://t.me/BotFather))
- A host: VPS (recommended), your own machine, or Railway

### Environment Variables

| Variable | Description |
|---|---|
| `BOT_TOKEN` | Telegram bot token from BotFather (required) |
| `GROUP_CHAT_ID` | Chat/group ID for receiving reports (use `-100XXXX:TOPIC` for Telegram topics) |
| `AUTHORIZED_UID` | Your Telegram user ID — only this user can run commands |
| `DATA_DIR` | Directory for `transactions.json` (default: script directory) |

Keep secrets out of git — use an untracked `secrets/.env` file (chmod 600) or your host's env var UI.

### Install

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

---

## Hosting Guide

### Option A: VPS with systemd (recommended)

Works on any Linux VPS. The TradingView data backend is specifically chosen so cloud/VPS IPs are not a problem.

```bash
# 1. Clone and install
git clone https://github.com/ekkyvalent/idx-signal-bot.git
cd idx-signal-bot
python3 -m venv venv
venv/bin/pip install -r requirements.txt

# 2. Env file
mkdir -p secrets data
cat > secrets/.env <<'EOF'
BOT_TOKEN=your_bot_token_here
GROUP_CHAT_ID=-1001234567890:200
AUTHORIZED_UID=your_telegram_user_id
DATA_DIR=/absolute/path/to/idx-signal-bot/data
EOF
chmod 600 secrets/.env

# 3. Systemd unit (edit paths to match your install)
sudo cp idx-signal-bot.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now idx-signal-bot

# 4. Verify
systemctl status idx-signal-bot
journalctl -u idx-signal-bot -f
```

`idx-signal-bot.service` uses `Restart=always`, so the bot auto-recovers from crashes and reboots.

**Deploying code updates:**

```bash
git pull origin main
sudo systemctl restart idx-signal-bot
```

### Option B: Local machine (laptop/desktop)

Same install steps, then run in a terminal:

```bash
set -a; source secrets/.env; set +a
python3 bot.py
```

Fine for testing; not recommended 24/7 (the morning report needs the process alive at 08:15 WIB).

**CLI-only scan** (no Telegram, writes `signals_latest.md`):

```bash
python3 signal_generator.py
```

### Option C: Railway (legacy, still works)

1. Create a new Railway project from your GitHub repo
2. Set the environment variables listed above
3. Add a persistent volume mounted at `/data` — set `DATA_DIR=/data`
4. Railway uses the included `Procfile`: `worker: python bot.py`

```bash
railway up      # deploy
railway logs    # check logs
```

Note: Railway's free trial expired for this project in Aug 2026 — the bot moved to a VPS. Railway remains a valid alternative if you prefer managed hosting (Hobby plan is $5/mo).

---

## Institutional Signal Bot (GitHub Actions, gratis)

`institutional_bot.py` adalah scanner terpisah yang berjalan sekali jalan (tanpa VPS) lewat GitHub Actions pada **12:05 WIB** (tutup Sesi I) dan **16:15 WIB** (tutup Sesi II), Senin–Jumat. Hari libur IDX otomatis dilewati (tidak ada bar IHSG hari itu).

| Gate | Sumber | Aturan |
|------|--------|--------|
| 1. Macro | yfinance `^JKSE` | IHSG < EMA50 → HIGH-RISK. Default `BEAR_MODE=hold` (signal ditahan, hanya laporan); `warn` = tetap kirim dengan label high-risk |
| 2. Teknikal | yfinance | ADTV 5 hari > Rp10 M, RVOL ≥ 2x (vs rerata 20 hari), harga > EMA20 & EMA50, ATR14 > ATR14 5 hari lalu dan > rerata 20 hari |
| 3. Smart money | [Index Alpha](https://indexalpha.id/docs/endpoints) | Net foreign buy ≥ 3 hari berturut-turut (dalam 5 hari terakhir), Top-3 broker pembeli ≥ 50% volume (agregat 5 hari) |
| 4. Risiko | — | SL = entry − 1.5×ATR14, TP1 = 1:1.5, TP2 = 1:3, lot = min(2% modal ÷ risiko per lembar, modal ÷ harga) |

Gate teknikal dijalankan dulu, jadi kuota Index Alpha hanya terpakai untuk saham yang lolos (≈6 unit per saham per run: 5 foreign-flow harian + 1 broker summary).

**Aktifkan workflow:** pindahkan `deploy/bot.yml` ke `.github/workflows/bot.yml`, lalu hapus `.github/workflows/run-bot.yml` (workflow lama itu menjalankan `scanner.py` tiap jam, padahal file itu tidak punya entrypoint, jadi tidak melakukan apa pun). Bisa lewat GitHub web (Add file → Create new file) atau:

```bash
git mv deploy/bot.yml .github/workflows/bot.yml
git rm .github/workflows/run-bot.yml
git commit -m "ci: enable institutional bot workflow" && git push
```

**Catatan data:** Index Alpha memperbarui data setiap hari bursa pukul 19:00 WIB, jadi run 12:05 & 16:15 memakai flow sampai hari bursa sebelumnya. Pada run 12:05 volume hari ini baru setengah hari, sehingga RVOL ≥ 2x di sesi I berarti lonjakan yang sangat kuat.

### Setup Secrets

1. Buka repo di GitHub → **Settings → Secrets and variables → Actions → New repository secret**.
2. Tambahkan:
   - `TELEGRAM_BOT_TOKEN` — token dari @BotFather
   - `TELEGRAM_CHAT_ID` — ID chat/grup (mis. `-1001234567890`, atau `-1001234567890:123` untuk topic)
   - `INDEX_ALPHA_API_KEY` — token dari dashboard Index Alpha
   - `PORTFOLIO_CAPITAL` — opsional, default `100000000`
3. Opsional, di tab **Variables**: `BEAR_MODE` = `hold` atau `warn`.

### Uji workflow

1. Tab **Actions → Institutional IDX Signal Bot → Run workflow**.
2. Centang **Dry run** untuk melihat pesan di log tanpa mengirim ke Telegram; jalankan lagi tanpa centang untuk kirim sungguhan.
3. Lokal: `INDEX_ALPHA_API_KEY=... python institutional_bot.py --dry-run`.
4. Unit test offline (tanpa API): `pip install pytest && python -m pytest tests/`.

Parameter lain bisa diubah lewat env: `RISK_PER_TRADE_PCT`, `ADTV_MIN_RP`, `RVOL_MIN`, `ATR_SL_MULT`, `TP1_RR`, `TP2_RR`, `FOREIGN_LOOKBACK_DAYS`, `FOREIGN_MIN_STREAK`, `BROKER_TOP_N`, `BROKER_MIN_SHARE_PCT`, `MAX_SIGNALS`, `SEND_EMPTY_REPORT`, `UNIVERSE` (daftar ticker dipisah koma; default universe dari `scanner.py`).

---

## Files

```
stock-trading/
  scanner.py              Core logic — 3-category scoring, TradingView backend, portfolio tracking
  bot.py                  Telegram bot — commands, morning report, price alerts
  institutional_bot.py    One-shot institutional scanner (GitHub Actions, Index Alpha + yfinance)
  deploy/bot.yml          GitHub Actions workflow for institutional_bot.py (move to .github/workflows/)
  signal_generator.py     CLI runner — writes signals_latest.md
  backtest.py             Strategy backtester and capital simulator (yfinance)
  backtest_compare.py     Backtest variant comparing scoring configs
  idx-signal-bot.service  systemd unit for VPS deployment
  Procfile                Railway worker config
  requirements.txt        Python dependencies
  secrets/.env            Runtime env vars (untracked, chmod 600)
  data/transactions.json  Trade log (auto-created, gitignored)
```

---

## Run a Backtest

```bash
python3 backtest.py
```

Downloads 1 year of OHLCV data via yfinance for all tickers, scores every stock on every day (no lookahead), and simulates trading the top signals. Takes 5-10 minutes. **Requires a residential IP** — Yahoo Finance blocks most cloud VPS IPs.

---

## Limitations

- **Purely technical:** No macro/news awareness. A stock can look perfect on the charts and still be in a fundamental downtrend. Always do your own due diligence.
- **TradingView approximations:** Indicator windows are approximate (see Data Source above) — scores can differ slightly from raw-candle math.
- **Fees assumed:** 0.15% buy / 0.35% sell (0.25% broker + 0.1% PPh final). Adjust `FEE_BUY` and `FEE_SELL` in `scanner.py` if your broker charges differently.
- **Holiday calendar:** IDX 2026 holidays are hardcoded in `scanner.py` (`IDX_HOLIDAYS_2026`). Update at the start of each year from idx.co.id.
- **Universe is manual:** LQ45 rebalances every Feb/May/Aug/Nov — update the ticker lists in `scanner.py`.

---

_This tool is for informational purposes only. It is not financial advice. Always do your own research before trading._
