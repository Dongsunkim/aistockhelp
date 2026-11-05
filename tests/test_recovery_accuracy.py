"""Offline price-source correctness and separate technical/trade verification."""
from datetime import datetime,timedelta
from zoneinfo import ZoneInfo
import pandas as pd
import pytest

from naver_history import validated,xml_rows,mobile_rows,fetch_naver_daily
from data_quality import check

def rows(n=160,begin=None):
    begin=begin or (datetime.now(ZoneInfo("Asia/Seoul")).date()-timedelta(days=n))
    return [dict(date=str(begin+timedelta(days=i)),open=100,high=110,
                 low=90,close=105,volume=1000) for i in range(n)]

def test_parse_naver_xml():
    data=b'<protocol><chartdata><item data="20261102|100|120|90|110|1000"/></chartdata></protocol>'
    assert xml_rows(data)[0]["close"]==110

def test_parse_naver_mobile():
    data=[dict(localTradedAt="2026-11-02",openPrice="100",highPrice="120",
               lowPrice="90",closePrice="110",accumulatedTradingVolume="1,200")]
    assert mobile_rows(data)[0]["volume"]==1200

def test_reject_bad_ohlc():
    x=rows()
    x[-1]["high"]=80
    with pytest.raises(ValueError,match="OHLCV"):
        validated(x,"1y")

def test_reject_stale_daily():
    x=rows(begin=datetime(2024,1,1).date())
    with pytest.raises(ValueError,match="경과"):
        validated(x,"1y")

def test_reject_duplicate_day():
    x=rows();x.append(dict(x[-1]))
    with pytest.raises(ValueError,match="중복"):
        validated(x,"1y")

def test_reject_invalid_market_before_network():
    with pytest.raises(ValueError,match="6자리"):
        fetch_naver_daily("AAPL","KOSPI")

def test_domestic_fetch_never_falls_back_to_yahoo(monkeypatch):
    import sources
    def fail(*a,**kw):
        raise ValueError("NAVER_NOT_AVAILABLE")
    import naver_history
    monkeypatch.setattr(naver_history,"fetch_naver_daily",fail)
    with pytest.raises(ValueError,match="NAVER_NOT_AVAILABLE"):
        sources.fetch_daily("005930","KOSPI")

def quality_inputs(market="KOSPI"):
    bars=pd.DataFrame(rows())
    analysis={"levels":{"entry_low":98,"entry_high":102,"stop":94,"target_resistance":122,"rr":2.5}}
    meta={"source":"YAHOO_US_ONLY" if market=="US" else "NAVER","adjustment_verified":False}
    period=str(datetime.now(ZoneInfo("Asia/Seoul")).date()-timedelta(days=30))
    facts=[{"name":k,"value":10,"unit":"USD" if market=="US" else "KRW",
            "period":period,"basis":"연결",
            "source":"SEC XBRL companyfacts" if market=="US" else "OpenDART 주요 재무계정"}
           for k in ("매출액","영업이익","순이익")]
    return meta,bars,analysis,facts


@pytest.mark.parametrize("market",["KOSPI","KOSDAQ","US"])
def test_technical_prices_remain_visible_without_financials(market):
    meta,bars,analysis,_=quality_inputs(market)
    q=check(market,meta,bars,analysis,[],"재무계정 확인 불가: API 미연결")
    assert q["price_levels_eligible"]
    assert q["price_level_blockers"]==[]
    assert not q["verified_for_trade"]
    assert not q["financials_eligible"]
    assert q["financial_blockers"]
    assert any("재무" in w for w in q["warnings"])
    assert "재무계정 확인 불가: API 미연결" in q["warnings"]
    if market!="US":
        assert any("수정주가" in w for w in q["warnings"])


@pytest.mark.parametrize("market",["KOSPI","US"])
@pytest.mark.parametrize("field,value,warning",[
    ("period","2020-01-01","기간·연결/별도"),
    ("basis","별도","기간·연결/별도"),
    ("unit","EUR","통화"),
])
def test_financial_mismatches_warn_without_hiding_technical_prices(market,field,value,warning):
    meta,bars,analysis,facts=quality_inputs(market)
    facts[-1][field]=value
    q=check(market,meta,bars,analysis,facts)
    assert q["price_levels_eligible"]
    assert not q["price_level_blockers"]
    assert not q["verified_for_trade"]
    assert not q["financials_eligible"]
    assert q["financial_blockers"]
    assert any(warning in w for w in q["warnings"])


