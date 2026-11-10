"""Resolve stock identities before a name can be attached to another ticker.

The small bundled catalog makes common selections instant. Other identities
must come from an explicitly identified stock search provider; a guessed ticker
or a user-entered company label is never treated as a verified identity.
"""
from __future__ import annotations

from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import re
import threading
import time
import unicodedata

import requests


CATALOG_SOURCE = "기본 종목 목록"
NAVER_SOURCE = "네이버증권 종목 검색"
YAHOO_SOURCE = "Yahoo Finance 종목 검색"
_TIMEOUT = 2.5
_CACHE_TTL = 3600
_CACHE_SIZE = 128
_MAX_RESULTS = 20
_MARKETS = {"KOSPI", "KOSDAQ", "US"}
_US_EXCHANGES = {"NMS", "NGM", "NCM", "NYQ", "ASE", "PCX", "BTS"}
_US_SYMBOL = re.compile(r"[A-Z][A-Z0-9]{0,9}(?:[.\-][A-Z])?")
_FOREIGN_PRODUCT_TYPES = {"ETF", "ETN", "ELW", "FUND", "INDEX", "WARRANT", "BOND"}
_sessions = threading.local()
_cache_lock = threading.RLock()
_cache: OrderedDict[tuple[str, str | None], tuple[float, list[dict]]] = OrderedDict()


class StockLookupUnavailable(ValueError):
    """The provider could not establish whether a stock exists."""


def _stock(symbol, name, market, *aliases):
    return {"symbol": symbol, "name": name, "market": market,
            "aliases": list(aliases), "source": CATALOG_SOURCE}


_CATALOG = (
    _stock("005930", "삼성전자", "KOSPI", "Samsung Electronics"),
    _stock("005935", "삼성전자우", "KOSPI", "삼성전자 우선주", "Samsung Electronics Preferred"),
    _stock("000660", "SK하이닉스", "KOSPI", "SK Hynix", "에스케이하이닉스"),
    _stock("035420", "NAVER", "KOSPI", "네이버"),
    _stock("035720", "카카오", "KOSPI", "Kakao"),
    _stock("005380", "현대차", "KOSPI", "현대자동차", "Hyundai Motor"),
    _stock("000270", "기아", "KOSPI", "Kia"),
    _stock("373220", "LG에너지솔루션", "KOSPI", "LG Energy Solution"),
    _stock("068270", "셀트리온", "KOSPI", "Celltrion"),
    _stock("086520", "에코프로", "KOSDAQ", "EcoPro"),
    _stock("247540", "에코프로비엠", "KOSDAQ", "EcoPro BM"),
    _stock("196170", "알테오젠", "KOSDAQ", "Alteogen"),
    _stock("AAPL", "Apple", "US", "애플", "Apple Inc."),
    _stock("NVDA", "NVIDIA", "US", "엔비디아", "NVIDIA Corporation"),
    _stock("MSFT", "Microsoft", "US", "마이크로소프트", "Microsoft Corporation"),
    _stock("TSLA", "Tesla", "US", "테슬라", "Tesla, Inc."),
    _stock("AMZN", "Amazon", "US", "아마존", "Amazon.com, Inc."),
    _stock("GOOGL", "Alphabet", "US", "알파벳", "Alphabet Inc."),
)


def _normalize(value: str) -> str:
    return "".join(unicodedata.normalize("NFKC", value).split()).casefold()


def known_stocks() -> list[dict]:
    """Return the bundled catalog, including aliases for instant client lookup."""
    return deepcopy(list(_CATALOG))


def _market(value: str | None) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or value.strip().upper() not in _MARKETS:
        raise ValueError("시장 선택은 US, KOSPI, KOSDAQ 중 하나여야 합니다.")
    return value.strip().upper()


def _labels(item: dict):
    return (item["symbol"], item["name"], *item.get("aliases", []))


def _matches(item: dict, query: str, *, exact=False) -> bool:
    labels = [_normalize(label) for label in _labels(item)]
    return query in labels if exact else any(query in label for label in labels)


def _deduplicate(items: list[dict]) -> list[dict]:
    result = []
    seen = set()
    for item in items:
        key = (item["market"], item["symbol"])
        if key not in seen:
            result.append(item)
            seen.add(key)
    return result


