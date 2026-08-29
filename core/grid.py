import asyncio
import math
import time
from typing import Dict, List, Optional
from loguru import logger

from .state import StateManager


class GridEngine:
    def __init__(self, exchange, state: StateManager, config: dict):
        self.exchange = exchange
        self.state = state
        self.config = config

        self.symbol = config["symbol"]
        self.grid_lines = config["grid_lines"]
        self.range_pct = config["range_percent"]
        self.quote_currency = config["quote_currency"]
        self.max_position_pct = config.get("max_position_percent", 0.95)
        self.stop_loss_pct = config.get("stop_loss_percent", 0.25)
        self.min_notional = config.get("min_notional", 10.0)
        self.dynamic_floor_threshold = config.get("dynamic_floor_threshold", 0.10)

        self.grid_id = f"{self.symbol.replace('/', '').replace(':', '')}_{int(time.time())}"
        self._lock = asyncio.Lock()
        self._running = False
        self._entry_price = 0.0

        self._precision_amount: Optional[float] = None
        self._precision_price: Optional[float] = None

        # PnL tracking (will be set by Telegram controller)
        self._telegram = None

    def set_telegram(self, telegram_controller):
        """Inject Telegram controller for PnL tracking."""
        self._telegram = telegram_controller

    async def initialize(self) -> None:
        market = self.exchange.exchange.market(self.symbol)
        self._precision_amount = market["precision"]["amount"]
        self._precision_price = market["precision"]["price"]

        await self.exchange.set_leverage(self.symbol)

        saved_state = await self.state.get_latest_state(self.grid_id)
        if saved_state:
            logger.info(f"♻️ Recovered grid state from {len(saved_state.get('buys', []))} buys and {len(saved_state.get('sells', []))} sells")
            self._entry_price = saved_state.get("entry_price", 0)
            await self._reconcile_with_exchange(saved_state)
        else:
            logger.info("No saved state. Fresh start.")
            ticker = await self.exchange.fetch_ticker(self.symbol)
            self._entry_price = ticker["last"]
            await self._re_anchor(ticker["last"])

    async def _reconcile_with_exchange(self, saved_state: Dict) -> None:
        open_orders = await self.exchange.fetch_open_orders(self.symbol)
        open_ids = {o["id"] for o in open_orders}
        persisted = await self.state.get_persisted_orders(self.grid_id)

        for order in persisted:
            if order["id"] not in open_ids:
                logger.warning(f"⚠️ Order {order['id']} missing. Marking as filled.")
                await self.state.mark_order_closed(order["id"])

        if not persisted or len(open_ids) == 0:
            ticker = await self.exchange.fetch_ticker(self.symbol)
            self._entry_price = ticker["last"]
            await self._re_anchor(ticker["last"])

    def _round_amount(self, amount: float) -> float:
        if self._precision_amount is None:
            return amount
        return math.floor(amount / self._precision_amount) * self._precision_amount

    def _round_price(self, price: float) -> float:
        if self._precision_price is None:
            return price
        return math.floor(price / self._precision_price) * self._precision_price

    def _validate_notional(self, price: float, amount: float) -> bool:
        return (amount * price) >= self.min_notional

    async def _emergency_stop(self, current_price: float, reason: str = "Stop-Loss") -> None:
        logger.critical(f"🔴 {reason} TRIGGERED at {current_price:.2f}")
        await self.exchange.cancel_all_orders(self.symbol)

        positions = await self.exchange.fetch_positions([self.symbol])
        if positions and float(positions[0].get("contracts", 0)) > 0:
            size = abs(float(positions[0]["contracts"]))
            side = "sell" if positions[0]["side"] == "long" else "buy"
            await self.exchange.create_market_order(self.symbol, side, size)
            logger.warning(f"💀 Market {side} executed for {size} contracts")

        self._running = False
        await self.state.save_grid_state(self.grid_id, {"status": "emergency_stopped", "reason": reason})

    async def _re_anchor(self, current_price: float) -> None:
        async with self._lock:
            logger.info(f"📍 Re-anchoring grid at {current_price:.2f}")
            await self.exchange.cancel_all_orders(self.symbol)

            balance = await self.exchange.fetch_balance()
            free_margin = balance["free"].get(self.quote_currency, 0)
            # Try to get leverage, default to 1 if the object doesn't have it (simulator)
            leverage = getattr(self.exchange, 'leverage', 1)
            max_notional = free_margin * leverage
            usable_notional = max_notional * self.max_position_pct

            if usable_notional < self.min_notional * 2:
                logger.error(f"❌ Insufficient equity: {usable_notional:.2f}")
                return

            half_lines = self.grid_lines // 2
            lower = current_price * (1 - self.range_pct)
            upper = current_price * (1 + self.range_pct)
            slice_size = usable_notional / self.grid_lines

            buy_orders = []
            sell_orders = []

            for i in range(1, half_lines + 1):
                buy_price = self._round_price(lower + (current_price - lower) * (i / (half_lines + 1)))
                buy_amount = self._round_amount(slice_size / buy_price)
                sell_price = self._round_price(current_price + (upper - current_price) * (i / (half_lines + 1)))
                sell_amount = self._round_amount(slice_size / sell_price)

                if self._validate_notional(buy_price, buy_amount):
                    buy_orders.append((buy_price, buy_amount))
                if self._validate_notional(sell_price, sell_amount):
                    sell_orders.append((sell_price, sell_amount))

            all_orders = [("buy", p, q) for p, q in buy_orders] + [("sell", p, q) for p, q in sell_orders]
            placed_buys, placed_sells = [], []

            for i in range(0, len(all_orders), 5):
                chunk = all_orders[i:i+5]
                tasks, meta = [], []
                for side, price, amount in chunk:
                    cid = f"grid_{self.grid_id}_{side}_{int(price*10000)}_{int(time.time()*1000)%100000}"
                    tasks.append(self.exchange.create_limit_order(self.symbol, side, amount, price, cid))
                    meta.append((side, price, amount, cid))
                results = await asyncio.gather(*tasks, return_exceptions=True)
                for m, res in zip(meta, results):
                    side, price, amount, cid = m
                    if isinstance(res, Exception):
                        logger.error(f"❌ Failed {side} at {price}: {res}")
                    else:
                        logger.info(f"✅ Placed {side} at {price:.2f} (qty: {amount:.6f})")
                        if side == "buy":
                            placed_buys.append(res)
                        else:
                            placed_sells.append(res)
                        await self.state.save_order(res, self.grid_id)
                await asyncio.sleep(0.1)

            grid_state = {
                "entry_price": current_price,
                "current_price": current_price,
                "lower": lower,
                "upper": upper,
                "buys": placed_buys,
                "sells": placed_sells,
                "equity": free_margin,
                "leverage": leverage,
                "updated_at": time.time()
            }
            await self.state.save_grid_state(self.grid_id, grid_state)
            logger.info("✅ Grid re-anchored successfully")

    async def _dynamic_floor_check(self, current_price: float) -> bool:
        state = await self.state.get_latest_state(self.grid_id)
        if not state:
            return False
        lower = state.get("lower", 0)
        if lower == 0:
            return False
        if (current_price - lower) / lower < self.dynamic_floor_threshold:
            logger.warning(f"🔄 Price {current_price:.2f} too close to floor {lower:.2f}. Re-anchoring downward.")
            await self._re_anchor(current_price * 0.95)
            return True
        return False

    async def _stop_loss_check(self, current_price: float) -> bool:
        if self._entry_price == 0:
            return False
        if current_price < self._entry_price * (1 - self.stop_loss_pct):
            await self._emergency_stop(current_price, "HARD STOP-LOSS")
            return True
        return False

    async def handle_fill(self, trade: Dict) -> None:
        """Recycle the grid on every fill and track PnL for Telegram."""
        price = trade.get("price") or trade.get("average")
        if price is None:
            return

        # --- PnL Tracking ---
        side = trade.get("side")
        amount = trade.get("amount", 0)
        cost = amount * price
        fee = trade.get("fee", {}).get("cost", 0) if isinstance(trade.get("fee"), dict) else 0

        if self._telegram:
            self._telegram._trade_history.append({
                'side': side,
                'price': price,
                'amount': amount,
                'cost': cost,
                'fee': fee,
                'timestamp': time.time()
            })
            self._telegram._total_fees += fee
            self._telegram._total_trades += 1
            # Update realized PnL (optional: we can just recalc on demand)
        # --- End PnL ---

        logger.info(f"💰 Trade filled at {price:.2f}. Recycling grid...")
        await self._re_anchor(price)

    async def run(self) -> None:
        self._running = True
        ticker = await self.exchange.fetch_ticker(self.symbol)
        self._entry_price = ticker["last"]
        await self._re_anchor(ticker["last"])

        async def watch_prices():
            async for ticker in self.exchange.watch_ticker(self.symbol):
                current = ticker["last"]
                if not self._running:
                    break
                if await self._stop_loss_check(current):
                    break
                if await self._dynamic_floor_check(current):
                    continue
                state = await self.state.get_latest_state(self.grid_id)
                if state:
                    lower = state.get("lower", 0)
                    upper = state.get("upper", 0)
                    if current < lower * 0.98 or current > upper * 1.02:
                        logger.warning(f"🚀 Breakout! Re-anchoring at {current:.2f}")
                        self._entry_price = current
                        await self._re_anchor(current)

        async def watch_trades():
            async for trades in self.exchange.watch_my_trades(self.symbol):
                if not self._running:
                    break
                for trade in trades:
                    await self.handle_fill(trade)

        try:
            await asyncio.gather(watch_prices(), watch_trades())
        except asyncio.CancelledError:
            logger.info("Grid engine shutting down.")
        finally:
            self._running = False