"""Offline private API checks; no real market or messaging calls."""
import json
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import alert_api


TOKEN = "private-monitor-token-for-tests-only"
AUTH = {"Authorization": "Bearer " + TOKEN}
RULE = {"symbol": "005930", "name": "삼성전자", "market": "KOSPI",
        "action": "buy_zone", "lower": 100, "upper": 110}


def test_mobile_retry_at_capacity_keeps_existing_paused_rule(client):
    rule = client.post('/api/alerts/rules', headers=AUTH, json=RULE).json()
    client.patch(f"/api/alerts/rules/{rule['id']}", headers=AUTH, json={'enabled': False})
    store = alert_api.get_store()
    for threshold in range(1, 100):
        store.create({'symbol': '005930', 'name': '삼성전자', 'market': 'KOSPI',
                      'action': 'sell_above', 'threshold': threshold})
    retry = client.post('/api/alerts/rules', headers=AUTH, json=RULE)
    assert retry.status_code == 201
    assert retry.json()['id'] == rule['id']
    assert retry.json()['enabled'] is False
    extra = client.post('/api/alerts/rules', headers=AUTH, json={**RULE, 'lower': 90})
    assert extra.status_code == 409
    assert len(store.list()) == 100


@pytest.fixture
def client(monkeypatch, tmp_path):
    monkeypatch.delenv("VERCEL", raising=False)
    monkeypatch.delenv("VERCEL_ENV", raising=False)
    monkeypatch.delenv("ALERT_SERVICE_URL", raising=False)
    monkeypatch.setenv("ALERT_ACCESS_TOKEN", TOKEN)
    monkeypatch.setenv("SIGNAL_ALERT_DB", str(tmp_path / "alerts.sqlite3"))
    app = FastAPI()
    app.include_router(alert_api.router)
    return TestClient(app)


def test_auth_precedes_private_store_and_identity_lookup(client, monkeypatch):
    lookup = Mock(side_effect=AssertionError("must not look up identity before authentication"))
    store = Mock(side_effect=AssertionError("must not open private database"))
    monkeypatch.setattr(alert_api, "resolve_stock", lookup)
    monkeypatch.setattr(alert_api, "get_store", store)
    for method, path, body in [
        ("GET", "/rules", None), ("POST", "/rules", RULE),
        ("PATCH", "/rules/1", {"enabled": False}),
        ("DELETE", "/rules/1", None), ("GET", "/events", None),
    ]:
        response = client.request(method, "/api/alerts" + path, json=body)
        assert response.status_code == 401
        assert TOKEN not in response.text
    assert not lookup.called and not store.called


def test_unconfigured_never_opens_store(client, monkeypatch):
    monkeypatch.delenv("SIGNAL_ALERT_DB")
    store = Mock(side_effect=AssertionError("must not open an implicit db"))
    monkeypatch.setattr(alert_api, "get_store", store)
    response = client.get("/api/alerts/status")
    assert response.json()["configured"] is False
    assert response.json()["worker_running"] is False
    assert client.get("/api/alerts/rules", headers=AUTH).status_code == 503
    assert not store.called


def test_too_short_key_does_not_enable_service(client, monkeypatch):
    monkeypatch.setenv("ALERT_ACCESS_TOKEN", "short")
    assert client.get("/api/alerts/status").json()["configured"] is False


def test_public_status_is_sanitized(client, monkeypatch):
    store = Mock()
    checked = datetime.now(timezone.utc).isoformat()
    store.status.return_value = {"last_cycle_at": checked, "worker_running": True,
                                 "rule_count": 3, "last_error": "private-company-name-secret"}
    monkeypatch.setattr(alert_api, "get_store", lambda: store)
    monkeypatch.setattr(alert_api, "quote_configured", lambda: True)
    response = client.get("/api/alerts/status")
    data = response.json()
    assert data["worker_running"] is True
    assert data["quote_configured"] is True
    assert data["last_checked_at"] == checked
    assert "rule_count" not in data and "private-company" not in response.text


