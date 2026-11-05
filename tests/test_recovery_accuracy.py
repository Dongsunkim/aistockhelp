"""Offline price-source correctness and fail-closed valuation protection."""
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

def test_quality_gate_fails_without_financials():
    bars=pd.DataFrame(rows())
    analysis={"levels":{"entry_low":98,"entry_high":102,"stop":94,"target_resistance":122}}
    m={"source":"NAVER","completed_daily_bar":str(bars.iloc[-1]["date"]),"adjustment_verified":False}
    q=check("KOSPI",m,bars,analysis,[])
    assert not q["verified_for_trade"]
    assert not q["price_levels_eligible"]
    assert any("재무" in w for w in q["warnings"])

def test_quality_gate_rejects_inconsistent_levels():
    bars=pd.DataFrame(rows())
    m={"source":"NAVER","adjustment_verified":True}
    analysis={"levels":{"entry_low":98,"entry_high":102,"stop":105,"target_resistance":122}}
    facts=[{"name":k,"value":10,"unit":"KRW","period":"2026년 3분기","basis":"연결"} for k in ("매출액","영업이익","순이익")]
    q=check("KOSPI",m,bars,analysis,facts)
    assert not q["price_levels_eligible"]

def test_accessibility_font_and_audit_display():
    from pathlib import Path
    root=Path(__file__).resolve().parents[1]
    assert "font-size:18px!important" in (root/"static/style.css").read_text()
    assert 'id="quality-warnings"' in (root/"static/index.html").read_text()
    assert "data_quality" in (root/"static/app.js").read_text()
