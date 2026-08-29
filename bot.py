#!/usr/bin/env python3
"""
KuCoin Futures Grid Bot - Production Entry Point
Supports Local Simulator and Live KuCoin Futures trading with Telegram monitoring.
"""

import os
import sys
import asyncio
import yaml
from pathlib import Path
from loguru import logger

# Try to load .env file locally (for development)
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

from core.grid import GridEngine
from core.state import StateManager
from core.telegram import TelegramController


# --- THE SWITCH (Can be overridden by ENV) ---
# Set USE_SIMULATOR=false on Render for live trading
USE_SIMULATOR = os.getenv('USE_SIMULATOR', 'true').lower() == 'true'
# ---------------------------------------------

if USE_SIMULATOR:
    from simulator import LocalSimulator
else:
    from core.exchange import KucoinFuturesExchange


def load_config():
    """
    Load config.yaml if exists, otherwise create a default config.
    Environment variables always override.
    """
    # Default config (used if config.yaml is missing)
    default_config = {
        "exchange": {
            "api_key": "",
            "api_secret": "",
            "api_passphrase": "",
            "sandbox": False,
            "leverage": 2,
            "margin_mode": "isolated"
        },
        "grid": {
            "symbol": "SOL/USDT:USDT",
            "grid_lines": 20,
            "range_percent": 0.05,
            "quote_currency": "USDT"
        },
        "risk": {
            "max_position_percent": 0.95,
            "stop_loss_percent": 0.25,
            "min_notional": 10.0,
            "dynamic_floor_threshold": 0.10
        },
        "telegram": {
            "enabled": False,
            "bot_token": "",
            "allowed_chat_ids": []
        },
        "logging": {
            "level": "INFO",
            "file": "logs/grid_bot.log"
        }
    }

    # Try to load config.yaml
    try:
        with open("config.yaml", "r") as f:
            config = yaml.safe_load(f)
        logger.info("Loaded config.yaml")
    except FileNotFoundError:
        logger.warning("config.yaml not found. Using default configuration.")
        config = default_config
    except Exception as e:
        logger.error(f"Error loading config.yaml: {e}. Using defaults.")
        config = default_config

    # --- OVERRIDE WITH ENVIRONMENT VARIABLES ---
    # Exchange
    if os.getenv('KUCOIN_API_KEY'):
        config['exchange']['api_key'] = os.getenv('KUCOIN_API_KEY')
    if os.getenv('KUCOIN_API_SECRET'):
        config['exchange']['api_secret'] = os.getenv('KUCOIN_API_SECRET')
    if os.getenv('KUCOIN_PASSPHRASE'):
        config['exchange']['api_passphrase'] = os.getenv('KUCOIN_PASSPHRASE')
    if os.getenv('SANDBOX_MODE'):
        config['exchange']['sandbox'] = os.getenv('SANDBOX_MODE').lower() == 'true'
    if os.getenv('LEVERAGE'):
        config['exchange']['leverage'] = int(os.getenv('LEVERAGE'))

    # Grid
    if os.getenv('SYMBOL'):
        config['grid']['symbol'] = os.getenv('SYMBOL')
    if os.getenv('GRID_LINES'):
        config['grid']['grid_lines'] = int(os.getenv('GRID_LINES'))
    if os.getenv('RANGE_PERCENT'):
        config['grid']['range_percent'] = float(os.getenv('RANGE_PERCENT'))

    # Risk
    if os.getenv('STOP_LOSS_PERCENT'):
        config['risk']['stop_loss_percent'] = float(os.getenv('STOP_LOSS_PERCENT'))
    if os.getenv('MIN_NOTIONAL'):
        config['risk']['min_notional'] = float(os.getenv('MIN_NOTIONAL'))

    # Telegram
    if os.getenv('TELEGRAM_ENABLED'):
        config['telegram']['enabled'] = os.getenv('TELEGRAM_ENABLED').lower() == 'true'
    if os.getenv('TELEGRAM_BOT_TOKEN'):
        config['telegram']['bot_token'] = os.getenv('TELEGRAM_BOT_TOKEN')
    if os.getenv('ALLOWED_CHAT_IDS'):
        allowed = [int(x.strip()) for x in os.getenv('ALLOWED_CHAT_IDS').split(',') if x.strip().isdigit()]
        if allowed:
            config['telegram']['allowed_chat_ids'] = allowed

    # Logging
    if os.getenv('LOG_LEVEL'):
        config['logging']['level'] = os.getenv('LOG_LEVEL')

    return config


def setup_logging(config):
    """Configure loguru logging with support for Render persistent disk."""
    log_level = config.get("logging", {}).get("level", "INFO")
    log_file = config.get("logging", {}).get("file", "logs/grid_bot.log")

    data_dir = os.getenv('DATA_DIR')
    if data_dir:
        log_dir = Path(data_dir) / "logs"
    else:
        log_dir = Path(log_file).parent

    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / "grid_bot.log"

    logger.remove()
    logger.add(sys.stdout, level=log_level, colorize=True)
    logger.add(str(log_path), level=log_level, rotation="10 MB", retention="3 days")
    logger.info(f"Logging configured (level={log_level}, file={log_path})")


async def main():
    config = load_config()
    setup_logging(config)

    logger.info("🚀 Starting KuCoin Futures Grid Bot")

    if USE_SIMULATOR:
        logger.warning("🧪 RUNNING IN LOCAL SIMULATOR - No real funds, no API keys needed.")
        exchange = LocalSimulator(start_price=120.0)
    else:
        logger.critical("🔥 RUNNING IN LIVE MODE - Real funds at risk!")
        exchange = KucoinFuturesExchange(config["exchange"])
        await exchange.connect()
        await exchange.set_leverage(config["exchange"]["leverage"])

    state = StateManager()
    grid = GridEngine(exchange, state, config["grid"])
    await grid.initialize()

    telegram = None
    if config.get("telegram", {}).get("enabled", False):
        logger.info("📱 Telegram is enabled. Attempting to start...")
        telegram = TelegramController(
            config["telegram"]["bot_token"],
            config["telegram"]["allowed_chat_ids"],
            grid
        )
        task = asyncio.create_task(telegram.run())
        await asyncio.sleep(1.0)

        if task.done() and task.exception():
            logger.error(f"❌ Telegram task crashed on startup: {task.exception()}")
            logger.error("Check your bot token and internet connection.")
            telegram = None
        else:
            logger.success("✅ Telegram task is running successfully.")
            grid.set_telegram(telegram)

    try:
        await grid.run()
    except KeyboardInterrupt:
        logger.info("⚠️ Interrupt received.")
    except Exception as e:
        logger.error(f"❌ Fatal error: {e}")
        raise
    finally:
        logger.info("🛑 Shutting down... Executing Dead-Man Switch.")
        try:
            await exchange.cancel_all_orders(grid.symbol)
        except Exception as e:
            logger.error(f"Cleanup error: {e}")

        await exchange.close()

        if telegram:
            await telegram.stop()

        logger.info("👋 Bot stopped.")


if __name__ == "__main__":
    asyncio.run(main())