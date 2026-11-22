"""Async JSON-over-TLS client for the cTrader Open API.

Every message is a UTF-8 JSON object terminated by ``\\n``. The server may
return several objects in one TCP read and a read may split an object, so the
reader decodes incrementally instead of assuming any fixed framing.
"""

from __future__ import annotations

import asyncio
import codecs
import logging
import ssl

from . import protocol

logger = logging.getLogger(__name__)

MAX_FRAME = 15_000_000
_READ_CHUNK = 65536
_CLOSED = object()


class ConnectionClosed(ConnectionError):
    pass


class CTraderJSONClient:
    def __init__(self, host: str, port: int = 5036, *, connect_timeout: float = 15.0,
                 request_timeout: float = 12.0, heartbeat_seconds: float = 10.0):
        self.host = host
        self.port = port
        self.connect_timeout = connect_timeout
        self.request_timeout = request_timeout
        self.heartbeat_seconds = heartbeat_seconds
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._read_task: asyncio.Task | None = None
        self._heartbeat_task: asyncio.Task | None = None
        self._pending: dict[str, asyncio.Future] = {}
        self._events: asyncio.Queue = asyncio.Queue()
        self._closed = asyncio.Event()
        self._close_reason = ""

    @property
    def closed(self) -> bool:
        return self._closed.is_set()

    async def connect(self) -> None:
        context = ssl.create_default_context()
        try:
            self._reader, self._writer = await asyncio.wait_for(
                asyncio.open_connection(
                    self.host, self.port, ssl=context, server_hostname=self.host
                ),
                timeout=self.connect_timeout,
            )
        except (OSError, asyncio.TimeoutError) as exc:
            raise ConnectionClosed(f"could not connect to {self.host}:{self.port}: {exc}") from None
        self._read_task = asyncio.create_task(self._read_loop(), name=f"ctrader-read-{self.host}")
        self._heartbeat_task = asyncio.create_task(
            self._heartbeat_loop(), name=f"ctrader-heartbeat-{self.host}"
        )
        logger.info("connected to cTrader JSON endpoint %s:%s", self.host, self.port)

    async def close(self) -> None:
        self._fail(ConnectionClosed("client closed"))
        if self._heartbeat_task:
            self._heartbeat_task.cancel()
        if self._read_task:
            self._read_task.cancel()
        writer = self._writer
        self._writer = None
        if writer:
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass

    async def _read_loop(self) -> None:
        assert self._reader is not None
        text_decoder = codecs.getincrementaldecoder("utf-8")()
        buffer = ""
        try:
            while True:
                chunk = await self._reader.read(_READ_CHUNK)
                if not chunk:
                    raise ConnectionClosed("cTrader closed the connection")
                buffer += text_decoder.decode(chunk)
                if len(buffer) > MAX_FRAME:
                    raise protocol.ProtocolError(
                        "incomplete cTrader message exceeded the maximum size"
                    )
                messages, buffer = protocol.decode_available(buffer)
                for message in messages:
                    self._dispatch(message)
        except asyncio.CancelledError:
            raise
        except (asyncio.IncompleteReadError, ConnectionError, OSError) as exc:
            self._fail(ConnectionClosed(f"connection lost: {exc}"))
        except Exception as exc:
            self._fail(exc)

    def _dispatch(self, message: dict) -> None:
        message_id = message.get("clientMsgId")
        future = self._pending.pop(message_id, None) if message_id else None
        if future is not None and not future.done():
            future.set_result(message)
        elif (message.get("payloadType") in (protocol.PROTO_ERROR_RES, protocol.OA_ERROR_RES)
              and len(self._pending) == 1):
            # Error responses are not guaranteed to echo clientMsgId.
            # This connector keeps at most one request in flight per
            # connection, so attribute an untagged error to it.
            (only_future,) = self._pending.values()
            if not only_future.done():
                only_future.set_result(message)
            else:
                self._events.put_nowait(message)
        else:
            self._events.put_nowait(message)

    async def _heartbeat_loop(self) -> None:
        try:
            while not self.closed:
                await asyncio.sleep(self.heartbeat_seconds)
                if self.closed:
                    return
                await self.send_message({"payloadType": protocol.PROTO_HEARTBEAT_EVENT})
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._fail(exc)

    def _fail(self, exc: Exception) -> None:
        if self._closed.is_set():
            return
        self._closed.set()
        self._close_reason = str(exc)
        for future in list(self._pending.values()):
            if not future.done():
                future.set_exception(ConnectionClosed(self._close_reason))
        self._pending.clear()
        try:
            self._events.put_nowait(_CLOSED)
        except Exception:
            pass
        logger.warning("cTrader connection closed: %s", self._close_reason)

    async def send_message(self, message: dict) -> None:
        writer = self._writer
        if writer is None or self.closed:
            raise ConnectionClosed(self._close_reason or "not connected")
        writer.write(protocol.encode_frame(message))
        await writer.drain()

    async def send(self, payload_type: int, payload: dict | None = None) -> str:
        message = protocol.new_message(payload_type, payload)
        await self.send_message(message)
        return message["clientMsgId"]

    async def request(self, payload_type: int, payload: dict | None = None, *,
                      expect: int | tuple[int, ...] | None = None,
                      timeout: float | None = None) -> dict:
        message = protocol.new_message(payload_type, payload)
        future = asyncio.get_running_loop().create_future()
        self._pending[message["clientMsgId"]] = future
        try:
            await self.send_message(message)
            result = await asyncio.wait_for(
                future, timeout=timeout if timeout is not None else self.request_timeout
            )
        except asyncio.TimeoutError:
            raise protocol.ProtocolError(
                f"cTrader request {payload_type} timed out"
            ) from None
        finally:
            self._pending.pop(message["clientMsgId"], None)

        error = protocol.error_from_message(result)
        if error:
            raise error
        if expect is not None:
            allowed = (expect,) if isinstance(expect, int) else expect
            if result.get("payloadType") not in allowed:
                raise protocol.ProtocolError(
                    f"unexpected payload type {result.get('payloadType')} "
                    f"for request {payload_type}"
                )
        return result

    async def next_event(self) -> dict:
        if self.closed and self._events.empty():
            raise ConnectionClosed(self._close_reason or "connection closed")
        item = await self._events.get()
        if item is _CLOSED:
            raise ConnectionClosed(self._close_reason or "connection closed")
        return item
