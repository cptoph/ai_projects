"""Optional DeepSeek forecast provider; API keys are read from the environment."""

import json
import os
from pathlib import Path

import aiohttp
from dotenv import load_dotenv

DEEPSEEK_API_URL = "https://api.deepseek.com/chat/completions"
DEEPSEEK_MODEL = "deepseek-chat"

load_dotenv(Path(__file__).with_name(".env"))


def get_deepseek_api_key() -> str:
    """Return the configured DeepSeek key, supporting the legacy variable name."""
    return (
        os.environ.get("DEEP_SEEK_API_KEY")
        or os.environ.get("DEEPSEEK_API_KEY", "")
    ).strip()


def deepseek_api_key_configured() -> bool:
    return bool(get_deepseek_api_key())


def deepseek_auth_headers(api_key: str) -> dict[str, str]:
    """Build the bearer authorization header required by DeepSeek."""
    api_key = api_key.strip()
    if not api_key:
        raise ValueError("DEEP_SEEK_API_KEY is empty")
    return {"Authorization": f"Bearer {api_key}"}


class DeepSeekAPIError(ValueError):
    """An HTTP error returned by the DeepSeek API."""

    def __init__(self, status: int) -> None:
        self.status = status
        super().__init__(f"DeepSeek API returned HTTP {status}")


def parse_deepseek_forecasts(content: str, request: dict) -> list[dict]:
    """Validate DeepSeek's JSON forecast and normalize it for the dashboard."""
    try:
        result = json.loads(content)
    except json.JSONDecodeError as error:
        raise ValueError("DeepSeek returned invalid JSON") from error

    raw_forecasts = result.get("forecasts")
    if not isinstance(raw_forecasts, list):
        raise ValueError("DeepSeek response must contain a forecasts list")
    by_horizon: dict[int, dict] = {}
    for item in raw_forecasts:
        if not isinstance(item, dict):
            raise ValueError("DeepSeek forecast entries must be objects")
        try:
            horizon_minutes = int(item["horizon_minutes"])
            expected_return = float(item["expected_return"])
            probability_up = float(item["probability_up"])
        except (KeyError, TypeError, ValueError) as error:
            raise ValueError("DeepSeek forecast fields are invalid") from error
        if horizon_minutes not in (5, 15) or horizon_minutes in by_horizon:
            raise ValueError("DeepSeek must return one forecast for 5 and 15 minutes")
        if not -0.03 <= expected_return <= 0.03:
            raise ValueError("DeepSeek expected return is outside the allowed range")
        if not 0 <= probability_up <= 1:
            raise ValueError("DeepSeek probability_up must be between 0 and 1")
        by_horizon[horizon_minutes] = {
            "expected_return": expected_return,
            "probability_up": probability_up,
        }

    if set(by_horizon) != {5, 15}:
        raise ValueError("DeepSeek must return both 5- and 15-minute forecasts")

    now = float(request["time"])
    price = float(request["price"])
    forecasts = []
    for horizon_minutes in (5, 15):
        values = by_horizon[horizon_minutes]
        horizon_seconds = horizon_minutes * 60
        expected_return = values["expected_return"]
        probability_up = values["probability_up"]
        predicted_price = price * (1 + expected_return)
        baseline = request["baseline"][horizon_minutes]
        spread = max(
            abs(float(baseline["high"]) - float(baseline["predicted"])),
            price * 0.00025,
        )
        forecasts.append(
            {
                "time": now,
                "horizon": now + horizon_seconds,
                "horizon_seconds": horizon_seconds,
                "price": price,
                "predicted": predicted_price,
                "low": predicted_price - spread,
                "high": predicted_price + spread,
                "direction": "UP" if expected_return >= 0 else "DOWN",
                "confidence": 50 + 50 * abs(2 * probability_up - 1),
                "probability_up": probability_up,
                "trained": True,
                "features": [],
                "provider": "DeepSeek",
            }
        )
    return forecasts


async def request_deepseek_forecasts(request: dict) -> list[dict]:
    """Fetch one DeepSeek forecast without tying the provider to a UI toolkit."""
    api_key = get_deepseek_api_key()
    if not api_key:
        raise ValueError("DEEP_SEEK_API_KEY is not configured")
    timeout = aiohttp.ClientTimeout(total=8)
    headers = deepseek_auth_headers(api_key)
    async with aiohttp.ClientSession(timeout=timeout, headers=headers) as session:
        return await _request_forecast(session, request)


async def _request_forecast(
    session: aiohttp.ClientSession, request: dict
) -> list[dict]:
    prompt = {
        "time": request["time"],
        "price": request["price"],
        "recent_prices": request["recent_prices"],
        "features": request["features"],
        "baseline": request["baseline"],
    }
    payload = {
        "model": DEEPSEEK_MODEL,
        "temperature": 0,
        "max_tokens": 300,
        "response_format": {"type": "json_object"},
        "messages": [
            {
                "role": "system",
                "content": (
                    "Estimate BTC-USD direction using only the supplied numerical "
                    "market data. Return JSON only with a forecasts array containing "
                    "exactly two objects: horizon_minutes (5 or 15), expected_return "
                    "(decimal fractional return, from -0.03 to 0.03), and probability_up "
                    "(number from 0 to 1). These are uncalibrated estimates, not "
                    "investment advice."
                ),
            },
            {"role": "user", "content": json.dumps(prompt, separators=(",", ":"))},
        ],
    }
    async with session.post(DEEPSEEK_API_URL, json=payload) as response:
        if response.status >= 400:
            raise DeepSeekAPIError(response.status)
        body = await response.json()
    try:
        content = body["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as error:
        raise ValueError("DeepSeek API response has no message content") from error
    if not isinstance(content, str):
        raise ValueError("DeepSeek API message content is not text")
    return parse_deepseek_forecasts(content, request)
