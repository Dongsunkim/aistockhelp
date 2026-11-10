"""Pure monitoring decisions over trusted server-side quote/evidence adapters.

This module never places orders. A quote's ``verified_live`` flag is supplied by
the server adapter, never by a public request. Invalid observations preserve the
previous crossing state: ``inside=None`` must not rearm an alert.
"""
from __future__ import annotations

from datetime import datetime, time, timezone
import math
import re
from zoneinfo import ZoneInfo


MAX_QUOTE_AGE_SECONDS = 60
MAX_EVIDENCE_AGE_SECONDS = 900
_SEOUL = ZoneInfo("Asia/Seoul")
_GATES = ("technical", "financial", "macro", "news", "filings")


def aware_timestamp(value) -> datetime | None:
    """Parse a real timezone-aware timestamp, never infer a missing timezone."""
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        stamp = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        return stamp if stamp.tzinfo is not None and stamp.utcoffset() is not None else None
    except (ValueError, OverflowError):
        return None


def _positive(value, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (float, int)):
        raise ValueError(f"{label}은 양의 숫자로 입력해 주세요.")
    try:
        number = float(value)
    except (OverflowError, ValueError):
        raise ValueError(f"{label}은 유효한 가격이어야 합니다.") from None
    if not math.isfinite(number) or number <= 0:
        raise ValueError(f"{label}은 유효한 양수여야 합니다.")
    return number


def validate_rule(payload: dict) -> dict:
    """Validate shape/prices; the store resolves canonical company identity."""
    if not isinstance(payload, dict):
        raise ValueError("알림 조건을 확인해 주세요.")
    rule = dict(payload)
    action = rule.get("action")
    if action not in {"buy_zone", "sell_above", "stop_below"}:
        raise ValueError("지원하지 않는 알림 조건입니다.")
    market = rule.get("market")
    symbol = rule.get("symbol")
    if market not in {"KOSPI", "KOSDAQ", "US"} or not isinstance(symbol, str):
        raise ValueError("종목코드와 시장을 확인해 주세요.")
    pattern = r"[A-Z][A-Z0-9]{0,9}(?:[.\-][A-Z])?" if market == "US" else r"[0-9]{6}"
    if re.fullmatch(pattern, symbol) is None:
        raise ValueError("종목코드와 시장을 확인해 주세요.")
    if "enabled" in rule and not isinstance(rule["enabled"], bool):
        raise ValueError("감시 활성화 상태를 확인해 주세요.")
    if action == "buy_zone":
        rule["lower"] = _positive(rule.get("lower"), "매수 관찰 하한")
        rule["upper"] = _positive(rule.get("upper"), "매수 관찰 상한")
        if rule["lower"] > rule["upper"]:
            raise ValueError("매수 관찰 하한은 상한보다 클 수 없습니다.")
        rule["threshold"] = None
    else:
        rule["threshold"] = _positive(rule.get("threshold"), "관찰 기준 가격")
        rule["lower"] = rule["upper"] = None
    return rule


