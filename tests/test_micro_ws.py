"""Manual OKX microstructure stream test for one instrument.

This is not a pytest test. It is a standalone script for inspecting
trade-level and shallow order-book data from OKX.

By default it subscribes to:
    - `books5` on the public WebSocket
    - `trades` on the public WebSocket
    - `trades-all` on the business WebSocket

Why subscribe to both trade channels:
    - `trades` may aggregate multiple matches into one update
    - `trades-all` contains one trade per update and is closer to tick-by-tick

Usage:
    cd FactorEngine
    python -m tests.test_micro_ws
    python -m tests.test_micro_ws --inst-id BTC-USDT-SWAP --duration 30
    python -m tests.test_micro_ws --trade-mode trades
    python -m tests.test_micro_ws --trade-mode trades-all
    python tests/test_micro_ws.py --inst-id BTC-USDT-SWAP --duration 60
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import json
import logging
from dataclasses import dataclass
from pathlib import Path
import sys
import time

import aiohttp

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

OKX_WS_PUBLIC = "wss://ws.okx.com:8443/ws/v5/public"
OKX_WS_BUSINESS = "wss://ws.okx.com:8443/ws/v5/business"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("test_micro_ws")


def _utc_iso(ts_ms: str | int | float) -> str:
    return dt.datetime.fromtimestamp(float(ts_ms) / 1000, tz=dt.timezone.utc).isoformat()


def _json_dumps(data: dict) -> str:
    return json.dumps(data, ensure_ascii=True, separators=(",", ":"))


class NDJSONWriter:
    """Small helper for appending JSON lines to a file."""

    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self._fp = path.open("a", encoding="utf-8")

    def write(self, row: dict):
        self._fp.write(_json_dumps(row))
        self._fp.write("\n")
        self._fp.flush()

    def close(self):
        self._fp.close()


@dataclass
class StreamStats:
    name: str
    total_messages: int = 0
    total_rows: int = 0
    window_messages: int = 0
    window_rows: int = 0
    last_summary: str = "waiting"
    last_event_ts_ms: int | None = None

    def record(self, rows: int, summary: str, event_ts_ms: int | None):
        self.total_messages += 1
        self.total_rows += rows
        self.window_messages += 1
        self.window_rows += rows
        self.last_summary = summary
        self.last_event_ts_ms = event_ts_ms

    def snapshot_and_reset(self) -> dict:
        age_ms = None
        if self.last_event_ts_ms is not None:
            age_ms = int(time.time() * 1000) - self.last_event_ts_ms

        snapshot = {
            "name": self.name,
            "window_messages": self.window_messages,
            "window_rows": self.window_rows,
            "total_messages": self.total_messages,
            "total_rows": self.total_rows,
            "age_ms": age_ms,
            "summary": self.last_summary,
        }

        self.window_messages = 0
        self.window_rows = 0
        return snapshot


class MicroWSRunner:
    def __init__(self, inst_id: str, trade_mode: str, save_dir: Path):
        self.inst_id = inst_id
        self.trade_mode = trade_mode
        self.save_dir = save_dir
        self.stop_event = asyncio.Event()
        self.stats = {
            "books5": StreamStats("books5"),
            "trades": StreamStats("trades"),
            "trades-all": StreamStats("trades-all"),
        }
        self.writers: dict[str, NDJSONWriter] = {}

    async def run(self, duration: float | None):
        self._open_writers()
        logger.info("Capture dir: %s", self.save_dir)
        logger.info("Instrument: %s | trade_mode=%s", self.inst_id, self.trade_mode)

        async with aiohttp.ClientSession() as session:
            tasks = [
                asyncio.create_task(self._run_public_ws(session), name="public-ws"),
                asyncio.create_task(self._reporter(), name="reporter"),
            ]
            if self.trade_mode in {"trades-all", "both"}:
                tasks.append(
                    asyncio.create_task(self._run_business_ws(session), name="business-ws")
                )

            try:
                if duration is None:
                    await self.stop_event.wait()
                else:
                    await asyncio.wait_for(self.stop_event.wait(), timeout=duration)
            except asyncio.TimeoutError:
                logger.info("Reached duration limit: %.1fs", duration)
            finally:
                self.stop_event.set()
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
                self._close_writers()

    async def _run_public_ws(self, session: aiohttp.ClientSession):
        args = [{"channel": "books5", "instId": self.inst_id}]
        if self.trade_mode in {"trades", "both"}:
            args.append({"channel": "trades", "instId": self.inst_id})
        await self._stream_loop(session, OKX_WS_PUBLIC, args)

    async def _run_business_ws(self, session: aiohttp.ClientSession):
        args = [{"channel": "trades-all", "instId": self.inst_id}]
        await self._stream_loop(session, OKX_WS_BUSINESS, args)

    async def _stream_loop(self, session: aiohttp.ClientSession, url: str, args: list[dict]):
        while not self.stop_event.is_set():
            try:
                async with session.ws_connect(url, heartbeat=20) as ws:
                    await ws.send_json({"op": "subscribe", "args": args})
                    logger.info("Connected to %s for %s", url, ", ".join(arg["channel"] for arg in args))

                    async for msg in ws:
                        if self.stop_event.is_set():
                            break
                        if msg.type == aiohttp.WSMsgType.TEXT:
                            await self._handle_message(msg.data)
                        elif msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                            break
            except asyncio.CancelledError:
                raise
            except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                logger.warning("WS reconnecting after error from %s: %s", url, exc)
                await asyncio.sleep(1)
            except Exception:
                logger.exception("Unexpected WS error from %s", url)
                await asyncio.sleep(1)

    async def _handle_message(self, raw: str):
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            return

        if "event" in payload:
            event = payload.get("event")
            channel = payload.get("arg", {}).get("channel")
            if event == "subscribe":
                logger.info("Subscribed: channel=%s arg=%s", channel, payload.get("arg"))
            elif event == "error":
                logger.error("Subscription error: %s", payload)
            return

        data = payload.get("data")
        if not data:
            return

        channel = payload.get("arg", {}).get("channel", "")
        if channel == "books5":
            self._handle_books5(payload)
        elif channel in {"trades", "trades-all"}:
            self._handle_trades(payload)

    def _handle_books5(self, payload: dict):
        rows = payload.get("data", [])
        if not rows:
            return

        row = rows[0]
        bids = row.get("bids", [])
        asks = row.get("asks", [])
        best_bid = bids[0] if bids else None
        best_ask = asks[0] if asks else None

        summary = "books5 empty"
        if best_bid and best_ask:
            spread = float(best_ask[0]) - float(best_bid[0])
            summary = (
                f"bid={best_bid[0]}@{best_bid[1]} ask={best_ask[0]}@{best_ask[1]} "
                f"spread={spread:.10f}"
            )

        ts_ms = int(row["ts"])
        self.stats["books5"].record(rows=1, summary=summary, event_ts_ms=ts_ms)

        self.writers["books5"].write({
            "received_at_local": dt.datetime.now().astimezone().isoformat(),
            "channel": "books5",
            "instId": self.inst_id,
            "ts": row["ts"],
            "book_time_utc": _utc_iso(row["ts"]),
            "seqId": row.get("seqId"),
            "checksum": row.get("checksum"),
            "best_bid_px": best_bid[0] if best_bid else None,
            "best_bid_sz": best_bid[1] if best_bid else None,
            "best_bid_orders": best_bid[3] if best_bid and len(best_bid) > 3 else None,
            "best_ask_px": best_ask[0] if best_ask else None,
            "best_ask_sz": best_ask[1] if best_ask else None,
            "best_ask_orders": best_ask[3] if best_ask and len(best_ask) > 3 else None,
            "bids": [
                {"px": level[0], "sz": level[1], "orders": level[3] if len(level) > 3 else None}
                for level in bids
            ],
            "asks": [
                {"px": level[0], "sz": level[1], "orders": level[3] if len(level) > 3 else None}
                for level in asks
            ],
        })

    def _handle_trades(self, payload: dict):
        channel = payload.get("arg", {}).get("channel", "")
        rows = payload.get("data", [])
        if not rows:
            return

        last = rows[-1]
        summary = (
            f"last_px={last.get('px')} sz={last.get('sz')} side={last.get('side')} "
            f"count={last.get('count', '1')}"
        )
        ts_ms = int(last["ts"])
        self.stats[channel].record(rows=len(rows), summary=summary, event_ts_ms=ts_ms)

        writer = self.writers[channel]
        for row in rows:
            writer.write({
                "received_at_local": dt.datetime.now().astimezone().isoformat(),
                "channel": channel,
                "instId": row.get("instId", self.inst_id),
                "tradeId": row.get("tradeId"),
                "ts": row["ts"],
                "trade_time_utc": _utc_iso(row["ts"]),
                "px": row.get("px"),
                "sz": row.get("sz"),
                "side": row.get("side"),
                "count": row.get("count"),
            })

    async def _reporter(self):
        while not self.stop_event.is_set():
            await asyncio.sleep(1)
            snapshots = [self.stats["books5"].snapshot_and_reset()]
            if self.trade_mode in {"trades", "both"}:
                snapshots.append(self.stats["trades"].snapshot_and_reset())
            if self.trade_mode in {"trades-all", "both"}:
                snapshots.append(self.stats["trades-all"].snapshot_and_reset())

            lines = [
                f"[{dt.datetime.now().strftime('%H:%M:%S')}] {self.inst_id}",
                *[self._format_stats_line(snapshot) for snapshot in snapshots],
            ]
            logger.info("\n%s", "\n".join(lines))

    @staticmethod
    def _format_stats_line(snapshot: dict) -> str:
        age_ms = snapshot["age_ms"]
        age_text = f"{age_ms:>4}ms" if age_ms is not None else "   -"
        return (
            f"  {snapshot['name']:<10} "
            f"msg/s={snapshot['window_messages']:>4} "
            f"rows/s={snapshot['window_rows']:>4} "
            f"total_msg={snapshot['total_messages']:>5} "
            f"total_rows={snapshot['total_rows']:>5} "
            f"age={age_text}  "
            f"{snapshot['summary']}"
        )

    def _open_writers(self):
        self.writers["books5"] = NDJSONWriter(self.save_dir / "books5.ndjson")
        if self.trade_mode in {"trades", "both"}:
            self.writers["trades"] = NDJSONWriter(self.save_dir / "trades.ndjson")
        if self.trade_mode in {"trades-all", "both"}:
            self.writers["trades-all"] = NDJSONWriter(self.save_dir / "trades_all.ndjson")

    def _close_writers(self):
        for writer in self.writers.values():
            writer.close()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Inspect OKX trades/books5 stream quality.")
    parser.add_argument("--inst-id", default="BTC-USDT-SWAP", help="Instrument ID.")
    parser.add_argument(
        "--trade-mode",
        choices=("trades", "trades-all", "both"),
        default="both",
        help="Which trade stream to subscribe to.",
    )
    parser.add_argument(
        "--duration",
        type=float,
        default=30.0,
        help="Run duration in seconds. Use 0 or a negative value for infinite run.",
    )
    parser.add_argument(
        "--save-dir",
        type=Path,
        default=None,
        help="Directory for NDJSON captures. Default: ./okx_micro_ws_<timestamp>/",
    )
    return parser.parse_args()


async def async_main():
    args = parse_args()
    duration = None if args.duration <= 0 else args.duration
    save_dir = args.save_dir
    if save_dir is None:
        stamp = dt.datetime.now().astimezone().strftime("%Y%m%d_%H%M%S")
        save_dir = Path.cwd() / f"okx_micro_ws_{stamp}"

    runner = MicroWSRunner(
        inst_id=args.inst_id,
        trade_mode=args.trade_mode,
        save_dir=save_dir,
    )
    await runner.run(duration)


def main():
    try:
        asyncio.run(async_main())
    except KeyboardInterrupt:
        logger.info("Stopped by user.")


if __name__ == "__main__":
    main()
