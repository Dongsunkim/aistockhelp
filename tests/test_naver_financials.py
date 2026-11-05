"""Offline fixtures only: Naver is outside this environment's network allowlist."""
from datetime import date, datetime
from html import escape

import pytest

import naver_financials
from naver_financials import fetch_naver_financials, parse_naver_financials


TODAY = date(2026, 11, 6)
ANNUAL = ["2023.12", "2024.12", "2025.12", "2026.12(E)"]
QUARTERS = ["2025.09", "2025.12", "2026.03", "2026.06", "2026.09(E)", "2026.12(E)"]


def earnings_html(*, annual=None, quarters=None, basis="IFRS연결", values=None,
                  unit_note="단위 : 억원, 배, %, 원", third_header=True, combined_basis=False):
    """Mirror Naver's grouped headers and optional third IFRS header row."""
    annual = ANNUAL if annual is None else annual
    quarters = QUARTERS if quarters is None else quarters
    dates = annual + quarters
    width = len(dates)
    values = values or {
        "매출액": ["1000"] * width,
        "영업이익": ["200"] * width,
        "당기순이익": ["100"] * width,
    }
    groups = ""
    if annual:
        groups += f'<th colspan="{len(annual)}">최근 연간 실적</th>'
    if quarters:
        groups += f'<th colspan="{len(quarters)}">최근 분기 실적</th>'
    # Nested E markup must still mark the complete date column as an estimate.
    bases = [basis] * width if isinstance(basis, str) else basis
    headings = "".join('<th><div>' + escape(v).replace("(E)", "<em>(E)</em>")
                       + ('<br><em>' + escape(bases[i]) + '</em>' if combined_basis else '')
                       + '</div></th>' for i, v in enumerate(dates))
    basis_row = '<tr>' + ''.join(f'<th>{escape(v)}</th>' for v in bases) + '</tr>' if third_header else ''
    body = ''.join('<tr><th scope="row"><div>' + escape(name) + '</div></th>'
                   + ''.join('<td><em>' + escape(str(v)) + '</em></td>' for v in row) + '</tr>'
                   for name, row in values.items())
    # These rows have different units and must never be confused with profits.
    body += '<tr><th>영업이익률</th>' + '<td>13.5</td>' * width + '</tr>'
    body += '<tr><th>EPS(원)</th>' + '<td>9,999</td>' * width + '</tr>'
    body += '<tr><th>BPS(원)</th>' + '<td>55,555</td>' * width + '</tr>'
    rowspan = 3 if third_header else 2
    return f'''<!doctype html><html><head><meta charset="utf-8"></head><body>
    <table class="other"><tr><td>시세 정보</td></tr></table>
    <div class="section cop_analysis"><h4>기업실적분석</h4><p>{unit_note}</p>
    <table class="tb_type1 tb_num tb_type1_ifrs"><caption>주요재무정보</caption><thead>
    <tr><th rowspan="{rowspan}">주요재무정보</th>{groups}</tr>
    <tr>{headings}</tr>{basis_row}</thead><tbody>{body}</tbody></table></div></body></html>'''


def parse(html):
    return parse_naver_financials(html, "005930", asof=TODAY)


def test_grouped_headers_select_same_actual_quarter_and_scale_earnings_not_eps():
    values = {
        "매출액": ["1"] * 7 + ["1,234.5", "9,999", "8,888"],
        "영업이익": ["2"] * 7 + ["-25", "777", "666"],
        "당기순이익": ["3"] * 7 + ["0", "555", "444"],
    }
    facts = parse(earnings_html(values=values))
    assert [f["name"] for f in facts] == ["매출액", "영업이익", "순이익"]
    assert [f["value"] for f in facts] == [123_450_000_000, -2_500_000_000, 0]
    assert {f["period"] for f in facts} == {"2026-06-30"}
    assert {f["period_type"] for f in facts} == {"quarterly"}
    assert all(f["basis_verified"] and "IFRS연결" in f["basis"] for f in facts)
    assert all(f["source"] == "네이버증권 기업실적분석" and f["unit"] == "KRW" for f in facts)
    assert all(f["data_origin"] == "aggregated" and not f["official_source"] and not f["is_estimate"] for f in facts)
    assert all(f["link"] == "https://finance.naver.com/item/main.naver?code=005930" for f in facts)
    assert all(datetime.fromisoformat(f["fetched_at"]).tzinfo is not None for f in facts)


