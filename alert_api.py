"""Private monitoring API and fixed-path proxy for the public Vercel UI."""
from __future__ import annotations

import hmac
import json
import math
import os
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

import requests
from fastapi import APIRouter, Header, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field, StrictBool, field_validator, model_validator

from stock_lookup import resolve_stock
from quotes import configured as quote_configured


router = APIRouter(prefix="/api/alerts", tags=["timing alerts"])
_stores = {}
_store_lock = threading.Lock()
_MAX_RESPONSE = 512 * 1024


def _serverless():
    return bool(os.getenv("VERCEL") or os.getenv("VERCEL_ENV"))


def _remote_url():
    value = os.getenv("ALERT_SERVICE_URL", "").strip().rstrip("/")
    if not value:
        return None
    parts = urlsplit(value)
    if (parts.scheme != "https" or not parts.hostname or parts.username or parts.password
            or parts.query or parts.fragment or parts.path not in ("", "/")):
        return None
    return value


def _local_configured():
    return bool(len(os.getenv("ALERT_ACCESS_TOKEN", "")) >= 24
                and os.getenv("SIGNAL_ALERT_DB", "").strip() and not _serverless())


def _authorize(authorization):
    if not _local_configured():
        raise HTTPException(503, "감시 서버가 연결되지 않았습니다.")
    expected = os.environ["ALERT_ACCESS_TOKEN"]
    supplied = authorization[7:] if authorization and authorization.startswith("Bearer ") else ""
    if not supplied or not hmac.compare_digest(supplied.encode(), expected.encode()):
        raise HTTPException(401, "개인 알림 접근키를 확인해 주세요.", headers={"WWW-Authenticate": "Bearer"})


def get_store():
    """Only called after configuration/auth; Vercel never opens a SQLite file."""
    if not _local_configured():
        raise HTTPException(503, "감시 서버가 연결되지 않았습니다.")
    from alert_store import AlertStore
    path = str(Path(os.environ["SIGNAL_ALERT_DB"]).expanduser().resolve())
    with _store_lock:
        if path not in _stores:
            _stores[path] = AlertStore(path)
        return _stores[path]


def _proxy(method, path, authorization=None, body=None, params=None):
    """The configured service is the sole destination; redirects are refused."""
    base = _remote_url()
    if not base:
        raise HTTPException(503, "감시 서버가 연결되지 않았습니다.")
    headers = {"Accept": "application/json"}
    if authorization:
        # Never substitute a shared owner's token for an unauthenticated caller.
        headers["Authorization"] = authorization
    try:
        with requests.request(method, base + "/api/alerts" + path, headers=headers,
                              json=body, params=params, timeout=4, stream=True,
                              allow_redirects=False) as response:
            if response.status_code in (401, 403):
                raise HTTPException(401, "개인 알림 접근키를 확인해 주세요.")
            if response.status_code == 404:
                raise HTTPException(404, "알림 규칙을 찾을 수 없습니다.")
            if response.status_code == 422:
                raise HTTPException(422, "알림 가격과 종목을 확인해 주세요.")
            if response.status_code == 409:
                raise HTTPException(409, "알림 규칙을 다시 확인해 주세요.")
            if not 200 <= response.status_code < 300:
                raise HTTPException(503, "감시 서버에 연결할 수 없습니다.")
            chunks, size = [], 0
            for chunk in response.iter_content(16384):
                size += len(chunk)
                if size > _MAX_RESPONSE:
                    raise ValueError("oversized monitoring response")
                chunks.append(chunk)
            def invalid_constant(value):
                raise ValueError("nonfinite upstream JSON")
            return json.loads(b"".join(chunks), parse_constant=invalid_constant)
    except HTTPException:
        raise
    except (requests.RequestException, ValueError, TypeError):
        raise HTTPException(503, "감시 서버 응답을 확인할 수 없습니다.") from None


def _public_status(raw, configured=True):
    # A public status must never leak rule names, counts, errors or credentials.
    checked = raw.get("last_checked_at") or raw.get("last_cycle_at")
    interval = raw.get("interval_seconds", 15)
    if isinstance(interval, bool) or not isinstance(interval, (int, float)) or not math.isfinite(interval) or not 5 <= interval <= 300:
        interval = 15
    recent = False
    if isinstance(checked, str):
        try:
            stamp = datetime.fromisoformat(checked.replace("Z", "+00:00"))
            recent = stamp.utcoffset() is not None and 0 <= (datetime.now(timezone.utc) - stamp).total_seconds() <= max(60, interval * 3)
        except (ValueError, TypeError):
            checked = None
    running = configured and raw.get("worker_running") is True and recent
    note = "감시 서버 미연결 · 자동 알림을 받으려면 감시 서버를 연결하세요."
    if configured:
        note = "감시 서버 작동 중 · 시세·자료 조건이 맞을 때만 알림을 생성합니다." if running else "감시 서버의 최근 점검이 확인되지 않았습니다. 자동 알림 대기 중입니다."
    return {"configured": configured, "worker_running": running,
            "last_checked_at": checked if isinstance(checked, str) else None,
            "interval_seconds": interval,
            "quote_configured": raw.get("quote_configured") is True,
            "delivery_configured": raw.get("delivery_configured") is True,
            "note": note}


