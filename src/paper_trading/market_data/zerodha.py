"""Zerodha tick normalization and read-only KiteTicker transport."""

from __future__ import annotations

import logging
from datetime import datetime
from queue import Empty, Full, Queue
from threading import Lock
from typing import Any, Callable, Iterable, Mapping, Protocol, Sequence
from zoneinfo import ZoneInfo

from ..events import CandleEvent, TickEvent
from ..models import Instrument, Tick
from .candle_builder import OneMinuteCandleBuilder, TickDisposition
from .health import FeedHealth, FeedHealthSnapshot

logger = logging.getLogger(__name__)
IST = ZoneInfo("Asia/Kolkata")
_STOP = object()


class LiveTickError(ValueError):
    pass


class UnknownInstrumentTokenError(LiveTickError):
    pass


class LiveFeedConnectionError(RuntimeError):
    pass


class KiteTickerLike(Protocol):
    MODE_FULL: str
    MODE_QUOTE: str
    MODE_LTP: str
    on_ticks: Any
    on_connect: Any
    on_close: Any
    on_error: Any
    on_reconnect: Any
    on_noreconnect: Any

    def connect(self, threaded: bool = False) -> None: ...
    def subscribe(self, tokens: list[int]) -> Any: ...
    def set_mode(self, mode: str, tokens: list[int]) -> Any: ...
    def close(self, *args: Any, **kwargs: Any) -> Any: ...
    def stop_retry(self) -> Any: ...


