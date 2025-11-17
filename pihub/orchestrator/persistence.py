
from __future__ import annotations
import asyncio
import json
import sqlite3
from typing import Any, Dict, Iterable, Optional

class SQLiteKV:
    def __init__(self, path: str) -> None:
        self._path = path
        self._lock = asyncio.Lock()
        self._ensure_schema()

    def _ensure_schema(self) -> None:
        conn = sqlite3.connect(self._path)
        try:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS kv (
                    k TEXT PRIMARY KEY,
                    v TEXT NOT NULL
                )
            """)
            conn.commit()
        finally:
            conn.close()

    async def get(self, key: str, default: Any = None) -> Any:
        def _get():
            conn = sqlite3.connect(self._path)
            try:
                cur = conn.execute("SELECT v FROM kv WHERE k=?", (key,))
                row = cur.fetchone()
                return json.loads(row[0]) if row else default
            finally:
                conn.close()
        async with self._lock:
            return await asyncio.to_thread(_get)

    async def set(self, key: str, value: Any) -> None:
        def _set():
            conn = sqlite3.connect(self._path)
            try:
                conn.execute("REPLACE INTO kv (k, v) VALUES (?,?)", (key, json.dumps(value)))
                conn.commit()
            finally:
                conn.close()
        async with self._lock:
            await asyncio.to_thread(_set)

    async def mget(self, keys: Iterable[str]) -> Dict[str, Any]:
        res: Dict[str, Any] = {}
        for k in keys:
            res[k] = await self.get(k)
        return res
