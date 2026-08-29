#!/usr/bin/env python3
"""
KuCoin Futures Grid Bot - Production Entry Point
Supports Local Simulator and Live KuCoin Futures trading with Telegram monitoring.
Includes health check server for Render deployment.
"""

import os
import sys
import asyncio
import yaml
from pathlib import Path
from loguru import logger

# Import aiohttp for health check server
try:
    from aiohttp import web
except ImportError:
    # Fallback: if aiohttp not installed, warn and skip
    logger.warning("aiohttp not installed. Health check server disabled.")
    web = None

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

from core.grid import GridEngine
from core.state import StateManager
from core.telegram import TelegramController


# --- THE SWITCH ---
USE_SIMULATOR = os.getenv('USE_SIMULATOR', 'true').lower() == 'true'
# -----------------

if USE_SIMULATOR:
    from simulator import LocalSimulator
else:
    from core.exchange import KucoinFuturesExchange


def load_config():
    """Load config.yaml if exists, otherwise use defaults. ENV overrides."""
    default_config = {
        "exchange": {
            "api_key": "", "api_secret": "", "api_passphrase": "",
            "sandbox": False, "leverage": 2, "margin_mode": "isolated"
        },
        "grid": {
            "symbol": "SOL/USDT:USDT", "grid_lines": 20,
            "range_percent": 0.05, "quote_currency": "USDT"
        },
        "risk": {
            "max_position_percent": 0.95, "stop_loss_percent": 0.25,
            "min_notional": 10.0,
            "dynamic_floor_threshold": 0.02   # Can be overridden by ENV
        },
        "telegram": {
            "enabled": False, "bot_token": "", "allowed_chat_ids": []
        },
        "logging": {"level": "INFO", "file": "logs/grid_bot.log"}
    }

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

    # Override with ENV
    for key in ['KUCOIN_API_KEY', 'KUCOIN_API_SECRET', 'KUCOIN_PASSPHRASE']:
        if os.getenv(key):
            config['exchange'][key.lower().replace('kucoin_', '')] = os.getenv(key)
    if os.getenv('SANDBOX_MODE'):
        config['exchange']['sandbox'] = os.getenv('SANDBOX_MODE').lower() == 'true'
    if os.getenv('LEVERAGE'):
        config['exchange']['leverage'] = int(os.getenv('LEVERAGE'))
    if os.getenv('SYMBOL'):
        config['grid']['symbol'] = os.getenv('SYMBOL')
    if os.getenv('GRID_LINES'):
        config['grid']['grid_lines'] = int(os.getenv('GRID_LINES'))
    if os.getenv('RANGE_PERCENT'):
        config['grid']['range_percent'] = float(os.getenv('RANGE_PERCENT'))
    if os.getenv('STOP_LOSS_PERCENT'):
        config['risk']['stop_loss_percent'] = float(os.getenv('STOP_LOSS_PERCENT'))
    if os.getenv('MIN_NOTIONAL'):
        config['risk']['min_notional'] = float(os.getenv('MIN_NOTIONAL'))
    if os.getenv('DYNAMIC_FLOOR_THRESHOLD'):
        config['risk']['dynamic_floor_threshold'] = float(os.getenv('DYNAMIC_FLOOR_THRESHOLD'))
    if os.getenv('TELEGRAM_ENABLED'):
        config['telegram']['enabled'] = os.getenv('TELEGRAM_ENABLED').lower() == 'true'
    if os.getenv('TELEGRAM_BOT_TOKEN'):
        config['telegram']['bot_token'] = os.getenv('TELEGRAM_BOT_TOKEN')
    if os.getenv('ALLOWED_CHAT_IDS'):
        allowed = [int(x.strip()) for x in os.getenv('ALLOWED_CHAT_IDS').split(',') if x.strip().isdigit()]
        if allowed:
            config['telegram']['allowed_chat_ids'] = allowed
    if os.getenv('LOG_LEVEL'):
        config['logging']['level'] = os.getenv('LOG_LEVEL')

    return config


def setup_logging(config):
    """Configure logging with fallback if directory creation fails."""
    log_level = config.get("logging", {}).get("level", "INFO")
    log_file = config.get("logging", {}).get("file", "logs/grid_bot.log")

    data_dir = os.getenv('DATA_DIR')
    if data_dir:
        log_dir = Path(data_dir) / "logs"
    else:
        log_dir = Path("logs")

    try:
        log_dir.mkdir(parents=True, exist_ok=True)
    except (PermissionError, FileNotFoundError, OSError) as e:
        logger.warning(f"Cannot create {log_dir}: {e}. Falling back to './logs'.")
        log_dir = Path("logs")
        log_dir.mkdir(parents=True, exist_ok=True)

    log_path = log_dir / "grid_bot.log"

    logger.remove()
    logger.add(sys.stdout, level=log_level, colorize=True)
    logger.add(str(log_path), level=log_level, rotation="10 MB", retention="3 days")
    logger.info(f"Logging configured (level={log_level}, file={log_path})")


async def health_check(request):
    """Simple health check endpoint for Render."""
    return web.Response(text="OK")


async def run_http_server():
    """Start a minimal HTTP server for Render health checks."""
    port = int(os.environ.get("PORT", 8080))
    app = web.Application()
    app.router.add_get("/", health_check)
    app.router.add_get("/health", health_check)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host="0.0.0.0", port=port)
    await site.start()
    logger.info(f"✅ Health check server running on port {port}")


async def main():
    config = load_config()
    setup_logging(config)

    logger.info("🚀 Starting KuCoin Futures Grid Bot")

    # Start the HTTP server for Render's health checks (if aiohttp available)
    if web is not None:
        asyncio.create_task(run_http_server())
    else:
        logger.warning("aiohttp not available; health check server not started.")

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