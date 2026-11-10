"""Offline fixtures use KIS's documented output2 minute-bar field names.

See official examples_llm/domestic_stock/inquire_time_itemchartprice in
https://github.com/koreainvestment/open-trading-api . No broker call is made.
"""
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

import quotes

NOW = datetime(2026, 11, 10, 10, 11, 20, tzinfo=ZoneInfo('Asia/Seoul'))


def row(**changes):
    return {'stck_bsop_date': '20261110', 'stck_cntg_hour': '101100',
            'stck_prpr': '72000', 'cntg_vol': '542', **changes}


def fixture(monkeypatch, rows):
    calls = []

    def payload(path, tr_id, params):
        calls.append((path, tr_id, params))
        # output1 price deliberately differs. It has no transaction timestamp.
        return {'rt_cd': '0', 'output1': {'stck_prpr': '99999'}, 'output2': rows}

    monkeypatch.setattr(quotes, '_get_payload', payload)
    return calls


def test_dated_row_price_and_timestamp_stay_together(monkeypatch):
    calls = fixture(monkeypatch, [row(stck_cntg_hour='101000', stck_prpr='71000'), row()])
    result = quotes.fetch_monitor_quote('005930', 'KOSPI', now=NOW)
    assert result['price'] == 72000
    assert result['symbol'] == '005930' and result['market'] == 'KOSPI'
    assert result['exchange_timestamp'] == '2026-11-10T10:11:00+09:00'
    assert result['verified_live'] is True
    assert result['observation_type'] == 'minute_bar'
    assert result['resolution_seconds'] == 60
    path, tr_id, params = calls[0]
    assert path.endswith('/inquire-time-itemchartprice')
    assert tr_id == 'FHKST03010200'
    assert params == {'FID_COND_MRKT_DIV_CODE': 'J', 'FID_INPUT_ISCD': '005930',
                      'FID_INPUT_HOUR_1': '101120', 'FID_PW_DATA_INCU_YN': 'N', 'FID_ETC_CLS_CODE': ''}


@pytest.mark.parametrize('changes', [
    {'stck_bsop_date': ''}, {'stck_bsop_date': 'not-a-date'},
    {'stck_cntg_hour': ''}, {'stck_cntg_hour': '999999'},
    {'stck_prpr': 'NaN'}, {'stck_prpr': 'inf'}, {'stck_prpr': '0'},
    {'stck_prpr': True}, {'cntg_vol': '0'}, {'cntg_vol': 'inf'},
    {'stck_cntg_hour': '101200'}, {'stck_shrn_iscd': '000660'},
])
def test_incomplete_or_invalid_rows_cannot_enable_alert(monkeypatch, changes):
    fixture(monkeypatch, [row(**changes)])
    with pytest.raises(ValueError, match='검증하지 못했습니다'):
        quotes.fetch_monitor_quote('005930', 'KOSPI', now=NOW)


@pytest.mark.parametrize('changes', [
    {'stck_cntg_hour': '101000'},
    {'stck_bsop_date': '20261109'},
    {'stck_bsop_date': '20261108'},
])
def test_stale_or_prior_session_observation_stays_unverified(monkeypatch, changes):
    fixture(monkeypatch, [row(**changes)])
    result = quotes.fetch_monitor_quote('005930', 'KOSPI', now=NOW)
    assert result['verified_live'] is False
    assert result['exchange_timestamp']


def test_utc_now_is_converted_to_exchange_timezone(monkeypatch):
    fixture(monkeypatch, [row()])
    result = quotes.fetch_monitor_quote('005930', 'KOSDAQ', now=NOW.astimezone(ZoneInfo('UTC')))
    assert result['verified_live'] is True


def test_weekend_cannot_become_fresh_even_with_matching_provider_date(monkeypatch):
    fixture(monkeypatch, [row(stck_bsop_date='20261108')])
    now = NOW.replace(day=8)
    result = quotes.fetch_monitor_quote('005930', 'KOSPI', now=now)
    assert result['verified_live'] is False


def test_seconds_after_regular_close_are_not_live(monkeypatch):
    fixture(monkeypatch, [row(stck_cntg_hour='153000')])
    now = NOW.replace(hour=15, minute=30, second=20)
    result = quotes.fetch_monitor_quote('005930', 'KOSPI', now=now)
    assert result['verified_live'] is False


def test_us_quote_preserves_unverified_state(monkeypatch):
    monkeypatch.setattr(quotes, 'fetch_kis_quote', lambda symbol, market: {
        'price': 200, 'verified_live': False, 'exchange_timestamp': None})
    result = quotes.fetch_monitor_quote('AAPL', 'US', now=NOW)
    assert result['symbol'] == 'AAPL'
    assert result['market'] == 'US'
    assert result['verified_live'] is False


@pytest.mark.parametrize('symbol,market', [('5930', 'KOSPI'), ('005930', 'OTHER'), ('AAPL', 'KOSPI')])
def test_wrong_domestic_identity_is_rejected_without_provider_call(monkeypatch, symbol, market):
    calls = fixture(monkeypatch, [])
    with pytest.raises(ValueError):
        quotes.fetch_monitor_quote(symbol, market, now=NOW)
    assert calls == []


def test_missing_output_rows_is_explicit_error(monkeypatch):
    monkeypatch.setattr(quotes, '_get_payload', lambda *args: {'output1': {'stck_prpr': '72000'}})
    with pytest.raises(ValueError, match='관측 목록'):
        quotes.fetch_monitor_quote('005930', 'KOSPI', now=NOW)


def test_naive_validation_clock_is_rejected(monkeypatch):
    calls = fixture(monkeypatch, [row()])
    with pytest.raises(ValueError, match='시간대'):
        quotes.fetch_monitor_quote('005930', 'KOSPI', now=NOW.replace(tzinfo=None))
    assert calls == []
