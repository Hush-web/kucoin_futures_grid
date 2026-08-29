import asyncio
import random
import time
from loguru import logger


class LocalSimulator:
    """Fully mocked exchange that implements the exact interface your GridEngine expects."""

    def __init__(self, start_price=120.0):
        self.price = start_price
        self.balance = {"USDT": 10000.0, "SOL": 0.0}
        self.orders = {}  # order_id -> dict
        self.trade_log = []
        self._order_counter = 0
        self._running = True

        # Required by GridEngine to access market data
        self.exchange = self

    def market(self, symbol):
        """Simulates ccxt's market() method for precision and min notional."""
        return {
            "precision": {"amount": 0.001, "price": 0.01},
            "limits": {"cost": {"min": 10.0}},
        }

    async def set_leverage(self, symbol, leverage=2):
        """Dummy for GridEngine. Simulator doesn't use leverage."""
        pass

    async def fetch_positions(self, symbols=None):
        """Dummy for GridEngine._emergency_stop."""
        return []  # No open positions in simulator

    async def cancel_all_orders(self, symbol):
        """Cancel every open order."""
        for oid in list(self.orders.keys()):
            if self.orders[oid]["status"] == "open":
                self.orders[oid]["status"] = "canceled"
        logger.info("⏹️ [SIM] Cancelled all open orders.")

    def _gen_id(self):
        self._order_counter += 1
        return f"local_{self._order_counter}"

    async def fetch_ticker(self, symbol="SOL/USDT"):
        return {"last": self.price, "symbol": symbol}

    async def fetch_balance(self):
        return {"free": self.balance, "total": self.balance}

    async def fetch_open_orders(self, symbol):
        return [o for o in self.orders.values() if o["status"] == "open"]

    async def create_limit_order(self, symbol, side, amount, price, client_order_id=None):
        """Places an order. If price crosses market, fills it instantly."""
        order_id = client_order_id or self._gen_id()

        # Simulate immediate fill if price crosses
        is_filled = False
        if side == "buy" and price >= self.price:
            is_filled = True
        elif side == "sell" and price <= self.price:
            is_filled = True

        order = {
            "id": order_id,
            "symbol": symbol,          # <--- ADDED THIS LINE
            "side": side,
            "price": price,
            "amount": amount,
            "status": "closed" if is_filled else "open",
            "cost": amount * price,
        }
        self.orders[order_id] = order

        if is_filled:
            if side == "buy":
                cost = amount * price
                if self.balance["USDT"] >= cost:
                    self.balance["USDT"] -= cost
                    self.balance["SOL"] += amount
                    logger.info(f"✅ [SIM] BUY {amount:.4f} SOL at ${price:.2f}")
            else:
                if self.balance["SOL"] >= amount:
                    self.balance["SOL"] -= amount
                    self.balance["USDT"] += amount * price
                    logger.info(f"✅ [SIM] SELL {amount:.4f} SOL at ${price:.2f}")

        return order

    async def cancel_order(self, order_id, symbol):
        if order_id in self.orders:
            self.orders[order_id]["status"] = "canceled"
            logger.info(f"⏹️ [SIM] Cancelled order {order_id}")

    async def watch_ticker(self, symbol):
        """Simulate price updates every 1.5 seconds."""
        while self._running:
            change = random.uniform(-0.02, 0.02)
            self.price = max(10.0, self.price * (1 + change))
            yield {"last": self.price, "symbol": symbol}
            await asyncio.sleep(1.5)

    async def watch_my_trades(self, symbol):
        """Yield empty list to keep the GridEngine's trade loop alive."""
        while self._running:
            yield []
            await asyncio.sleep(0.5)

    async def close(self):
        self._running = False
        logger.info("[SIM] Simulator closed.")