def test_unreadable_database_does_not_claim_running(client, monkeypatch):
    monkeypatch.setattr(alert_api, "get_store", Mock(side_effect=sqlite3.OperationalError("private-path")))
    response = client.get("/api/alerts/status")
    assert response.status_code == 200 and response.json()["worker_running"] is False
    assert "private-path" not in response.text


def test_canonical_identity_saved_and_known_mismatch_rejected(client, monkeypatch):
    store = Mock()
    store.list.return_value = []
    store.create.side_effect = lambda data: dict(data, id=7)
    monkeypatch.setattr(alert_api, "get_store", lambda: store)
    response = client.post("/api/alerts/rules", headers=AUTH, json={**RULE, "name": "Samsung Electronics"})
    assert response.status_code == 201
    assert response.json()["name"] == "삼성전자"
    assert store.create.call_args.args[0]["name"] == "삼성전자"
    response = client.post("/api/alerts/rules", headers=AUTH, json={**RULE, "name": "SK하이닉스"})
    assert response.status_code == 422
    assert store.create.call_count == 1


@pytest.mark.parametrize("change", [
    {"lower": 120, "upper": 100}, {"lower": 0}, {"lower": None},
    {"threshold": 12}, {"action": "buy_now"}, {"action": "sell_above"},
    {"enabled": "yes"}, {"evidence": {"eligible": True}},
])
def test_invalid_rule_and_client_evidence_rejected(client, change):
    response = client.post("/api/alerts/rules", headers=AUTH, json={**RULE, **change})
    assert response.status_code == 422


def test_nonfinite_rule_price_rejected(client):
    body = json.dumps({**RULE, "lower": float("inf")})
    response = client.post("/api/alerts/rules", headers={**AUTH, "Content-Type": "application/json"}, content=body)
    assert response.status_code == 422


def test_crud_wraps_lists_and_handles_missing_rule(client, monkeypatch):
    store = Mock()
    store.list.return_value = [{"id": 1, **RULE}]
    store.events.return_value = [{"id": 5, "delivery_status": "pending"}]
    store.update_enabled.return_value = {"id": 1, "enabled": False}
    store.delete.return_value = False
    monkeypatch.setattr(alert_api, "get_store", lambda: store)
    assert client.get("/api/alerts/rules", headers=AUTH).json()["items"][0]["id"] == 1
    assert client.get("/api/alerts/events?limit=10", headers=AUTH).json()["items"][0]["id"] == 5
    store.events.assert_called_once_with(limit=10)
    assert client.patch("/api/alerts/rules/1", headers=AUTH, json={"enabled": False}).json()["enabled"] is False
    assert client.delete("/api/alerts/rules/1", headers=AUTH).status_code == 404
    assert client.get("/api/alerts/events?limit=101", headers=AUTH).status_code == 422


class FakeUpstream:
    def __init__(self, payload, status=200):
        self.payload, self.status_code = payload, status
    def __enter__(self): return self
    def __exit__(self, *args): return False
    def iter_content(self, size):
        yield json.dumps(self.payload).encode()


def test_vercel_proxies_only_caller_token_and_never_opens_sqlite(client, monkeypatch):
    monkeypatch.setenv("VERCEL", "1")
    monkeypatch.setenv("ALERT_SERVICE_URL", "https://monitor.example.org")
    remote = Mock(return_value=FakeUpstream({"items": []}))
    monkeypatch.setattr(alert_api.requests, "request", remote)
    monkeypatch.setattr(alert_api, "get_store", Mock(side_effect=AssertionError("Vercel must not open SQLite")))
    assert client.get("/api/alerts/rules", headers=AUTH).status_code == 200
    args, kwargs = remote.call_args
    assert args == ("GET", "https://monitor.example.org/api/alerts/rules")
    assert kwargs["headers"]["Authorization"] == AUTH["Authorization"]
    assert kwargs["allow_redirects"] is False and kwargs["timeout"] == 4
    remote.reset_mock()
    assert client.get("/api/alerts/rules").status_code == 200
    assert "Authorization" not in remote.call_args.kwargs["headers"]


