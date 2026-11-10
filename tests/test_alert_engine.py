from copy import deepcopy
from datetime import datetime, timedelta, timezone

import pytest

from alert_engine import evaluate, validate_rule


NOW = datetime.fromisoformat("2026-11-10T10:00:00+09:00")


def rule(action="buy_zone"):
    return {"symbol": "005930", "name": "삼성전자", "market": "KOSPI",
            "action": action, "lower": 69000, "upper": 71000, "threshold": 70000}


def quote(**changes):
    return {"symbol": "005930", "market": "KOSPI", "price": 70000,
            "provider": "한국투자증권 KIS REST", "verified_live": True,
            "is_delayed": False, "exchange_timestamp": NOW.isoformat(), **changes}


def evidence(**changes):
    return {"symbol": "005930", "market": "KOSPI", "eligible": True,
            "checked_at": NOW.isoformat(), "technical": True,
            "financial": True, "macro": True, "news": True, "filings": True,
            "levels": {"entry_low": 68000, "entry_high": 72000, "stop": 65000, "resistance": 90000},
            **changes}


@pytest.mark.parametrize("price,inside", [(68999, False), (69000, True), (71000, True), (71001, False)])
def test_buy_zone_inclusive_boundaries(price, inside):
    decision = evaluate(rule(), quote(price=price), evidence(), now=NOW)
    assert decision["inside"] is inside
    assert decision["status"] == ("inside" if inside else "outside")
    assert "매수 관찰" in decision["message"] if inside else "도달하지" in decision["message"]


@pytest.mark.parametrize("action,price,inside", [
    ("sell_above", 69999, False), ("sell_above", 70000, True),
    ("stop_below", 70001, False), ("stop_below", 70000, True),
])
def test_user_exit_rules_need_no_buy_evidence(action, price, inside):
    decision = evaluate(rule(action), quote(price=price), now=NOW)
    assert decision["inside"] is inside
    if inside:
        assert "사용자 설정" in decision["message"]


@pytest.mark.parametrize("changes", [
    {"symbol": "000660"}, {"market": "KOSDAQ"}, {"price": True},
    {"price": float("nan")}, {"price": float("inf")}, {"price": -1},
    {"verified_live": False}, {"verified_live": 1}, {"is_delayed": True},
    {"provider": ""}, {"exchange_timestamp": None},
    {"exchange_timestamp": "2026-11-10T10:00:00"},
])
def test_unusable_observation_never_rearms(changes):
    decision = evaluate(rule(), quote(**changes), evidence(), now=NOW)
    assert decision["inside"] is None
    assert decision["status"] in {"invalid", "blocked"}


@pytest.mark.parametrize("age,usable", [(60, True), (60.001, False), (-0.001, False)])
def test_quote_age_boundary_and_future_rejected(age, usable):
    q = quote(exchange_timestamp=(NOW - timedelta(seconds=age)).isoformat())
    decision = evaluate(rule("sell_above"), q, now=NOW)
    assert (decision["inside"] is True) is usable


@pytest.mark.parametrize("changes", [
    {"eligible": False}, {"macro": False}, {"financial": None}, {"news": 1},
    {"filings": False}, {"technical": "true"},
    {"checked_at": "2026-11-10T09:44:59+09:00"},
    {"checked_at": "2026-11-10T10:00:01+09:00"},
    {"checked_at": "2026-11-10T10:00:00"},
])
def test_buy_requires_all_current_server_evidence(changes):
    decision = evaluate(rule(), quote(price=100), evidence(**changes), now=NOW)
    assert decision["inside"] is None  # Even outside price must not rearm without evidence.
    assert decision["status"] == "blocked"


def test_buy_missing_evidence_and_exact_age_boundary():
    assert evaluate(rule(), quote(), now=NOW)["inside"] is None
    decision = evaluate(rule(), quote(), evidence(checked_at=(NOW-timedelta(minutes=15)).isoformat()), now=NOW)
    assert decision["inside"] is True


@pytest.mark.parametrize("stamp", [
    "2026-11-10T08:59:59+09:00", "2026-11-10T15:30:01+09:00",
    "2026-11-14T10:00:00+09:00",  # Saturday, despite a purported fresh tick.
])
def test_closed_session_refuses_purported_live_quote(stamp):
    when = datetime.fromisoformat(stamp)
    decision = evaluate(rule("sell_above"), quote(exchange_timestamp=stamp), now=when)
    assert decision["inside"] is None


def test_timezone_conversion_and_close_boundary():
    close = datetime.fromisoformat("2026-11-10T06:30:00+00:00")
    decision = evaluate(rule("sell_above"), quote(exchange_timestamp=close.isoformat()), now=close)
    assert decision["inside"] is True
    assert evaluate(rule(), quote(), evidence(), now=NOW.replace(tzinfo=None))["inside"] is None


def test_us_always_waits_for_live_adapter():
    r = {**rule("sell_above"), "symbol": "AAPL", "market": "US"}
    q = quote(symbol="AAPL", market="US")
    decision = evaluate(r, q, now=NOW)
    assert decision["inside"] is None
    assert "미국" in decision["reason"]


