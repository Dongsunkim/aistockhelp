"""Offline fixtures for Naver's legacy, EUC-KR company-research listing."""
from html import escape

import pytest

from naver_research import LIST_URL, fetch_naver_research, parse_naver_research


def row(symbol="005930", title="이익 회복과 사업 전망", broker="테스트증권",
        published="26.11.02", nid="12345", detail=None, pdf=None):
    detail = detail if detail is not None else f"company_read.naver?nid={nid}&amp;page=1"
    pdf = pdf if pdf is not None else f"https://stock.pstatic.net/stock-research/company/{nid}.pdf"
    return f'''<tr>
      <td><a href="/item/main.naver?code={symbol}" title="삼성전자">삼성전자</a></td>
      <td><a href="{escape(detail, quote=True).replace('&amp;amp;', '&amp;')}"><span>{title}</span></a></td>
      <td><span>{broker}</span></td>
      <td class="file"><a href="{escape(pdf, quote=True)}" target="_blank"><img src="/pdf.gif"></a></td>
      <td class="date">{published}</td><td class="number">1,234</td>
    </tr>'''


def listing(*rows, encoding="utf-8"):
    return f'''<!doctype html><html><head><meta http-equiv="Content-Type"
        content="text/html; charset={encoding}"></head><body>
      <table class="type_1" summary="종목분석 리포트">
        <colgroup><col><col><col><col><col><col></colgroup>
        <tr><th>종목명</th><th>제목</th><th>증권사</th><th>첨부파일</th><th>작성일</th><th>조회수</th></tr>
        <tr><td colspan="6" class="blank_07"></td></tr>
        {''.join(rows)}
      </table></body></html>'''


def test_parse_listing_metadata_and_real_links_without_claiming_report_read():
    report = parse_naver_research(listing(row(title="사업 전망 &amp; 수익성")), "005930")[0]
    assert report["title"] == "사업 전망 & 수익성"
    assert report["source"] == "네이버증권 리서치"
    assert report["broker"] == "테스트증권"
    assert report["date"] == report["published_at"] == "2026-11-02"
    assert report["link"] == "https://finance.naver.com/research/company_read.naver?nid=12345&page=1"
    assert report["report_url"] == "https://stock.pstatic.net/stock-research/company/12345.pdf"
    assert report["verification"] == "목록 확인 · 원문 미확인"
    assert report["type"] == "research_report"
    assert not any(key in report for key in ("target_price", "recommendation", "content", "summary"))


@pytest.mark.parametrize("stamp,expected", [("19.03.04", "2019-03-04"),
                                             ("1999.12.31", "1999-12-31"),
                                             ("99.12.31", "1999-12-31"),
                                             ("2020-01-02", "2020-01-02")])
def test_historical_reports_keep_their_original_publication_dates(stamp, expected):
    reports = parse_naver_research(listing(row(published=stamp)), "005930")
    assert reports[0]["date"] == expected
    assert "원문 미확인" in reports[0]["verification"]


def test_rows_with_foreign_stock_or_conflicting_detail_ticker_are_rejected():
    html = listing(row(symbol="000660", nid="1"),
                   row(detail="company_read.naver?nid=2&itemCode=000660", nid="2"),
                   row(detail="company_read.naver?nid=3&code=005930&code=000660", nid="3"),
                   row(nid="4"))
    reports = parse_naver_research(html, "005930")
    assert len(reports) == 1
    assert "nid=4" in reports[0]["link"]


def test_foreign_modern_naver_stock_path_is_rejected():
    html = listing(row().replace("/item/main.naver?code=005930",
                                 "https://stock.naver.com/domestic/stock/000660/total"))
    assert parse_naver_research(html, "005930") == []


@pytest.mark.parametrize("detail", ["javascript:openReport(1)", "#", "", "/research/company_read.naver",
                                     "https://evil.example/company_read.naver?nid=1",
                                     "https://finance.naver.com.evil.example/research/company_read.naver?nid=1",
                                     "company_read.naver?nid=bad", "company_read.naver?nid=1&nid=2"])
def test_missing_malformed_and_script_detail_links_do_not_become_sources(detail):
    assert parse_naver_research(listing(row(detail=detail)), "005930") == []


@pytest.mark.parametrize("pdf", ["javascript:alert(1)", "https://evil.example/report.pdf",
                                  "https://stock.pstatic.net.evil.example/report.pdf",
                                  "https://stock.pstatic.net/stock-research/report.html", "#"])
def test_unsafe_pdf_is_omitted_while_valid_naver_detail_remains(pdf):
    report = parse_naver_research(listing(row(pdf=pdf)), "005930")[0]
    assert report["link"].startswith("https://finance.naver.com/research/company_read.naver?")
    assert "report_url" not in report


@pytest.mark.parametrize("stamp", ["26.02.30", "9999.01.01", "작성일 없음", "", "26.13.01"])
def test_invalid_or_future_dates_are_not_fabricated(stamp):
    assert parse_naver_research(listing(row(published=stamp)), "005930") == []


def test_empty_or_incomplete_rows_and_duplicates_are_skipped():
    html = listing(row(title="", nid="1"), row(broker="", nid="2"),
                   "<tr><td>연결된 보고서 없음</td></tr>", row(nid="3"), row(nid="3"), row(nid="4"))
    reports = parse_naver_research(html, "005930", limit=1)
    assert len(reports) == 1
    assert "nid=3" in reports[0]["link"]


def test_no_reports_is_empty_but_missing_listing_table_is_an_explicit_failure():
    assert parse_naver_research(listing("<tr><td colspan='6'>등록된 자료가 없습니다.</td></tr>"), "005930") == []
    with pytest.raises(ValueError, match="목록 표"):
        parse_naver_research("<html><h1>서비스 점검 중</h1></html>", "005930")


def test_fetch_uses_stock_filter_decodes_euc_kr_and_never_downloads_pdf():
    class Response:
        content = listing(row(), encoding="euc-kr").encode("euc-kr")

        def raise_for_status(self):
            pass

    class Session:
        calls = []

        def get(self, url, **kwargs):
            self.calls.append((url, kwargs))
            return Response()

    session = Session()
    reports = fetch_naver_research("005930", session=session)
    assert len(reports) == 1
    assert reports[0]["broker"] == "테스트증권"
    assert len(session.calls) == 1
    url, kwargs = session.calls[0]
    assert url == LIST_URL
    assert kwargs["params"] == {"searchType": "itemCode", "itemCode": "005930"}
    assert kwargs["timeout"] == 8


def test_invalid_request_is_rejected_before_network():
    class NoNetwork:
        def get(self, *args, **kwargs):
            raise AssertionError("invalid input must not make a request")

    for symbol in ("AAPL", "00593", "005930&itemCode=000660", "１２３４５６"):
        with pytest.raises(ValueError, match="6자리"):
            fetch_naver_research(symbol, session=NoNetwork())
    with pytest.raises(ValueError, match="조회 건수"):
        fetch_naver_research("005930", session=NoNetwork(), limit=0)
