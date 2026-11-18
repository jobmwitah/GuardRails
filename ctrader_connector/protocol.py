"""cTrader Open API payload constants and JSON message helpers.

The connector intentionally talks JSON over TCP (port 5036) instead of using
the official Twisted/protobuf SDK. That keeps the VPS service small and avoids
pinning an old protobuf runtime, while speaking the same protocol.
"""

from __future__ import annotations

import json
import struct
import uuid

# Common payloads
PROTO_ERROR_RES = 50
PROTO_HEARTBEAT_EVENT = 51

# Open API payloads used by this connector
APP_AUTH_REQ = 2100
APP_AUTH_RES = 2101
ACCOUNT_AUTH_REQ = 2102
ACCOUNT_AUTH_RES = 2103
NEW_ORDER_REQ = 2106
CANCEL_ORDER_REQ = 2108
AMEND_POSITION_SLTP_REQ = 2110
CLOSE_POSITION_REQ = 2111
ASSET_LIST_REQ = 2112
ASSET_LIST_RES = 2113
SYMBOLS_LIST_REQ = 2114
SYMBOLS_LIST_RES = 2115
SYMBOL_BY_ID_REQ = 2116
SYMBOL_BY_ID_RES = 2117
SYMBOLS_FOR_CONVERSION_REQ = 2118
SYMBOLS_FOR_CONVERSION_RES = 2119
TRADER_REQ = 2121
TRADER_RES = 2122
TRADER_UPDATE_EVENT = 2123
RECONCILE_REQ = 2124
RECONCILE_RES = 2125
EXECUTION_EVENT = 2126
SUBSCRIBE_SPOTS_REQ = 2127
SUBSCRIBE_SPOTS_RES = 2128
UNSUBSCRIBE_SPOTS_REQ = 2129
UNSUBSCRIBE_SPOTS_RES = 2130
SPOT_EVENT = 2131
ORDER_ERROR_EVENT = 2132
DEAL_LIST_REQ = 2133
DEAL_LIST_RES = 2134
EXPECTED_MARGIN_REQ = 2139
EXPECTED_MARGIN_RES = 2140
GET_TRENDBARS_REQ = 2137
GET_TRENDBARS_RES = 2138
OA_ERROR_RES = 2142
ACCOUNTS_TOKEN_INVALIDATED_EVENT = 2147
CLIENT_DISCONNECT_EVENT = 2148
GET_ACCOUNTS_REQ = 2149
GET_ACCOUNTS_RES = 2150
POSITION_UNREALIZED_PNL_REQ = 2187
POSITION_UNREALIZED_PNL_RES = 2188


class ProtocolError(RuntimeError):
    pass


class APIError(ProtocolError):
    def __init__(self, error_code: str, description: str = ""):
        self.error_code = error_code or "UNKNOWN_ERROR"
        self.description = description or ""
        super().__init__(self.error_code + (f": {self.description}" if self.description else ""))


def encode_frame(message: dict) -> bytes:
    raw = json.dumps(message, separators=(",", ":")).encode("utf-8")
    if len(raw) > 15_000_000:
        raise ProtocolError("refusing to send a message larger than 15 MB")
    return struct.pack(">I", len(raw)) + raw


def decode_frame(raw: bytes) -> dict:
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProtocolError("received an invalid JSON frame") from exc
    if not isinstance(value, dict):
        raise ProtocolError("received a JSON frame that is not an object")
    return value


def new_message(payload_type: int, payload: dict | None = None) -> dict:
    message = {"clientMsgId": uuid.uuid4().hex, "payloadType": int(payload_type)}
    if payload is not None:
        message["payload"] = payload
    return message


def error_from_message(message: dict) -> APIError | None:
    if message.get("payloadType") not in (PROTO_ERROR_RES, OA_ERROR_RES):
        return None
    payload = message.get("payload") or {}
    return APIError(
        str(payload.get("errorCode") or payload.get("error_code") or "UNKNOWN_ERROR"),
        str(payload.get("description") or ""),
    )
