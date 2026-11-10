"""Offline identity and provider-contract checks; never query live markets."""
from copy import deepcopy

import pytest
import requests

import stock_lookup as lookup


@pytest.fixture(autouse=True)
def isolated_lookup(monkeypatch):
    with lookup._cache_lock:
        lookup._cache.clear()
    monkeypatch.setattr(lookup, "_get_session", lambda: pytest.fail("unexpected live stock lookup"))


@pytest.mark.parametrize("query,symbol,market", [
    ("삼성전자", "005930", "KOSPI"),
    ("  ＳＡＭＳＵＮＧ  Electronics ", "005930", "KOSPI"),
    ("005930", "005930", "KOSPI"),
    ("SK 하이닉스", "000660", "KOSPI"),
    ("네이버", "035420", "KOSPI"),
    ("에코프로", "086520", "KOSDAQ"),
    ("애플", "AAPL", "US"),
    ("nvidia", "NVDA", "US"),
])
def test_common_names_codes_and_aliases_are_instant(query, symbol, market):
    result = lookup.search_stocks(query)
    assert (result[0]["symbol"], result[0]["market"]) == (symbol, market)
    assert result[0]["source"] == lookup.CATALOG_SOURCE


def test_preferred_shares_and_ambiguous_substrings_are_separate():
    result = lookup.search_stocks("삼성")
    assert {s["symbol"] for s in result} == {"005930", "005935"}
    assert lookup.search_stocks("삼성전자우")[0]["symbol"] == "005935"
    assert lookup.search_stocks("삼성전자")[0]["symbol"] == "005930"
    assert {s["symbol"] for s in lookup.search_stocks("에코프로")} == {"086520", "247540"}


def test_catalog_and_results_cannot_be_mutated_by_callers():
    catalog = lookup.known_stocks()
    assert len(catalog) == 18
    catalog[0]["name"] = "wrong"
    catalog[0]["aliases"].append("wrong alias")
    result = lookup.search_stocks("삼성전자")
    result[0]["aliases"].clear()
    assert lookup.known_stocks()[0]["name"] == "삼성전자"
    assert "Samsung Electronics" in lookup.known_stocks()[0]["aliases"]
    assert all('wrong alias' not in stock['aliases'] for stock in lookup.known_stocks())


def test_no_market_filter_permits_automatic_market_change():
    assert lookup.search_stocks("에코프로")[0]["market"] == "KOSDAQ"
    assert all(s["market"] == "KOSDAQ" for s in lookup.search_stocks("에코프로", market="KOSDAQ"))


@pytest.mark.parametrize("symbol,market,name,expected", [
    ("005930", "KOSPI", "삼성전자", "삼성전자"),
    ("005930", "KOSPI", "아직 모르는 기업명", "삼성전자"),
    ("aapl", "US", "애플", "Apple"),
    ("005935", "KOSPI", "삼성전자우", "삼성전자우"),
])
def test_resolve_uses_symbol_canonical_name(symbol, market, name, expected):
    result = lookup.resolve_stock(symbol, market, name)
    assert result["symbol"] == symbol.upper()
    assert result["name"] == expected


@pytest.mark.parametrize("symbol,market,name,expected", [
    ("000660", "KOSPI", "삼성전자", "005930"),
    ("005935", "KOSPI", "삼성전자", "005930"),
    ("AAPL", "US", "엔비디아", "NVDA"),
    ("086520", "KOSPI", "에코프로", "KOSDAQ"),
])
def test_resolve_rejects_known_name_or_market_contradiction(symbol, market, name, expected):
    with pytest.raises(ValueError, match=expected):
        lookup.resolve_stock(symbol, market, name)


def install_response(monkeypatch, payload):
    calls = []

    class Response:
        def raise_for_status(self):
            pass

        def json(self):
            return deepcopy(payload)

    class Session:
        def get(self, url, **kwargs):
            calls.append((url, kwargs))
            return Response()

    monkeypatch.setattr(lookup, "_get_session", Session)
    return calls


def test_naver_uses_validated_equity_code_name_market_contract(monkeypatch):
    calls = install_response(monkeypatch, {"items": [
        {"code": "012450", "name": "한화에어로스페이스", "typeCode": "KOSPI"},
        {"code": "041510", "name": "에스엠", "typeCode": "KOSDAQ"},
        {"code": "123456", "name": "펀드", "typeCode": "ETF"},
        {"code": "123457", "name": "다른 펀드", "typeCode": "KOSPI", "quoteType": "ETF"},
        {"code": "ABCDEF", "name": "잘못된 코드", "typeCode": "KOSPI"},
        {"code": "005930", "name": "잘못된 기업", "typeCode": "KOSPI"},
        {"code": "005930", "name": "삼성전자", "typeCode": "KOSDAQ"},
        {"code": "123458", "name": "마켓 미확인", "typeCode": "KR"},
    ]})
    result = lookup.search_stocks("한화에어로", market="KOSPI")
    assert [(s["symbol"], s["name"], s["market"]) for s in result] == [
        ("012450", "한화에어로스페이스", "KOSPI")]
    assert result[0]["source"] == lookup.NAVER_SOURCE
    assert calls[0][0] == "https://ac.stock.naver.com/ac"
    assert calls[0][1]["params"] == {"q": "한화에어로", "target": "stock"}
    assert calls[0][1]["timeout"] <= 3


