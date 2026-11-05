"""Realistic offline FRED/Yahoo fixtures; no network or paid API access."""
from copy import deepcopy
from datetime import datetime, timezone
from threading import Barrier, Lock
from urllib.parse import unquote

import pytest
import requests

import macro_data as macro


ASOF = datetime(2026, 11, 5, 17, tzinfo=macro.NY)


def chart(ticker, last=100, dates=("2026-11-03", "2026-11-04", "2026-11-05")):
    stamps = [int(datetime.fromisoformat(day + "T16:00:00").replace(tzinfo=macro.NY).timestamp()) for day in dates]
    return {"chart": {"error": None, "result": [{"meta": {"symbol": ticker},
            "timestamp": stamps, "indicators": {"quote": [{"close": [100, 100, last]}]}}]}}


def cpi_csv():
    rows = ["observation_date,CPIAUCSL"]
    for year, months in ((2025, range(9, 13)), (2026, range(1, 11))):
        for month in months:
            value = 100 if year == 2025 else 102
            if (year, month) == (2026, 9):
                value = 102.2
            if (year, month) == (2026, 10):
                value = 102.5
            rows.append(f"{year}-{month:02}-01,{value}")
    return "\n".join(rows)


class Response:
    def __init__(self, value):
        self.value = value
        self.text = value if isinstance(value, str) else ""

    def raise_for_status(self):
        pass

    def json(self):
        return deepcopy(self.value)


class Session:
    def __init__(self, overrides=None):
        self.calls = []
        self.values = {"DFF": "DATE,DFF\n2026-11-03,4\n2026-11-04,4.25\n2026-11-05,.\n",
                       "DEXKOUS": "observation_date,DEXKOUS\n2026-11-03,1000\n2026-11-04,1015\n",
                       "CPIAUCSL": cpi_csv()}
        for ticker in macro.STOCKS.values():
            self.values[ticker] = chart(ticker, 97 if ticker in ("^GSPC", "^IXIC", "^SOX") else 101)
        self.values.update(overrides or {})

    def get(self, url, **kwargs):
        key = kwargs["params"]["id"] if "graph.csv" in url else unquote(url.rsplit("/", 1)[-1])
        self.calls.append((key, url, kwargs))
        value = self.values[key]
        if isinstance(value, Exception):
            raise value
        return Response(value)


@pytest.fixture(autouse=True)
def clear_cache(monkeypatch):
    monkeypatch.setattr(macro, "_cache", None)


def test_complete_context_uses_observation_dates_and_computes_actual_changes():
    session = Session()
    result = macro.fetch_macro_context(session=session, asof=ASOF)
    assert result["eligible"] is True
    assert set(result["metrics"]) == {*macro.FRED, *macro.STOCKS}
    assert not result["warnings"]
    assert len(session.calls) == 9
    metrics = result["metrics"]
    assert metrics["rate"]["value"] == 4.25
    assert metrics["rate"]["previous"] == 4
    assert metrics["rate"]["change_pp"] == 0.25
    assert metrics["usdkrw"]["unit"] == "KRW/USD"
    assert metrics["usdkrw"]["change_pct"] == 1.5
    assert metrics["inflation"]["value"] == 2.5
    assert metrics["inflation"]["previous"] == 2.2
    assert metrics["inflation"]["change_pp"] == 0.3
    assert metrics["inflation"]["asof"] == "2026-10"
    assert "발표일 아님" in metrics["inflation"]["date_basis"]
    assert metrics["sp500"]["change_pct"] == -3
    assert metrics["aapl"]["unit"] == "USD"
    assert len(result["risk_flags"]) == 6  # 3 indices, FX, rate, inflation.
    assert all(item["source"] and item["url"].startswith("https://") for item in metrics.values())
    assert result["checked_at"] == ASOF.astimezone(timezone.utc).isoformat()


def test_unclosed_us_daily_bar_is_excluded_using_new_york_close_time():
    now = ASOF.replace(hour=10)
    result = macro.fetch_macro_context(session=Session({"AAPL": chart("AAPL", 999)}), asof=now)
    assert result["eligible"] is True
    metric = result["metrics"]["aapl"]
    assert metric["asof"] == "2026-11-04"
    assert metric["value"] == metric["previous"] == 100
    assert metric["change_pct"] == 0


@pytest.mark.parametrize("series", ["DATE,DEXKOUS\n2026-11-03,1000\n2026-11-04,-1",
                                     "DATE,DEXKOUS\n2026-11-03,1000\n2026-11-04,0",
                                     "DATE,DEXKOUS\n2026-11-03,1000\n2026-11-04,nan",
                                     "DATE,DEXKOUS\n2026-11-03,1000\n2026-11-04,inf",
                                     "DATE,DEXKOUS\n2026-11-03,.\n2026-11-04,.",
                                     "DATE,DEXKOUS\n2026-10-01,1000\n2026-10-02,1015",
                                     "DATE,DEXKOUS\n2026-11-04,1000\n2026-11-06,1015",
                                     "<html>Service unavailable</html>"])
def test_invalid_missing_stale_or_future_fx_blocks_overall_context(series):
    result = macro.fetch_macro_context(session=Session({"DEXKOUS": series}), asof=ASOF)
    assert result["eligible"] is False
    assert "usdkrw" not in result["metrics"]
    assert len(result["metrics"]) == 8
    assert any("환율" in warning for warning in result["warnings"])
    assert "판단 보류" in result["summary"]


