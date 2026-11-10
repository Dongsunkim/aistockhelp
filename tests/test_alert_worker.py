from datetime import datetime, timedelta, timezone
import threading

from alert_store import AlertStore
from alert_worker import MonitorWorker, fetch_official_quote


NOW = datetime(2026, 11, 10, 1, 0, tzinfo=timezone.utc)


def rule(store, **overrides):
    return store.create({'symbol': '005930', 'name': '삼성전자', 'market': 'KOSPI',
                         'action': 'sell_above', 'threshold': 70000, **overrides})


def quote(symbol='005930', market='KOSPI', price=71000, **overrides):
    return {'symbol': symbol, 'market': market, 'price': price,
            'provider': '한국투자증권 KIS', 'verified_live': True, 'is_delayed': False,
            'exchange_timestamp': NOW.isoformat(), **overrides}


def test_shared_quotes_and_restart_dedup(tmp_path):
    store = AlertStore(tmp_path / 'alerts.sqlite3')
    rule(store)
    rule(store, action='stop_below', threshold=72000)
    calls = []
    def provider(symbol, market):
        calls.append(symbol)
        return quote(symbol, market)
    worker = MonitorWorker(store, provider)
    assert worker.run_once(NOW)['events'] == 2
    assert calls == ['005930']
    assert MonitorWorker(AlertStore(store.path), provider).run_once(NOW)['events'] == 0
    assert store.status(NOW)['worker_running'] is True


def test_provider_failure_does_not_stop_other_rule(tmp_path):
    store = AlertStore(tmp_path / 'alerts.sqlite3')
    bad = rule(store)
    rule(store, symbol='000660', name='SK하이닉스')
    def provider(symbol, market):
        if symbol == '005930':
            raise RuntimeError('secret provider URL should not escape')
        return quote(symbol, market)
    result = MonitorWorker(store, provider).run_once(NOW)
    assert result['failures'] == 1
    assert result['events'] == 1
    assert next(r for r in store.list() if r['id'] == bad['id'])['last_status'] == 'blocked'
    assert 'secret' not in str(store.status(NOW))


def test_unverified_quotes_never_emit_or_rearm(tmp_path):
    store = AlertStore(tmp_path / 'alerts.sqlite3')
    rule(store)
    worker = MonitorWorker(store, lambda *args: quote())
    worker.run_once(NOW)
    worker.quote_provider = lambda *args: quote(price=60000, verified_live=False)
    worker.run_once(NOW)
    worker.quote_provider = lambda *args: quote()
    worker.run_once(NOW)
    assert len(store.events()) == 1


def test_missing_research_blocks_buy_but_not_price_rule(tmp_path):
    store = AlertStore(tmp_path / 'alerts.sqlite3')
    rule(store, action='buy_zone', lower=69000, upper=72000)
    rule(store)
    result = MonitorWorker(store, lambda *args: quote()).run_once(NOW)
    assert result['events'] == 1
    assert len(store.events()) == 1
    assert store.events()[0]['action'] == 'sell_above'


def test_send_failure_retries_without_marking_sent(tmp_path):
    store = AlertStore(tmp_path / 'alerts.sqlite3')
    rule(store)
    sent = []
    def failure(event):
        sent.append(event['id'])
        raise RuntimeError('https://bot-secret-should-not-be-stored')
    worker = MonitorWorker(store, lambda *args: quote(), send_provider=failure)
    worker.run_once(NOW)
    assert len(sent) == 1
    assert store.events()[0]['delivery_status'] == 'pending'
    assert 'secret' not in str(store.events())
    worker.send_provider = lambda event: sent.append(event['id'])
    worker.run_once(NOW + timedelta(seconds=31))
    assert len(sent) == 2
    assert sent[0] == sent[1]
    assert store.events()[0]['delivery_status'] == 'sent'


def test_no_config_never_attempts_external_delivery(tmp_path, monkeypatch):
    monkeypatch.delenv('TELEGRAM_BOT_TOKEN', raising=False)
    monkeypatch.delenv('TELEGRAM_CHAT_ID', raising=False)
    monkeypatch.setattr('alert_worker.send_telegram', lambda event: (_ for _ in ()).throw(AssertionError()))
    store = AlertStore(tmp_path / 'alerts.sqlite3')
    rule(store)
    MonitorWorker(store, lambda *args: quote()).run_once(NOW)
    assert store.events()[0]['delivery_status'] == 'pending'


def test_official_adapter_never_overwrites_explicit_identity(monkeypatch):
    monkeypatch.setattr('quotes.fetch_monitor_quote', lambda *args: quote('000660'), raising=False)
    assert fetch_official_quote('005930', 'KOSPI')['symbol'] == '000660'