def _get_session():
    session = getattr(_sessions, "session", None)
    if session is None:
        session = requests.Session()
        session.headers.update({"User-Agent": "Mozilla/5.0 (stock identity search)"})
        _sessions.session = session
    return session


def _json(url: str, params: dict):
    try:
        response = _get_session().get(url, params=params, timeout=_TIMEOUT)
        response.raise_for_status()
        return response.json()
    except (requests.RequestException, ValueError) as exc:
        raise StockLookupUnavailable("종목 검색에 연결하지 못했습니다. 잠시 후 다시 검색해 주세요.") from exc


def _provider_identity(symbol, name, market, source) -> dict | None:
    if not isinstance(symbol, str) or not isinstance(name, str):
        return None
    symbol, name = symbol.strip().upper(), name.strip()
    if not name or len(name) > 80 or any(unicodedata.category(c).startswith("C") for c in name):
        return None
    if market == "US":
        if not _US_SYMBOL.fullmatch(symbol):
            return None
    elif market not in {"KOSPI", "KOSDAQ"} or not re.fullmatch(r"[0-9]{6}", symbol):
        return None
    # An inconsistent provider record must not overwrite the bundled identity.
    seed = next((s for s in _CATALOG if s["symbol"] == symbol), None)
    if seed:
        if seed["market"] != market or not _matches(seed, _normalize(name), exact=True):
            return None
        item = deepcopy(seed)
        item["source"] = source
        return item
    return {"symbol": symbol, "name": name, "market": market,
            "aliases": [], "source": source}


def _search_naver(query: str) -> list[dict]:
    data = _json("https://ac.stock.naver.com/ac", {"q": query, "target": "stock"})
    if not isinstance(data, dict) or not isinstance(data.get("items"), list):
        raise StockLookupUnavailable("네이버증권 종목 검색 응답을 확인할 수 없습니다.")
    results = []
    for item in data["items"][:100]:
        if not isinstance(item, dict):
            continue
        market = item.get("typeCode")
        if market not in {"KOSPI", "KOSDAQ"}:
            continue
        if any(str(item.get(field, "")).upper() in _FOREIGN_PRODUCT_TYPES
               for field in ("securityType", "productType", "category", "quoteType")):
            continue
        identity = _provider_identity(item.get("code"), item.get("name"), market, NAVER_SOURCE)
        if identity:
            results.append(identity)
    return _deduplicate(results)


def _search_yahoo(query: str) -> list[dict]:
    data = _json("https://query1.finance.yahoo.com/v1/finance/search",
                 {"q": query, "quotesCount": _MAX_RESULTS, "newsCount": 0})
    if not isinstance(data, dict) or not isinstance(data.get("quotes"), list):
        raise StockLookupUnavailable("미국 종목 검색 응답을 확인할 수 없습니다.")
    results = []
    for item in data["quotes"][:100]:
        if not isinstance(item, dict) or item.get("quoteType") != "EQUITY":
            continue
        if item.get("exchange") not in _US_EXCHANGES:
            continue
        identity = _provider_identity(item.get("symbol"), item.get("shortname") or item.get("longname"),
                                      "US", YAHOO_SOURCE)
        if identity:
            results.append(identity)
    return _deduplicate(results)


def _cached(key):
    with _cache_lock:
        entry = _cache.get(key)
        if entry is None:
            return None
        at, items = entry
        if time.monotonic() - at >= _CACHE_TTL:
            del _cache[key]
            return None
        _cache.move_to_end(key)
        return deepcopy(items)


def _remember(key, items):
    with _cache_lock:
        _cache[key] = (time.monotonic(), deepcopy(items))
        _cache.move_to_end(key)
        while len(_cache) > _CACHE_SIZE:
            _cache.popitem(last=False)


