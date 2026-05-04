"""BaseCollector — shared reconnect / lifecycle logic for WS collectors.

All exchange collectors (OKX, Binance, …) share the same pattern:
  1. Chunk symbols into groups (≤ max per connection).
  2. For each group, run a ``_ws_loop`` that reconnects on failure.
  3. Inside each connection, subscribe and dispatch incoming messages.

This mixin extracts the reconnect loop so subclasses only implement:
  * ``_connect_and_listen(session, symbols)`` — WS-level logic.
  * ``_dispatch(raw_text)`` — message parsing.

Subclasses must set ``self._running`` and may override ``stop()``.
"""

from __future__ import annotations

import asyncio
import logging
from abc import ABC, abstractmethod

import aiohttp

logger = logging.getLogger(__name__)


class BaseCollector(ABC):
    """Abstract WebSocket collector with auto-reconnect.

    Subclasses must:
      * Call ``super().__init__(symbols, max_per_conn)`` in ``__init__``.
      * Implement :meth:`_connect_and_listen`.
    """

    def __init__(self, symbols: list[str], max_per_conn: int = 200):
        self.symbols = list(symbols)
        self._max_per_conn = max_per_conn
        self._running = True

    async def run(self) -> None:
        """Block forever, streaming data and auto-reconnecting."""
        async with aiohttp.ClientSession() as session:
            groups = list(self._chunk(self.symbols, self._max_per_conn))
            tasks = [
                asyncio.create_task(self._ws_loop(session, group))
                for group in groups
            ]
            await asyncio.gather(*tasks)

    def stop(self) -> None:
        self._running = False

    @abstractmethod
    async def _connect_and_listen(
        self, session: aiohttp.ClientSession, symbols: list[str]
    ) -> None:
        """Open WS, subscribe, and dispatch messages until disconnect."""
        ...

    # ── reconnect loop ─────────────────────────────────────────

    async def _ws_loop(
        self, session: aiohttp.ClientSession, symbols: list[str]
    ) -> None:
        while self._running:
            try:
                await self._connect_and_listen(session, symbols)
            except asyncio.CancelledError:
                raise
            except (
                aiohttp.WSServerHandshakeError,
                aiohttp.ClientError,
                asyncio.TimeoutError,
            ) as exc:
                logger.warning(
                    "%s WS disconnected: %s — reconnecting in 3s",
                    type(self).__name__, exc,
                )
            except Exception:
                logger.exception(
                    "%s unexpected WS error — reconnecting in 5s",
                    type(self).__name__,
                )
                await asyncio.sleep(5)
                continue
            await asyncio.sleep(3)

    @staticmethod
    def _chunk(lst: list, size: int):
        for idx in range(0, len(lst), size):
            yield lst[idx : idx + size]
