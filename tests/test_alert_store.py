from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import pytest

from alert_store import AlertStore


NOW = datetime(2026, 11, 10, 1, 0, tzinfo=timezone.utc)


@pytest.fixture
def store(tmp_path):
    return AlertStore(tmp_path / 'monitor.sqlite3')


def create(store, **overrides):
    return store.create({'symbol': '005930', 'name': '삼성전자', 'market': 'KOSPI',
                         'action': 'sell_above', 'threshold': 70000, **overrides})


def observation(inside=True, **overrides):
    return {'status': 'inside' if inside else 'outside', 'inside': inside,
            'price': 70000, 'reason': '', 'message': '관찰 가격 도달',
            'quote_timestamp': NOW.isoformat(), **overrides}


def test_rules_use_canonical_identity_and_persist(tmp_path):
    path = tmp_path / 'alerts.sqlite3'
    store = AlertStore(path)
    created = create(store, name='Samsung Electronics')
    assert created['name'] == '삼성전자'
    assert AlertStore(path).list()[0] == created


def test_known_contradictory_name_rejected(store):
    with pytest.raises(ValueError, match='일치하지'):
        create(store, name='SK하이닉스')
    assert store.list() == []


def test_restart_dedup_and_valid_rearm(store):
    rule = create(store)
    first = store.record_observation(rule['id'], 1, observation(), NOW)
    assert first['sequence'] == 1
    reopened = AlertStore(store.path)
    assert reopened.record_observation(rule['id'], 1, observation(), NOW) is None
    reopened.record_observation(rule['id'], 1, observation(False), NOW)
    second = reopened.record_observation(rule['id'], 1, observation(), NOW)
    assert second['sequence'] == 2
    assert len(reopened.events()) == 2


def test_invalid_observation_never_rearms(store):
    rule = create(store)
    store.record_observation(rule['id'], 1, observation(), NOW)
    store.record_observation(rule['id'], 1,
                             observation(None, status='blocked', reason='시세 오래됨'), NOW)
    assert store.list()[0]['inside'] is True
    assert store.record_observation(rule['id'], 1, observation(), NOW) is None


def test_parallel_first_observation_generates_one_event(store):
    rule = create(store)
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda _: store.record_observation(rule['id'], 1,
                                                                   observation(), NOW), range(12)))
    assert sum(result is not None for result in results) == 1
    assert len(store.events()) == 1


def test_pause_cancels_outbox_and_rejects_inflight_old_revision(store):
    rule = create(store)
    store.record_observation(rule['id'], 1, observation(), NOW)
    paused = store.update_enabled(rule['id'], False)
    assert paused['revision'] == 2
    assert store.record_observation(rule['id'], 1, observation(), NOW) is None
    assert store.events()[0]['delivery_status'] == 'cancelled'
    assert store.claim_delivery(NOW) is None
    active = store.update_enabled(rule['id'], True)
    assert active['revision'] == 3
    assert store.record_observation(rule['id'], 3, observation(), NOW)


def test_repeated_enable_is_idempotent(store):
    rule = create(store)
    assert store.update_enabled(rule['id'], True)['revision'] == 1
    assert store.update_enabled(9999, True) is None


def test_deleted_rules_cannot_be_observed_or_delivered(store):
    rule = create(store)
    store.record_observation(rule['id'], 1, observation(), NOW)
    assert store.delete(rule['id']) is True
    assert store.delete(rule['id']) is False
    assert store.record_observation(rule['id'], 1, observation(), NOW) is None
    assert store.claim_delivery(NOW) is None
    assert store.events()[0]['delivery_status'] == 'cancelled'


def test_delivery_leases_are_exclusive_and_expire(store):
    rule = create(store)
    store.record_observation(rule['id'], 1, observation(), NOW)
    first = store.claim_delivery(NOW)
    assert first is not None
    assert store.claim_delivery(NOW) is None
    retry = store.claim_delivery(NOW + timedelta(seconds=91))
    assert retry['id'] == first['id']
    assert retry['lease_token'] != first['lease_token']
    assert not store.complete_delivery(first['id'], first['lease_token'], success=True, now=NOW)
    assert store.complete_delivery(retry['id'], retry['lease_token'], success=True, now=NOW)
    assert store.events()[0]['delivery_status'] == 'sent'


def test_failed_delivery_keeps_event_and_backoff(store):
    rule = create(store)
    store.record_observation(rule['id'], 1, observation(), NOW)
    event = store.claim_delivery(NOW)
    store.complete_delivery(event['id'], event['lease_token'], success=False, now=NOW)
    failed = store.events()[0]
    assert failed['delivery_status'] == 'pending'
    assert failed['attempts'] == 1
    assert store.claim_delivery(NOW + timedelta(seconds=29)) is None
    assert store.claim_delivery(NOW + timedelta(seconds=31))
    assert 'lease_token' not in failed


def test_heartbeat_requires_real_recent_completed_cycle(store):
    assert store.status(NOW)['worker_running'] is False
    store.heartbeat(now=NOW, interval_seconds=15)
    assert store.status(NOW + timedelta(seconds=59))['worker_running'] is True
    assert store.status(NOW + timedelta(seconds=61))['worker_running'] is False
    assert store.status(NOW - timedelta(seconds=1))['worker_running'] is False


def test_trade_levels_survive_event_recording(store):
    rule = create(store)
    store.record_observation(rule['id'], 1, observation(risk_reward=2.5), NOW)
    assert store.events()[0]['risk_reward'] == 2.5


@pytest.mark.parametrize('quoted', [None, 'bad', '2026-11-10T01:00:00',
                                  (NOW + timedelta(seconds=1)).isoformat(),
                                  (NOW - timedelta(seconds=121)).isoformat()])
def test_invalid_or_old_event_timestamps_expire_before_claim(store, quoted):
    target = create(store)
    store.record_observation(target['id'], 1, observation(quote_timestamp=quoted), NOW)
    assert store.claim_delivery(NOW) is None
    assert store.events()[0]['delivery_status'] == 'expired'


def test_duplicate_create_is_idempotent_and_preserves_pause(store):
    first = create(store)
    store.record_observation(first['id'], 1, observation(), NOW)
    assert create(store, name='Samsung Electronics')['id'] == first['id']
    assert len(store.list()) == 1
    assert store.list()[0]['inside'] is True
    paused = store.update_enabled(first['id'], False)
    retried = create(store)
    assert retried == paused
    assert retried['enabled'] is False


def test_concurrent_duplicate_creates_return_one_rule(store):
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda _: create(store), range(12)))
    assert len({result['id'] for result in results}) == 1
    assert len(store.list()) == 1


def test_duplicate_buy_zone_null_threshold_deduplicates(store):
    first = create(store, action='buy_zone', lower=68000, upper=70000)
    assert create(store, action='buy_zone', lower=68000.0, upper=70000.0)['id'] == first['id']
    assert create(store, action='buy_zone', lower=68000, upper=71000)['id'] != first['id']