def _remote_search(query: str, market: str | None) -> list[dict]:
    if market == "US":
        return _search_yahoo(query)
    if market in {"KOSPI", "KOSDAQ"} or re.search(r"[가-힣ㄱ-ㅎㅏ-ㅣ]", query) or query.isdigit():
        return _search_naver(query)
    # A Latin company name can belong to either market. Fetch concurrently so
    # an unknown name does not incur two sequential provider waits.
    results, errors = [], []
    with ThreadPoolExecutor(max_workers=2, thread_name_prefix="stock-search") as pool:
        jobs = [pool.submit(provider, query) for provider in (_search_naver, _search_yahoo)]
        for job in jobs:
            try:
                results.extend(job.result())
            except StockLookupUnavailable as exc:
                errors.append(exc)
    if not results and errors:
        raise errors[0]
    return _deduplicate(results)


def search_stocks(query: str, market: str | None = None, limit: int = 8) -> list[dict]:
    """Search names/codes; omit market to allow switching between markets.

    Exact and partial bundled matches are served without a network request.
    Partial matches remain suggestions; callers must not choose the first one
    as though it were a unique exact name. Provider failures raise instead of
    being reported as a successful search with no matching stock.
    """
    market = _market(market)
    if not isinstance(query, str) or len(query) > 80:
        raise ValueError("검색어는 80자 이내로 입력해 주세요.")
    if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= _MAX_RESULTS:
        raise ValueError("검색 결과 수는 1~20개여야 합니다.")
    normalized = _normalize(query)
    if not normalized:
        return []
    candidates = [s for s in _CATALOG if market is None or s["market"] == market]
    matches = [s for s in candidates if _matches(s, normalized)]
    if matches:
        matches.sort(key=lambda s: (not _matches(s, normalized, exact=True), s["name"], s["symbol"]))
        return deepcopy(matches[:limit])
    key = (normalized, market)
    matches = _cached(key)
    if matches is None:
        matches = _remote_search(query.strip(), market)
        matches = [s for s in matches if market is None or s["market"] == market]
        matches.sort(key=lambda s: (not _matches(s, normalized, exact=True), s["name"], s["symbol"]))
        matches = matches[:_MAX_RESULTS]
        _remember(key, matches)
    return deepcopy(matches[:limit])


def _known_identities() -> list[dict]:
    # Reuse successfully looked-up names when checking subsequent requests.
    with _cache_lock:
        now = time.monotonic()
        remote = [item for at, items in _cache.values() if now - at < _CACHE_TTL for item in items]
        return _deduplicate(list(_CATALOG) + remote)


def resolve_stock(symbol: str, market: str, name: str = "") -> dict:
    """Return a canonical identity, rejecting known name/code contradictions."""
    market = _market(market)
    if not isinstance(symbol, str):
        raise ValueError("종목코드를 확인해 주세요.")
    symbol = unicodedata.normalize("NFKC", symbol).strip().upper()
    if market == "US":
        valid = _US_SYMBOL.fullmatch(symbol)
    else:
        valid = re.fullmatch(r"[0-9]{6}", symbol)
    if not valid:
        raise ValueError("종목코드와 시장을 확인해 주세요.")
    if not isinstance(name, str) or len(name) > 80:
        raise ValueError("기업명은 80자 이내로 입력해 주세요.")
    known = _known_identities()
    identity = next((s for s in known if s["symbol"] == symbol and s["market"] == market), None)
    other_market = next((s for s in known if s["symbol"] == symbol and s["market"] != market), None)
    if identity is None and other_market:
        raise ValueError(f'{other_market["name"]}({symbol})의 시장은 {other_market["market"]}입니다. 시장을 변경해 주세요.')
    name_matches = [s for s in known if name.strip() and _matches(s, _normalize(name), exact=True)]
    name_matches = _deduplicate(name_matches)
    if len(name_matches) == 1:
        match = name_matches[0]
        if (match["symbol"], match["market"]) != (symbol, market):
            raise ValueError(f'기업명과 종목코드가 일치하지 않습니다. {match["name"]}의 종목코드는 '
                             f'{match["symbol"]}({match["market"]})입니다. 다시 선택해 주세요.')
    if identity is None:
        matches = search_stocks(symbol, market=market, limit=_MAX_RESULTS)
        exact = [s for s in matches if s["symbol"] == symbol and s["market"] == market]
        if len(exact) != 1:
            raise ValueError("해당 종목코드의 기업명과 시장을 확인할 수 없습니다. 종목을 다시 검색해 주세요.")
        identity = exact[0]
    return deepcopy(identity)