def test_yahoo_accepts_only_us_exchange_equities(monkeypatch):
    install_response(monkeypatch, {"quotes": [
        {"symbol": "AVGO", "shortname": "Broadcom Inc.", "exchange": "NMS", "quoteType": "EQUITY"},
        {"symbol": "SPY", "shortname": "SPDR ETF", "exchange": "PCX", "quoteType": "ETF"},
        {"symbol": "005930.KS", "shortname": "Samsung", "exchange": "KSC", "quoteType": "EQUITY"},
        {"symbol": "BAD.KS", "shortname": "Bad suffix", "exchange": "NMS", "quoteType": "EQUITY"},
        {"symbol": "SHOP.TO", "shortname": "Foreign", "exchange": "TOR", "quoteType": "EQUITY"},
        {"symbol": "XYZ", "shortname": "Unknown exchange", "exchange": "UNKNOWN", "quoteType": "EQUITY"},
    ]})
    result = lookup.search_stocks("Broadcom", market="US")
    assert [(s["symbol"], s["name"]) for s in result] == [("AVGO", "Broadcom Inc.")]
    assert result[0]["source"] == lookup.YAHOO_SOURCE


def test_unknown_symbol_requires_exact_provider_symbol_then_canonicalizes(monkeypatch):
    calls = install_response(monkeypatch, {"items": [
        {"code": "012450", "name": "한화에어로스페이스", "typeCode": "KOSPI"},
    ]})
    result = lookup.resolve_stock("012450", "KOSPI", "임의의 이름")
    assert result["name"] == "한화에어로스페이스"
    assert result["source"] == lookup.NAVER_SOURCE
    result["name"] = "tampered"
    assert lookup.resolve_stock("012450", "KOSPI")["name"] == "한화에어로스페이스"
    assert len(calls) == 1
    with pytest.raises(ValueError, match="012450"):
        lookup.resolve_stock("005930", "KOSPI", "한화에어로스페이스")


def test_unknown_code_does_not_use_fuzzy_result_or_user_label(monkeypatch):
    install_response(monkeypatch, {"items": [
        {"code": "012450", "name": "한화에어로스페이스", "typeCode": "KOSPI"},
    ]})
    with pytest.raises(ValueError, match="확인할 수 없습니다"):
        lookup.resolve_stock("999999", "KOSPI", "입력한 이름")


def test_provider_failures_are_not_cached_as_no_matches(monkeypatch):
    calls = []

    class Session:
        def get(self, *args, **kwargs):
            calls.append(args)
            raise requests.Timeout("sensitive provider details")

    monkeypatch.setattr(lookup, "_get_session", Session)
    for _ in range(2):
        with pytest.raises(lookup.StockLookupUnavailable, match="연결하지 못했습니다") as failure:
            lookup.search_stocks("조회실패종목", market="KOSPI")
        assert "sensitive" not in str(failure.value)
    assert len(calls) == 2
    assert not lookup._cache


@pytest.mark.parametrize("payload", [None, [], {"items": {}}, {"quotes": []}])
def test_unknown_naver_schema_fails_instead_of_inventing_identity(monkeypatch, payload):
    install_response(monkeypatch, payload)
    with pytest.raises(lookup.StockLookupUnavailable):
        lookup.search_stocks("미확인종목", market="KOSPI")


def test_cache_returns_copies_expires_and_is_bounded(monkeypatch):
    clock = [100.0]
    monkeypatch.setattr(lookup.time, "monotonic", lambda: clock[0])
    calls = []

    def search(query):
        calls.append(query)
        return [{"symbol": "012450", "name": "한화에어로스페이스", "market": "KOSPI",
                 "aliases": [], "source": lookup.NAVER_SOURCE}]

    monkeypatch.setattr(lookup, "_search_naver", search)
    first = lookup.search_stocks("한화검색", market="KOSPI")
    first[0]["aliases"].append("wrong")
    assert lookup.search_stocks("한화검색", market="KOSPI")[0]["aliases"] == []
    assert calls == ["한화검색"]
    clock[0] += lookup._CACHE_TTL
    lookup.search_stocks("한화검색", market="KOSPI")
    assert len(calls) == 2
    for i in range(lookup._CACHE_SIZE + 3):
        lookup.search_stocks(f"다른검색{i}", market="KOSPI")
    assert len(lookup._cache) == lookup._CACHE_SIZE


def test_latin_names_without_market_search_both_providers(monkeypatch):
    domestic = {"symbol": "012450", "name": "Hanwha Aerospace", "market": "KOSPI",
                "aliases": [], "source": lookup.NAVER_SOURCE}
    american = {"symbol": "AVGO", "name": "Broadcom", "market": "US",
                "aliases": [], "source": lookup.YAHOO_SOURCE}
    monkeypatch.setattr(lookup, "_search_naver", lambda q: [domestic])
    monkeypatch.setattr(lookup, "_search_yahoo", lambda q: [american])
    assert {s["symbol"] for s in lookup.search_stocks("uncatalogued")} == {"012450", "AVGO"}


def test_partial_provider_failure_does_not_become_a_definitive_empty_result(monkeypatch):
    def fail(q):
        raise lookup.StockLookupUnavailable("조회 실패")

    monkeypatch.setattr(lookup, "_search_naver", fail)
    monkeypatch.setattr(lookup, "_search_yahoo", lambda q: [])
    with pytest.raises(lookup.StockLookupUnavailable):
        lookup.search_stocks("uncatalogued")


@pytest.mark.parametrize("args", [
    ("삼성전자", "UNKNOWN", 8), ("a" * 81, None, 8), ("삼성전자", None, 0),
    ("삼성전자", None, 21), ("삼성전자", None, True),
])
def test_search_rejects_invalid_inputs(args):
    with pytest.raises(ValueError):
        lookup.search_stocks(*args)


def test_empty_search_is_local_and_invalid_codes_are_rejected():
    assert lookup.search_stocks("  ") == []
    for symbol, market in [("../005930", "KOSPI"), ("005930.KS", "US"), ("AAPL?", "US")]:
        with pytest.raises(ValueError):
            lookup.resolve_stock(symbol, market)