def test_latest_completed_actual_quarter_wins_even_if_future_column_is_not_marked_estimate():
    html = earnings_html(quarters=["2025.09", "2025.12", "2026.03", "2026.06", "2026.09", "2026.12"])
    assert {f["period"] for f in parse(html)} == {"2026-09-30"}


def test_basis_comes_from_selected_column_and_not_annual_header():
    bases = ["IFRS연결"] * 4 + ["IFRS별도"] * 6
    facts = parse(earnings_html(basis=bases))
    assert all(f["basis_verified"] and "IFRS별도" in f["basis"] for f in facts)


def test_absent_basis_header_is_explicitly_unverified():
    facts = parse(earnings_html(third_header=False))
    assert all(f["basis"] == "분기 실적 · 네이버증권 표기 기준(연결/별도 미확인)" for f in facts)
    assert all(not f["basis_verified"] for f in facts)


@pytest.mark.parametrize("basis",["IFRS연결","IFRS별도"])
def test_real_two_row_header_combines_period_and_ifrs_marker_in_same_cell(basis):
    facts = parse(earnings_html(basis=basis, third_header=False, combined_basis=True))
    assert {f["period"] for f in facts} == {"2026-06-30"}
    assert all(f["basis_verified"] and basis in f["basis"] for f in facts)


def test_separate_basis_header_estimate_marker_excludes_that_column():
    quarters = QUARTERS[:-2] + ["2026.09", "2026.12(E)"]
    bases = ["IFRS연결"] * 10
    bases[8] = "IFRS연결 (E)"
    facts = parse(earnings_html(quarters=quarters, basis=bases))
    assert {f["period"] for f in facts} == {"2026-06-30"}


def test_unknown_stray_text_in_combined_header_is_rejected():
    html = earnings_html(third_header=False, combined_basis=True)
    with pytest.raises(ValueError, match="알 수 없는"):
        parse(html.replace("2026.06<br>", "2026.06추가정보<br>"))


def test_known_table_schema_documents_scale_when_visible_unit_note_is_absent():
    facts = parse(earnings_html(unit_note=""))
    assert facts[0]["value"] == 100_000_000_000
    assert all("tb_type1_ifrs" in f["unit_basis"] for f in facts)


@pytest.mark.parametrize("note", ["단위:백만원", "단위 : 천원", "단위:원", "단위:조원", "단위:백억원", "단위:백만달러"])
def test_contradictory_monetary_units_are_rejected(note):
    with pytest.raises(ValueError, match="억원 단위"):
        parse(earnings_html(unit_note=note))


def test_explicit_unsupported_unit_is_rejected_even_without_visible_section_title():
    html = earnings_html(unit_note="단위:조원").replace("<h4>기업실적분석</h4>", "")
    with pytest.raises(ValueError, match="억원 단위"):
        parse(html)


def test_row_specific_units_supported_and_conflicting_row_unit_rejected():
    html = earnings_html(unit_note="").replace("<div>매출액</div>", "<div>매출액(억원)</div>")
    assert parse(html)[0]["value"] == 100_000_000_000
    with pytest.raises(ValueError, match="억원 단위"):
        parse(html.replace("매출액(억원)", "매출액(백만원)"))