@router.get("/status")
def status():
    if _serverless() or _remote_url():
        try:
            upstream = _proxy("GET", "/status")
            if not isinstance(upstream, dict) or upstream.get("configured") is not True:
                raise ValueError("unconfigured")
            return _public_status(upstream)
        except (HTTPException, ValueError):
            return _public_status({}, configured=False)
    if not _local_configured():
        return _public_status({}, configured=False)
    try:
        raw = get_store().status()
    except (sqlite3.Error, OSError):
        return _public_status({}, configured=False)
    raw["quote_configured"] = quote_configured()
    raw["delivery_configured"] = bool(os.getenv("TELEGRAM_BOT_TOKEN") and os.getenv("TELEGRAM_CHAT_ID"))
    return _public_status(raw)


class RuleCreate(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    symbol: str = Field(min_length=1, max_length=18)
    name: str = Field(default="", max_length=80)
    market: Literal["KOSPI", "KOSDAQ", "US"]
    action: Literal["buy_zone", "sell_above", "stop_below"]
    lower: float | None = Field(default=None, gt=0, strict=True)
    upper: float | None = Field(default=None, gt=0, strict=True)
    threshold: float | None = Field(default=None, gt=0, strict=True)
    enabled: StrictBool = True

    @field_validator("lower", "upper", "threshold", mode="before")
    @classmethod
    def finite_price(cls, value):
        # FastAPI's default validation error cannot JSON-encode a raw Infinity/NaN.
        if isinstance(value, float) and not math.isfinite(value):
            raise HTTPException(422, "알림 가격은 유한한 양수여야 합니다.")
        return value

    @model_validator(mode="after")
    def validate_prices(self):
        if self.action == "buy_zone":
            if self.lower is None or self.upper is None or self.lower > self.upper or self.threshold is not None:
                raise ValueError("매수 관찰 하단·상단 가격을 순서대로 입력하세요.")
        elif self.threshold is None or self.lower is not None or self.upper is not None:
            raise ValueError("매도·손절 관찰 기준 가격을 입력하세요.")
        return self


class RuleUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: StrictBool


def _is_proxy():
    return _serverless() or bool(_remote_url())


@router.get("/rules")
def list_rules(authorization: str | None = Header(default=None)):
    if _is_proxy():
        return _proxy("GET", "/rules", authorization)
    _authorize(authorization)
    return {"items": get_store().list()}


@router.post("/rules", status_code=201)
def create_rule(req: RuleCreate, authorization: str | None = Header(default=None)):
    if _is_proxy():
        return _proxy("POST", "/rules", authorization, req.model_dump())
    _authorize(authorization)
    try:
        identity = resolve_stock(req.symbol, req.market, req.name)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from None
    store = get_store()
    data = req.model_dump()
    data.update({key: identity[key] for key in ("symbol", "name", "market")})
    try:
        # The store checks capacity inside its transaction, after returning an
        # identical existing rule. Retrying a timed-out POST remains idempotent.
        return store.create(data)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from None


@router.patch("/rules/{rule_id}")
def update_rule(rule_id: int, req: RuleUpdate, authorization: str | None = Header(default=None)):
    if rule_id <= 0:
        raise HTTPException(404, "알림 규칙을 찾을 수 없습니다.")
    if _is_proxy():
        return _proxy("PATCH", f"/rules/{rule_id}", authorization, req.model_dump())
    _authorize(authorization)
    result = get_store().update_enabled(rule_id, req.enabled)
    if result is None:
        raise HTTPException(404, "알림 규칙을 찾을 수 없습니다.")
    return result


@router.delete("/rules/{rule_id}")
def delete_rule(rule_id: int, authorization: str | None = Header(default=None)):
    if rule_id <= 0:
        raise HTTPException(404, "알림 규칙을 찾을 수 없습니다.")
    if _is_proxy():
        return _proxy("DELETE", f"/rules/{rule_id}", authorization)
    _authorize(authorization)
    if not get_store().delete(rule_id):
        raise HTTPException(404, "알림 규칙을 찾을 수 없습니다.")
    return {"ok": True}


@router.get("/events")
def list_events(limit: int = Query(default=50, ge=1, le=100), authorization: str | None = Header(default=None)):
    if _is_proxy():
        return _proxy("GET", "/events", authorization, params={"limit": limit})
    _authorize(authorization)
    return {"items": get_store().events(limit=limit)}