@pytest.mark.parametrize("period",["2020-01-01",None,"not a date","future"])
def test_unclear_or_stale_financial_period_does_not_hide_technical_prices(period):
    meta,bars,analysis,facts=quality_inputs()
    if period=="future":
        period=str(datetime.now(ZoneInfo("Asia/Seoul")).date()+timedelta(days=1))
    for fact in facts:fact["period"]=period
    q=check("KOSPI",meta,bars,analysis,facts)
    assert q["price_levels_eligible"]
    assert not q["verified_for_trade"]
    assert not q["financials_eligible"]
    assert any("재무 회계기간" in w for w in q["warnings"])


@pytest.mark.parametrize("market,source",[
    ("KOSPI",None),("KOSPI","YAHOO_US_ONLY"),("KOSDAQ","UNKNOWN"),
    ("US",None),("US","NAVER"),("US","UNKNOWN"),
])
def test_unrecognized_price_source_blocks_display(market,source):
    meta,bars,analysis,facts=quality_inputs(market)
    if source is None:meta.pop("source")
    else:meta["source"]=source
    q=check(market,meta,bars,analysis,facts)
    assert not q["price_levels_eligible"]
    assert not q["verified_for_trade"]
    assert any("출처" in w for w in q["price_level_blockers"])
    assert all(w in q["warnings"] for w in q["price_level_blockers"])


@pytest.mark.parametrize("last_date",["2020-01-01","not a date",None,"future"])
def test_invalid_daily_bar_date_blocks_display(last_date):
    meta,bars,analysis,facts=quality_inputs()
    if last_date=="future":
        last_date=str(datetime.now(ZoneInfo("Asia/Seoul")).date()+timedelta(days=1))
    bars.loc[bars.index[-1],"date"]=last_date
    q=check("KOSPI",meta,bars,analysis,facts)
    assert not q["price_levels_eligible"]
    assert any("종가" in w for w in q["price_level_blockers"])


def test_insufficient_price_history_blocks_display():
    meta,bars,analysis,facts=quality_inputs()
    q=check("KOSPI",meta,bars.tail(149),analysis,facts)
    assert not q["price_levels_eligible"]
    assert "가격 관측 수 부족" in q["price_level_blockers"]


@pytest.mark.parametrize("field,value",[
    ("stop",105),("stop",0),("entry_low",103),("target_resistance",101),
    ("entry_low",None),("entry_high",float("nan")),("target_resistance",float("inf")),
    ("rr",float("nan")),("rr",-1),
])
def test_invalid_price_levels_block_display(field,value):
    meta,bars,analysis,facts=quality_inputs()
    analysis["levels"][field]=value
    q=check("KOSPI",meta,bars,analysis,facts)
    assert not q["price_levels_eligible"]
    assert q["price_level_blockers"]


@pytest.mark.parametrize("missing_key",["entry_low","stop","rr"])
def test_missing_price_level_fields_block_display(missing_key):
    meta,bars,analysis,facts=quality_inputs()
    del analysis["levels"][missing_key]
    q=check("KOSPI",meta,bars,analysis,facts)
    assert not q["price_levels_eligible"]
    assert any("산식" in w for w in q["price_level_blockers"])


@pytest.mark.parametrize("market",["KOSPI","US"])
def test_valid_technical_and_financial_data_never_claims_trade_verification(market):
    meta,bars,analysis,facts=quality_inputs(market)
    meta["adjustment_verified"]=True
    q=check(market,meta,bars,analysis,facts)
    assert q["price_levels_eligible"]
    assert q["financials_eligible"]
    assert q["financial_blockers"]==[]
    assert not q["verified_for_trade"]
    assert q["price_level_blockers"]==[]
    assert any("가치평가" in w for w in q["warnings"])


