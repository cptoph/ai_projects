"""Background market runtime and read-only dashboard snapshots."""

import asyncio
import copy
import logging
import queue
import threading
import time
from datetime import date, datetime, time as datetime_time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import aiohttp

from deepseek import (
    DeepSeekAPIError,
    deepseek_api_key_configured,
    request_deepseek_forecasts,
)
from feeds import FeedWorker
from paper_trading import (
    DEFAULT_ROUND_TRIP_FEE_BPS,
    PaperTradeStore,
    PaperTradingEngine,
)
from scoreboard import ScoreboardStore
from signals import SignalEngine

LOGGER = logging.getLogger(__name__)
EASTERN_TIME = ZoneInfo("America/New_York")
HORIZON_LABELS = {60: "1 min", 300: "5 min", 900: "15 min", 3600: "1 hour"}
OUTCOME_WINDOWS = (3600, 21600, 86400, 604800, 2592000)
OUTLOOK_REFRESH_SECONDS = {300: 150, 900: 300, 3600: 900}
SIGNIFICANT_PRICE_CHANGE = 0.0025


class DashboardService:
    """Run feeds, forecasts, persistence, and paper trades outside Flask requests."""

    def __init__(self, database_path: Path | None = None) -> None:
        self.events: queue.Queue[dict] = queue.Queue()
        self.engine = SignalEngine()
        self.score_store = ScoreboardStore(database_path)
        self.paper_store = PaperTradeStore(database_path)
        self.paper_trader = PaperTradingEngine(self.paper_store)
        self.engine.scoreboard.extend(self.score_store.load_recent())
        self._persisted_score_keys = {
            (result["time"], result["horizon"]) for result in self.engine.scoreboard
        }

        self._lock = threading.RLock()
        self._stop_event = threading.Event()
        self._started = False
        self._last_periodic_update = 0.0
        self._feed_worker: FeedWorker | None = None
        self._service_thread: threading.Thread | None = None
        self._deepseek_thread: threading.Thread | None = None
        self._deepseek_last_request = 0.0
        self._deepseek_retry_at: float | None = None
        self.model_selected = (
            "deepseek" if deepseek_api_key_configured() else "local"
        )
        self.model_active = "local"
        self.paper_fee_bps = DEFAULT_ROUND_TRIP_FEE_BPS
        self.price: float | None = None
        self.price_time: float | None = None
        self.network_fees: dict = {}
        self.mempool: dict | None = None
        self.feed_status: dict[str, dict[str, object]] = {}
        self.transactions: list[dict] = []
        self._transaction_ids: set[str] = set()
        self.forecasts: dict[int, dict] = {}
        self._last_outlook_refresh_check: dict[int, float] = {}
        self.daily_target: dict = {
            "price": None,
            "detail": "Waiting for live BTC price and local history.",
            "projected_return": None,
        }
        self._daily_forecasts_saved: set[date] = set()
        self.service_error: str | None = None

    def start(self) -> None:
        with self._lock:
            if self._started:
                return
            self._started = True
            self._feed_worker = FeedWorker(self.events)
            self._service_thread = threading.Thread(
                target=self._run,
                name="dashboard-service",
                daemon=True,
            )
            self._feed_worker.start()
            self._service_thread.start()

    def health_status(self) -> tuple[bool, str | None]:
        with self._lock:
            return self._started, self.service_error

    def stop(self) -> None:
        self._stop_event.set()
        if self._feed_worker is not None:
            self._feed_worker.requestInterruption()
            self._feed_worker.join(timeout=6)
        if self._service_thread is not None:
            self._service_thread.join(timeout=6)
        if self._deepseek_thread is not None:
            self._deepseek_thread.join(timeout=2)
        with self._lock:
            self.score_store.close()
            self.paper_store.close()

    def set_settings(self, settings: dict) -> dict:
        with self._lock:
            provider = settings.get("provider")
            fee_bps = settings.get("fee_bps")
            if provider is not None:
                if provider not in ("local", "deepseek"):
                    raise ValueError("provider must be 'local' or 'deepseek'")
                if provider == "deepseek" and not deepseek_api_key_configured():
                    raise ValueError("DeepSeek requires DEEP_SEEK_API_KEY")
            if fee_bps is not None:
                try:
                    fee_bps = float(fee_bps)
                except (TypeError, ValueError) as error:
                    raise ValueError("fee_bps must be a number") from error
                if not 0 <= fee_bps <= 2000:
                    raise ValueError("fee_bps must be between 0 and 2000")

            if provider is not None:
                self.model_selected = provider
                self.model_active = "local"
                self._deepseek_retry_at = None
                if provider == "local":
                    for horizon, forecast in self.engine.latest_forecasts.items():
                        self.forecasts[horizon] = forecast
            if fee_bps is not None:
                self.paper_fee_bps = fee_bps
            return {
                "provider": self.model_selected,
                "fee_bps": self.paper_fee_bps,
            }

    def snapshot(self, window_seconds: int | None = 86400) -> dict:
        if window_seconds is not None and window_seconds not in OUTCOME_WINDOWS:
            raise ValueError("unsupported outcome time window")

        with self._lock:
            now = time.time()
            since_time = (
                now - window_seconds if window_seconds is not None else None
            )
            total, correct = self.score_store.summary()
            today = datetime.now(EASTERN_TIME).date()
            today_rows = self.score_store.load_for_date(
                today, limit=10, horizon_seconds=60
            )
            timeline = self.score_store.load_outcome_timeline(since_time)
            outcomes_by_interval = {
                interval: [
                    item for item in timeline if item["interval"] == interval
                ]
                for interval in ("1m", "5m", "15m", "1h", "5 PM ET")
            }
            outcome_summary = {
                interval: {
                    "correct": sum(item["correct"] for item in rows),
                    "total": len(rows),
                }
                for interval, rows in outcomes_by_interval.items()
            }
            score_rows = [
                self._score_row(result)
                for result in reversed(self.score_store.load_recent(40))
            ]
            today_total, today_correct = self.score_store.summary_for_date(
                today, horizon_seconds=60
            )
            trade_count, trade_wins, trade_pnl, open_count = (
                self.paper_store.summary()
            )
            recent_transactions = [
                {
                    "time": item["time"],
                    "txid": item["txid"],
                    "btc": item["btc"],
                    "fee_rate": item["fee_rate"],
                    "source": item["source"],
                }
                for item in self.transactions
            ]
            price_history = [
                (timestamp, price)
                for timestamp, price in self.engine.prices
                if timestamp >= now - 3600
            ]
            if len(price_history) > 1800:
                stride = (len(price_history) - 1) / 1799
                price_history = [
                    price_history[round(index * stride)] for index in range(1800)
                ]
            return {
                "generated_at": now,
                "started": self._started,
                "service_error": self.service_error,
                "connection": self._connection_status(),
                "feed_status": copy.deepcopy(self.feed_status),
                "price": {"value": self.price, "time": self.price_time},
                "price_history": [
                    {"time": timestamp, "price": price}
                    for timestamp, price in price_history
                ],
                "network": {
                    "fees": copy.deepcopy(self.network_fees),
                    "mempool": copy.deepcopy(self.mempool),
                },
                "forecasts": [
                    self._forecast_card(horizon, forecast)
                    for horizon, forecast in sorted(self.forecasts.items())
                ],
                "model": {
                    "selected": self.model_selected,
                    "active": self.model_active,
                },
                "signals": self._signal_summary(now),
                "transactions": recent_transactions,
                "scoreboard": {
                    "total": total,
                    "correct": correct,
                    "accuracy": correct / total if total else None,
                    "recent": score_rows,
                    "today": {"correct": today_correct, "total": today_total},
                    "today_1m": [
                        self._score_row(result) for result in today_rows
                    ],
                },
                "timeline": timeline,
                "outcome_summary": outcome_summary,
                "daily_target": copy.deepcopy(self.daily_target),
                "paper": {
                    "completed": trade_count,
                    "wins": trade_wins,
                    "pnl": trade_pnl,
                    "open_count": open_count,
                    "fee_bps": self.paper_fee_bps,
                    "trades": self.paper_store.recent(),
                },
            }

    def _run(self) -> None:
        while not self._stop_event.is_set():
            try:
                self._tick()
                with self._lock:
                    self.service_error = None
            except Exception as error:
                LOGGER.exception("Dashboard background update failed")
                with self._lock:
                    self.service_error = f"{type(error).__name__}: {error}"
            self._stop_event.wait(0.25)

    def _tick(self) -> None:
        with self._lock:
            self._drain_events()
            now = time.time()
            forecasts = self.engine.update_all(now)
            if forecasts:
                self._handle_forecasts(forecasts, now)
            self._refresh_outlooks(now)
            if now - self._last_periodic_update >= 1:
                self._persist_new_scores()
                self._refresh_daily_target(now)
                self._last_periodic_update = now

    def _drain_events(self) -> None:
        for _ in range(1000):
            try:
                event = self.events.get_nowait()
            except queue.Empty:
                break
            kind = event.get("type")
            if kind == "deepseek_forecasts":
                if self.model_selected != "deepseek":
                    continue
                self.model_active = "deepseek"
                for forecast in event["forecasts"]:
                    self.forecasts[forecast["horizon_seconds"]] = forecast
                    self._last_outlook_refresh_check[
                        forecast["horizon_seconds"]
                    ] = float(forecast["time"])
                self._set_status(
                    "forecast model", "connected", "DeepSeek estimates · uncalibrated"
                )
                continue
            if kind == "deepseek_error":
                self._handle_deepseek_error(event["detail"])
                continue
            self.engine.handle(event)
            if kind == "price":
                self.price = float(event["price"])
                self.price_time = float(event["time"])
            elif kind == "candles" and event.get("candles"):
                candle = event["candles"][-1]
                self.price = float(candle["close"])
                self.price_time = float(candle["time"]) + 60
            elif kind == "fees":
                self.network_fees = event["fees"]
            elif kind == "mempool":
                self.mempool = {
                    "count": event.get("count", 0),
                    "vsize": event.get("vsize", 0.0),
                    "time": event["time"],
                }
            elif kind == "transaction":
                self._add_transaction(event)
            elif kind == "status":
                self._set_status(
                    event["source"], event["state"], event.get("detail", "")
                )

    def _set_status(self, source: str, state: str, detail: str) -> None:
        self.feed_status[source] = {
            "state": state,
            "detail": detail[:240],
            "time": time.time(),
        }

    def _connection_status(self) -> str:
        coinbase = self.feed_status.get("Coinbase")
        if coinbase and coinbase["state"] == "connected":
            return "LIVE FEEDS"
        if coinbase and coinbase["state"] in ("error", "reconnecting"):
            return f"COINBASE {coinbase['state'].upper()}"
        return "CONNECTING FEEDS"

    def _add_transaction(self, event: dict) -> None:
        txid = event["txid"]
        if txid in self._transaction_ids:
            return
        self._transaction_ids.add(txid)
        self.transactions.insert(
            0,
            {
                "time": event["time"],
                "txid": txid,
                "btc": event["btc"],
                "fee_rate": event["fee_rate"],
                "source": event.get("source", "mempool.space"),
            },
        )
        if len(self.transactions) > 400:
            removed = self.transactions.pop()
            self._transaction_ids.discard(removed["txid"])

    def _handle_forecasts(self, forecasts: list[dict], now: float) -> None:
        for forecast in forecasts:
            horizon = int(forecast["horizon_seconds"])
            if horizon in (60, 3600) or self.model_active == "local":
                self.forecasts[horizon] = forecast
                self._last_outlook_refresh_check[horizon] = float(forecast["time"])

        eligible = [
            forecast
            for forecast in forecasts
            if forecast["horizon_seconds"] in (300, 900)
        ]
        if eligible and now - self.engine.order_book_time <= 10:
            self.paper_trader.evaluate(
                eligible,
                self.engine.best_bid,
                self.engine.best_ask,
                self.paper_fee_bps,
            )

        if self.model_selected != "deepseek":
            return
        forecast_15m = self.engine.latest_forecasts.get(900)
        forecast_5m = self.engine.latest_forecasts.get(300)
        if forecast_15m is None or forecast_5m is None:
            return
        scheduled_time = float(forecast_15m["time"])
        if (
            scheduled_time <= self._deepseek_last_request
            or now - scheduled_time > 2
            or now - self._deepseek_last_request < 90
            or (
                self._deepseek_retry_at is not None
                and now < self._deepseek_retry_at
            )
            or (self._deepseek_thread is not None and self._deepseek_thread.is_alive())
        ):
            return
        request = {
            "time": scheduled_time,
            "price": forecast_15m["price"],
            "recent_prices": [
                {"time": timestamp, "price": price}
                for timestamp, price in list(self.engine.prices)[-16:]
            ],
            "features": forecast_5m["features"],
            "baseline": {
                horizon // 60: {
                    "predicted": forecast["predicted"],
                    "low": forecast["low"],
                    "high": forecast["high"],
                }
                for horizon, forecast in self.engine.latest_forecasts.items()
            },
        }
        self._deepseek_last_request = scheduled_time
        self._set_status("forecast model", "connecting", "DeepSeek request pending")
        self._deepseek_thread = threading.Thread(
            target=self._request_deepseek,
            args=(request,),
            name="deepseek-request",
            daemon=True,
        )
        self._deepseek_thread.start()

    def _refresh_outlooks(self, now: float) -> None:
        for horizon, interval in OUTLOOK_REFRESH_SECONDS.items():
            published = self.forecasts.get(horizon)
            if published is None:
                continue
            last_check = self._last_outlook_refresh_check.get(
                horizon, float(published["time"])
            )
            if now - last_check < interval:
                continue
            self._last_outlook_refresh_check[horizon] = now

            candidate = self.engine.preview_forecast(now, horizon)
            price_change = abs(candidate["predicted"] - published["predicted"]) / max(
                abs(published["price"]), 1.0
            )
            direction_changed = candidate["direction"] != published["direction"]
            if not direction_changed and price_change < SIGNIFICANT_PRICE_CHANGE:
                continue

            reasons = []
            if direction_changed:
                reasons.append(
                    f"direction changed {published['direction']} to {candidate['direction']}"
                )
            if price_change >= SIGNIFICANT_PRICE_CHANGE:
                reasons.append(
                    f"predicted price shifted {price_change:.2%} "
                    f"({published['predicted']:,.2f} to {candidate['predicted']:,.2f} USD)"
                )
            candidate["provider"] = "Local"
            candidate["updated_at"] = now
            candidate["update_note"] = (
                "; ".join(reasons)
                + ". Recomputed from the latest BTC price, recent returns, "
                "transaction flow, fee pressure, and order-book imbalance."
            )
            self.engine.record_forecast(candidate)
            self.forecasts[horizon] = candidate

    def scenario_likelihood(
        self, target_time: datetime, target_price: float, direction: str
    ) -> dict:
        with self._lock:
            if self.price is None or self.price <= 0:
                raise RuntimeError("live BTC price is not available yet")
            minutes_to_target = (target_time.timestamp() - time.time()) / 60
            if minutes_to_target <= 0:
                raise ValueError("target date and time must be in the future")
            estimate = self.engine.scenario_probability(
                self.price, target_price, minutes_to_target, direction
            )
            volatility_percent = estimate["volatility"] * 100
            explanation = (
                "Estimated with a zero-drift lognormal approximation: recent "
                f"minute-price volatility ({estimate['sample_count']} intervals) "
                f"scales to about {volatility_percent:.2f}% over "
                f"{minutes_to_target:.1f} minutes. The threshold's distance from "
                "the live price relative to that range determines the probability. "
                "This is a rough, uncalibrated estimate; it assumes no sustained "
                "upward or downward drift and excludes news and sudden market moves."
            )
            return {
                "probability_percent": estimate["probability"] * 100,
                "current_price": self.price,
                "target_price": target_price,
                "direction": direction,
                "target_time": target_time.isoformat(),
                "minutes_to_target": minutes_to_target,
                "explanation": explanation,
            }

    def _request_deepseek(self, request: dict) -> None:
        try:
            forecasts = asyncio.run(request_deepseek_forecasts(request))
        except DeepSeekAPIError as error:
            detail = (
                "DeepSeek rate limit reached (HTTP 429)"
                if error.status == 429
                else str(error)
            )
            self.events.put({"type": "deepseek_error", "detail": detail})
        except (aiohttp.ClientError, asyncio.TimeoutError) as error:
            self.events.put(
                {
                    "type": "deepseek_error",
                    "detail": f"DeepSeek network request failed: {type(error).__name__}",
                }
            )
        except (ValueError, KeyError) as error:
            self.events.put(
                {
                    "type": "deepseek_error",
                    "detail": f"DeepSeek forecast rejected: {error}",
                }
            )
        else:
            self.events.put(
                {"type": "deepseek_forecasts", "forecasts": forecasts}
            )

    def _handle_deepseek_error(self, detail: str) -> None:
        self.model_active = "local"
        has_auth_error = "HTTP 401" in detail or "HTTP 403" in detail
        self._deepseek_retry_at = (
            time.time() + 300
            if deepseek_api_key_configured() and not has_auth_error
            else None
        )
        self._set_status(
            "forecast model",
            "error",
            f"{detail}; using local model"
            + ("; retry in 5 minutes" if self._deepseek_retry_at else ""),
        )

    def _persist_new_scores(self) -> None:
        for result in self.engine.scoreboard:
            key = (result["time"], result["horizon"])
            if key not in self._persisted_score_keys:
                self.score_store.save(result)
                self._persisted_score_keys.add(key)

    def _refresh_daily_target(self, now: float) -> None:
        if not self.engine.prices:
            return
        for pending in self.score_store.pending_daily_target_forecasts(now):
            target_time = pending["target_time"]
            actual = next(
                (
                    (sample_time, price)
                    for sample_time, price in self.engine.prices
                    if sample_time >= target_time
                ),
                None,
            )
            if actual is not None:
                self.score_store.resolve_daily_target_forecast(
                    date.fromisoformat(pending["target_date"]),
                    actual[0],
                    actual[1],
                )

        current_price = self.engine.prices[-1][1]
        eastern_now = datetime.fromtimestamp(now, tz=EASTERN_TIME)
        target_day = eastern_now.date()
        if eastern_now.time() >= datetime_time(17, 0):
            target_day += timedelta(days=1)
        target_time = datetime.combine(
            target_day, datetime_time(17, 0), tzinfo=EASTERN_TIME
        )
        remaining_minutes = max((target_time.timestamp() - now) / 60, 0)
        summaries = self.score_store.local_history_outcomes()
        historical_rates = [
            result["average_minute_return"]
            for result in summaries
            if result["total"] and result["average_minute_return"] is not None
        ]
        if not historical_rates:
            self.daily_target = {
                "price": None,
                "detail": (
                    f"BTC ${current_price:,.2f} now · target is 5 PM ET "
                    f"({remaining_minutes / 60:.1f} hours away)."
                ),
                "projected_return": None,
            }
            return

        average_minute_return = sum(historical_rates) / len(historical_rates)
        projected_return = average_minute_return * remaining_minutes
        target_price = current_price * (1 + projected_return)
        count = sum(result["total"] for result in summaries)
        self.daily_target = {
            "price": target_price,
            "detail": (
                f"From ${current_price:,.2f} now · {projected_return:+.2%} rough move · "
                f"{remaining_minutes / 60:.1f}h to 5 PM ET. Based on average realized "
                f"per-minute returns across {count:,} local 5m/15m forecasts; "
                "highly approximate, not a calibrated daily forecast."
            ),
            "projected_return": projected_return,
        }
        if target_day not in self._daily_forecasts_saved:
            self.score_store.save_daily_target_forecast(
                target_day,
                now,
                target_time.timestamp(),
                current_price,
                target_price,
            )
            self._daily_forecasts_saved.add(target_day)

    def _signal_summary(self, now: float) -> dict:
        recent = [
            row for row in self.engine.transactions if now - row[0] <= 300
        ]
        btc_moved = sum(row[1] for row in recent)
        whales = sum(row[1] > 50 for row in recent)
        congestion = self.engine.congestion
        trend = "—"
        if len(congestion) > 1:
            delta = congestion[-1][1] - congestion[0][1]
            direction = "up" if delta > 0 else "down" if delta < 0 else "steady"
            trend = f"{direction} {abs(delta) / 1e6:.1f} M vB"
        return {
            "tx_rate": len(recent) / 300,
            "btc_moved": btc_moved,
            "whales": whales,
            "mempool_trend": trend,
            "fee_pressure": self.engine.fees.get("fastestFee"),
            "order_book_imbalance": self.engine.order_book_imbalance,
        }

    @staticmethod
    def _forecast_card(horizon: int, forecast: dict) -> dict:
        return {
            "horizon_seconds": horizon,
            "label": HORIZON_LABELS.get(horizon, f"{horizon // 60} min"),
            "time": float(forecast["time"]),
            "target_time": float(forecast["horizon"]),
            "price": float(forecast["price"]),
            "predicted": float(forecast["predicted"]),
            "low": float(forecast["low"]),
            "high": float(forecast["high"]),
            "direction": forecast["direction"],
            "confidence": float(forecast["confidence"]),
            "trained": bool(forecast["trained"]),
            "provider": forecast.get("provider", "Local"),
            "updated_at": forecast.get("updated_at"),
            "update_note": forecast.get("update_note"),
        }

    @staticmethod
    def _score_row(result: dict) -> dict:
        return {
            "time": float(result["time"]),
            "horizon": float(result["horizon"]),
            "price": float(result["price"]),
            "predicted": float(result["predicted"]),
            "actual": float(result["actual"]),
            "direction": result["direction"],
            "confidence": float(result["confidence"]),
            "correct": bool(result["correct"]),
        }