class ZerodhaTickNormalizer:
    def __init__(
        self,
        instruments: Sequence[Instrument],
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.instruments = {
            instrument.instrument_token: instrument
            for instrument in instruments
            if instrument.instrument_token is not None
        }
        self.clock = clock or (lambda: datetime.now(IST))

    def normalize(self, raw: Mapping[str, Any], received_at: datetime | None = None) -> Tick:
        try:
            token = int(raw["instrument_token"])
            last_price = float(raw["last_price"])
        except (KeyError, TypeError, ValueError) as exc:
            raise LiveTickError(f"malformed required tick fields: {exc}") from exc
        instrument = self.instruments.get(token)
        if instrument is None:
            raise UnknownInstrumentTokenError(f"unsubscribed instrument token: {token}")
        if instrument.instrument_token != token:
            raise UnknownInstrumentTokenError(f"instrument token mismatch: {token}")
        exchange_timestamp = self._timestamp(raw.get("exchange_timestamp"))
        received = self._received_timestamp(received_at or self.clock())
        volume_value = raw.get("volume_traded", raw.get("volume"))
        try:
            volume = int(volume_value) if volume_value is not None else None
        except (TypeError, ValueError) as exc:
            raise LiveTickError(f"invalid cumulative volume: {volume_value!r}") from exc
        bid, ask = self._best_prices(raw.get("depth"))
        try:
            return Tick(
                instrument=instrument,
                exchange_timestamp=exchange_timestamp,
                received_timestamp=received,
                last_price=last_price,
                volume=volume,
                best_bid=bid,
                best_ask=ask,
            )
        except ValueError as exc:
            raise LiveTickError(str(exc)) from exc

    @staticmethod
    def _timestamp(value: Any) -> datetime | None:
        if value is None:
            return None
        if not isinstance(value, datetime):
            raise LiveTickError("exchange_timestamp must be a datetime when present")
        if value.tzinfo is None:
            return value.replace(tzinfo=IST)
        return value.astimezone(IST)

    @staticmethod
    def _received_timestamp(value: datetime) -> datetime:
        if value.tzinfo is None:
            return value.replace(tzinfo=IST)
        return value.astimezone(IST)

    @staticmethod
    def _best_prices(depth: Any) -> tuple[float | None, float | None]:
        if not isinstance(depth, Mapping):
            return None, None
        buy = depth.get("buy") or []
        sell = depth.get("sell") or []
        try:
            bid = float(buy[0]["price"]) if buy else None
            ask = float(sell[0]["price"]) if sell else None
        except (KeyError, TypeError, ValueError, IndexError) as exc:
            raise LiveTickError(f"invalid market depth: {exc}") from exc
        return bid, ask


class ZerodhaLiveFeed:
    """Queue-backed read-only adapter around KiteTicker callbacks."""

    def __init__(
        self,
        api_key: str,
        access_token: str,
        instruments: Sequence[Instrument],
        *,
        mode: str = "full",
        stale_after_seconds: float = 10,
        reconnect_max_tries: int = 10,
        reconnect_max_delay: int = 30,
        queue_size: int = 10_000,
        clock: Callable[[], datetime] | None = None,
        ticker_factory: Callable[..., KiteTickerLike] | None = None,
        candle_builder: OneMinuteCandleBuilder | None = None,
    ) -> None:
        if not api_key or not access_token:
            raise ValueError("KITE_API_KEY and KITE_ACCESS_TOKEN are required")
        if not instruments or any(item.instrument_token is None for item in instruments):
            raise ValueError("all live instruments require instrument_token")
        if mode not in {"full", "quote", "ltp"}:
            raise ValueError("mode must be full, quote, or ltp")
        self.api_key = api_key
        self.access_token = access_token
        self.instruments = tuple(instruments)
        self.tokens = [int(item.instrument_token) for item in instruments if item.instrument_token is not None]
        self.mode = mode
        self.reconnect_max_tries = reconnect_max_tries
        self.reconnect_max_delay = reconnect_max_delay
        self.clock = clock or (lambda: datetime.now(IST))
        self.health = FeedHealth(stale_after_seconds, self.clock)
        self.normalizer = ZerodhaTickNormalizer(instruments, self.clock)
        self.candle_builder = candle_builder or OneMinuteCandleBuilder()
        self._ticker_factory = ticker_factory or self._default_ticker_factory
        self._ticker: KiteTickerLike | None = None
        self._queue: Queue[object] = Queue(maxsize=queue_size)
        self._running = False
        self._fatal_error: LiveFeedConnectionError | None = None
        self._stale_logged = False
        self._lock = Lock()

    def start(self) -> None:
        with self._lock:
            if self._running:
                raise RuntimeError("live feed is already running")
            self._running = True
            self._fatal_error = None
        self.health.mark_connecting()
        logger.info("connecting to Zerodha live market data")
        try:
            ticker = self._ticker_factory(
                self.api_key,
                self.access_token,
                reconnect=True,
                reconnect_max_tries=self.reconnect_max_tries,
                reconnect_max_delay=self.reconnect_max_delay,
            )
            self._ticker = ticker
            ticker.on_ticks = self._on_ticks
            ticker.on_connect = self._on_connect
            ticker.on_close = self._on_close
            ticker.on_error = self._on_error
            ticker.on_reconnect = self._on_reconnect
            ticker.on_noreconnect = self._on_noreconnect
            ticker.connect(threaded=True)
        except Exception as exc:
            self._fatal(f"unable to connect to KiteTicker: {exc}")
            raise LiveFeedConnectionError(str(exc)) from exc

    def stop(self) -> None:
        with self._lock:
            if not self._running:
                return
            self._running = False
        if self._ticker is not None:
            try:
                self._ticker.stop_retry()
                self._ticker.close()
            except Exception as exc:
                logger.warning("error during KiteTicker shutdown: %s", exc)
        self.health.mark_stopped()
        self._put(_STOP)
        logger.info("Zerodha live feed stopped cleanly")

    def events(self) -> Iterable[TickEvent | CandleEvent]:
        if not self._running and self._fatal_error is None:
            raise RuntimeError("feed must be started before reading events")
        while self._running or not self._queue.empty():
            try:
                item = self._queue.get(timeout=0.5)
            except Empty:
                if self._fatal_error is not None:
                    raise self._fatal_error
                continue
            if item is _STOP:
                break
            assert isinstance(item, Tick)
            update = self.candle_builder.process(item)
            if update.disposition is TickDisposition.DUPLICATE:
                self.health.increment("duplicate_ticks")
                self.health.increment("dropped_ticks")
                continue
            if update.disposition is TickDisposition.OUT_OF_ORDER:
                self.health.increment("out_of_order_ticks")
                self.health.increment("dropped_ticks")
                logger.warning("dropped out-of-order tick for %s", item.instrument.key)
                continue
            if update.disposition is TickDisposition.MISSING_TIMESTAMP:
                self.health.increment("dropped_ticks")
                logger.warning("tick lacks exchange timestamp; candle aggregation skipped")
                yield TickEvent(item)
                continue
            if update.disposition is TickDisposition.VOLUME_RESET:
                self.health.increment("volume_regressions")
                logger.warning("cumulative volume regressed for %s; baseline reset", item.instrument.key)
            if update.missing_minutes:
                logger.warning(
                    "%s has %d minute(s) without ticks; no candles fabricated",
                    item.instrument.key,
                    update.missing_minutes,
                )
            if update.completed_candle is not None:
                yield CandleEvent(update.completed_candle)
            yield TickEvent(item)
        if self._fatal_error is not None:
            raise self._fatal_error

    def health_snapshot(self) -> FeedHealthSnapshot:
        snapshot = self.health.snapshot()
        if snapshot.state.value == "STALE" and not self._stale_logged:
            logger.warning("live market-data feed is stale")
            self._stale_logged = True
        elif snapshot.state.value == "HEALTHY":
            self._stale_logged = False
        return snapshot

    def _on_ticks(self, ws: KiteTickerLike, raw_ticks: list[Mapping[str, Any]]) -> None:
        received = self.clock()
        for raw in raw_ticks:
            try:
                tick = self.normalizer.normalize(raw, received)
            except LiveTickError as exc:
                self.health.increment("malformed_ticks")
                self.health.increment("dropped_ticks")
                logger.warning("dropped malformed Zerodha tick: %s", exc)
                continue
            self.health.record_tick(tick.received_timestamp, tick.exchange_timestamp)
            self._put(tick)

    def _on_connect(self, ws: KiteTickerLike, response: Any) -> None:
        self.health.mark_connected()
        ws.subscribe(self.tokens)
        mode = getattr(ws, f"MODE_{self.mode.upper()}")
        ws.set_mode(mode, self.tokens)
        logger.info("connected and subscribed in %s mode: %s", self.mode, self.tokens)

    def _on_close(self, ws: KiteTickerLike, code: int, reason: str) -> None:
        if self._is_auth_error(code, reason):
            try:
                ws.stop_retry()
            finally:
                self._fatal(
                    "Zerodha authentication failed; refresh KITE_ACCESS_TOKEN manually "
                    f"(KiteTicker closed {code}: {reason})"
                )
            return
        self.health.mark_disconnected(str(reason))
        logger.info("KiteTicker disconnected (%s): %s", code, reason)

    def _on_error(self, ws: KiteTickerLike, code: int, reason: str) -> None:
        message = f"KiteTicker error {code}: {reason}"
        if self._is_auth_error(code, reason):
            try:
                ws.stop_retry()
            finally:
                self._fatal(
                    f"Zerodha authentication failed; refresh KITE_ACCESS_TOKEN manually ({message})"
                )
            return
        self.health.mark_disconnected(message)
        logger.error(message)

    def _on_reconnect(self, ws: KiteTickerLike, attempts_count: int) -> None:
        self.health.record_reconnect()
        self.health.mark_connecting()
        logger.info("KiteTicker reconnect attempt %s", attempts_count)

    def _on_noreconnect(self, ws: KiteTickerLike) -> None:
        self._fatal("KiteTicker exhausted reconnect attempts")

    def _fatal(self, message: str) -> None:
        logger.error(message)
        self.health.mark_error(message)
        self._fatal_error = LiveFeedConnectionError(message)
        with self._lock:
            self._running = False
        self._put(_STOP)

    @staticmethod
    def _is_auth_error(code: int, reason: str) -> bool:
        return code in {401, 403, 4001, 4003} or any(
            word in str(reason).lower() for word in ("token", "auth", "permission")
        )

    def _put(self, item: object) -> None:
        try:
            self._queue.put_nowait(item)
        except Full:
            self.health.increment("dropped_ticks")
            logger.warning("live tick queue is full; item dropped")

    @staticmethod
    def _default_ticker_factory(*args: Any, **kwargs: Any) -> KiteTickerLike:
        from kiteconnect import KiteTicker

        return KiteTicker(*args, **kwargs)
