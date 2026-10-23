# Binance Spot Paper Trading Bot

This Python bot reads public Binance Spot production candles and prices from
Binance's official data-only API host,
calculates the supplied Confirm-only WAIT-flow signals, and simulates trades.
Market prices are live, but the bot submits no exchange orders and has no
live-trading mode. The default signal interval is 5 minutes, with a required
15-minute higher-timeframe confirmation.

## What it does

- Scans the seven USDT pairs in `WATCHLIST`: SOL, FET, MAV, DASH, SUI, ZEN, ZEC.
- Uses confirmed, closed candles for signal generation and the fixed 15m HTF
  trend filter.
- Uses the supplied Pine source's lookback 34, volume SMA 20, and volume
  multiplier 1.5 rules. Immediate flip entries are disabled
  (`FLIP_ENTRY_MODE = None`); `MIN_HOLD_BARS = 1`.
- Uses a $10 paper entry, at most 3 positions, and a $30 total exposure limit.
- Sets a fixed +3% target and -2% stop on every paper entry. The risk monitor
  checks current public Binance prices about every five seconds and simulates a fill at
  the exact threshold.
- Closes a paper position at a confirmed primary-timeframe candle close when a
  matching, higher-timeframe-confirmed WAIT-flow sell signal appears.
- Records each trade in `trading_history.csv`, with PostgreSQL as the durable
  source of truth so the CSV can be recreated after a publish or JSON cleanup.
- Calculates gross P/L, 0.075% Binance fees on each side, net P/L, and a
  cumulative summary.
- Provides allowlisted Telegram `/start`, `/stop`, and `/status` commands, plus
  entry, target, stop, and close alerts.
- Provides a minimal `/healthz` endpoint that does not reveal the trade ledger.

Signal and exit timestamps are stored as Binance/TradingView candle-open epoch
milliseconds and UTC ISO 8601 text. Paper entries use the signal candle close.
Risk exits use the exact target or stop. No exchange order request is sent.

## Important limits

- Pine's structural RR/trailing state still controls its flat-position gate;
  paper trades close using the requested 3% / 2% rules or a confirmed matching
  sell signal.
- The bot must stay running for its five-second paper exit checks. These are
  not exchange-held stop orders.
- Use a Reserved VM deployment for an always-on worker. The published app's
  local filesystem resets on redeployment, so open positions and the durable
  trade ledger are stored in PostgreSQL. The local CSV is synchronized from it.
- Binance's public Spot API may restrict access from some network locations or
  may not list every requested pair. The bot reports API errors and does not
  submit orders.

## Run and test

Replit supplies `DATABASE_URL`; the bot uses `psycopg` to store history
durably. Local runs need a PostgreSQL URL and the same `paper_trades` schema.

```bash
uv run --project . python -m unittest discover -s trading-bot -v
uv run --project . python trading-bot/main.py
```

In Replit, run the **Trading Bot** workflow or publish as a Reserved VM. The
`paper_trades` table must be present in the Replit database. The bot reads live
public prices but remains paper-only. Use Telegram
`/status` for the current state, open positions, and P/L summary. `/healthz`
returns only process health.

## Telegram setup

Set `TELEGRAM_BOT_TOKEN` and `TELEGRAM_ALLOWED_CHAT_ID` in Replit Secrets. The
bot accepts commands only from that numeric chat ID. Never put the token in
`.env`, source code, or chat.

- `/start` enables new paper entries.
- `/stop` pauses new entries but continues monitoring open positions.
- `/status` returns the current state and cumulative results.

## History and fee calculation

`trading_history.csv` contains one row per trade, including active trades,
entry/exit prices, target and stop, exact UTC candle times, gross P/L, fees,
net P/L, and close reason. PostgreSQL is the durable ledger; the CSV is
regenerated from it at startup and synchronized after trade changes. Removing
JSON state files does not delete the CSV or the database history.

Fees are simulated at 0.075% of entry notional plus 0.075% of exit notional.
Net P/L is gross P/L minus both fees. Cumulative P/L percent is closed net P/L
divided by total closed quote notional.

## Signal logic and troubleshooting

`pine_signals.py` contains the Python calculation from the supplied Pine source.
The scanner processes closed primary and 15m candles and only sends primary
WAIT-flow signals matching the latest 15m direction to the paper engine.
Indicator state is saved in `data/paper_state.json`; open positions and trade
history are restored from PostgreSQL.

- **Market data error:** the bot reports Binance public data API failures and keeps
  paper state safe. Check workflow logs and `/healthz`.
- **Pair not listed:** remove the unavailable symbol from `WATCHLIST`.
- **No signals yet:** the bot seeds from closed candles without replaying
  historical trades and waits for a new confirmed primary signal plus a matching
  15m direction.
- **A sell signal closes a trade early:** it is a separate signal exit; the
  fixed target and stop remain attached to every entry and are reported in the
  trade ledger.
