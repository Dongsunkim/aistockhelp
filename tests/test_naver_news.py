"""Offline fixtures for Naver's legacy ticker-news table, including related rows."""
from datetime import datetime
from urllib.parse import parse_qs, urlsplit
from zoneinfo import ZoneInfo

import pytest
import requests

import naver_news
from naver_news import fetch_naver_news


class FrozenDateTime(datetime):
    @classmethod
    def now(cls, tz=None):
        fixed = cls(2026, 11, 6, 11, 0, tzinfo=ZoneInfo("Asia/Seoul"))
        return fixed.astimezone(tz) if tz else fixed.replace(tzinfo=None)


@pytest.fixture(autouse=True)
def fixed_time(monkeypatch):
    monkeypatch.setattr(naver_news, "datetime", FrozenDateTime)


class Session:
    def __init__(self, html, encoding="utf-8", content_type="text/html; charset=utf-8", status=200):
        self.calls = []
        self.response = requests.Response()
        self.response.status_code = status
        self.response._content = html.encode(encoding)
        self.response.headers["Content-Type"] = content_type
        self.response.encoding = encoding
        self.response.url = "https://finance.naver.com/item/news_news.naver?code=005930&page=1"

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return self.response


def article(article_id="000000001", code="005930", title="삼성전자 실적 발표", source="연합뉴스",
            stamp="2026.11.06 10:30", office_id="001", extra=""):
    return (f'<tr><td class="title" {extra}><a href="/item/news_read.naver?article_id={article_id}'
            f'&amp;office_id={office_id}&amp;code={code}&amp;page=1">{title}</a></td>'
            f'<td class="info">{source}</td><td class="date">{stamp}</td></tr>')


def table(rows):
    return ('<table summary="주요뉴스의 제목, 정보제공, 날짜별정보제공." class="type5">'
            '<caption>주요뉴스</caption><thead><tr><th>제목</th><th>정보제공</th><th>날짜</th></tr></thead>'
            f'<tbody>{rows}</tbody></table>')


def test_parses_realistic_title_press_date_and_preserves_article_provenance():
    session = Session(table(article(title="삼성전자 &amp; 반도체 <b>실적</b>")))
    items = fetch_naver_news("005930", session=session)
    assert len(items) == 1
    item = items[0]
    assert item["title"] == "삼성전자 & 반도체 실적"
    assert item["source"] == "연합뉴스"
    assert item["published_at"] == "2026-11-06T10:30:00+09:00"
    assert item["age_hours"] == 0.5
    assert item["link"].startswith("https://finance.naver.com/item/news_read.naver?")
    query = parse_qs(urlsplit(item["link"]).query)
    assert query["office_id"] == ["001"]
    assert query["article_id"] == ["000000001"]
    assert query["code"] == ["005930"]
    assert "원문 미확인" in item["basis"]
    assert "sentiment" not in item and "content" not in item
    url, kwargs = session.calls[0]
    assert url == "https://finance.naver.com/item/news_news.naver"
    assert kwargs["params"] == {"code": "005930", "page": 1}
    assert kwargs["timeout"] == 8


def test_nested_related_rows_keep_own_sources_and_do_not_duplicate_articles():
    related = ('<tr><td class="sub_news" colspan="3">' + table(
        article("000000002", title="관련 보도", source="한국경제", stamp="2026.11.06 09:20")
    ) + '</td></tr>')
    html = table(article(extra='rowspan="1"') + related + article("000000002", source="한국경제",
        stamp="2026.11.06 09:20"))
    items = fetch_naver_news("005930", session=Session(html))
    assert [item["source"] for item in items] == ["연합뉴스", "한국경제"]
    assert [item["published_at"] for item in items] == [
        "2026-11-06T10:30:00+09:00", "2026-11-06T09:20:00+09:00"]


def test_related_row_cannot_inherit_parent_press_or_date():
    broken = ('<tr><td class="title"><a href="/item/news_read.naver?article_id=2'
              '&amp;office_id=001&amp;code=005930">관련 기사</a></td></tr>')
    html = table(article() + '<tr><td class="sub_news" colspan="3">' + table(broken) + '</td></tr>')
    with pytest.raises(ValueError, match="누락"):
        fetch_naver_news("005930", session=Session(html))


