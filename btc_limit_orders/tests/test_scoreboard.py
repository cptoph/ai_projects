import tempfile
import sqlite3
import unittest
from datetime import datetime, time, timedelta
from pathlib import Path

from scoreboard import ScoreboardStore


class ScoreboardStoreTests(unittest.TestCase):
    def test_results_persist_and_restore_across_store_instances(self) -> None:
        result = {
            "time": 1_800_000_000.0,
            "horizon": 1_800_000_300.0,
            "price": 60_000.0,
            "predicted": 60_100.0,
            "low": 59_900.0,
            "high": 60_300.0,
            "actual": 60_200.0,
            "direction": "UP",
            "confidence": 72.5,
            "correct": True,
            "return": 200 / 60_000,
            "features": [0.1, 0.2, 0.3],
        }
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "scoreboard.sqlite3"
            first = ScoreboardStore(database)
            self.assertTrue(first.save(result))
            self.assertFalse(first.save(result))
            first.close()

            second = ScoreboardStore(database)
            restored = second.load_recent()
            self.assertEqual(restored, [result])
            self.assertEqual(second.summary(), (1, 1))
            second.close()

    def test_legacy_timestamp_key_migrates_to_time_and_horizon_key(self) -> None:
        today = datetime.now().date()
        timestamp = datetime.combine(today, time(hour=12)).timestamp()
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "legacy.sqlite3"
            connection = sqlite3.connect(database)
            connection.execute(
                """
                CREATE TABLE predictions (
                    prediction_time REAL PRIMARY KEY,
                    horizon REAL NOT NULL,
                    price REAL NOT NULL,
                    predicted REAL NOT NULL,
                    low REAL NOT NULL,
                    high REAL NOT NULL,
                    actual REAL NOT NULL,
                    direction TEXT NOT NULL,
                    confidence REAL NOT NULL,
                    correct INTEGER NOT NULL,
                    return REAL NOT NULL,
                    features TEXT NOT NULL
                )
                """
            )
            connection.execute(
                """
                INSERT INTO predictions VALUES (
                    ?, ?, 60000, 60100, 59900, 60300, 60200,
                    'UP', 72.5, 1, 0.0033, '[]'
                )
                """,
                (timestamp, timestamp + 300),
            )
            connection.commit()
            connection.close()

            store = ScoreboardStore(database)
            try:
                second_horizon = {
                    "time": timestamp,
                    "horizon": timestamp + 900,
                    "price": 60_000.0,
                    "predicted": 59_900.0,
                    "low": 59_800.0,
                    "high": 60_000.0,
                    "actual": 59_950.0,
                    "direction": "DOWN",
                    "confidence": 70.0,
                    "correct": True,
                    "return": -50 / 60_000,
                    "features": [],
                }
                self.assertTrue(store.save(second_horizon))
                self.assertEqual(store.summary(), (2, 2))
                self.assertEqual(len(store.load_for_date(today)), 1)
            finally:
                store.close()

    def test_date_queries_include_only_local_calendar_day(self) -> None:
        today = datetime.now().date()
        today_time = datetime.combine(today, time(hour=12)).timestamp()
        yesterday_time = datetime.combine(
            today - timedelta(days=1), time(hour=12)
        ).timestamp()

        def result(
            timestamp: float,
            correct: bool,
            horizon_seconds: int = 900,
            direction: str = "UP",
            actual_return: float = 0.01,
        ) -> dict:
            return {
                "time": timestamp,
                "horizon": timestamp + horizon_seconds,
                "price": 60_000.0,
                "predicted": 60_100.0,
                "low": 59_900.0,
                "high": 60_300.0,
                "actual": 60_200.0,
                "direction": direction,
                "confidence": 72.5,
                "correct": correct,
                "return": actual_return,
                "features": [],
            }

        with tempfile.TemporaryDirectory() as directory:
            store = ScoreboardStore(Path(directory) / "scoreboard.sqlite3")
            try:
                store.save(result(yesterday_time, False, actual_return=-0.02))
                store.save(result(today_time, True))
                store.save(
                    result(
                        today_time + 1,
                        False,
                        horizon_seconds=300,
                        actual_return=-0.5,
                    )
                )
                store.save(
                    result(
                        today_time + 2,
                        True,
                        direction="DOWN",
                        actual_return=0.02,
                    )
                )
                store.save(
                    result(
                        today_time + 3,
                        True,
                        horizon_seconds=60,
                    )
                )

                self.assertEqual(store.summary_for_date(today), (4, 3))
                self.assertEqual(store.summary_for_date(today, 60), (1, 1))
                rows = store.load_for_date(today)
                self.assertEqual(len(rows), 2)
                self.assertEqual(
                    [row["time"] for row in rows],
                    [today_time + 2, today_time],
                )
                self.assertAlmostEqual(
                    sum(
                        20 * (row["price"] - row["predicted"]) / row["price"]
                        for row in rows
                    )
                    / len(rows),
                    -20 / 600,
                )
                self.assertEqual(len(store.load_for_date(today, limit=1)), 1)
                self.assertEqual(
                    len(store.load_for_date(today, horizon_seconds=60)), 1
                )
            finally:
                store.close()

    def test_local_history_outcomes_summarize_each_horizon_and_signal_pnl(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = ScoreboardStore(Path(directory) / "scoreboard.sqlite3")
            try:
                base_time = 1_800_000_000.0
                results = (
                    (300, "UP", 60_100.0, 0.002, True),
                    (900, "DOWN", 59_900.0, -0.01, True),
                    (900, "UP", 60_100.0, 0.005, False),
                    (3600, "DOWN", 59_900.0, 0.005, False),
                )
                for index, (horizon, direction, predicted, actual_return, correct) in enumerate(results):
                    timestamp = base_time + index
                    price = 60_000.0
                    self.assertTrue(
                        store.save(
                            {
                                "time": timestamp,
                                "horizon": timestamp + horizon,
                                "price": price,
                                "predicted": predicted,
                                "low": predicted - 100,
                                "high": predicted + 100,
                                "actual": price * (1 + actual_return),
                                "direction": direction,
                                "confidence": 70.0,
                                "correct": correct,
                                "return": actual_return,
                                "features": [],
                            }
                        )
                    )

                five_minute, fifteen_minute, hourly = (
                    store.local_history_outcomes()
                )
                self.assertEqual((five_minute["total"], five_minute["correct"]), (1, 1))
                self.assertAlmostEqual(five_minute["accuracy"], 1.0)
                self.assertAlmostEqual(
                    five_minute["average_minute_return"], 0.002 / 5
                )
                self.assertAlmostEqual(five_minute["forecast_pnl"], 20 / 600)
                self.assertAlmostEqual(five_minute["realized_pnl"], 0.04)
                self.assertEqual(
                    (fifteen_minute["total"], fifteen_minute["correct"]), (2, 1)
                )
                self.assertAlmostEqual(fifteen_minute["accuracy"], 0.5)
                self.assertAlmostEqual(
                    fifteen_minute["average_minute_return"],
                    (-0.01 + 0.005) / 2 / 15,
                )
                self.assertAlmostEqual(fifteen_minute["forecast_pnl"], 20 / 600)
                self.assertAlmostEqual(fifteen_minute["realized_pnl"], 0.15)
                self.assertEqual((hourly["total"], hourly["correct"]), (1, 0))
                self.assertAlmostEqual(hourly["accuracy"], 0)
                self.assertAlmostEqual(hourly["forecast_pnl"], 20 / 600)
                self.assertAlmostEqual(hourly["realized_pnl"], -0.1)
                self.assertAlmostEqual(hourly["average_minute_return"], 0.005 / 60)
            finally:
                store.close()

    def test_outcome_timeline_filters_and_includes_resolved_five_pm_targets(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = ScoreboardStore(Path(directory) / "scoreboard.sqlite3")
            try:
                base_time = 1_800_000_000.0
                for timestamp, horizon_seconds, correct in (
                    (base_time + 5, 60, False),
                    (base_time - 1000, 300, True),
                    (base_time + 10, 900, False),
                    (base_time + 20, 3600, True),
                ):
                    store.save(
                        {
                            "time": timestamp,
                            "horizon": timestamp + horizon_seconds,
                            "price": 100.0,
                            "predicted": 101.0,
                            "low": 99.0,
                            "high": 102.0,
                            "actual": 100.5,
                            "direction": "UP",
                            "confidence": 70.0,
                            "correct": correct,
                            "return": 0.005,
                            "features": [],
                        }
                    )

                target_date = datetime.now().date()
                target_time = base_time + 500
                self.assertTrue(
                    store.save_daily_target_forecast(
                        target_date,
                        base_time + 100,
                        target_time,
                        100.0,
                        110.0,
                    )
                )
                self.assertFalse(
                    store.save_daily_target_forecast(
                        target_date,
                        base_time + 200,
                        target_time,
                        100.0,
                        105.0,
                    )
                )
                self.assertEqual(
                    store.pending_daily_target_forecasts(target_time),
                    [
                        {
                            "target_date": target_date.isoformat(),
                            "target_time": target_time,
                        }
                    ],
                )
                self.assertFalse(
                    store.resolve_daily_target_forecast(
                        target_date, target_time - 1, 90.0
                    )
                )
                self.assertTrue(
                    store.resolve_daily_target_forecast(
                        target_date, target_time + 1, 90.0
                    )
                )
                self.assertFalse(
                    store.resolve_daily_target_forecast(
                        target_date, target_time + 2, 95.0
                    )
                )

                all_outcomes = store.load_outcome_timeline()
                self.assertEqual(
                    [outcome["interval"] for outcome in all_outcomes],
                    ["5m", "1m", "15m", "1h", "5 PM ET"],
                )
                self.assertEqual(all_outcomes[-1]["correct"], False)
                self.assertEqual(
                    [outcome["interval"] for outcome in
                     store.load_outcome_timeline(base_time)],
                    ["1m", "15m", "1h", "5 PM ET"],
                )
            finally:
                store.close()


if __name__ == "__main__":
    unittest.main()
