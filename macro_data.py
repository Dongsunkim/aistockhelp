"""Public US macro context, with conservative operational risk thresholds.

Risk assumptions: a broad index or SOX falling at least 2% in a completed
session, KRW per USD rising at least 1.5%, the effective federal funds rate
rising at least 0.25 percentage points, or CPI YoY accelerating by at least
0.2 percentage points warrant review. These thresholds are not forecasts.
FRED observation dates are not publication dates. Missing data never clears
the macro gate. No API keys or paid services are used.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from copy import deepcopy
import csv
from datetime import date, datetime, timedelta, timezone
from io import StringIO
import math
from threading import Lock
import time
from urllib.parse import quote
from zoneinfo import ZoneInfo

import requests


NY = ZoneInfo("America/New_York")
CACHE_SECONDS = 60
_cache_lock = Lock()
_cache = None
FRED = {"rate": "DFF", "usdkrw": "DEXKOUS", "inflation": "CPIAUCSL"}
STOCKS = {"sp500": "^GSPC", "nasdaq": "^IXIC", "sox": "^SOX",
          "nvda": "NVDA", "aapl": "AAPL", "msft": "MSFT"}
LABELS = {"rate": "미국 기준금리(실효 연방기금금리)", "usdkrw": "달러/원 환율",
          "inflation": "미국 CPI 전년 동월 대비", "sp500": "S&P 500", "nasdaq": "나스닥",
          "sox": "필라델피아 반도체", "nvda": "NVIDIA", "aapl": "Apple", "msft": "Microsoft"}


def _number(value, *, positive=False):
    if isinstance(value, bool):
        raise ValueError("지표 숫자가 비정상입니다.")
    try:
        number = float(value)
    except (ValueError, TypeError) as error:
        raise ValueError("지표 숫자가 없거나 비정상입니다.") from error
    if not math.isfinite(number) or (positive and number <= 0):
        raise ValueError("지표 숫자가 비정상입니다.")
    return number


def _recent(day, today, maximum):
    age = (today - day).days
    if age < 0:
        raise ValueError("미래 관측일입니다.")
    if age > maximum:
        raise ValueError(f"관측일 {day.isoformat()}이 허용 기간 {maximum}일을 지났습니다.")


def _fred_rows(text, series, today):
    reader = csv.DictReader(StringIO(text.lstrip("\ufeff")))
    fields = reader.fieldnames or []
    date_key = next((key for key in ("observation_date", "DATE") if key in fields), None)
    if date_key is None or series not in fields:
        raise ValueError(f"FRED {series} CSV 열을 확인할 수 없습니다.")
    rows = {}
    for row in reader:
        stamp, value = row.get(date_key), row.get(series)
        if not stamp and not value:
            continue
        try:
            day = date.fromisoformat(stamp)
        except (TypeError, ValueError) as error:
            raise ValueError("FRED 관측일을 해석할 수 없습니다.") from error
        # FRED uses '.' on holidays. These are absent observations, not zero.
        if value in (".", "", None):
            continue
        number = _number(value, positive=series != "DFF")
        if number < 0:
            raise ValueError("FRED 관측값은 음수일 수 없습니다.")
        if day > today:
            raise ValueError("FRED에 미래 관측값이 포함되어 있습니다.")
        if day in rows:
            raise ValueError("FRED 관측일이 중복되었습니다.")
        rows[day] = number
    if not rows:
        raise ValueError("FRED에서 유효한 관측값을 찾지 못했습니다.")
    return rows


def _fred_metric(text, key, now):
    series = FRED[key]
    rows = _fred_rows(text, series, now.astimezone(NY).date())
    dates = sorted(rows)
    latest = dates[-1]
    today = now.astimezone(NY).date()
    source = {"source": f"FRED · {series}", "url": f"https://fred.stlouisfed.org/series/{series}"}
    if key == "inflation":
        _recent(latest, today, 100)
        if any(day.day != 1 for day in dates):
            raise ValueError("CPI 관측월 형식이 월초 기준이 아닙니다.")
        prior_month = date(latest.year - (latest.month == 1), 12 if latest.month == 1 else latest.month - 1, 1)
        base = date(latest.year - 1, latest.month, 1)
        prior_base = date(prior_month.year - 1, prior_month.month, 1)
        if any(day not in rows for day in (base, prior_month, prior_base)):
            raise ValueError("CPI 전년 동월 또는 직전 월 비교 관측값이 없습니다.")
        value = _number((rows[latest] / rows[base] - 1) * 100)
        previous = _number((rows[prior_month] / rows[prior_base] - 1) * 100)
        return dict(source, value=round(value, 6), unit="% YoY", asof=latest.strftime("%Y-%m"),
                    previous=round(previous, 6), change_pp=round(_number(value - previous), 6),
                    previous_asof=prior_month.strftime("%Y-%m"), index_value=rows[latest],
                    date_basis="CPI 관측월 · 발표일 아님")
    if len(dates) < 2:
        raise ValueError("FRED 직전 유효 관측값이 없습니다.")
    previous_day = dates[-2]
    _recent(latest, today, 7)
    _recent(previous_day, today, 7)
    value, previous = rows[latest], rows[previous_day]
    result = dict(source, value=value, previous=previous, asof=latest.isoformat(),
                  previous_asof=previous_day.isoformat(), unit="%" if key == "rate" else "KRW/USD")
    if key == "rate":
        result["change_pp"] = round(value - previous, 6)
    else:
        result["change_pct"] = round(_number((value / previous - 1) * 100), 6)
    return result


def _stock_metric(payload, key, now):
    if not isinstance(payload, dict) or not isinstance(payload.get("chart"), dict):
        raise ValueError("미국 시장 차트 응답 구조가 불명확합니다.")
    chart = payload["chart"]
    if chart.get("error"):
        raise ValueError("미국 시장 차트 공급원 오류")
    results = chart.get("result")
    if not isinstance(results, list) or len(results) != 1 or not isinstance(results[0], dict):
        raise ValueError("미국 시장 차트 결과가 없습니다.")
    raw = results[0]
    metadata = raw.get("meta")
    if isinstance(metadata, dict) and metadata.get("symbol") not in (None, STOCKS[key]):
        raise ValueError("요청한 미국 지수·종목과 응답 종목이 다릅니다.")
    indicators = raw.get("indicators")
    if not isinstance(indicators, dict):
        raise ValueError("미국 시장 지표 응답 구조가 불명확합니다.")
    quotes = indicators.get("quote") or []
    if not quotes or not isinstance(quotes[0], dict):
        raise ValueError("미국 시장 종가 열이 없습니다.")
    closes, stamps = quotes[0].get("close"), raw.get("timestamp")
    if not isinstance(closes, list) or not isinstance(stamps, list) or len(closes) != len(stamps):
        raise ValueError("미국 시장 관측 시각과 종가 길이가 다릅니다.")
    local_now = now.astimezone(NY)
    complete_today = (local_now.hour, local_now.minute) >= (16, 30)
    completed = {}
    for stamp, close in zip(stamps, closes):
        day = datetime.fromtimestamp(_number(stamp, positive=True), NY).date()
        if day > local_now.date():
            raise ValueError("미국 시장에 미래 일봉이 포함되어 있습니다.")
        if day == local_now.date() and not complete_today:
            continue
        if day in completed:
            raise ValueError("미국 시장 일봉 관측일이 중복되었습니다.")
        completed[day] = close
    dates = sorted(completed)
    if len(dates) < 2:
        raise ValueError("완료된 미국 시장 일봉이 2개 미만입니다.")
    latest, previous_day = dates[-1], dates[-2]
    _recent(latest, local_now.date(), 5)
    _recent(previous_day, local_now.date(), 5)
    value = _number(completed[latest], positive=True)
    previous = _number(completed[previous_day], positive=True)
    ticker = STOCKS[key]
    return {"value": value, "previous": previous, "change_pct": round(_number((value / previous - 1) * 100), 6),
            "unit": "index" if ticker.startswith("^") else "USD", "asof": latest.isoformat(),
            "previous_asof": previous_day.isoformat(), "source": "Yahoo Finance 비공식 확정 일봉",
            "url": f"https://finance.yahoo.com/quote/{quote(ticker, safe='')}/history/"}


def _fetch_metric(key, now, session):
    client = session if session is not None else requests.Session()
    try:
        if key in FRED:
            # Daily comparisons need two recent observations; CPI additionally
            # needs the exact current/prior month and both year-ago months.
            # Retain ample publication-lag room without downloading decades.
            days = 800 if key == "inflation" else 30
            start = now.astimezone(NY).date() - timedelta(days=days)
            response = client.get("https://fred.stlouisfed.org/graph/graph.csv",
                                  params={"id": FRED[key], "cosd": start.isoformat()}, timeout=8)
            response.raise_for_status()
            return _fred_metric(response.text, key, now)
        response = client.get(f"https://query1.finance.yahoo.com/v8/finance/chart/{quote(STOCKS[key], safe='')}",
                              params={"range": "1mo", "interval": "1d"}, timeout=8)
        response.raise_for_status()
        return _stock_metric(response.json(), key, now)
    finally:
        if session is None:
            client.close()


def _risks(metrics):
    risks = []
    for key in ("sp500", "nasdaq", "sox"):
        if key in metrics and metrics[key]["change_pct"] <= -2:
            risks.append(f"{LABELS[key]} 일간 {metrics[key]['change_pct']:+.2f}% · -2% 급락 기준 충족")
    if "usdkrw" in metrics and metrics["usdkrw"]["change_pct"] >= 1.5:
        risks.append("달러/원 환율 일간 +1.5% 이상 상승 · 원화 약세")
    if "rate" in metrics and metrics["rate"]["change_pp"] >= 0.25:
        risks.append("실효 연방기금금리 직전 유효 관측 대비 +0.25%p 이상 상승")
    if "inflation" in metrics and metrics["inflation"]["change_pp"] >= 0.2:
        risks.append("CPI 전년 동월 대비 상승률이 직전 월보다 +0.2%p 이상 가속")
    return risks


def fetch_macro_context(session=None, asof=None):
    """Collect all nine inputs; an injected asof bypasses the successful-result cache.

    An injected session should support concurrent requests (primarily for test fakes).
    Default HTTP sessions belong to one worker each. A naive asof denotes UTC.
    """
    global _cache
    use_cache = asof is None
    if use_cache:
        with _cache_lock:
            cached = _cache
            if cached and time.monotonic() - cached["stored_at"] < CACHE_SECONDS:
                result = deepcopy(cached["result"])
                result["cached_at"] = result["checked_at"]
                return result
    now = asof or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    metrics, warnings = {}, []
    keys = [*FRED, *STOCKS]
    with ThreadPoolExecutor(max_workers=6, thread_name_prefix="stockdesk-macro") as pool:
        jobs = {pool.submit(_fetch_metric, key, now, session): key for key in keys}
        for job in as_completed(jobs):
            key = jobs[job]
            try:
                metrics[key] = job.result()
            except (requests.RequestException, ValueError, KeyError, TypeError, OverflowError, csv.Error) as error:
                warnings.append(f"{LABELS[key]} 확인 실패: {str(error)[:150]}")
    # Stable ordering makes partial results and warnings straightforward to compare.
    metrics = {key: metrics[key] for key in keys if key in metrics}
    warnings.sort()
    eligible = len(metrics) == len(keys)
    risks = _risks(metrics)
    summary = (f"미국 거시지표 9개 확인 · 주의 요인 {len(risks)}건" if eligible
               else f"미국 거시지표 확인 미완료 ({len(metrics)}/9) · 판단 보류")
    result = {"eligible": eligible, "summary": summary, "metrics": metrics,
              "risk_flags": risks, "warnings": warnings,
              "checked_at": now.astimezone(timezone.utc).isoformat(), "cached_at": None}
    if use_cache and eligible:
        with _cache_lock:
            _cache = {"stored_at": time.monotonic(), "result": deepcopy(result)}
    return result
