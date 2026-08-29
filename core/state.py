import os
import sqlite3
import json
import asyncio
from typing import Optional, Dict, List
from datetime import datetime
from pathlib import Path
from loguru import logger


class StateManager:
    """SQLite-based persistence with crash recovery and Render persistent disk support."""

    def __init__(self, db_path: str = None):
        """
        Initialize the state manager.
        If DATA_DIR environment variable is set (Render), the database is stored there.
        Otherwise, it uses the current directory.
        """
        if db_path is None:
            data_dir = os.getenv('DATA_DIR')
            if data_dir:
                # Render persistent disk
                data_path = Path(data_dir)
                data_path.mkdir(parents=True, exist_ok=True)
                db_path = str(data_path / 'grid_state.db')
                logger.info(f"📁 Using Render persistent disk: {db_path}")
            else:
                # Local development
                db_path = 'grid_state.db'

        self.db_path = db_path
        self._lock = asyncio.Lock()
        self._init_db()

    def _init_db(self):
        """Create the database tables if they don't exist."""
        conn = sqlite3.connect(self.db_path, check_same_thread=False)

        # Grid state table
        conn.execute("""
            CREATE TABLE IF NOT EXISTS state (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                grid_id TEXT NOT NULL,
                data TEXT NOT NULL,
                created_at REAL NOT NULL
            )
        """)

        # Orders table (for crash recovery)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS orders (
                id TEXT PRIMARY KEY,
                symbol TEXT NOT NULL,
                side TEXT NOT NULL,
                price REAL NOT NULL,
                amount REAL NOT NULL,
                status TEXT NOT NULL,
                grid_id TEXT NOT NULL,
                created_at REAL NOT NULL
            )
        """)

        conn.commit()
        conn.close()
        logger.info(f"✅ SQLite database initialized at {self.db_path}")

    async def save_grid_state(self, grid_id: str, state: Dict) -> None:
        """Persist full grid state."""
        async with self._lock:
            conn = sqlite3.connect(self.db_path, check_same_thread=False)
            conn.execute(
                "INSERT INTO state (grid_id, data, created_at) VALUES (?, ?, ?)",
                (grid_id, json.dumps(state), datetime.now().timestamp())
            )
            conn.commit()
            conn.close()

    async def get_latest_state(self, grid_id: str) -> Optional[Dict]:
        """Retrieve the most recent grid state."""
        async with self._lock:
            conn = sqlite3.connect(self.db_path, check_same_thread=False)
            cursor = conn.execute(
                "SELECT data FROM state WHERE grid_id = ? ORDER BY created_at DESC LIMIT 1",
                (grid_id,)
            )
            row = cursor.fetchone()
            conn.close()
            return json.loads(row[0]) if row else None

    async def save_order(self, order: Dict, grid_id: str) -> None:
        """Persist a single order for reconciliation."""
        async with self._lock:
            conn = sqlite3.connect(self.db_path, check_same_thread=False)
            conn.execute("""
                INSERT OR REPLACE INTO orders (id, symbol, side, price, amount, status, grid_id, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                order.get("id") or order.get("clientOrderId"),
                order.get("symbol"),
                order.get("side"),
                order.get("price", 0),
                order.get("amount", 0),
                order.get("status", "open"),
                grid_id,
                datetime.now().timestamp()
            ))
            conn.commit()
            conn.close()

    async def get_persisted_orders(self, grid_id: str) -> List[Dict]:
        """Get all persisted orders for reconciliation on startup."""
        async with self._lock:
            conn = sqlite3.connect(self.db_path, check_same_thread=False)
            conn.row_factory = sqlite3.Row
            cursor = conn.execute(
                "SELECT * FROM orders WHERE grid_id = ? AND status != 'closed'",
                (grid_id,)
            )
            rows = cursor.fetchall()
            conn.close()
            return [dict(row) for row in rows]

    async def mark_order_closed(self, order_id: str) -> None:
        """Mark an order as closed after reconciliation."""
        async with self._lock:
            conn = sqlite3.connect(self.db_path, check_same_thread=False)
            conn.execute(
                "UPDATE orders SET status = 'closed' WHERE id = ?",
                (order_id,)
            )
            conn.commit()
            conn.close()

    async def delete_order(self, order_id: str) -> None:
        """Delete an order from the database (used for cleanup)."""
        async with self._lock:
            conn = sqlite3.connect(self.db_path, check_same_thread=False)
            conn.execute("DELETE FROM orders WHERE id = ?", (order_id,))
            conn.commit()
            conn.close()