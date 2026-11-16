"""Durable local storage for resolved prediction results."""

import json
import os
import sqlite3
from datetime import date, datetime, time, timedelta
from pathlib import Path


class ScoreboardStore:
    """Keep the complete prediction history in a workspace SQLite database."""

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
            CREATE TABLE IF NOT EXISTS predictions (
                prediction_time REAL NOT NULL,
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
                features TEXT NOT NULL,
                PRIMARY KEY (prediction_time, horizon)
            )
            """
        )
        self.connection.execute(
            """
            CREATE TABLE IF NOT EXISTS daily_target_predictions (
                target_date TEXT PRIMARY KEY,
                prediction_time REAL NOT NULL,
                target_time REAL NOT NULL,
                price REAL NOT NULL,
                predicted REAL NOT NULL,
                actual_time REAL,
                actual REAL,
                correct INTEGER
            )
            """
        )
        self.connection.commit()
        primary_key = {
            row["name"]: row["pk"]
            for row in self.connection.execute("PRAGMA table_info(predictions)")
            if row["pk"]
        }
        if primary_key != {"prediction_time": 1} and primary_key != {
            "prediction_time": 1,
            "horizon": 2,
        }:
            raise RuntimeError("Unsupported predictions table primary key")
        if primary_key == {"prediction_time": 1}:
            self.connection.execute("BEGIN")
            try:
                self.connection.execute(
                    """
                    CREATE TABLE predictions_migrated (
                        prediction_time REAL NOT NULL,
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
                        features TEXT NOT NULL,
                        PRIMARY KEY (prediction_time, horizon)
                    )
                    """
                )
                self.connection.execute(
                    """
                    INSERT INTO predictions_migrated
                    SELECT prediction_time, horizon, price, predicted, low, high,
                           actual, direction, confidence, correct, return, features
                    FROM predictions
                    """
                )
                self.connection.execute("DROP TABLE predictions")
                self.connection.execute(
                    "ALTER TABLE predictions_migrated RENAME TO predictions"
                )
            except Exception:
                self.connection.rollback()
                raise
            else:
                self.connection.commit()

    def save(self, result: dict) -> bool:
        """Insert a resolved result once; return whether it was newly stored."""
        cursor = self.connection.execute(
            """
            INSERT OR IGNORE INTO predictions (
                prediction_time, horizon, price, predicted, low, high, actual,
                direction, confidence, correct, return, features
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                result["time"],
                result["horizon"],
                result["price"],
                result["predicted"],
                result["low"],
                result["high"],
                result["actual"],
                result["direction"],
                result["confidence"],
                int(result["correct"]),
                result["return"],
                json.dumps(result["features"]),
            ),
        )
        self.connection.commit()
        return cursor.rowcount == 1

    def load_recent(self, limit: int = 40) -> list[dict]:
        rows = self.connection.execute(
            """
            SELECT prediction_time, horizon, price, predicted, low, high, actual,
                   direction, confidence, correct, return, features
            FROM predictions
            ORDER BY prediction_time DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
        return [
            {
                "time": row["prediction_time"],
                "horizon": row["horizon"],
                "price": row["price"],
                "predicted": row["predicted"],
                "low": row["low"],
                "high": row["high"],
                "actual": row["actual"],
                "direction": row["direction"],
                "confidence": row["confidence"],
                "correct": bool(row["correct"]),
                "return": row["return"],
                "features": json.loads(row["features"]),
            }
            for row in reversed(rows)
        ]

    def summary(self) -> tuple[int, int]:
        row = self.connection.execute(
            "SELECT COUNT(*) AS total, COALESCE(SUM(correct), 0) AS correct "
            "FROM predictions"
        ).fetchone()
        return int(row["total"]), int(row["correct"])

    def local_history_outcomes(self, stake: float = 20.0) -> list[dict]:
        if stake <= 0:
            raise ValueError("stake must be positive")
        summaries = []
        for horizon_seconds in (300, 900, 3600):
            row = self.connection.execute(
                """
                SELECT COUNT(*) AS total,
                       COALESCE(SUM(correct), 0) AS correct,
                       AVG("return") AS average_return,
                       AVG(
                           CASE WHEN direction = 'UP' THEN
                               ? * (predicted - price) / price
                           ELSE
                               ? * (price - predicted) / price
                           END
                       ) AS forecast_pnl,
                       AVG(
                           CASE WHEN direction = 'UP' THEN
                               ? * "return"
                           ELSE
                               -? * "return"
                           END
                       ) AS realized_pnl
                FROM predictions
                WHERE ABS((horizon - prediction_time) - ?) < 0.001
                """,
                (stake, stake, stake, stake, horizon_seconds),
            ).fetchone()
            total = int(row["total"])
            summaries.append(
                {
                    "horizon_seconds": horizon_seconds,
                    "total": total,
                    "correct": int(row["correct"]),
                    "accuracy": row["correct"] / total if total else None,
                    "average_minute_return": (
                        float(row["average_return"]) / (horizon_seconds / 60)
                        if row["average_return"] is not None
                        else None
                    ),
                    "forecast_pnl": (
                        float(row["forecast_pnl"])
                        if row["forecast_pnl"] is not None
                        else None
                    ),
                    "realized_pnl": (
                        float(row["realized_pnl"])
                        if row["realized_pnl"] is not None
                        else None
                    ),
                }
            )
        return summaries

    def save_daily_target_forecast(
        self,
        target_date: date,
        prediction_time: float,
        target_time: float,
        price: float,
        predicted: float,
    ) -> bool:
        """Save the first estimate made for a particular 5 PM ET target."""
        if target_time <= prediction_time:
            raise ValueError("target_time must be after prediction_time")
        if price <= 0 or predicted <= 0:
            raise ValueError("prices must be positive")
        cursor = self.connection.execute(
            """
            INSERT OR IGNORE INTO daily_target_predictions (
                target_date, prediction_time, target_time, price, predicted
            ) VALUES (?, ?, ?, ?, ?)
            """,
            (
                target_date.isoformat(),
                prediction_time,
                target_time,
                price,
                predicted,
            ),
        )
        if cursor.rowcount != 1:
            self.connection.rollback()
            return False
        self.connection.commit()
        return True

    def pending_daily_target_forecasts(self, now: float) -> list[dict]:
        rows = self.connection.execute(
            """
            SELECT target_date, target_time
            FROM daily_target_predictions
            WHERE actual IS NULL AND target_time <= ?
            ORDER BY target_time
            """,
            (now,),
        ).fetchall()
        return [
            {"target_date": row["target_date"], "target_time": row["target_time"]}
            for row in rows
        ]

    def resolve_daily_target_forecast(
        self, target_date: date, actual_time: float, actual_price: float
    ) -> bool:
        """Resolve a saved estimate from the first observed price at/after target."""
        if actual_price <= 0:
            raise ValueError("actual_price must be positive")
        cursor = self.connection.execute(
            """
            UPDATE daily_target_predictions
            SET actual_time = ?,
                actual = ?,
                correct = CASE
                    WHEN (predicted >= price) = (? >= price) THEN 1
                    ELSE 0
                END
            WHERE target_date = ? AND actual IS NULL AND target_time <= ?
            """,
            (
                actual_time,
                actual_price,
                actual_price,
                target_date.isoformat(),
                actual_time,
            ),
        )
        self.connection.commit()
        return cursor.rowcount == 1

    def load_outcome_timeline(self, since_time: float | None = None) -> list[dict]:
        interval_filter = """
            (ABS((horizon - prediction_time) - 60) < 0.001
             OR ABS((horizon - prediction_time) - 300) < 0.001
             OR ABS((horizon - prediction_time) - 900) < 0.001
             OR ABS((horizon - prediction_time) - 3600) < 0.001)
        """
        time_filter = " AND prediction_time >= ?" if since_time is not None else ""
        parameters = (since_time,) if since_time is not None else ()
        rows = self.connection.execute(
            f"""
            SELECT prediction_time, horizon AS target_time,
                   CASE
                       WHEN ABS((horizon - prediction_time) - 60) < 0.001
                           THEN '1m'
                       WHEN ABS((horizon - prediction_time) - 300) < 0.001
                           THEN '5m'
                       WHEN ABS((horizon - prediction_time) - 900) < 0.001
                           THEN '15m'
                       ELSE '1h'
                   END AS interval,
                   correct
            FROM predictions
            WHERE {interval_filter}{time_filter}
            """,
            parameters,
        ).fetchall()
        outcomes = [
            {
                "time": row["prediction_time"],
                "target_time": row["target_time"],
                "interval": row["interval"],
                "correct": bool(row["correct"]),
            }
            for row in rows
        ]

        daily_time_filter = (
            " AND prediction_time >= ?" if since_time is not None else ""
        )
        daily_rows = self.connection.execute(
            f"""
            SELECT prediction_time, target_time, correct
            FROM daily_target_predictions
            WHERE correct IS NOT NULL{daily_time_filter}
            """,
            parameters,
        ).fetchall()
        outcomes.extend(
            {
                "time": row["prediction_time"],
                "target_time": row["target_time"],
                "interval": "5 PM ET",
                "correct": bool(row["correct"]),
            }
            for row in daily_rows
        )
        return sorted(outcomes, key=lambda outcome: outcome["time"])

    def summary_for_date(
        self, day: date, horizon_seconds: int | None = None
    ) -> tuple[int, int]:
        if horizon_seconds is not None and horizon_seconds <= 0:
            raise ValueError("horizon_seconds must be positive")
        start = datetime.combine(day, time.min).timestamp()
        end = datetime.combine(day + timedelta(days=1), time.min).timestamp()
        horizon_filter = ""
        parameters: tuple[float, ...] = (start, end)
        if horizon_seconds is not None:
            horizon_filter = (
                " AND ABS((horizon - prediction_time) - ?) < 0.001"
            )
            parameters += (horizon_seconds,)
        row = self.connection.execute(
            f"""
            SELECT COUNT(*) AS total, COALESCE(SUM(correct), 0) AS correct
            FROM predictions
            WHERE prediction_time >= ? AND prediction_time < ?
            {horizon_filter}
            """,
            parameters,
        ).fetchone()
        return int(row["total"]), int(row["correct"])

    def load_for_date(
        self, day: date, limit: int = 10, horizon_seconds: int = 900
    ) -> list[dict]:
        if limit <= 0:
            raise ValueError("limit must be positive")
        if horizon_seconds <= 0:
            raise ValueError("horizon_seconds must be positive")
        start = datetime.combine(day, time.min).timestamp()
        end = datetime.combine(day + timedelta(days=1), time.min).timestamp()
        rows = self.connection.execute(
            """
            SELECT prediction_time, horizon, price, predicted, actual,
                   direction, confidence, correct
            FROM predictions
            WHERE prediction_time >= ? AND prediction_time < ?
              AND ABS((horizon - prediction_time) - ?) < 0.001
            ORDER BY prediction_time DESC
            LIMIT ?
            """,
            (start, end, horizon_seconds, limit),
        ).fetchall()
        return [
            {
                "time": row["prediction_time"],
                "horizon": row["horizon"],
                "price": row["price"],
                "predicted": row["predicted"],
                "actual": row["actual"],
                "direction": row["direction"],
                "confidence": row["confidence"],
                "correct": bool(row["correct"]),
            }
            for row in rows
        ]

    def close(self) -> None:
        self.connection.close()