def test_upstream_auth_rejection_and_failed_status_are_safe(client, monkeypatch):
    monkeypatch.setenv("VERCEL", "1")
    monkeypatch.setenv("ALERT_SERVICE_URL", "https://monitor.example.org")
    remote = Mock(return_value=FakeUpstream({"detail": "secret"}, status=401))
    monkeypatch.setattr(alert_api.requests, "request", remote)
    assert client.get("/api/alerts/rules", headers=AUTH).status_code == 401
    response = client.get("/api/alerts/status")
    assert response.status_code == 200 and response.json()["configured"] is False
    assert "secret" not in response.text


@pytest.mark.parametrize("stamp", [None, "bad-time", "2026-11-10T01:00:00", "stale", "future"])
def test_upstream_cannot_claim_running_without_recent_aware_cycle(client, monkeypatch, stamp):
    monkeypatch.setenv("VERCEL", "1")
    monkeypatch.setenv("ALERT_SERVICE_URL", "https://monitor.example.org")
    if stamp == "stale":
        stamp = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()
    if stamp == "future":
        stamp = (datetime.now(timezone.utc) + timedelta(seconds=5)).isoformat()
    remote = Mock(return_value=FakeUpstream({"configured": True, "worker_running": True, "last_checked_at": stamp}))
    monkeypatch.setattr(alert_api.requests, "request", remote)
    response = client.get("/api/alerts/status")
    assert response.json()["configured"] is True
    assert response.json()["worker_running"] is False


@pytest.mark.parametrize("url", ["http://monitor.example.org", "https://u:p@monitor.example.org", "https://monitor.example.org/path", "https://monitor.example.org?secret=1"])
def test_invalid_proxy_base_never_connects(client, monkeypatch, url):
    monkeypatch.setenv("VERCEL", "1")
    monkeypatch.setenv("ALERT_SERVICE_URL", url)
    remote = Mock(side_effect=AssertionError("invalid destination must not connect"))
    monkeypatch.setattr(alert_api.requests, "request", remote)
    assert client.get("/api/alerts/status").json()["configured"] is False
    assert client.get("/api/alerts/rules", headers=AUTH).status_code == 503
    assert not remote.called


def test_upstream_redirect_refused_and_size_limit_enforced(client, monkeypatch):
    monkeypatch.setenv("VERCEL", "1")
    monkeypatch.setenv("ALERT_SERVICE_URL", "https://monitor.example.org")
    remote = Mock(return_value=FakeUpstream({}, status=302))
    monkeypatch.setattr(alert_api.requests, "request", remote)
    assert client.get("/api/alerts/rules", headers=AUTH).status_code == 503
    remote.return_value = FakeUpstream({"items": ["x" * (alert_api._MAX_RESPONSE + 1)]})
    assert client.get("/api/alerts/rules", headers=AUTH).status_code == 503


def test_dedicated_service_starts_and_stops_worker_without_sending(client, monkeypatch):
    import alert_worker
    import monitor_service
    started, stopped = threading.Event(), threading.Event()
    store = object()
    monkeypatch.setattr(monitor_service, "get_store", lambda: store)

    class FakeWorker:
        def __init__(self, actual_store, **kwargs):
            assert actual_store is store
            assert callable(kwargs["evidence_provider"])
        def run(self, stop):
            started.set()
            stop.wait(3)
            stopped.set()

    monkeypatch.setattr(alert_worker, "MonitorWorker", FakeWorker)
    with TestClient(monitor_service.app) as service:
        assert started.wait(1)
        assert service.get("/health").json()["ok"] is True
    assert stopped.is_set()


def test_dedicated_service_refuses_serverless_startup(client, monkeypatch):
    import monitor_service
    monkeypatch.setenv("VERCEL", "1")
    with pytest.raises(RuntimeError, match="상시 실행"):
        with TestClient(monitor_service.app):
            pass
