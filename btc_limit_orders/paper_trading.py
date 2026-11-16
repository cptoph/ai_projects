"""Persistent, simulation-only BTC paper trades."""

import os
import sqlite3
import time
from pathlib import Path

MAX_TRADE_BUDGET_USD = 10.0
ENTRY_CONFIDENCE = 90.0
DEFAULT_ROUND_TRIP_FEE_BPS = 50.0


class PaperTradeStore:
    """Persist simulated positions and their realized outcomes."""

    def __init__(self, path: Path | None = None) -> None:
        configured_path = os.environ.get("BTC_DATABASE_PATH")
        self.path = path or (
            Path(configured_path).expanduser()
            if configured_path
            else Path(__file__).with_name("btc_outlook.sqlite3")
        )
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(self.path, check_same_thread=False)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute(
            """
            CREATE TABLE IF NOT EXISTS paper_trades (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                horizon_seconds INTEGER NOT NULL,
                entry_time REAL NOT NULL,
                exit_deadline REAL NOT NULL,
                entry_price REAL NOT NULL,
                quantity REAL NOT NULL,
                entry_notional REAL NOT NULL,
                entry_fee REAL NOT NULL,
                budget REAL NOT NULL,
                fee_bps REAL NOT NULL,
                state TEXT NOT NULL,
                exit_time REAL,
                exit_price REAL,
                exit_fee REAL,
                pnl REAL,
                exit_reason TEXT
            )
            """
        )
        self.connection.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS one_open_paper_trade_per_horizon
            ON paper_trades (horizon_seconds)
            WHERE state = 'OPEN'
            """
        )
        self.connection.commit()

    def open_trade(
        self,
        forecast: dict,
        ask: float,
        fee_bps: float,
        budget: float = MAX_TRADE_BUDGET_USD,
    ) -> int:
        if (
            ask <= 0
            or budget <= 0
            or budget > MAX_TRADE_BUDGET_USD
            or fee_bps < 0
        ):
            raise ValueError(
                f"ask must be positive, budget must be in (0, "
                f"{MAX_TRADE_BUDGET_USD}], and fee_bps must be nonnegative"
            )
        horizon = int(forecast["horizon_seconds"])
        if horizon <= 0:
            raise ValueError("forecast horizon must be positive")

        per_side_fee_rate = fee_bps / 20_000
        entry_notional = budget / (1 + per_side_fee_rate)
        entry_fee = entry_notional * per_side_fee_rate
        quantity = entry_notional / ask
        cursor = self.connection.execute(
            """
            INSERT INTO paper_trades (
                horizon_seconds, entry_time, exit_deadline, entry_price, quantity,
                entry_notional, entry_fee, budget, fee_bps, state
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'OPEN')
            """,
            (
                horizon,
                float(forecast["time"]),
                float(forecast["horizon"]),
                ask,
                quantity,
                entry_notional,
                entry_fee,
                budget,
                fee_bps,
            ),
        )
        self.connection.commit()
        return int(cursor.lastrowid)

    def close_trade(
        self, trade_id: int, timestamp: float, bid: float, reason: str
    ) -> None:
        if bid <= 0:
            raise ValueError("bid must be positive")
        trade = self.connection.execute(
            "SELECT quantity, entry_notional, entry_fee, fee_bps "
            "FROM paper_trades WHERE id = ? AND state = 'OPEN'",
            (trade_id,),
        ).fetchone()
        if trade is None:
            raise ValueError(f"paper trade {trade_id} is not open")

        exit_notional = trade["quantity"] * bid
        exit_fee = exit_notional * trade["fee_bps"] / 20_000
        pnl = exit_notional - exit_fee - trade["entry_notional"] - trade["entry_fee"]
        self.connection.execute(
            """
            UPDATE paper_trades
            SET state = 'CLOSED', exit_time = ?, exit_price = ?, exit_fee = ?,
                pnl = ?, exit_reason = ?
            WHERE id = ? AND state = 'OPEN'
            """,
            (timestamp, bid, exit_fee, pnl, reason, trade_id),
        )
        self.connection.commit()

    def open_trades(self) -> list[dict]:
        rows = self.connection.execute(
            "SELECT * FROM paper_trades WHERE state = 'OPEN' ORDER BY entry_time"
        ).fetchall()
        return [dict(row) for row in rows]

    def recent(self, limit: int = 50) -> list[dict]:
        rows = self.connection.execute(
            "SELECT * FROM paper_trades ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(row) for row in rows]

    def summary(self) -> tuple[int, int, float, int]:
        row = self.connection.execute(
            """
            SELECT COUNT(*) AS completed,
                   COALESCE(SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END), 0) AS wins,
                   COALESCE(SUM(pnl), 0) AS pnl
            FROM paper_trades
            WHERE state = 'CLOSED'
            """
        ).fetchone()
        open_count = self.connection.execute(
            "SELECT COUNT(*) FROM paper_trades WHERE state = 'OPEN'"
        ).fetchone()[0]
        return int(row["completed"]), int(row["wins"]), float(row["pnl"]), int(open_count)

    def close(self) -> None:
        self.connection.close()


class PaperTradingEngine:
    """Simulate long entries and exits from the forecast without broker access."""

    def __init__(self, store: PaperTradeStore) -> None:
        self.store = store

    def evaluate(
        self,
        forecasts: list[dict],
        bid: float,
        ask: float,
        fee_bps: float = DEFAULT_ROUND_TRIP_FEE_BPS,
    ) -> None:
        if bid <= 0 or ask <= 0 or bid > ask:
            return
        if fee_bps < 0:
            raise ValueError("fee_bps must be nonnegative")

        open_by_horizon = {
            trade["horizon_seconds"]: trade for trade in self.store.open_trades()
        }
        for forecast in forecasts:
            horizon = int(forecast["horizon_seconds"])
            trade = open_by_horizon.get(horizon)
            if trade is not None:
                reversed_signal = (
                    forecast["direction"] == "DOWN"
                    and forecast["confidence"] >= ENTRY_CONFIDENCE
                )
                expired = forecast["time"] >= trade["exit_deadline"]
                if reversed_signal or expired:
                    reason = "opposite signal" if reversed_signal else "horizon expired"
                    self.store.close_trade(
                        trade["id"], forecast["time"], bid, reason
                    )
                    del open_by_horizon[horizon]
            elif (
                forecast["direction"] == "UP"
                and forecast["confidence"] >= ENTRY_CONFIDENCE
            ):
                self.store.open_trade(
                    forecast,
                    ask,
                    fee_bps,
                    budget=MAX_TRADE_BUDGET_USD,
                )
                open_by_horizon[horizon] = {
                    "id": None,
                    "exit_deadline": forecast["horizon"],
                }