@pytest.mark.parametrize("value",[None,float("nan"),float("inf"),"10",True])
def test_unavailable_or_nonnumeric_financial_value_does_not_pass_audit(value):
    meta,bars,analysis,facts=quality_inputs()
    facts[-1]["value"]=value
    q=check("KOSPI",meta,bars,analysis,facts)
    assert not q["financials_eligible"]
    assert any("숫자" in b for b in q["financial_blockers"])
    assert q["price_levels_eligible"]


@pytest.mark.parametrize("market,source",[
    ("KOSPI",None),("KOSPI","SEC XBRL companyfacts"),
    ("US","OpenDART 주요 재무계정"),("US","unofficial SEC copy"),
])
def test_financial_source_must_match_supported_market_provider(market,source):
    meta,bars,analysis,facts=quality_inputs(market)
    facts[-1]["source"]=source
    q=check(market,meta,bars,analysis,facts)
    assert not q["financials_eligible"]
    assert any("출처" in b for b in q["financial_blockers"])
    assert q["price_levels_eligible"]


@pytest.mark.parametrize("field,value",[("period",None),("period"," "),("basis",None),("basis","")])
def test_financial_period_and_basis_cannot_all_be_missing(field,value):
    meta,bars,analysis,facts=quality_inputs()
    for fact in facts:fact[field]=value
    q=check("KOSPI",meta,bars,analysis,facts)
    assert not q["financials_eligible"]
    assert any("기준 불일치 또는 누락" in b for b in q["financial_blockers"])
    assert q["price_levels_eligible"]


def test_valid_zero_or_negative_financial_values_are_available_but_not_trade_verification():
    meta,bars,analysis,facts=quality_inputs()
    facts[1]["value"]=0
    facts[2]["value"]=-10
    q=check("KOSPI",meta,bars,analysis,facts)
    assert q["financials_eligible"]
    assert not q["verified_for_trade"]


def test_financial_fetch_error_blocks_financial_eligibility_even_with_returned_facts():
    meta,bars,analysis,facts=quality_inputs()
    q=check("KOSPI",meta,bars,analysis,facts,"공식 API 일부 조회 실패")
    assert not q["financials_eligible"]
    assert "공식 API 일부 조회 실패" in q["financial_blockers"]
    assert q["price_levels_eligible"]


def test_duplicate_financial_accounts_are_not_silently_overwritten():
    meta,bars,analysis,facts=quality_inputs()
    facts.append(dict(facts[-1],value=999))
    q=check("KOSPI",meta,bars,analysis,facts)
    assert not q["financials_eligible"]
    assert any("중복" in b for b in q["financial_blockers"])


def test_financial_period_range_must_have_ordered_dates():
    meta,bars,analysis,facts=quality_inputs("US")
    today=datetime.now(ZoneInfo("Asia/Seoul")).date()
    for fact in facts:fact["period"]=f"{today} ~ {today-timedelta(days=30)}"
    q=check("US",meta,bars,analysis,facts)
    assert not q["financials_eligible"]
    assert any("회계기간" in b for b in q["financial_blockers"])


@pytest.mark.parametrize("market",["KOSPI","US"])
def test_current_provider_period_formats_pass_financial_audit(market):
    meta,bars,analysis,facts=quality_inputs(market)
    today=datetime.now(ZoneInfo("Asia/Seoul")).date()
    if market=="US":
        period=f"{today-timedelta(days=120)} ~ {today-timedelta(days=30)}"
    elif today.month>=10:period=f"{today.year}년 3분기"
    elif today.month>=7:period=f"{today.year}년 반기"
    elif today.month>=4:period=f"{today.year}년 1분기"
    else:period=f"{today.year-1}년 사업보고서"
    for fact in facts:fact["period"]=period
    q=check(market,meta,bars,analysis,facts)
    assert q["financials_eligible"]
    assert not q["financial_blockers"]
    assert not q["verified_for_trade"]

def test_accessibility_font_and_audit_display():
    from pathlib import Path
    root=Path(__file__).resolve().parents[1]
    assert "font-size:18px!important" in (root/"static/style.css").read_text()
    assert 'id="quality-warnings"' in (root/"static/index.html").read_text()
    assert "data_quality" in (root/"static/app.js").read_text()