def evaluate(rule: dict, quote: dict, evidence: dict | None = None,
             now: datetime | None = None) -> dict:
    """Return inside/outside only when every required observation is usable."""
    result = {"status": "invalid", "inside": None, "price": None,
              "message": "", "reason": "", "quote_timestamp": None, "risk_reward": None}

    def stop(reason, status="blocked"):
        result.update(status=status, reason=reason, message=reason)
        return result

    try:
        rule = validate_rule(rule)
    except ValueError as exc:
        return stop(str(exc), "invalid")
    now = now or datetime.now(timezone.utc)
    if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
        return stop("감시 서버의 기준 시각을 확인할 수 없습니다.", "invalid")
    if not isinstance(quote, dict):
        return stop("현재가 응답을 확인할 수 없습니다.", "invalid")
    if quote.get("symbol") != rule["symbol"] or quote.get("market") != rule["market"]:
        return stop("현재가의 종목코드·시장이 감시 종목과 일치하지 않습니다.", "invalid")
    try:
        price = _positive(quote.get("price"), "현재가")
    except ValueError as exc:
        return stop(str(exc), "invalid")
    result["price"] = price
    if rule["market"] == "US":
        return stop("미국 주식의 최신 시세 관측 시각 검증이 연결되지 않아 자동 감시를 보류합니다.")
    provider = quote.get("provider")
    if not isinstance(provider, str) or not provider.strip():
        return stop("현재가 출처를 확인할 수 없습니다.")
    if quote.get("verified_live") is not True or quote.get("is_delayed") is True:
        return stop("시세 관측 시각이 검증되지 않은 가격은 알림에 사용하지 않습니다.")
    stamp = aware_timestamp(quote.get("exchange_timestamp"))
    if stamp is None:
        return stop("거래일과 시간대가 포함된 시세 관측 시각을 확인할 수 없습니다.")
    result["quote_timestamp"] = stamp.isoformat()
    age = (now - stamp).total_seconds()
    if not 0 <= age <= MAX_QUOTE_AGE_SECONDS:
        return stop("시세 관측 시각이 60초를 넘었거나 미래 시각이어서 감시를 보류합니다.")
    local_now, local_stamp = now.astimezone(_SEOUL), stamp.astimezone(_SEOUL)
    # A weekday alone does not prove an exchange session exists. Requiring an
    # independently dated, fresh exchange tick also excludes holiday snapshots.
    def in_session(value):
        return value.weekday() < 5 and time(9) <= value.time() <= time(15, 30)
    if local_now.date() != local_stamp.date() or not in_session(local_now) or not in_session(local_stamp):
        return stop("국내 정규장 거래 시각과 일치하는 최신 관측가가 필요합니다.")
    if rule["action"] == "buy_zone":
        if not isinstance(evidence, dict):
            return stop("매수 관찰에 필요한 분석 근거의 검증이 완료되지 않았습니다.")
        if evidence.get("symbol") != rule["symbol"] or evidence.get("market") != rule["market"]:
            return stop("분석 근거의 종목코드·시장이 감시 종목과 일치하지 않습니다.")
        if evidence.get("eligible") is not True:
            blocked_reasons = evidence.get("blocked_reasons")
            if isinstance(blocked_reasons, (list, tuple)):
                reasons = [" ".join(reason.split())[:160] for reason in blocked_reasons
                           if isinstance(reason, str) and reason.strip()][:3]
                if reasons:
                    return stop(("매수 관찰 보류: " + "; ".join(reasons))[:500])
            return stop("매수 관찰에 필요한 분석 근거의 검증이 완료되지 않았습니다.")
        checked_at = aware_timestamp(evidence.get("checked_at"))
        if checked_at is None or not 0 <= (now - checked_at).total_seconds() <= MAX_EVIDENCE_AGE_SECONDS:
            return stop("매수 관찰 근거가 15분을 넘었거나 검증 시각을 확인할 수 없습니다.")
        missing = [gate for gate in _GATES if evidence.get(gate) is not True]
        if missing:
            labels = {"technical": "차트", "financial": "재무", "macro": "미국 경제·시장",
                      "news": "기사", "filings": "공시"}
            return stop("매수 관찰 근거 미확인: " + ", ".join(labels[gate] for gate in missing))
        levels = evidence.get("levels")
        if not isinstance(levels, dict):
            return stop("분석으로 검증한 매수 관찰 구간·손절·저항 가격이 필요합니다.")
        try:
            entry_low, entry_high, stop_price, resistance = (
                _positive(levels.get(key), label) for key, label in (
                    ("entry_low", "분석 구간 하한"), ("entry_high", "분석 구간 상한"),
                    ("stop", "분석 손절 기준"), ("resistance", "분석 저항 기준")))
        except ValueError:
            return stop("분석으로 검증한 가격 기준이 유효하지 않습니다.")
        if not stop_price < entry_low <= entry_high < resistance:
            return stop("분석 구간·손절·저항 가격의 순서가 유효하지 않습니다.")
        # Once a verified observation leaves the user's band, the next entrance
        # is a new crossing. A trusted analysis band/RR limits entrances only;
        # using it to block exits would leave an alert permanently latched.
        if not rule["lower"] <= price <= rule["upper"]:
            result.update(status="outside", inside=False,
                          message="설정한 관찰 가격 조건에 아직 도달하지 않았습니다.", reason="")
            return result
        if not entry_low <= price <= entry_high:
            return stop("현재 관측가가 분석으로 검증한 매수 관찰 구간 밖입니다.")
        if not stop_price < price < resistance:
            return stop("현재 관측가의 손절·저항 관계를 검증할 수 없습니다.")
        risk_reward = (resistance - price) / (price - stop_price)
        if not math.isfinite(risk_reward):
            return stop("현재 관측가 기준 예상 손익비를 확인할 수 없습니다.")
        result["risk_reward"] = risk_reward
        if risk_reward < 2:
            return stop("현재 관측가 기준 예상 손익비가 2 미만이어서 매수 관찰을 보류합니다.")
        inside = rule["lower"] <= price <= rule["upper"]
        message = "매수 관찰 구간 진입: 분석 조건을 충족했습니다. 공시·기사 원문과 최신 시세를 확인하세요."
    elif rule["action"] == "sell_above":
        inside = price >= rule["threshold"]
        message = "사용자 설정 매도 관찰 가격 도달: 보유 수량과 최신 시세를 확인하세요."
    else:
        inside = price <= rule["threshold"]
        message = "사용자 설정 손절 관찰 가격 도달: 보유 수량과 최신 시세를 확인하세요."
    result.update(status="inside" if inside else "outside", inside=inside,
                  message=message if inside else "설정한 관찰 가격 조건에 아직 도달하지 않았습니다.", reason="")
    return result
