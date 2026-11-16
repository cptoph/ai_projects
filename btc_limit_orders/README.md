# Bitcoin Live Outlook

A Flask-served BTC-USD dashboard for live Coinbase market data, Bitcoin network
activity, 1-minute / 5-minute / 15-minute / 1-hour forecasts, and forecast outcome
history. The browser UI uses a high-resolution dark market-terminal design with
amber price visualization and green/red directional results.

The live price chart plots Coinbase BTC-USD samples with price and time axes, range
high/low, percentage move, and 5-minute / 15-minute / 1-hour views. The outcome
timeline shows forecast-to-target bars with separate lanes for one-minute,
5-minute, 15-minute, hourly, and variable-horizon 5 PM ET estimates; choose a time
window above the timeline. The 5 PM estimate is scored against the first stored BTC
price at or after 5 PM ET and is rough, not a true 24-hour forecast.

The dashboard also includes a live transaction feed, network and order-book signals,
today's resolved one-minute forecasts and outcomes, scoreboard accuracy, an optional DeepSeek
forecast provider, and persistent BTC paper trades. Paper trades are simulations
only; the app does not connect to a broker or submit orders. Model scores are not
calibrated probabilities and none of the forecasts are financial advice.
Outlooks are checked for material changes every 2.5 minutes (5-minute outlook),
5 minutes (15-minute outlook), and 15 minutes (hourly outlook). A changed direction
or at least a 0.25% predicted-price shift updates the card with the reason.
**Tools → Scenario Likelihood** estimates the chance BTC will be above or below a
user-entered USD threshold at a future date and time (entered in Eastern Time). It
uses recent minute-price volatility and a zero-drift approximation; it is rough and
uncalibrated, not financial advice.

## Run locally on Windows

PowerShell:

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe main.py
```

Open [http://127.0.0.1:5000](http://127.0.0.1:5000). To use DeepSeek, set
`DEEP_SEEK_API_KEY` in the project-root `.env` file or the process environment.
The key is read from the environment and is not included in the Docker image. If no
key is configured, the local logistic model is used.

## Run in Docker

Install Docker Desktop, then from the project directory run:

```powershell
docker compose up --build -d
```

Open [http://127.0.0.1:5000](http://127.0.0.1:5000). The compose file binds the
dashboard to localhost by default. To enable DeepSeek, set `DEEP_SEEK_API_KEY` in
the project-root `.env` file before starting the container. The SQLite database is
stored in the persistent `btc-outlook-data` Docker volume and survives container
rebuilds. To stop the service:

```powershell
docker compose down
```

The container runs as a non-root user and does not copy `.env` or the local SQLite
database into the image. The dashboard has no authentication; keep it on localhost
unless you put it behind an appropriately secured reverse proxy.

## Data and behavior

The app loads the latest sixty minutes of one-minute Coinbase candles, then consumes
public WebSocket feeds from Coinbase, mempool.space, and Blockchain.com. Network
statistics and recommended fees are refreshed over HTTPS. Public endpoints can
rate-limit or change behavior; feed connection state and errors appear in the UI.

Forecasts are generated on fixed clock boundaries. Resolved predictions are saved in
`btc_outlook.sqlite3` (or `BTC_DATABASE_PATH` when configured) and restored at
startup. The paper-trade simulator uses Coinbase order-book bid/ask as estimated
fills, caps each entry at $10, and lets the user adjust the assumed round-trip fee.
Paper trades do not place broker orders.

DeepSeek uses the `deepseek-chat` model and requests are made only on the 15-minute
forecast boundary. DeepSeek estimates are advisory and explicitly uncalibrated;
paper trades continue to use local model forecasts. If a request fails, the app
falls back to the local model and retries after a delay. API usage may incur charges.

## Tests

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests
```