def test_stopped_worker_does_not_claim_alive(tmp_path):
    store = AlertStore(tmp_path / 'alerts.sqlite3')
    stop = threading.Event()
    stop.set()
    MonitorWorker(store).run(stop)
    assert not store.status()['worker_running']


def test_minimum_poll_interval(tmp_path):
    assert MonitorWorker(AlertStore(tmp_path / 'alerts.sqlite3'), interval_seconds=0).interval_seconds == 5


def test_deleted_rule_during_provider_call_has_no_event(tmp_path):
    store = AlertStore(tmp_path / 'alerts.sqlite3')
    target = rule(store)
    def provider(*args):
        store.delete(target['id'])
        return quote()
    MonitorWorker(store, provider).run_once(NOW)
    assert store.events() == []


def test_expired_failed_message_is_never_sent_later(tmp_path):
    store = AlertStore(tmp_path / 'alerts.sqlite3')
    rule(store)
    attempted = []
    def failed(event):
        attempted.append(event['id'])
        return False
    worker = MonitorWorker(store, lambda *args: quote(), send_provider=failed)
    worker.run_once(NOW)
    worker.send_provider = lambda event: attempted.append(event['id'])
    worker.run_once(NOW + timedelta(seconds=121))
    assert len(attempted) == 1
    assert store.events()[0]['delivery_status'] == 'expired'


def test_old_unconfigured_backlog_expires_before_delivery_enabled(tmp_path, monkeypatch):
    monkeypatch.delenv('TELEGRAM_BOT_TOKEN', raising=False)
    monkeypatch.delenv('TELEGRAM_CHAT_ID', raising=False)
    store = AlertStore(tmp_path / 'alerts.sqlite3')
    rule(store)
    worker = MonitorWorker(store, lambda *args: quote())
    worker.run_once(NOW)
    worker.run_once(NOW + timedelta(seconds=121))
    assert store.events()[0]['delivery_status'] == 'expired'


def test_delivery_clock_advances_for_each_claim(tmp_path, monkeypatch):
    store = AlertStore(tmp_path / 'alerts.sqlite3')
    first = rule(store)
    second = rule(store, action='stop_below', threshold=72000)
    observed = {'status': 'inside', 'inside': True, 'price': 71000, 'reason': '',
                'message': '관찰 가격 도달', 'quote_timestamp': NOW.isoformat()}
    store.record_observation(first['id'], 1, observed, NOW)
    store.record_observation(second['id'], 1, observed, NOW)
    times = iter([NOW, NOW, NOW + timedelta(seconds=119), NOW + timedelta(seconds=121)])
    class AdvancingDatetime:
        @staticmethod
        def now(tz):
            return next(times)
    monkeypatch.setattr('alert_worker.datetime', AdvancingDatetime)
    sent = []
    MonitorWorker(store, send_provider=lambda event: sent.append(event['id']))._deliver()
    assert len(sent) == 1
    assert sorted(e['delivery_status'] for e in store.events()) == ['expired', 'sent']


def test_slow_research_does_not_delay_price_checks_or_initially_allow_buy(tmp_path):
    store = AlertStore(tmp_path / 'alerts.sqlite3')
    rule(store, action='buy_zone', lower=69000, upper=72000)
    rule(store)
    started, release = threading.Event(), threading.Event()
    def slow(*args):
        started.set()
        release.wait(2)
        return {'eligible': False}
    worker = MonitorWorker(store, lambda *args: quote(), evidence_provider=slow)
    try:
        assert worker.run_once(NOW)['events'] == 1
        assert started.wait(1)
        assert store.events()[0]['action'] == 'sell_above'
        assert store.status(NOW)['worker_running']
        worker.run_once(NOW + timedelta(seconds=15))
        assert len(worker._pending_evidence) == 1
    finally:
        release.set()
        worker.close()


def test_background_evidence_keeps_original_timestamp_and_enables_later_cycle(tmp_path):
    store = AlertStore(tmp_path / 'alerts.sqlite3')
    rule(store, action='buy_zone', lower=69000, upper=71000)
    proof = {'eligible': True, 'symbol': '005930', 'market': 'KOSPI',
             'checked_at': NOW.isoformat(), 'technical': True, 'financial': True,
             'macro': True, 'news': True, 'filings': True,
             'levels': {'entry_low': 69000, 'entry_high': 72000, 'stop': 68000, 'resistance': 80000}}
    worker = MonitorWorker(store, lambda *args: quote(), evidence_provider=lambda *args: proof)
    try:
        assert worker.run_once(NOW)['events'] == 0
        next(iter(worker._pending_evidence.values())).result(timeout=1)
        assert worker.run_once(NOW + timedelta(seconds=15))['events'] == 1
        assert next(iter(worker._evidence.values()))[1]['checked_at'] == NOW.isoformat()
    finally:
        worker.close()
