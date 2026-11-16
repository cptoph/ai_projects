import tempfile
import time
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from app import create_app
from dashboard import (
    OUTLOOK_REFRESH_SECONDS,
    DashboardService,
    EASTERN_TIME,
)


class FlaskDashboardTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        database_path = Path(self.temp_dir.name) / "dashboard.sqlite3"
        self.service = DashboardService(database_path)
        self.client = create_app(self.service).test_client()

    def tearDown(self) -> None:
        self.service.stop()
        self.temp_dir.cleanup()

    def test_dashboard_and_default_state_are_served(self) -> None:
        page = self.client.get("/")
        state = self.client.get("/api/state")

        self.assertEqual(page.status_code, 200)
        self.assertIn(b"BTC-USD Market Terminal", page.data)
        self.assertIn(b"Today", page.data)
        self.assertIn(b"1-minute forecasts", page.data)
        self.assertIn(b"Scenario likelihood", page.data)
        self.assertEqual(state.status_code, 200)
        self.assertIn("timeline", state.json)
        self.assertIn("price_history", state.json)
        self.assertEqual(state.json["model"]["active"], "local")

    def test_state_rejects_unsupported_time_windows(self) -> None:
        response = self.client.get("/api/state?window=42")

        self.assertEqual(response.status_code, 400)
        self.assertIn("error", response.json)

    def test_snapshot_mutations_do_not_change_service_state(self) -> None:
        self.service.feed_status["Coinbase"] = {
            "state": "connected",
            "detail": "Live",
            "time": 1.0,
        }
        snapshot = self.service.snapshot()

        snapshot["feed_status"]["Coinbase"]["state"] = "error"
        snapshot["daily_target"]["detail"] = "changed"
        snapshot["network"]["fees"]["fastestFee"] = 99

        self.assertEqual(
            self.service.feed_status["Coinbase"]["state"], "connected"
        )
        self.assertNotEqual(self.service.daily_target["detail"], "changed")
        self.assertNotIn("fastestFee", self.service.network_fees)

    def test_today_summary_counts_one_minute_forecasts_only(self) -> None:
        now = datetime.now(EASTERN_TIME).replace(
            hour=12, minute=0, second=0, microsecond=0
        ).timestamp()
        for offset, horizon_seconds, correct in (
            (0, 60, True),
            (1, 900, False),
        ):
            timestamp = now + offset
            self.service.score_store.save(
                {
                    "time": timestamp,
                    "horizon": timestamp + horizon_seconds,
                    "price": 65_000.0,
                    "predicted": 65_100.0,
                    "low": 64_900.0,
                    "high": 65_300.0,
                    "actual": 65_200.0,
                    "direction": "UP",
                    "confidence": 70.0,
                    "correct": correct,
                    "return": 0.003,
                    "features": [],
                }
            )

        scoreboard = self.service.snapshot()["scoreboard"]

        self.assertEqual(scoreboard["today"], {"correct": 1, "total": 1})
        self.assertEqual(len(scoreboard["today_1m"]), 1)

    def test_settings_validate_and_update_paper_fee(self) -> None:
        response = self.client.post(
            "/api/settings",
            json={"fee_bps": 75},
        )
        invalid = self.client.post(
            "/api/settings",
            json={"fee_bps": 2001},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json["fee_bps"], 75)
        self.assertEqual(invalid.status_code, 400)

    def test_settings_reject_unknown_forecast_provider(self) -> None:
        response = self.client.post(
            "/api/settings",
            json={"provider": "unknown"},
        )

        self.assertEqual(response.status_code, 400)
        self.assertIn("provider", response.json["error"])

    def test_feed_events_are_reflected_in_the_flask_snapshot(self) -> None:
        now = time.time()
        self.service.events.put(
            {"type": "price", "price": 65_000.0, "time": now}
        )
        self.service.events.put(
            {
                "type": "book",
                "imbalance": 0.25,
                "bid": 64_999.0,
                "ask": 65_001.0,
                "time": now,
            }
        )
        self.service.events.put(
            {
                "type": "fees",
                "fees": {
                    "fastestFee": 12,
                    "halfHourFee": 8,
                    "hourFee": 5,
                },
                "time": now,
            }
        )

        self.service._tick()
        state = self.client.get("/api/state?window=3600").json

        self.assertEqual(state["price"]["value"], 65_000.0)
        self.assertEqual(state["price_history"][-1]["price"], 65_000.0)
        self.assertEqual(state["network"]["fees"]["fastestFee"], 12)
        self.assertEqual(state["signals"]["order_book_imbalance"], 0.25)


    def test_scenario_likelihood_uses_live_price_and_returns_explanation(self) -> None:
        now = time.time()
        for minute in range(31):
            self.service.events.put(
                {
                    "type": "price",
                    "price": 65_000.0 + minute,
                    "time": now - (30 - minute) * 60,
                }
            )
        self.service._tick()
        target_time = datetime.now(EASTERN_TIME) + timedelta(hours=1)
        response = self.client.post(
            "/api/scenario-likelihood",
            json={
                "target_datetime": target_time.isoformat(),
                "direction": "above",
                "target_price": self.service.price,
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertAlmostEqual(response.json["probability_percent"], 50.0)
        self.assertIn("zero-drift", response.json["explanation"])
        self.assertIn("volatility", response.json["explanation"])

    def test_scenario_likelihood_rejects_invalid_condition(self) -> None:
        response = self.client.post(
            "/api/scenario-likelihood",
            json={
                "target_datetime": (
                    datetime.now(EASTERN_TIME) + timedelta(hours=1)
                ).isoformat(),
                "direction": "sideways",
                "target_price": 65_000,
            },
        )

        self.assertEqual(response.status_code, 400)
        self.assertIn("direction", response.json["error"])

    def test_significant_live_refresh_updates_forecast_with_reason(self) -> None:
        now = time.time()
        initial = {
            "time": now - 300,
            "horizon": now,
            "horizon_seconds": 300,
            "price": 65_000.0,
            "predicted": 65_000.0,
            "low": 64_900.0,
            "high": 65_100.0,
            "direction": "UP",
            "confidence": 60.0,
            "trained": True,
            "features": [0.0] * 8,
        }
        candidate = {
            **initial,
            "time": now,
            "horizon": now + 300,
            "predicted": 65_200.0,
            "direction": "DOWN",
        }
        self.service.forecasts[300] = initial
        self.service._last_outlook_refresh_check[300] = now - 150

        with patch.object(
            self.service.engine, "preview_forecast", return_value=candidate
        ):
            self.service._refresh_outlooks(now)

        snapshot_forecast = next(
            item
            for item in self.service.snapshot()["forecasts"]
            if item["horizon_seconds"] == 300
        )
        self.assertEqual(snapshot_forecast["direction"], "DOWN")
        self.assertIn("direction changed", snapshot_forecast["update_note"])
        self.assertIn("predicted price shifted", snapshot_forecast["update_note"])
        self.assertEqual(len(self.service.engine.pending[300]), 1)

    def test_outlooks_are_checked_on_each_configured_refresh_interval(self) -> None:
        now = time.time()
        for horizon, interval in OUTLOOK_REFRESH_SECONDS.items():
            self.service.forecasts[horizon] = {
                "time": now - interval,
                "horizon": now,
                "horizon_seconds": horizon,
                "price": 65_000.0,
                "predicted": 65_000.0,
                "low": 64_900.0,
                "high": 65_100.0,
                "direction": "UP",
                "confidence": 60.0,
                "trained": True,
                "features": [0.0] * 8,
            }
            self.service._last_outlook_refresh_check[horizon] = now - interval

        def candidate_for(candidate_time: float, horizon: int) -> dict:
            return {
                **self.service.forecasts[horizon],
                "time": candidate_time,
                "horizon": candidate_time + horizon,
                "predicted": 65_200.0,
                "direction": "UP",
            }

        with patch.object(
            self.service.engine, "preview_forecast", side_effect=candidate_for
        ) as preview:
            self.service._refresh_outlooks(now)

        self.assertEqual(
            {call.args[1] for call in preview.call_args_list},
            set(OUTLOOK_REFRESH_SECONDS),
        )
        self.assertTrue(
            all(
                self.service.forecasts[horizon].get("update_note")
                for horizon in OUTLOOK_REFRESH_SECONDS
            )
        )


if __name__ == "__main__":
    unittest.main()
