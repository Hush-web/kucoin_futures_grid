import asyncio
import ccxt.pro as ccxtpro
from loguru import logger
from typing import Optional, Dict, List

class KucoinFuturesExchange:
    def __init__(self, config: dict):
        self.config = config
        self.sandbox = config.get("sandbox", True)
        self.leverage = config.get("leverage", 2)
        self.margin_mode = config.get("margin_mode", "isolated")
        self._semaphore = asyncio.Semaphore(5)
        self._exchange: Optional[ccxtpro.kucoinfutures] = None

    async def connect(self) -> None:
        api_key = self.config["api_key"]
        api_secret = self.config["api_secret"]
        passphrase = self.config["api_passphrase"]

        self._exchange = ccxtpro.kucoinfutures({
            "apiKey": api_key,
            "secret": api_secret,
            "password": passphrase,
            "enableRateLimit": True,
            "options": {"defaultType": "swap"},
        })

        if self.sandbox:
            self._exchange.urls["api"] = {
                "public": "https://api-sandbox-futures.kucoin.com/api/v1",
                "private": "https://api-sandbox-futures.kucoin.com/api/v1",
            }
            logger.warning("🚨 RUNNING IN KUCOIN FUTURES SANDBOX")
        else:
            logger.warning("🔥 RUNNING IN LIVE KUCOIN FUTURES")

        await self._rate_limited(self._exchange.load_markets)
        logger.info(f"Connected to KuCoin Futures ({'sandbox' if self.sandbox else 'live'})")

    async def _rate_limited(self, coro, *args, **kwargs):
        async with self._semaphore:
            return await coro(*args, **kwargs)

    @property
    def exchange(self):
        if self._exchange is None:
            raise RuntimeError("Not connected.")
        return self._exchange

    async def set_leverage(self, symbol: str) -> None:
        await self._rate_limited(self.exchange.set_margin_mode, self.margin_mode, symbol)
        await self._rate_limited(self.exchange.set_leverage, self.leverage, symbol)
        logger.info(f"Set {self.leverage}x leverage on {symbol}")

    async def fetch_ticker(self, symbol: str) -> Dict:
        return await self._rate_limited(self.exchange.fetch_ticker, symbol)

    async def fetch_balance(self) -> Dict:
        return await self._rate_limited(self.exchange.fetch_balance)

    async def fetch_positions(self, symbols: List[str] = None) -> List[Dict]:
        return await self._rate_limited(self.exchange.fetch_positions, symbols)

    async def create_limit_order(self, symbol: str, side: str, amount: float, price: float,
                                  client_order_id: str) -> Dict:
        return await self._rate_limited(
            self.exchange.create_limit_order,
            symbol, side, amount, price,
            {"clientOrderId": client_order_id}
        )

    async def create_market_order(self, symbol: str, side: str, amount: float) -> Dict:
        return await self._rate_limited(
            self.exchange.create_market_order,
            symbol, side, amount
        )

    async def cancel_order(self, order_id: str, symbol: str) -> Dict:
        return await self._rate_limited(self.exchange.cancel_order, order_id, symbol)

    async def cancel_all_orders(self, symbol: str) -> None:
        open_orders = await self.fetch_open_orders(symbol)
        for o in open_orders:
            await self.cancel_order(o["id"], symbol)

    async def fetch_open_orders(self, symbol: str) -> list:
        return await self._rate_limited(self.exchange.fetch_open_orders, symbol)

    async def fetch_order(self, order_id: str, symbol: str) -> Optional[Dict]:
        try:
            return await self._rate_limited(self.exchange.fetch_order, order_id, symbol)
        except Exception:
            return None

    async def watch_my_trades(self, symbol: str):
        async for trades in self.exchange.watch_my_trades(symbol):
            yield trades

    async def watch_ticker(self, symbol: str):
        while True:
            ticker = await self.exchange.watch_ticker(symbol)
            yield ticker

    async def close(self):
        if self._exchange:
            await self.exchange.close()