"""Python port of the supplied indicator's signal/state logic.

Only Confirm-only WAIT-flow buy/sell entries are returned to the paper bot.
The other default Pine entry paths are simulated only because they can change
the source indicator's long_on/short_on gate.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from exchange import Kline


LOOKBACK_LEN = 34
VOLUME_LEN = 20
VOLUME_MULTIPLIER = 1.5
DEFAULT_PARTIAL_R = 1.0
DEFAULT_FINAL_R = 2.0
MIN_HOLD_BARS = 1
TRADE_SIDE = "Both"
ENTRY_MODE = "Confirm only"
FLIP_ENTRY_MODE = None
SIGNAL_INTERVAL_MS = 15 * 60 * 1000


@dataclass(frozen=True)
class _HistoryBar:
    candle: Kline
    is_low_close: bool
    is_high_close: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "open_time": self.candle.open_time,
            "open": self.candle.open,
            "high": self.candle.high,
            "low": self.candle.low,
            "close": self.candle.close,
            "volume": self.candle.volume,
            "close_time": self.candle.close_time,
            "is_low_close": self.is_low_close,
            "is_high_close": self.is_high_close,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "_HistoryBar":
        candle = Kline(
            open_time=int(value["open_time"]),
            open=float(value["open"]),
            high=float(value["high"]),
            low=float(value["low"]),
            close=float(value["close"]),
            volume=float(value["volume"]),
            close_time=int(value["close_time"]),
        )
        return cls(
            candle=candle,
            is_low_close=bool(value["is_low_close"]),
            is_high_close=bool(value["is_high_close"]),
        )


class PineSignalSimulator:
    """Incrementally reproduce the supplied Pine script's default state."""

    def __init__(self, tick_size: float) -> None:
        if tick_size <= 0:
            raise ValueError("Pine tick size must be positive.")
        self.tick_size = tick_size
        self.bar_index = -1
        self.last_open_time: int | None = None
        self.history: list[_HistoryBar] = []

        self.wb_level: float | None = None
        self.wb_low: float | None = None
        self.ws_high: float | None = None
        self.anchor_high: float | None = None
        self.anchor_low: float | None = None

        self.prev_wb_event = False
        self.prev_wb_confirm = False
        self.prev_sw_event = False
        self.prev_buy_s1 = False
        self.prev_sell_s1 = False
        self.prev_sell_s2 = False
        self.prev_sell_s3 = False

        self.long_partial_target: float | None = None
        self.long_final_target: float | None = None
        self.short_partial_target: float | None = None
        self.short_final_target: float | None = None
        self.position: dict[str, Any] | None = None
        self.buy_mfe_history: list[float] = []
        self.sell_mfe_history: list[float] = []

    @classmethod
    def from_dict(
        cls, value: dict[str, Any] | None, tick_size: float
    ) -> "PineSignalSimulator":
        simulator = cls(tick_size)
        if value is None:
            return simulator
        if value.get("version") != 1:
            raise ValueError("Unsupported saved Pine simulator state.")

        simulator.bar_index = int(value.get("bar_index", -1))
        last_open_time = value.get("last_open_time")
        simulator.last_open_time = (
            int(last_open_time) if last_open_time is not None else None
        )
        simulator.history = [
            _HistoryBar.from_dict(item)
            for item in value.get("history", [])
            if isinstance(item, dict)
        ][-LOOKBACK_LEN:]
        for name in (
            "wb_level",
            "wb_low",
            "ws_high",
            "anchor_high",
            "anchor_low",
            "long_partial_target",
            "long_final_target",
            "short_partial_target",
            "short_final_target",
        ):
            raw = value.get(name)
            setattr(simulator, name, float(raw) if raw is not None else None)
        for name in (
            "prev_wb_event",
            "prev_wb_confirm",
            "prev_sw_event",
            "prev_buy_s1",
            "prev_sell_s1",
            "prev_sell_s2",
            "prev_sell_s3",
        ):
            setattr(simulator, name, bool(value.get(name, False)))

        position = value.get("position")
        if position is not None:
            if not isinstance(position, dict) or position.get("side") not in {
                "long",
                "short",
            }:
                raise ValueError("Invalid saved Pine position state.")
            simulator.position = {
                "side": str(position["side"]),
                "entry": float(position["entry"]),
                "stop": float(position["stop"]),
                "risk": float(position["risk"]),
                "partial": (
                    float(position["partial"])
                    if position.get("partial") is not None
                    else None
                ),
                "final": (
                    float(position["final"])
                    if position.get("final") is not None
                    else None
                ),
                "mfe": float(position["mfe"]),
                "opened_index": int(position["opened_index"]),
            }
        simulator.buy_mfe_history = [
            float(item) for item in value.get("buy_mfe_history", [])
        ][-20:]
        simulator.sell_mfe_history = [
            float(item) for item in value.get("sell_mfe_history", [])
        ][-20:]
        return simulator

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": 1,
            "bar_index": self.bar_index,
            "last_open_time": self.last_open_time,
            "history": [bar.to_dict() for bar in self.history],
            "wb_level": self.wb_level,
            "wb_low": self.wb_low,
            "ws_high": self.ws_high,
            "anchor_high": self.anchor_high,
            "anchor_low": self.anchor_low,
            "long_partial_target": self.long_partial_target,
            "long_final_target": self.long_final_target,
            "short_partial_target": self.short_partial_target,
            "short_final_target": self.short_final_target,
            "prev_wb_event": self.prev_wb_event,
            "prev_wb_confirm": self.prev_wb_confirm,
            "prev_sw_event": self.prev_sw_event,
            "prev_buy_s1": self.prev_buy_s1,
            "prev_sell_s1": self.prev_sell_s1,
            "prev_sell_s2": self.prev_sell_s2,
            "prev_sell_s3": self.prev_sell_s3,
            "position": self.position,
            "buy_mfe_history": self.buy_mfe_history,
            "sell_mfe_history": self.sell_mfe_history,
        }

    def process(self, candle: Kline, *, emit_signal: bool = True) -> str | None:
        """Process one confirmed candle; return buy/sell only for WAIT flow."""
        if self.last_open_time is not None and candle.open_time <= self.last_open_time:
            return None

        current = candle
        previous = self._lag(1)
        previous2 = self._lag(2)
        previous3 = self._lag(3)
        index = self.bar_index + 1
        close = current.close
        open_price = current.open
        high = current.high
        low = current.low
        volume = current.volume

        close_window = [
            item.candle.close for item in self.history[-(LOOKBACK_LEN - 1) :]
        ] + [close]
        is_low_close = close == min(close_window)
        is_high_close = close == max(close_window)

        prior_close = (
            previous.candle.close if previous is not None else None
        )
        enough = index >= 3
        wb_event = False
        if enough and previous2 is not None and previous3 is not None and previous is not None:
            # Pine's ta.crossover(close, high[2]) compares the previous close
            # with the previous bar's high[2], which is high[3] now.
            wb_event = (
                previous2.is_low_close
                and close > previous2.candle.high
                and prior_close is not None
                and prior_close <= previous3.candle.high
                and close > previous2.candle.high
            )

        if wb_event:
            self.wb_level = high
            self.wb_low = low

        wb_confirm = (
            self.wb_level is not None
            and prior_close is not None
            and close > self.wb_level
            and prior_close <= self.wb_level
        )

        sw_event = (
            self.wb_low is not None
            and prior_close is not None
            and close < self.wb_low
            and prior_close >= self.wb_low
        )
        if sw_event:
            self.ws_high = high

        volume_sma = self._volume_sma(volume)
        volume_spike = volume >= VOLUME_MULTIPLIER * (
            volume_sma if volume_sma is not None else volume
        )
        down = close < open_price
        up = close > open_price

        sell_s1 = False
        sell_s2 = False
        sell_s3 = False
        buy_s1 = False
        if enough and previous is not None and previous2 is not None and previous3 is not None:
            p1 = previous.candle
            p2 = previous2.candle
            p3 = previous3.candle
            down1 = p1.close < p1.open
            down2 = p2.close < p2.open
            up1 = p1.close > p1.open
            up2 = p2.close > p2.open
            volumes = (p1.volume, p2.volume, p3.volume)

            sell_s1 = (
                previous3.is_high_close
                and previous2.is_high_close
                and down1
                and down
                and (
                    volume_spike
                    or volume < min(volumes)
                )
            )
            sell_s2 = (
                up2
                and previous3.candle.close > previous3.candle.open
                and down1
                and down
                and close < p2.low
                and p1.close >= p2.low
                and (
                    volume_spike
                    or volume > max(volumes)
                )
            )
            breakdown_level = min(p3.low, p2.low)
            sell_s3 = (
                previous3.candle.close > previous3.candle.open
                and p2.close > p2.open
                and down1
                and down
                and (
                    volume_spike
                    or max(volume, p1.volume) > max(p2.volume, p3.volume)
                )
                and close < breakdown_level
                and p1.close >= breakdown_level
            )
            buy_level = max(p3.high, p2.high)
            buy_s1 = (
                p3.close < p3.open
                and p2.close < p2.open
                and up1
                and up
                and (
                    volume_spike
                    or max(volume, p1.volume) > max(p2.volume, p3.volume)
                )
                and close > buy_level
                and p1.close <= buy_level
            )

        new_sw = sw_event and not self.prev_sw_event
        new_buy_s1 = buy_s1 and not self.prev_buy_s1
        new_sell_any = (
            (sell_s1 or sell_s2 or sell_s3)
            and not (self.prev_sell_s1 or self.prev_sell_s2 or self.prev_sell_s3)
        )

        enter_confirm_buy = wb_confirm and not self.prev_wb_confirm
        enter_confirm_sell = new_sw and self.ws_high is not None

        anchor_event = (
            wb_event
            or wb_confirm
            or sw_event
            or sell_s1
            or sell_s2
            or sell_s3
            or buy_s1
        )
        if anchor_event:
            self.anchor_high = high
            self.anchor_low = low

        flip_buy_cross = (
            self.anchor_high is not None
            and prior_close is not None
            and close > self.anchor_high
            and prior_close <= self.anchor_high
        )
        flip_sell_cross = (
            self.anchor_low is not None
            and prior_close is not None
            and close < self.anchor_low
            and prior_close >= self.anchor_low
        )
        flip_up_ok = (
            self.anchor_high is not None
            and self.anchor_low is not None
            and flip_buy_cross
        )
        flip_dn_ok = (
            self.anchor_high is not None
            and self.anchor_low is not None
            and flip_sell_cross
        )

        self._update_mfe(high, low)
        buy_entry_average = self._average_last(self.buy_mfe_history, 3)
        sell_entry_average = self._average_last(self.sell_mfe_history, 3)
        buy_live_average = self._average_with_live(
            self.buy_mfe_history,
            self.position["mfe"] if self.position and self.position["side"] == "long" else 0.0,
            bool(self.position and self.position["side"] == "long"),
            3,
        )
        sell_live_average = self._average_with_live(
            self.sell_mfe_history,
            self.position["mfe"] if self.position and self.position["side"] == "short" else 0.0,
            bool(self.position and self.position["side"] == "short"),
            3,
        )

        selected_signal: str | None = None
        if self.position is None and enter_confirm_buy and self.wb_low is not None:
            self._enter("long", close, self.wb_low, buy_entry_average, index)
            selected_signal = "buy"

        if self.position is None and enter_confirm_sell and self.ws_high is not None:
            self._enter("short", close, self.ws_high, sell_entry_average, index)
            selected_signal = "sell"

        if (
            self.position is None
            and new_buy_s1
            and self.anchor_low is not None
        ):
            self._enter("long", close, self.anchor_low, buy_entry_average, index)

        if (
            self.position is None
            and new_sell_any
            and self.anchor_high is not None
        ):
            self._enter("short", close, self.anchor_high, sell_entry_average, index)

        if FLIP_ENTRY_MODE is not None and self.position is None:
            if flip_dn_ok and self.anchor_low is not None:
                self._enter("long", close, self.anchor_low, buy_entry_average, index)
            if flip_up_ok and self.anchor_high is not None:
                self._enter("short", close, self.anchor_high, sell_entry_average, index)

        self._update_trailing_stop(
            index,
            buy_live_average,
            sell_live_average,
        )
        self._check_indicator_exit(index, high, low)

        self.prev_wb_event = wb_event
        self.prev_wb_confirm = wb_confirm
        self.prev_sw_event = sw_event
        self.prev_buy_s1 = buy_s1
        self.prev_sell_s1 = sell_s1
        self.prev_sell_s2 = sell_s2
        self.prev_sell_s3 = sell_s3

        self.history.append(_HistoryBar(current, is_low_close, is_high_close))
        self.history = self.history[-LOOKBACK_LEN:]
        self.bar_index = index
        self.last_open_time = candle.open_time
        return selected_signal if emit_signal else None

    def _lag(self, lag: int) -> _HistoryBar | None:
        if len(self.history) < lag:
            return None
        return self.history[-lag]

    def _volume_sma(self, current_volume: float) -> float | None:
        volumes = [
            item.candle.volume for item in self.history[-(VOLUME_LEN - 1) :]
        ] + [current_volume]
        if len(volumes) < VOLUME_LEN:
            return None
        return sum(volumes[-VOLUME_LEN:]) / VOLUME_LEN

    @staticmethod
    def _average_last(values: list[float], count: int) -> float | None:
        if not values:
            return None
        selected = values[-count:]
        return sum(selected) / len(selected)

    @staticmethod
    def _average_with_live(
        history: list[float], live_value: float, include_live: bool, count: int
    ) -> float | None:
        if not include_live:
            return PineSignalSimulator._average_last(history, count)
        selected = history[-max(0, count - 1):]
        values = [*selected, live_value]
        return sum(values) / len(values)

    def _update_mfe(self, high: float, low: float) -> None:
        if self.position is None or self.position["risk"] <= 0:
            return
        if self.position["side"] == "long":
            current_r = (high - self.position["entry"]) / self.position["risk"]
        else:
            current_r = (self.position["entry"] - low) / self.position["risk"]
        self.position["mfe"] = max(self.position["mfe"], current_r)

    def _enter(
        self,
        side: str,
        entry: float,
        stop: float,
        historical_average: float | None,
        index: int,
    ) -> None:
        adjusted_stop = stop
        if side == "long" and entry - adjusted_stop == 0:
            adjusted_stop -= self.tick_size
        elif side == "short" and adjusted_stop - entry == 0:
            adjusted_stop += self.tick_size

        risk = max(
            entry - adjusted_stop if side == "long" else adjusted_stop - entry,
            0.0,
        )
        if historical_average is None:
            final_r = DEFAULT_FINAL_R
            partial_r = DEFAULT_PARTIAL_R
        else:
            final_r = max(0.5, historical_average)
            partial_r = max(0.25, final_r / 2.0)

        if risk > 0:
            if side == "long":
                self.long_partial_target = entry + partial_r * risk
                self.long_final_target = entry + final_r * risk
            else:
                self.short_partial_target = entry - partial_r * risk
                self.short_final_target = entry - final_r * risk

        if side == "long":
            partial = self.long_partial_target
            final = self.long_final_target
        else:
            partial = self.short_partial_target
            final = self.short_final_target

        self.position = {
            "side": side,
            "entry": entry,
            "stop": adjusted_stop,
            "risk": risk,
            "partial": partial,
            "final": final,
            "mfe": 0.0,
            "opened_index": index,
        }

    def _update_trailing_stop(
        self,
        index: int,
        buy_live_average: float | None,
        sell_live_average: float | None,
    ) -> None:
        if self.position is None:
            return
        if index < self.position["opened_index"] + MIN_HOLD_BARS:
            return
        if self.position["risk"] <= 0:
            return

        if self.position["side"] == "long" and buy_live_average is not None:
            protect_r = max(
                0.0,
                min(self.position["mfe"], buy_live_average),
            )
            new_stop = self.position["entry"] + (protect_r - 1.0) * self.position["risk"]
            if new_stop > self.position["stop"]:
                self.position["stop"] = new_stop
        elif self.position["side"] == "short" and sell_live_average is not None:
            protect_r = max(
                0.0,
                min(self.position["mfe"], sell_live_average),
            )
            new_stop = self.position["entry"] + (1.0 - protect_r) * self.position["risk"]
            if new_stop < self.position["stop"]:
                self.position["stop"] = new_stop

    def _check_indicator_exit(self, index: int, high: float, low: float) -> None:
        position = self.position
        if position is None or index < position["opened_index"] + MIN_HOLD_BARS:
            return

        side = position["side"]
        if side == "short":
            hit_stop = high >= position["stop"]
            hit_final = position["final"] is not None and low <= position["final"]
            hit_partial = position["partial"] is not None and low <= position["partial"]
        else:
            hit_stop = low <= position["stop"]
            hit_final = position["final"] is not None and high >= position["final"]
            hit_partial = position["partial"] is not None and high >= position["partial"]

        if hit_stop or hit_final or hit_partial:
            history = (
                self.buy_mfe_history if side == "long" else self.sell_mfe_history
            )
            history.append(position["mfe"])
            del history[:-20]
            self.position = None