def test_rowspan_title_does_not_create_an_extra_article_in_the_next_row():
    first = article(extra='rowspan="2"')
    following = '<tr><td class="info">다른 언론사</td><td class="date">2026.11.06 09:00</td></tr>'
    items = fetch_naver_news("005930", session=Session(table(first + following)))
    assert len(items) == 1
    assert items[0]["source"] == "연합뉴스"


def test_rowspan_metadata_is_not_assigned_to_a_different_article():
    first = article().replace('class="info"', 'class="info" rowspan="2"').replace(
        'class="date"', 'class="date" rowspan="2"')
    second = ('<tr><td class="title"><a href="/item/news_read.naver?article_id=2'
              '&amp;office_id=001&amp;code=005930">다른 기사</a></td></tr>')
    with pytest.raises(ValueError, match="누락"):
        fetch_naver_news("005930", session=Session(table(first + second)))


@pytest.mark.parametrize("symbol", ["AAPL", "00593", "005930.KS", "００５９３０", None, 5930])
def test_symbol_is_validated_before_any_network_request(symbol):
    session = Session(table(""))
    with pytest.raises(ValueError, match="6자리"):
        fetch_naver_news(symbol, session=session)
    assert session.calls == []


def test_unsafe_links_cross_ticker_and_repeated_query_codes_are_rejected():
    rows = article("1", code="000660")
    rows += article("2").replace('/item/news_read.naver?', 'javascript:alert(1)?')
    rows += article("3").replace('/item/news_read.naver?', 'https://example.org/item/news_read.naver?')
    rows += article("4").replace('&amp;page=1', '&amp;code=000660&amp;page=1')
    rows += article("5")
    items = fetch_naver_news("005930", session=Session(table(rows)))
    assert len(items) == 1
    assert parse_qs(urlsplit(items[0]["link"]).query)["article_id"] == ["5"]


def test_code_optional_but_office_and_article_ids_required():
    rows = article("1").replace('&amp;code=005930', '')
    rows += article("2").replace('&amp;office_id=001', '')
    items = fetch_naver_news("005930", session=Session(table(rows)))
    assert len(items) == 1
    assert "code" not in parse_qs(urlsplit(items[0]["link"]).query)


def test_future_publication_is_never_clamped_to_fresh_zero_age():
    items = fetch_naver_news("005930", session=Session(table(article(stamp="2026.11.06 11:01"))))
    assert items[0]["age_hours"] < 0
    assert items[0]["published_at"] == "2026-11-06T11:01:00+09:00"


def test_euc_kr_declared_in_meta_decodes_korean_without_replacement():
    html = '<meta http-equiv="Content-Type" content="text/html; charset=euc-kr">' + table(article())
    session = Session(html, encoding="euc-kr", content_type="text/html")
    session.response.encoding = "ISO-8859-1"  # requests' generic text/html default is not authoritative.
    items = fetch_naver_news("005930", session=session)
    assert items[0]["title"] == "삼성전자 실적 발표"
    assert items[0]["source"] == "연합뉴스"


@pytest.mark.parametrize("html", ["<html>접근 제한</html>", '<table class="type5"><tr><td>차단</td></tr></table>',
                                 '<table class="type5"><tr><td class="title">차단</td></tr></table>',
                                 table(article()).replace('</table>', '')])
def test_missing_or_broken_news_table_raises_instead_of_empty_success(html):
    with pytest.raises(ValueError, match="표 형식"):
        fetch_naver_news("005930", session=Session(html))


def test_expected_empty_news_table_is_a_legitimate_empty_result():
    assert fetch_naver_news("005930", session=Session(table(
        '<tr><td colspan="3">등록된 뉴스가 없습니다.</td></tr>'))) == []


@pytest.mark.parametrize("stamp", ["2026.02.30 10:30", "2026.11.06", "", "발행일 미상"])
def test_invalid_or_missing_publication_is_not_invented(stamp):
    with pytest.raises(ValueError):
        fetch_naver_news("005930", session=Session(table(article(stamp=stamp))))


def test_result_limit_applies_after_article_deduplication_and_date_sorting():
    html = table(article("1", stamp="2026.11.06 08:00") +
                 article("2", stamp="2026.11.06 10:00") + article("3", stamp="2026.11.06 09:00"))
    items = fetch_naver_news("005930", session=Session(html), limit=2)
    assert [parse_qs(urlsplit(item["link"]).query)["article_id"][0] for item in items] == ["2", "3"]


def test_http_failure_is_not_reported_as_no_news():
    with pytest.raises(requests.HTTPError):
        fetch_naver_news("005930", session=Session("blocked", status=403))