def test_missing_latest_account_uses_one_older_complete_quarter_without_mixing():
    values = {"매출액": ["1"] * 10, "영업이익": ["2"] * 10, "당기순이익": ["3"] * 10}
    values["당기순이익"][8] = "-"
    html = earnings_html(quarters=QUARTERS[:-2] + ["2026.09", "2026.12(E)"], values=values)
    facts = parse(html)
    assert {f["period"] for f in facts} == {"2026-06-30"}
    assert [f["value"] for f in facts] == [100_000_000, 200_000_000, 300_000_000]


def test_recent_annual_actual_is_fallback_when_no_actual_quarter_available():
    html = earnings_html(annual=["2024.12", "2025.12", "2026.12(E)"], quarters=["2026.03(E)"])
    facts = parse_naver_financials(html, "005930", asof=date(2026, 5, 1))
    assert {f["period"] for f in facts} == {"2025-12-31"}
    assert {f["period_type"] for f in facts} == {"annual"}


@pytest.mark.parametrize("value", ["-", "", "NaN", "inf", "12,34", "N/A"])
def test_no_valid_complete_period_rejects_missing_or_nonfinite_values(value):
    values = {"매출액": ["1"], "영업이익": ["2"], "당기순이익": [value]}
    html = earnings_html(annual=[], quarters=["2026.09"], values=values)
    with pytest.raises(ValueError, match="같은 기간"):
        parse(html)


@pytest.mark.parametrize("dates", [["2026.09(E)"], ["2026.12"], ["2024.09"]])
def test_estimate_future_or_stale_only_table_has_no_valid_period(dates):
    with pytest.raises(ValueError, match="같은 기간"):
        parse(earnings_html(annual=[], quarters=dates))


@pytest.mark.parametrize("mutation", [
    lambda h: h.replace('colspan="6"', 'colspan="7"'),
    lambda h: h.replace('rowspan="3"', 'rowspan="2"'),
    lambda h: h.replace('colspan="6"', 'colspan="bad"'),
    lambda h: h.replace("최근 분기 실적", "실적"),
    lambda h: h.replace("2026.06", "2026.05"),
    lambda h: h.replace("당기순이익", "지배주주순이익"),
    lambda h: h.replace("<td><em>1000</em></td>", "", 1),
])
def test_ambiguous_headers_or_mismatched_account_shape_fail_closed(mutation):
    with pytest.raises(ValueError):
        parse(mutation(earnings_html()))


def test_multiple_matching_tables_are_ambiguous():
    with pytest.raises(ValueError, match="여러 개"):
        parse(earnings_html() + earnings_html())


def test_nonfinancial_page_is_not_treated_as_financials():
    with pytest.raises(ValueError, match="표가 없거나"):
        parse("<html><body>접근을 확인하세요.</body></html>")


def test_fetch_uses_injected_session_and_decodes_euc_kr_without_live_network(monkeypatch):
    # Keep recency deterministic while exercising the public fetch adapter.
    original = naver_financials.parse_naver_financials
    monkeypatch.setattr(naver_financials, "parse_naver_financials", lambda html, symbol: original(html, symbol, asof=TODAY))
    content = earnings_html().replace('charset="utf-8"', 'charset="euc-kr"').encode("euc-kr")

    class Response:
        def raise_for_status(self):
            pass

    response = Response()
    response.content = content
    calls = []

    class Session:
        def get(self, url, **kwargs):
            calls.append((url, kwargs))
            return response

    facts = fetch_naver_financials("005930", session=Session())
    assert facts[0]["period"] == "2026-06-30"
    assert calls[0][0] == "https://finance.naver.com/item/main.naver"
    assert calls[0][1]["params"] == {"code": "005930"}
    assert calls[0][1]["timeout"] == 10


def test_invalid_symbol_is_rejected_before_network():
    class Session:
        def get(self, *args, **kwargs):
            raise AssertionError("invalid symbols must not trigger requests")

    with pytest.raises(ValueError, match="6자리"):
        fetch_naver_financials("AAPL", session=Session())
