import tempfile
import unittest
from pathlib import Path

from paper_trading import PaperTradeStore, PaperTradingEngine


class PaperTradingTests(unittest.TestCase):
    def test_entry_is_capped_and_profitable_exit_persists(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "paper.sqlite3"
            store = PaperTradeStore(database)
            engine = PaperTradingEngine(store)
            entry = {
                "time": 1_800_000_000.0,
                "horizon": 1_800_000_300.0,
                "horizon_seconds": 300,
                "direction": "UP",
                "confidence": 95.0,
            }

            engine.evaluate([entry], bid=99.9, ask=100.0, fee_bps=50)
            opened = store.open_trades()

            self.assertEqual(len(opened), 1)
            self.assertAlmostEqual(opened[0]["entry_notional"] + opened[0]["entry_fee"], 10)
            self.assertLessEqual(opened[0]["entry_notional"] + opened[0]["entry_fee"], 10)
            with self.assertRaises(ValueError):
                store.open_trade(entry, ask=100.0, fee_bps=50, budget=10.01)

            exit_signal = {
                **entry,
                "time": 1_800_000_100.0,
                "direction": "DOWN",
                "confidence": 95.0,
            }
            engine.evaluate([exit_signal], bid=101.0, ask=101.1, fee_bps=50)
            summary = store.summary()
            self.assertEqual(summary[0:2], (1, 1))
            self.assertEqual(summary[3], 0)
            store.close()

            restored = PaperTradeStore(database)
            trade = restored.recent(1)[0]
            self.assertEqual(trade["state"], "CLOSED")
            self.assertEqual(trade["exit_reason"], "opposite signal")
            self.assertGreater(trade["pnl"], 0)
            restored.close()

    def test_low_confidence_and_down_signals_do_not_open_positions(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = PaperTradeStore(Path(directory) / "paper.sqlite3")
            engine = PaperTradingEngine(store)
            base = {
                "time": 1_800_000_000.0,
                "horizon": 1_800_000_300.0,
                "horizon_seconds": 300,
                "direction": "UP",
                "confidence": 89.9,
            }
            down = {**base, "direction": "DOWN", "confidence": 99.0}

            engine.evaluate([base, down], bid=99.9, ask=100.0)

            self.assertEqual(store.open_trades(), [])
            store.close()

    def test_open_trade_closes_when_its_forecast_horizon_expires(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = PaperTradeStore(Path(directory) / "paper.sqlite3")
            engine = PaperTradingEngine(store)
            entry = {
                "time": 1_800_000_000.0,
                "horizon": 1_800_000_300.0,
                "horizon_seconds": 300,
                "direction": "UP",
                "confidence": 95.0,
            }
            engine.evaluate([entry], bid=99.9, ask=100.0)

            expiry = {
                **entry,
                "time": entry["horizon"],
                "confidence": 70.0,
            }
            engine.evaluate([expiry], bid=100.5, ask=100.6)

            trade = store.recent(1)[0]
            self.assertEqual(trade["state"], "CLOSED")
            self.assertEqual(trade["exit_reason"], "horizon expired")
            store.close()


if __name__ == "__main__":
    unittest.main()
