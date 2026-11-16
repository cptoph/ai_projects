"""Rolling market/on-chain features and prediction outcome tracking."""

from collections import deque
from math import erf, log, sqrt
from statistics import pstdev

from model import OnlineForecastModel

HORIZON_SECONDS = 300
FORECAST_HORIZONS = (60, HORIZON_SECONDS, 900, 3600)
SLOT_GRACE_SECONDS = 2


class SignalEngine:
    """Build rolling features, forecast, and score matured predictions."""

    def __init__(self) -> None:
        self.models = {
            horizon: OnlineForecastModel() for horizon in FORECAST_HORIZONS
        }
        self.model = self.models[HORIZON_SECONDS]
        self.prices: deque[tuple[float, float]] = deque(maxlen=7200)
        self.minute_prices: deque[tuple[int, float]] = deque(maxlen=1440)
        self.transactions: deque[tuple[float, float, float, str]] = deque()
        self._transaction_ids: dict[str, float] = {}
        self.congestion: deque[tuple[float, float]] = deque(maxlen=120)
        self.fees: dict[str, float] = {}
        self.order_book_imbalance = 0.0
        self.best_bid = 0.0
        self.best_ask = 0.0
        self.order_book_time = 0.0
        self.latest_forecast: dict | None = None
        self.latest_forecasts: dict[int, dict] = {}
        self.pending: dict[int, deque[dict]] = {
            horizon: deque() for horizon in FORECAST_HORIZONS
        }
        self.scoreboard: deque[dict] = deque(maxlen=40)
        self._last_forecast_slot: dict[int, float] = {}
        self._candle_count = 0

    @property
    def candle_count(self) -> int:
        return self._candle_count

    def handle(self, event: dict) -> None:
        kind = event.get("type")
        if kind == "candles":
            candles = sorted(event["candles"], key=lambda candle: candle["time"])
            self._candle_count = len(candles)
            for horizon, model in self.models.items():
                model.warm_start(candles, horizon_minutes=horizon // 60)
            for candle in candles:
                self._add_price(candle["time"] + 60, candle["close"])
        elif kind == "price":
            self._add_price(event["time"], event["price"])
        elif kind == "transaction":
            txid = event.get("txid", "")
            if txid and txid in self._transaction_ids:
                return
            self.transactions.append(
                (
                    event["time"],
                    event.get("btc", 0.0),
                    event.get("fee_rate", 0.0),
                    txid,
                )
            )
            if txid:
                self._transaction_ids[txid] = event["time"]
        elif kind == "mempool":
            self.congestion.append((event["time"], event["vsize"]))
        elif kind == "fees":
            self.fees = event["fees"]
        elif kind == "book":
            self.order_book_imbalance = max(-1.0, min(1.0, event["imbalance"]))
            self.best_bid = float(event.get("bid", 0.0))
            self.best_ask = float(event.get("ask", 0.0))
            self.order_book_time = float(event.get("time", 0.0))

    def update(self, now: float) -> dict | None:
        forecasts = self.update_all(now)
        return next(
            (forecast for forecast in forecasts if forecast["horizon_seconds"] == HORIZON_SECONDS),
            None,
        )

    def update_all(self, now: float) -> list[dict]:
        self._resolve_predictions(now)
        if not self.prices:
            return []
        forecasts = []
        for horizon in FORECAST_HORIZONS:
            slot_start = now // horizon * horizon
            last_slot = self._last_forecast_slot.get(horizon)
            if last_slot is None:
                self._last_forecast_slot[horizon] = slot_start
                if now - slot_start > SLOT_GRACE_SECONDS:
                    continue
            elif slot_start <= last_slot:
                continue
            self._last_forecast_slot[horizon] = slot_start
            if now - slot_start > SLOT_GRACE_SECONDS:
                continue
            forecasts.append(self._make_forecast(slot_start, horizon))
        return forecasts

    def _make_forecast(self, now: float, horizon_seconds: int) -> dict:
        forecast = self.preview_forecast(now, horizon_seconds)
        self.record_forecast(forecast)
        return forecast

    def preview_forecast(self, now: float, horizon_seconds: int) -> dict:
        """Build a forecast without adding it to the published or pending set."""
        price = self.prices[-1][1]
        features = self._features(now)
        momentum_5m = features[1]
        horizon_minutes = horizon_seconds / 60
        horizon_scale = horizon_minutes / 5
        fallback = (
            0.55 * momentum_5m * horizon_scale
            + 0.25 * features[0] * horizon_minutes
            + 0.0015 * features[7]
            + 0.00015 * (features[3] - 0.5) * horizon_scale
        )
        fallback = max(-0.02, min(0.02, fallback))
        volatility = self._volatility(horizon_minutes)
        model = self.models[horizon_seconds]
        expected_return, probability_up = model.forecast(
            features, fallback, volatility
        )
        expected_return = max(-0.03, min(0.03, expected_return))
        predicted_price = price * (1 + expected_return)
        confidence = 50 + 50 * abs(2 * probability_up - 1)
        spread = max(volatility, 0.00025) * 1.65 * price
        forecast = {
            "time": now,
            "horizon": now + horizon_seconds,
            "horizon_seconds": horizon_seconds,
            "price": price,
            "predicted": predicted_price,
            "low": predicted_price - spread,
            "high": predicted_price + spread,
            "direction": "UP" if expected_return >= 0 else "DOWN",
            "confidence": confidence,
            "trained": model.is_fitted,
            "features": features,
        }
        return forecast

    def record_forecast(self, forecast: dict) -> None:
        """Publish a forecast and track it for later outcome scoring."""
        horizon_seconds = int(forecast["horizon_seconds"])
        self.latest_forecasts[horizon_seconds] = forecast
        if horizon_seconds == HORIZON_SECONDS:
            self.latest_forecast = forecast
        self.pending[horizon_seconds].append(forecast)

    def scenario_probability(
        self,
        current_price: float,
        target_price: float,
        minutes_to_target: float,
        direction: str,
    ) -> dict:
        """Estimate a threshold probability from recent minute-price volatility."""
        if current_price <= 0 or target_price <= 0 or minutes_to_target <= 0:
            raise ValueError("prices and time to target must be positive")
        if direction not in ("above", "below"):
            raise ValueError("direction must be 'above' or 'below'")

        samples = list(self.minute_prices)[-31:]
        squared_returns = 0.0
        elapsed_minutes = 0
        return_count = 0
        for (previous_minute, previous), (minute, current) in zip(
            samples, samples[1:]
        ):
            elapsed = minute - previous_minute
            if elapsed <= 0 or previous <= 0 or current <= 0:
                continue
            log_return = log(current / previous)
            squared_returns += log_return**2
            elapsed_minutes += elapsed
            return_count += 1

        variance_per_minute = (
            squared_returns / elapsed_minutes
            if return_count >= 2 and elapsed_minutes > 0
            else (0.001 / sqrt(5)) ** 2
        )
        horizon_volatility = max(
            sqrt(variance_per_minute * minutes_to_target), 0.0001
        )
        z_score = log(target_price / current_price) / horizon_volatility
        cdf = 0.5 * (1 + erf(z_score / sqrt(2)))
        probability = 1 - cdf if direction == "above" else cdf
        return {
            "probability": max(0.0, min(1.0, probability)),
            "volatility": horizon_volatility,
            "sample_count": return_count,
        }

    def _features(self, now: float) -> list[float]:
        self._prune(now)
        current = self.prices[-1][1]
        price_60 = self._price_at_or_before(now - 60, current)
        price_300 = self._price_at_or_before(now - 300, current)
        price_900 = self._price_at_or_before(now - 900, current)
        tx_values = [value for _, value, _, _ in self.transactions]
        fee_values = [fee for _, _, fee, _ in self.transactions if fee > 0]
        rate = len(self.transactions) / 300
        total_btc = sum(tx_values)
        whale_count = sum(value > 50 for value in tx_values)
        recent_fee = sum(fee_values) / len(fee_values) if fee_values else 0
        fastest_fee = self.fees.get("fastestFee", recent_fee)
        fee_pressure = min(3.0, fastest_fee / 30) / 3
        congestion_delta = 0.0
        if len(self.congestion) > 1:
            start, end = self.congestion[0][1], self.congestion[-1][1]
            congestion_delta = max(-1.0, min(1.0, (end - start) / max(start, 1)))
        return [
            current / price_60 - 1 if price_60 else 0,
            current / price_300 - 1 if price_300 else 0,
            current / price_900 - 1 if price_900 else 0,
            min(1.0, rate / 10),
            min(1.0, total_btc / 1000),
            min(1.0, whale_count / 10),
            max(-1.0, min(1.0, congestion_delta + fee_pressure * 0.25)),
            self.order_book_imbalance,
        ]

    def _resolve_predictions(self, now: float) -> None:
        for horizon, pending in self.pending.items():
            while pending and now - pending[0]["time"] >= horizon:
                prediction = pending[0]
                actual_price = next(
                    (
                        price
                        for sample_time, price in self.prices
                        if sample_time >= prediction["horizon"]
                    ),
                    None,
                )
                if actual_price is None:
                    break
                pending.popleft()
                actual_return = actual_price / prediction["price"] - 1
                predicted_up = prediction["direction"] == "UP"
                actual_up = actual_return >= 0
                self.scoreboard.append(
                    {
                        "time": prediction["time"],
                        "horizon": prediction["horizon"],
                        "price": prediction["price"],
                        "predicted": prediction["predicted"],
                        "low": prediction["low"],
                        "high": prediction["high"],
                        "actual": actual_price,
                        "direction": prediction["direction"],
                        "confidence": prediction["confidence"],
                        "correct": predicted_up == actual_up,
                        "return": actual_return,
                        "features": prediction["features"],
                    }
                )
                self.models[horizon].add_labeled_example(
                    prediction["features"], actual_return
                )

    def _prune(self, now: float) -> None:
        while self.transactions and now - self.transactions[0][0] > HORIZON_SECONDS:
            _, _, _, txid = self.transactions.popleft()
            self._transaction_ids.pop(txid, None)
        while self.congestion and now - self.congestion[0][0] > HORIZON_SECONDS:
            self.congestion.popleft()

    def _add_price(self, timestamp: float, price: float) -> None:
        if price <= 0:
            return
        if self.prices and timestamp < self.prices[-1][0]:
            return
        if self.prices and timestamp == self.prices[-1][0]:
            self.prices[-1] = (timestamp, price)
        else:
            self.prices.append((timestamp, price))
        minute = int(timestamp // 60)
        if self.minute_prices and minute < self.minute_prices[-1][0]:
            return
        if self.minute_prices and minute == self.minute_prices[-1][0]:
            self.minute_prices[-1] = (minute, price)
        else:
            self.minute_prices.append((minute, price))

    def _price_at_or_before(self, timestamp: float, default: float) -> float:
        for sample_time, price in reversed(self.prices):
            if sample_time <= timestamp:
                return price
        return self.prices[0][1] if self.prices else default

    def _volatility(self, horizon_minutes: float) -> float:
        returns: list[float] = []
        samples = list(self.prices)
        for (_, previous), (_, current) in zip(samples[-31:-1], samples[-30:]):
            if previous > 0 and current > 0:
                returns.append(log(current / previous))
        if len(returns) < 2:
            return max(0.0001, 0.001 * (horizon_minutes / 5) ** 0.5)
        return max(0.0001, pstdev(returns) * (horizon_minutes**0.5))
