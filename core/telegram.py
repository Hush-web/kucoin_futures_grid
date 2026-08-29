import os
import asyncio
import io
import matplotlib
matplotlib.use('Agg')  # Non-interactive backend for Render
import matplotlib.pyplot as plt
from typing import Optional, Union, List, Dict
from datetime import datetime
from loguru import logger
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes


class TelegramController:
    def __init__(self, token: str, allowed_chat_ids: Union[list, str], grid_engine):
        self.token = token  # <--- FIX: missing this line
        if isinstance(allowed_chat_ids, str):
            allowed_chat_ids = [int(x.strip()) for x in allowed_chat_ids.split(',') if x.strip().isdigit()]
        self.allowed_chat_ids = set(allowed_chat_ids)
        self.grid_engine = grid_engine
        self._app: Optional[Application] = None
        self._running = False
        # PnL tracking
        self._trade_history: List[Dict] = []
        self._total_fees = 0.0
        self._total_trades = 0

    def _authorize(self, update: Update) -> bool:
        if update.effective_user.id not in self.allowed_chat_ids:
            logger.warning(f"Unauthorized: {update.effective_user.id}")
            return False
        return True

    async def _generate_equity_chart(self) -> Optional[io.BytesIO]:
        """Generate a matplotlib chart of equity/price history."""
        state = await self.grid_engine.state.get_latest_state(self.grid_engine.grid_id)
        if not state:
            return None

        fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(8, 10))
        fig.patch.set_facecolor('#1a1a2e')

        # Price chart
        prices = [state.get('current_price', 0)]
        timestamps = [datetime.now()]
        ax1.plot(timestamps, prices, 'g-', linewidth=2, label='Price')
        ax1.axhline(y=state.get('upper', 0), color='r', linestyle='--', label='Upper')
        ax1.axhline(y=state.get('lower', 0), color='b', linestyle='--', label='Lower')
        ax1.set_title('SOL/USDT Price & Grid Range', color='white')
        ax1.set_ylabel('Price (USDT)', color='white')
        ax1.tick_params(colors='white')
        ax1.legend()
        ax1.grid(True, alpha=0.3)

        # PnL chart
        pnl = self._calculate_pnl()
        labels = ['Realized PnL', 'Fees', 'Net']
        values = [
            pnl['realized_pnl'],
            -pnl['fees'],
            pnl['realized_pnl'] - pnl['fees']
        ]
        colors = ['#00ff88' if v > 0 else '#ff4444' for v in values]
        ax2.bar(labels, values, color=colors)
        ax2.set_title('Performance Summary', color='white')
        ax2.set_ylabel('USDT', color='white')
        ax2.tick_params(colors='white')
        ax2.grid(True, alpha=0.3)

        buf = io.BytesIO()
        plt.tight_layout()
        plt.savefig(buf, format='png', facecolor='#1a1a2e')
        buf.seek(0)
        plt.close()
        return buf

    def _calculate_pnl(self) -> Dict:
        """Calculate P&L from trade history."""
        total_buy_cost = 0.0
        total_sell_revenue = 0.0
        for trade in self._trade_history:
            if trade.get('side') == 'buy':
                total_buy_cost += trade.get('cost', 0)
            elif trade.get('side') == 'sell':
                total_sell_revenue += trade.get('cost', 0)
        realized_pnl = total_sell_revenue - total_buy_cost
        return {
            'realized_pnl': realized_pnl,
            'total_buy_cost': total_buy_cost,
            'total_sell_revenue': total_sell_revenue,
            'total_trades': len(self._trade_history),
            'fees': self._total_fees
        }

    async def _start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self._authorize(update):
            return await update.message.reply_text("⛔ Unauthorized.")
        keyboard = [
            [InlineKeyboardButton("📊 Dashboard", callback_data="dashboard")],
            [InlineKeyboardButton("💰 Balance", callback_data="balance")],
            [InlineKeyboardButton("📈 Performance", callback_data="performance")],
            [InlineKeyboardButton("📉 Chart", callback_data="chart")],
            [InlineKeyboardButton("🔄 Re-anchor", callback_data="reanchor")],
            [InlineKeyboardButton("⛔ EMERGENCY STOP", callback_data="emergency_stop")],
            [InlineKeyboardButton("🔄 Refresh", callback_data="refresh")],
        ]
        await update.message.reply_text(
            "🤖 *KuCoin Futures Grid Bot*\n\n"
            f"Symbol: `{self.grid_engine.symbol}`\n"
            f"Leverage: `{getattr(self.grid_engine.exchange, 'leverage', 2)}x`\n"
            f"Stop-Loss: `-{int(self.grid_engine.stop_loss_pct*100)}%`\n"
            f"Status: `{'🟢 Running' if self.grid_engine._running else '🔴 Stopped'}`\n\n"
            "Select an action:",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="Markdown"
        )

    async def _callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self._authorize(update):
            return await update.callback_query.answer("⛔ Unauthorized", show_alert=True)
        query = update.callback_query
        await query.answer()
        data = query.data

        if data == "dashboard":
            state = await self.grid_engine.state.get_latest_state(self.grid_engine.grid_id)
            pnl = self._calculate_pnl()
            if state:
                msg = (
                    "📊 *DASHBOARD*\n\n"
                    f"📈 Price: `{state.get('current_price', 0):.2f}`\n"
                    f"📊 Range: `{state.get('lower', 0):.2f}` – `{state.get('upper', 0):.2f}`\n"
                    f"📦 Grid Lines: `{len(state.get('buys', [])) + len(state.get('sells', []))}`\n"
                    f"💰 Equity: `{state.get('equity', 0):.2f}` USDT\n"
                    f"📈 PnL: `{pnl['realized_pnl']:.2f}` USDT\n"
                    f"💸 Fees: `{pnl['fees']:.2f}` USDT\n"
                    f"📊 Net: `{pnl['realized_pnl'] - pnl['fees']:.2f}` USDT\n"
                    f"🔄 Trades: `{pnl['total_trades']}`\n"
                    f"⏱️ Running: `{'✅' if self.grid_engine._running else '❌'}`"
                )
            else:
                msg = "❌ No grid state available."
            await query.edit_message_text(msg, parse_mode="Markdown")

        elif data == "balance":
            bal = await self.grid_engine.exchange.fetch_balance()
            msg = "💰 *BALANCE*\n\n"
            for cur, amt in bal["free"].items():
                if amt > 0:
                    msg += f"`{cur}`: `{amt:.4f}`\n"
            await query.edit_message_text(msg, parse_mode="Markdown")

        elif data == "performance":
            pnl = self._calculate_pnl()
            msg = (
                "📈 *PERFORMANCE*\n\n"
                f"💰 Total Trades: `{pnl['total_trades']}`\n"
                f"📈 Realized PnL: `{pnl['realized_pnl']:.2f}` USDT\n"
                f"💸 Total Fees: `{pnl['fees']:.2f}` USDT\n"
                f"📊 Net Profit: `{pnl['realized_pnl'] - pnl['fees']:.2f}` USDT\n\n"
                "_Data resets on bot restart_"
            )
            await query.edit_message_text(msg, parse_mode="Markdown")

        elif data == "chart":
            await query.edit_message_text("📊 Generating chart...")
            chart = await self._generate_equity_chart()
            if chart:
                await query.delete_message()
                await context.bot.send_photo(
                    chat_id=update.effective_chat.id,
                    photo=chart,
                    caption="📊 *Grid Bot Performance Dashboard*\n_Price, Grid Range & PnL Summary_",
                    parse_mode="Markdown"
                )
            else:
                await query.edit_message_text("❌ Not enough data to generate chart.")

        elif data == "reanchor":
            await query.edit_message_text("🔄 Re-anchoring grid...")
            ticker = await self.grid_engine.exchange.fetch_ticker(self.grid_engine.symbol)
            await self.grid_engine._re_anchor(ticker["last"])
            await query.edit_message_text("✅ Grid re-anchored successfully.")

        elif data == "emergency_stop":
            await query.edit_message_text("⛔ EMERGENCY STOP issued!")
            ticker = await self.grid_engine.exchange.fetch_ticker(self.grid_engine.symbol)
            await self.grid_engine._emergency_stop(ticker["last"], "MANUAL EMERGENCY STOP")

        elif data == "refresh":
            await query.answer("🔄 Refreshing...")
            state = await self.grid_engine.state.get_latest_state(self.grid_engine.grid_id)
            if state:
                await query.edit_message_text(
                    f"📊 *Refreshed*\nPrice: `{state.get('current_price', 0):.2f}`",
                    parse_mode="Markdown"
                )
            await asyncio.sleep(0.5)
            # Re-show dashboard
            await self._callback(update, context)

    async def _balance_cmd(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self._authorize(update):
            return
        bal = await self.grid_engine.exchange.fetch_balance()
        msg = "💰 *Balance:*\n" + "\n".join([f"`{k}`: `{v:.4f}`" for k, v in bal["free"].items() if v > 0])
        await update.message.reply_text(msg, parse_mode="Markdown")

    async def _status_cmd(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self._authorize(update):
            return
        state = await self.grid_engine.state.get_latest_state(self.grid_engine.grid_id)
        if state:
            msg = (
                f"📊 *Status*\n"
                f"Running: `{self.grid_engine._running}`\n"
                f"Price: `{state.get('current_price', 0):.2f}`\n"
                f"Buys: `{len(state.get('buys', []))}`\n"
                f"Sells: `{len(state.get('sells', []))}`\n"
                f"Equity: `{state.get('equity', 0):.2f}`"
            )
        else:
            msg = "No state."
        await update.message.reply_text(msg, parse_mode="Markdown")

    async def run(self):
        if self._running:
            return
        logger.info("🤖 Telegram controller is initializing...")
        try:
            self._app = Application.builder().token(self.token).build()
            self._app.add_handler(CommandHandler("start", self._start))
            self._app.add_handler(CommandHandler("balance", self._balance_cmd))
            self._app.add_handler(CommandHandler("status", self._status_cmd))
            self._app.add_handler(CallbackQueryHandler(self._callback))
            self._running = True
            await self._app.initialize()
            await self._app.start()
            await self._app.updater.start_polling()
            logger.success("✅ Telegram bot is connected and polling. Send /start on Telegram.")
        except Exception as e:
            logger.error(f"❌ Telegram FAILED to start: {e}")
            self._running = False
            raise

    async def stop(self):
        if self._app:
            try:
                await self._app.updater.stop()
                await self._app.stop()
                await self._app.shutdown()
            except Exception as e:
                logger.error(f"Telegram shutdown error: {e}")
        self._running = False