@pytest.mark.parametrize("missing_month", ["2025-10-01", "2025-09-01", "2026-09-01"])
def test_cpi_requires_exact_comparison_months_not_an_arbitrary_12th_row(missing_month):
    text = "\n".join(line for line in cpi_csv().splitlines() if not line.startswith(missing_month))
    result = macro.fetch_macro_context(session=Session({"CPIAUCSL": text}), asof=ASOF)
    assert result["eligible"] is False
    assert "inflation" not in result["metrics"]
    assert any("CPI" in warning for warning in result["warnings"])


def test_stale_cpi_and_stale_previous_rate_are_not_verified():
    stale_cpi = "DATE,CPIAUCSL\n2025-05-01,100\n2025-06-01,100\n2026-05-01,102\n2026-06-01,103"
    stale_rate = "DATE,DFF\n2026-10-01,4\n2026-11-04,4.25"
    result = macro.fetch_macro_context(session=Session({"CPIAUCSL": stale_cpi, "DFF": stale_rate}), asof=ASOF)
    assert not result["eligible"]
    assert "rate" not in result["metrics"]
    assert "inflation" not in result["metrics"]


@pytest.mark.parametrize("value", [None, float("nan"), float("inf"), 0, -1])
def test_latest_complete_market_value_must_be_finite_and_positive(value):
    result = macro.fetch_macro_context(session=Session({"^SOX": chart("^SOX", value)}), asof=ASOF)
    assert not result["eligible"]
    assert "sox" not in result["metrics"]
    assert len(result["metrics"]) == 8


@pytest.mark.parametrize("dates", [("2026-10-01", "2026-10-02", "2026-10-05"),
                                    ("2026-11-03", "2026-11-04", "2026-11-06")])
def test_market_stale_or_future_observation_blocks_verification(dates):
    result = macro.fetch_macro_context(session=Session({"NVDA": chart("NVDA", dates=dates)}), asof=ASOF)
    assert not result["eligible"]
    assert "nvda" not in result["metrics"]


def test_provider_failure_keeps_other_metrics_and_cannot_be_called_benign():
    result = macro.fetch_macro_context(session=Session({"DFF": requests.Timeout("FRED unavailable")}), asof=ASOF)
    assert not result["eligible"]
    assert len(result["metrics"]) == 8
    assert any("FRED unavailable" in warning for warning in result["warnings"])
    assert "판단 보류" in result["summary"]


def test_missing_previous_observations_and_overflowing_changes_are_not_verified():
    missing_previous = chart("AAPL")
    missing_previous["chart"]["result"][0]["indicators"]["quote"][0]["close"][-2] = None
    result = macro.fetch_macro_context(session=Session({
        "DFF": "DATE,DFF\n2026-11-04,4.25",
        "DEXKOUS": "DATE,DEXKOUS\n2026-11-03,1e-300\n2026-11-04,1e300",
        "AAPL": missing_previous,
    }), asof=ASOF)
    assert not result["eligible"]
    assert not {"rate", "usdkrw", "aapl"}.intersection(result["metrics"])


def test_foreign_market_ticker_does_not_verify_requested_symbol():
    result = macro.fetch_macro_context(session=Session({"NVDA": chart("AAPL")}), asof=ASOF)
    assert not result["eligible"]
    assert "nvda" not in result["metrics"]


def freeze_now(monkeypatch):
    class FrozenDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return ASOF.astimezone(tz or timezone.utc)
    monkeypatch.setattr(macro, "datetime", FrozenDateTime)


def test_success_cache_is_deep_copied_expires_and_asof_bypasses_it(monkeypatch):
    freeze_now(monkeypatch)
    session = Session()
    first = macro.fetch_macro_context(session=session)
    assert first["eligible"]
    first["metrics"]["rate"]["value"] = 999
    first["risk_flags"].append("caller mutation")
    cached = macro.fetch_macro_context(session=session)
    assert len(session.calls) == 9
    assert cached["metrics"]["rate"]["value"] == 4.25
    assert "caller mutation" not in cached["risk_flags"]
    assert cached["cached_at"] == cached["checked_at"]
    assert macro.fetch_macro_context(session=session, asof=ASOF)["eligible"]
    assert len(session.calls) == 18
    macro._cache["stored_at"] -= macro.CACHE_SECONDS + 1
    assert macro.fetch_macro_context(session=session)["eligible"]
    assert len(session.calls) == 27


def test_incomplete_results_are_not_cached(monkeypatch):
    freeze_now(monkeypatch)
    session = Session({"AAPL": requests.Timeout("unavailable")})
    assert not macro.fetch_macro_context(session=session)["eligible"]
    assert not macro.fetch_macro_context(session=session)["eligible"]
    assert len(session.calls) == 18
    assert macro._cache is None


def test_retrieval_overlaps_with_at_most_six_workers_without_holding_cache_lock(monkeypatch):
    rendezvous = Barrier(6)
    lock = Lock()
    started, active, maximum = [], 0, 0
    real_fetch = macro._fetch_metric
    session = Session()

    def fetch(key, now, client):
        nonlocal active, maximum
        assert macro._cache_lock.acquire(blocking=False)
        macro._cache_lock.release()
        with lock:
            started.append(key)
            position = len(started)
            active += 1
            maximum = max(maximum, active)
        try:
            if position <= 6:
                rendezvous.wait(timeout=10)
            return real_fetch(key, now, client)
        finally:
            with lock:
                active -= 1

    monkeypatch.setattr(macro, "_fetch_metric", fetch)
    result = macro.fetch_macro_context(session=session, asof=ASOF)
    assert result["eligible"]
    assert len(started) == 9
    assert maximum == 6
