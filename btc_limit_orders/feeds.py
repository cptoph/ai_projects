"""Async public-market feeds isolated in a background thread."""

import asyncio
import json
import queue
import threading
import time
from datetime import datetime, timezone

import aiohttp
import websockets

COINBASE_WS = "wss://ws-feed.exchange.coinbase.com"
COINBASE_CANDLES = "https://api.exchange.coinbase.com/products/BTC-USD/candles"
MEMPOOL_WS = "wss://mempool.space/api/v1/ws"
MEMPOOL_FEES = "https://mempool.space/api/v1/fees/recommended"
MEMPOOL_STATUS = "https://mempool.space/api/mempool"
BLOCKCHAIN_WS = "wss://ws.blockchain.info/inv"
SATOSHIS_PER_BTC = 100_000_000


class FeedWorker(threading.Thread):
    """Own asyncio streams and publish plain dict events to a queue."""

    def __init__(self, events: queue.Queue[dict]) -> None:
        super().__init__(name="market-feed-worker", daemon=True)
        self.events = events
        self._interruption_requested = threading.Event()

    def requestInterruption(self) -> None:
        self._interruption_requested.set()

    def isInterruptionRequested(self) -> bool:
        return self._interruption_requested.is_set()

    def run(self) -> None:
        try:
            asyncio.run(self._run())
        except Exception as error:
            self._publish(
                {"type": "status", "source": "worker", "state": "error", "detail": str(error)}
            )

    async def _run(self) -> None:
        timeout = aiohttp.ClientTimeout(total=15)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            tasks = [
                asyncio.create_task(self._load_candles(session)),
                asyncio.create_task(self._mempool_stream()),
                asyncio.create_task(self._blockchain_stream()),
                asyncio.create_task(self._coinbase_stream()),
                asyncio.create_task(self._network_stats(session)),
            ]
            while not self.isInterruptionRequested():
                await asyncio.sleep(0.25)
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    def _publish(self, event: dict) -> None:
        self.events.put(event)

    async def _load_candles(self, session: aiohttp.ClientSession) -> None:
        while not self.isInterruptionRequested():
            end = datetime.now(timezone.utc)
            params = {
                "granularity": 60,
                "start": datetime.fromtimestamp(
                    end.timestamp() - 60 * 60, timezone.utc
                ).isoformat(timespec="seconds"),
                "end": end.isoformat(timespec="seconds"),
            }
            try:
                async with session.get(COINBASE_CANDLES, params=params) as response:
                    response.raise_for_status()
                    rows = await response.json()
                candles = [
                    {
                        "time": float(row[0]),
                        "low": float(row[1]),
                        "high": float(row[2]),
                        "open": float(row[3]),
                        "close": float(row[4]),
                        "volume": float(row[5]),
                    }
                    for row in rows
                    if isinstance(row, list) and len(row) >= 6
                ]
                candles.sort(key=lambda candle: candle["time"])
                self._publish({"type": "candles", "candles": candles})
                self._publish(
                    {
                        "type": "status",
                        "source": "warmup",
                        "state": "connected",
                        "detail": f"Loaded {len(candles)} one-minute candles",
                    }
                )
                return
            except (
                aiohttp.ClientError,
                asyncio.TimeoutError,
                ValueError,
                IndexError,
            ) as error:
                self._publish(
                    {
                        "type": "status",
                        "source": "warmup",
                        "state": "error",
                        "detail": f"Could not load Coinbase candles: {error}",
                    }
                )
                await self._sleep_or_stop(15)

    async def _network_stats(self, session: aiohttp.ClientSession) -> None:
        while not self.isInterruptionRequested():
            for url, event_type, source in (
                (MEMPOOL_FEES, "fees", "mempool fees"),
                (MEMPOOL_STATUS, "mempool", "mempool status"),
            ):
                try:
                    async with session.get(url) as response:
                        response.raise_for_status()
                        data = await response.json()
                    if event_type == "fees":
                        self._publish({"type": "fees", "fees": data, "time": time.time()})
                    else:
                        self._publish(
                            {
                                "type": "mempool",
                                "time": time.time(),
                                "vsize": float(data.get("vsize", 0)),
                                "count": int(data.get("count", 0)),
                            }
                        )
                except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as error:
                    self._publish(
                        {
                            "type": "status",
                            "source": source,
                            "state": "error",
                            "detail": str(error),
                        }
                    )
            await self._sleep_or_stop(15)

    async def _mempool_stream(self) -> None:
        while not self.isInterruptionRequested():
            try:
                async with websockets.connect(MEMPOOL_WS, ping_interval=20) as ws:
                    await ws.send(json.dumps({"track-mempool": True}))
                    self._publish(
                        {
                            "type": "status",
                            "source": "mempool.space",
                            "state": "connected",
                            "detail": "Streaming unconfirmed transaction hashes",
                        }
                    )
                    while not self.isInterruptionRequested():
                        try:
                            raw = await asyncio.wait_for(ws.recv(), timeout=1)
                        except asyncio.TimeoutError:
                            continue
                        payload = json.loads(raw)
                        self._handle_mempool(payload)
            except asyncio.CancelledError:
                raise
            except Exception as error:
                self._publish(
                    {
                        "type": "status",
                        "source": "mempool.space",
                        "state": "reconnecting",
                        "detail": str(error),
                    }
                )
                await self._sleep_or_stop(5)

    def _handle_mempool(self, payload: dict) -> None:
        transactions = payload.get("mempool")
        if isinstance(transactions, dict):
            for txid, details in transactions.items():
                if isinstance(details, dict):
                    self._publish_transaction(
                        txid,
                        float(details.get("value", 0)) / SATOSHIS_PER_BTC,
                        float(details.get("fee_rate", details.get("feeRate", 0))),
                        time.time(),
                    )
                else:
                    self._publish_transaction(txid, 0.0, 0.0, time.time())

    async def _blockchain_stream(self) -> None:
        while not self.isInterruptionRequested():
            try:
                async with websockets.connect(BLOCKCHAIN_WS, ping_interval=20) as ws:
                    await ws.send(json.dumps({"op": "unconfirmed_sub"}))
                    self._publish(
                        {
                            "type": "status",
                            "source": "Blockchain.com",
                            "state": "connected",
                            "detail": "Streaming unconfirmed transactions",
                        }
                    )
                    while not self.isInterruptionRequested():
                        try:
                            raw = await asyncio.wait_for(ws.recv(), timeout=1)
                        except asyncio.TimeoutError:
                            continue
                        payload = json.loads(raw)
                        transaction = payload.get("x")
                        if payload.get("op") == "utx" and isinstance(transaction, dict):
                            self._handle_blockchain_transaction(transaction)
            except asyncio.CancelledError:
                raise
            except Exception as error:
                self._publish(
                    {
                        "type": "status",
                        "source": "Blockchain.com",
                        "state": "reconnecting",
                        "detail": str(error),
                    }
                )
                await self._sleep_or_stop(5)

    def _handle_blockchain_transaction(self, transaction: dict) -> None:
        outputs = transaction.get("out", [])
        amount = sum(float(output.get("value", 0)) for output in outputs)
        size = float(transaction.get("size", 0))
        fee_rate = float(transaction.get("fee", 0)) / size if size > 0 else 0.0
        timestamp = float(transaction.get("time", time.time()))
        self._publish_transaction(
            transaction.get("hash", ""),
            amount / SATOSHIS_PER_BTC,
            fee_rate,
            timestamp,
            "Blockchain.com",
        )

    def _publish_transaction(
        self,
        txid: str,
        btc: float,
        fee_rate: float,
        timestamp: float,
        source: str = "mempool.space",
    ) -> None:
        if len(txid) != 64:
            return
        self._publish(
            {
                "type": "transaction",
                "txid": txid,
                "btc": btc,
                "fee_rate": fee_rate,
                "time": timestamp,
                "source": source,
            }
        )

    async def _coinbase_stream(self) -> None:
        bids: dict[float, float] = {}
        asks: dict[float, float] = {}
        while not self.isInterruptionRequested():
            try:
                async with websockets.connect(COINBASE_WS, ping_interval=20) as ws:
                    await ws.send(
                        json.dumps(
                            {
                                "type": "subscribe",
                                "channels": [
                                    {"name": "ticker", "product_ids": ["BTC-USD"]},
                                    {"name": "level2", "product_ids": ["BTC-USD"]},
                                    {"name": "market_trades", "product_ids": ["BTC-USD"]},
                                ],
                            }
                        )
                    )
                    bids.clear()
                    asks.clear()
                    self._publish(
                        {
                            "type": "status",
                            "source": "Coinbase",
                            "state": "connected",
                            "detail": "Streaming BTC-USD price, trades, and order book",
                        }
                    )
                    while not self.isInterruptionRequested():
                        try:
                            raw = await asyncio.wait_for(ws.recv(), timeout=1)
                        except asyncio.TimeoutError:
                            continue
                        message = json.loads(raw)
                        self._handle_coinbase_message(message, bids, asks)
            except asyncio.CancelledError:
                raise
            except Exception as error:
                self._publish(
                    {
                        "type": "status",
                        "source": "Coinbase",
                        "state": "reconnecting",
                        "detail": str(error),
                    }
                )
                await self._sleep_or_stop(5)

    def _handle_coinbase_message(
        self, message: dict, bids: dict[float, float], asks: dict[float, float]
    ) -> None:
        kind = message.get("type")
        if kind == "ticker" and message.get("price"):
            self._publish(
                {
                    "type": "price",
                    "price": float(message["price"]),
                    "time": time.time(),
                }
            )
        elif kind == "snapshot":
            bids.update({float(price): float(size) for price, size in message.get("bids", [])})
            asks.update({float(price): float(size) for price, size in message.get("asks", [])})
            self._publish_book(bids, asks)
        elif kind == "l2update":
            for side, price_text, size_text in message.get("changes", []):
                levels = bids if side == "buy" else asks
                price, size = float(price_text), float(size_text)
                if size == 0:
                    levels.pop(price, None)
                else:
                    levels[price] = size
            self._publish_book(bids, asks)
        elif kind == "market_trades":
            for trade in message.get("trades", []):
                if trade.get("price"):
                    self._publish(
                        {
                            "type": "trade",
                            "price": float(trade["price"]),
                            "size": float(trade.get("size", 0)),
                            "side": trade.get("side", ""),
                            "time": time.time(),
                        }
                    )

    def _publish_book(self, bids: dict[float, float], asks: dict[float, float]) -> None:
        best_bids = sorted(bids.items(), reverse=True)
        best_asks = sorted(asks.items())
        bid_size = sum(size for _, size in best_bids[:10])
        ask_size = sum(size for _, size in best_asks[:10])
        total = bid_size + ask_size
        if total:
            self._publish(
                {
                    "type": "book",
                    "imbalance": (bid_size - ask_size) / total,
                    "bid": best_bids[0][0] if best_bids else 0.0,
                    "ask": best_asks[0][0] if best_asks else 0.0,
                    "time": time.time(),
                }
            )

    async def _sleep_or_stop(self, seconds: float) -> None:
        for _ in range(int(seconds * 4)):
            if self.isInterruptionRequested():
                return
            await asyncio.sleep(0.25)
