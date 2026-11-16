"""Small logistic models for short-horizon BTC direction estimates."""

from collections import deque

import numpy as np
from sklearn.linear_model import LogisticRegression
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler

FEATURE_COUNT = 8


class OnlineForecastModel:
    """Fits historical candle examples and refreshes as forecasts resolve."""

    def __init__(self) -> None:
        self._features: deque[np.ndarray] = deque(maxlen=2000)
        self._returns: deque[float] = deque(maxlen=2000)
        self._pipeline = make_pipeline(
            StandardScaler(),
            LogisticRegression(C=0.5, max_iter=500, random_state=7),
        )
        self._is_fitted = False

    @property
    def is_fitted(self) -> bool:
        return self._is_fitted

    def warm_start(
        self, candles: list[dict[str, float]], horizon_minutes: int = 5
    ) -> int:
        """Create supervised examples from one-minute candle closes."""
        if horizon_minutes <= 0:
            raise ValueError("horizon_minutes must be positive")
        self._features.clear()
        self._returns.clear()
        closes = [candle["close"] for candle in candles]
        for index in range(15, len(candles) - horizon_minutes):
            reference = closes[index]
            future = closes[index + horizon_minutes]
            if reference <= 0 or future <= 0:
                continue
            features = np.zeros(FEATURE_COUNT, dtype=float)
            features[0] = reference / closes[index - 1] - 1
            features[1] = reference / closes[index - 5] - 1
            features[2] = reference / closes[index - 15] - 1
            self._features.append(features)
            self._returns.append(future / reference - 1)
        self._fit()
        return len(self._returns)

    def add_labeled_example(self, features: list[float], actual_return: float) -> None:
        if len(features) != FEATURE_COUNT or not np.isfinite(actual_return):
            return
        self._features.append(np.asarray(features, dtype=float))
        self._returns.append(float(actual_return))
        self._fit()

    def forecast(
        self,
        features: list[float],
        fallback_return: float,
        volatility: float,
    ) -> tuple[float, float]:
        """Return expected fractional return and probability of an upward move."""
        scale = max(volatility, 0.0001)
        if not self._is_fitted:
            logit = float(np.clip(fallback_return / scale, -30, 30))
            probability_up = 1 / (1 + np.exp(-logit))
            return float(fallback_return), float(probability_up)

        vector = np.asarray(features, dtype=float).reshape(1, -1)
        probability_up = float(self._pipeline.predict_proba(vector)[0, 1])
        positive = [value for value in self._returns if value > 0]
        negative = [value for value in self._returns if value <= 0]
        mean_up = float(np.mean(positive)) if positive else scale
        mean_down = float(np.mean(negative)) if negative else -scale
        expected = probability_up * mean_up + (1 - probability_up) * mean_down
        return float(np.clip(expected, -3 * scale, 3 * scale)), probability_up

    def _fit(self) -> None:
        if len(self._returns) < 10:
            self._is_fitted = False
            return
        labels = np.asarray([value > 0 for value in self._returns], dtype=int)
        if len(np.unique(labels)) < 2:
            self._is_fitted = False
            return
        self._pipeline.fit(np.asarray(self._features), labels)
        self._is_fitted = True