@pytest.mark.parametrize("changes", [
    {"lower": True}, {"upper": float("nan")}, {"lower": 72000},
    {"action": "buy_now"}, {"symbol": "005930.KS"}, {"enabled": 1},
    {"action": "stop_below", "threshold": float("inf")},
])
def test_invalid_rule_rejected_without_mutation(changes):
    value = {**rule(), **changes}
    before = deepcopy(value)
    with pytest.raises(ValueError):
        validate_rule(value)
    assert value.keys() == before.keys()
    assert evaluate(value, quote(), evidence(), now=NOW)["status"] == "invalid"


def test_equivalent_zero_width_zone_and_us_symbols_are_valid_shapes():
    value = validate_rule({**rule(), "lower": 70000, "upper": 70000})
    assert value["lower"] == value["upper"] == 70000
    assert validate_rule({**rule(), "market": "US", "symbol": "BRK.B"})["symbol"] == "BRK.B"


@pytest.mark.parametrize("levels", [
    None, {}, {"entry_low": 68000, "entry_high": 72000, "stop": 68000, "resistance": 90000},
    {"entry_low": 72000, "entry_high": 68000, "stop": 65000, "resistance": 90000},
    {"entry_low": 68000, "entry_high": 72000, "stop": 65000, "resistance": float("inf")},
])
def test_missing_or_invalid_trusted_levels_do_not_rearm(levels):
    decision = evaluate(rule(), quote(), evidence(levels=levels), now=NOW)
    assert decision["status"] == "blocked"
    assert decision["inside"] is None


def test_outside_trusted_zone_blocks_even_if_user_band_would_be_inside():
    r = {**rule(), "lower": 60000, "upper": 100000}
    decision = evaluate(r, quote(price=75000), evidence(), now=NOW)
    assert decision["status"] == "blocked"
    assert decision["inside"] is None


@pytest.mark.parametrize("resistance,allowed", [(79999, False), (80000, True), (80001, True)])
def test_buy_uses_current_tick_rr_not_previous_daily_close(resistance, allowed):
    levels = {**evidence()["levels"], "resistance": resistance}
    decision = evaluate(rule(), quote(), evidence(levels=levels), now=NOW)
    assert decision["risk_reward"] == pytest.approx((resistance - 70000) / 5000)
    assert (decision["inside"] is True) is allowed
    if not allowed:
        assert decision["inside"] is None


@pytest.mark.parametrize("changes", [{"symbol": None}, {"market": None},
    {"symbol": "000660"}, {"market": "KOSDAQ"}])
def test_buy_evidence_identity_required_even_outside_user_band(changes):
    decision = evaluate(rule(), quote(price=75000), evidence(**changes), now=NOW)
    assert decision["status"] == "blocked"
    assert decision["inside"] is None
    assert "종목코드·시장" in decision["reason"]


@pytest.mark.parametrize("price", [60000, 75000, 100000])
def test_outside_user_band_rearms_with_valid_quote_and_base_evidence(price):
    decision = evaluate(rule(), quote(price=price), evidence(), now=NOW)
    assert decision["status"] == "outside"
    assert decision["inside"] is False
    assert decision["risk_reward"] is None


def test_low_rr_outside_user_band_still_rearms():
    levels = {**evidence()["levels"], "resistance": 75000}
    decision = evaluate(rule(), quote(price=71500), evidence(levels=levels), now=NOW)
    assert decision["inside"] is False
    assert decision["status"] == "outside"


def test_missing_evidence_identity_is_blocked():
    value = evidence()
    value.pop("symbol")
    decision = evaluate(rule(), quote(), value, now=NOW)
    assert decision["inside"] is None
    assert decision["status"] == "blocked"


def test_ineligible_evidence_explains_first_three_bounded_server_reasons():
    reasons = ["미국 경제 자료 미확인", 1, "기사 자료 없음", "x" * 1000, "표시하지 않을 네 번째 사유"]
    decision = evaluate(rule(), quote(), evidence(eligible=False, blocked_reasons=reasons), now=NOW)
    assert decision["inside"] is None
    assert decision["reason"].startswith("매수 관찰 보류: 미국 경제 자료 미확인; 기사 자료 없음;")
    assert "네 번째" not in decision["reason"]
    assert len(decision["reason"]) <= 500


def test_mismatched_evidence_does_not_reveal_other_company_reasons():
    decision = evaluate(rule(), quote(), evidence(symbol="000660", eligible=False,
                        blocked_reasons=["다른 기업의 상세 사유"]), now=NOW)
    assert "종목코드·시장" in decision["reason"]
    assert "다른 기업" not in decision["reason"]


@pytest.mark.parametrize("reasons", [None, "임의 문자열", [None, 1, "  "]])
def test_ineligible_evidence_without_reason_list_keeps_generic_message(reasons):
    decision = evaluate(rule(), quote(), evidence(eligible=False, blocked_reasons=reasons), now=NOW)
    assert decision["reason"] == "매수 관찰에 필요한 분석 근거의 검증이 완료되지 않았습니다."
