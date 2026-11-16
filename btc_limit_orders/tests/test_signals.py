import unittest

from signals import SignalEngine


class SignalEngineTests(unittest.TestCase):
    def test_warmup_and_forecast(self) -> None:
        engine = SignalEngine()
        start = 1_800_000_000.0
        candles = [
            {
                "time": start + index * 60,
                "open": 60_000 + index,
                "high": 60_010 + index,
                "low": 59_990 + index,
                "close": 60_000 + index,
                "volume": 1.0,
            }
            for index in range(60)
        ]
        engine.handle({"type": "candles", "candles": candles})

        forecast = engine.update(start + 60 * 60)

        self.assertIsNotNone(forecast)
        self.assertEqual(forecast["horizon"] - forecast["time"], 300)
        self.assertGreater(forecast["predicted"], 0)
        self.assertGreaterEqual(forecast["confidence"], 50)
        self.assertLessEqual(forecast["confidence"], 100)

    def test_builds_distinct_five_fifteen_and_hourly_forecasts(self) -> None:
        engine = SignalEngine()
        start = 1_800_000_000.0
        candles = [
            {
                "time": start + index * 60,
                "open": 60_000 + index,
                "high": 60_010 + index,
                "low": 59_990 + index,
                "close": 60_000 + index,
                "volume": 1.0,
            }
            for index in range(60)
        ]
        engine.handle({"type": "candles", "candles": candles})

        forecasts = engine.update_all(start + 60 * 60)

        self.assertEqual(
            {forecast["horizon_seconds"] for forecast in forecasts},
            {60, 300, 900, 3600},
        )
        self.assertEqual(
            {forecast["horizon"] - forecast["time"] for forecast in forecasts},
            {60, 300, 900, 3600},
        )
        self.assertIsNot(engine.models[300], engine.models[900])
        self.assertIsNot(engine.models[900], engine.models[3600])
        self.assertIsNot(engine.models[60], engine.models[300])

    def test_forecasts_are_emitted_on_fixed_horizon_boundaries(self) -> None:
        engine = SignalEngine()
        start = 1_800_000_000.0
        engine.handle({"type": "price", "time": start, "price": 60_000})

        self.assertEqual(
            {item["horizon_seconds"] for item in engine.update_all(start + 60)},
            {60},
        )
        self.assertEqual(
            {item["horizon_seconds"] for item in engine.update_all(start + 300)},
            {60, 300},
        )
        self.assertEqual(
            {item["horizon_seconds"] for item in engine.update_all(start + 900)},
            {60, 300, 900},
        )
        forecasts = engine.update_all(start + 3600)
        self.assertEqual(
            {item["horizon_seconds"] for item in forecasts},
            {60, 300, 900, 3600},
        )
        for forecast in forecasts:
            self.assertEqual(forecast["time"] % forecast["horizon_seconds"], 0)

    def test_one_minute_forecast_resolves_against_next_minute_price(self) -> None:
        engine = SignalEngine()
        start = 1_800_000_000.0
        engine.handle({"type": "price", "time": start, "price": 60_000.0})

        forecast = next(
            item for item in engine.update_all(start + 60)
            if item["horizon_seconds"] == 60
        )
        actual = 60_100.0 if forecast["direction"] == "UP" else 59_900.0
        engine.handle({"type": "price", "time": start + 120, "price": actual})
        engine.update_all(start + 120)

        result = next(
            item for item in engine.scoreboard if item["horizon"] - item["time"] == 60
        )
        self.assertEqual(result["actual"], actual)
        self.assertTrue(result["correct"])

    def test_rolling_transaction_metrics_and_whale_threshold(self) -> None:
        engine = SignalEngine()
        now = 1_800_000_000.0
        engine.handle(
            {
                "type": "transaction",
                "time": now,
                "btc": 51.0,
                "fee_rate": 40.0,
            }
        )
        engine.handle({"type": "mempool", "time": now, "vsize": 1_000_000})
        engine.handle({"type": "fees", "fees": {"fastestFee": 60}})
        engine.handle({"type": "book", "imbalance": 0.4})
        engine.handle({"type": "price", "time": now, "price": 60_000})

        features = engine._features(now)

        self.assertEqual(features[3], 1 / 300 / 10)
        self.assertEqual(features[5], 0.1)
        self.assertEqual(features[7], 0.4)

    def test_scenario_probabilities_follow_threshold_and_direction(self) -> None:
        engine = SignalEngine()
        start = 1_800_000_000.0
        for minute in range(31):
            engine._add_price(start + minute * 60, 60_000 + minute * 10)

        near_above = engine.scenario_probability(
            60_300, 60_400, 60, "above"
        )["probability"]
        far_above = engine.scenario_probability(
            60_300, 61_000, 60, "above"
        )["probability"]
        near_below = engine.scenario_probability(
            60_300, 60_400, 60, "below"
        )["probability"]

        self.assertGreater(near_above, far_above)
        self.assertAlmostEqual(near_above + near_below, 1.0)


if __name__ == "__main__":
    unittest.main()
