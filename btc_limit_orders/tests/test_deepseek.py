import json
import os
import unittest
from unittest.mock import patch

from deepseek import (
    DeepSeekAPIError,
    deepseek_auth_headers,
    get_deepseek_api_key,
    parse_deepseek_forecasts,
)


class DeepSeekForecastTests(unittest.TestCase):
    def test_builds_bearer_header_from_trimmed_api_key(self) -> None:
        self.assertEqual(
            deepseek_auth_headers("  test-key  "),
            {"Authorization": "Bearer test-key"},
        )
        with self.assertRaisesRegex(ValueError, "DEEP_SEEK_API_KEY is empty"):
            deepseek_auth_headers(" ")

    def test_reads_new_and_legacy_environment_variable_names(self) -> None:
        with patch.dict(
            os.environ,
            {
                "DEEP_SEEK_API_KEY": "  preferred-key  ",
                "DEEPSEEK_API_KEY": "legacy-key",
            },
            clear=True,
        ):
            self.assertEqual(get_deepseek_api_key(), "preferred-key")

        with patch.dict(
            os.environ, {"DEEPSEEK_API_KEY": "  legacy-key  "}, clear=True
        ):
            self.assertEqual(get_deepseek_api_key(), "legacy-key")

    def test_api_error_preserves_http_status(self) -> None:
        error = DeepSeekAPIError(429)
        self.assertEqual(error.status, 429)
        self.assertIn("429", str(error))

    def test_normalizes_both_horizons(self) -> None:
        request = {
            "time": 1_800_000_000.0,
            "price": 60_000.0,
            "baseline": {
                5: {"predicted": 60_100.0, "low": 59_900.0, "high": 60_300.0},
                15: {"predicted": 60_200.0, "low": 59_700.0, "high": 60_700.0},
            },
        }
        content = json.dumps(
            {
                "forecasts": [
                    {
                        "horizon_minutes": 5,
                        "expected_return": 0.001,
                        "probability_up": 0.7,
                    },
                    {
                        "horizon_minutes": 15,
                        "expected_return": -0.002,
                        "probability_up": 0.3,
                    },
                ]
            }
        )

        forecasts = parse_deepseek_forecasts(content, request)

        self.assertEqual([item["horizon_seconds"] for item in forecasts], [300, 900])
        self.assertEqual([item["direction"] for item in forecasts], ["UP", "DOWN"])
        self.assertAlmostEqual(forecasts[0]["predicted"], 60_060.0)
        self.assertEqual(forecasts[0]["provider"], "DeepSeek")

    def test_rejects_missing_horizon_and_invalid_probability(self) -> None:
        request = {
            "time": 1_800_000_000.0,
            "price": 60_000.0,
            "baseline": {
                5: {"predicted": 60_100.0, "low": 59_900.0, "high": 60_300.0},
                15: {"predicted": 60_200.0, "low": 59_700.0, "high": 60_700.0},
            },
        }
        content = json.dumps(
            {
                "forecasts": [
                    {
                        "horizon_minutes": 5,
                        "expected_return": 0.001,
                        "probability_up": 1.2,
                    }
                ]
            }
        )

        with self.assertRaises(ValueError):
            parse_deepseek_forecasts(content, request)


if __name__ == "__main__":
    unittest.main()
