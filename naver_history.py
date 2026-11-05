"""Validated Naver Finance domestic daily bars. No Yahoo fallback for Korean stocks."""
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
import re, math
import xml.etree.ElementTree as ET
import requests, pandas as pd
KST=ZoneInfo("Asia/Seoul")
HEADERS={"User-Agent":"Mozilla/5.0 (StockDesk personal research)","Referer":"https://m.stock.naver.com/"}

def number(v):
    f=float(str(v).replace(",","").strip())
    if not math.isfinite(f): raise ValueError("시세 숫자가 비정상입니다.")
    return f

def xml_rows(content):
    root=ET.fromstring(content)
    rows=[]
    for i in root.findall(".//item"):
        x=i.get("data","").split("|")
        if len(x)!=6: continue
        date,o,h,l,c,v=x
        rows.append(dict(date=datetime.strptime(date,"%Y%m%d").date().isoformat(),
                         open=number(o),high=number(h),low=number(l),close=number(c),volume=number(v)))
    return rows

def mobile_rows(content):
    if not isinstance(content,list): raise ValueError("모바일 응답 형식 오류")
    rows=[]
    for x in content:
        rows.append(dict(date=datetime.strptime(x["localTradedAt"][:10],"%Y-%m-%d").date().isoformat(),
                         open=number(x["openPrice"]),high=number(x["highPrice"]),
                         low=number(x["lowPrice"]),close=number(x["closePrice"]),
                         volume=number(x["accumulatedTradingVolume"])))
    return rows

def validated(rows,history,now=None):
    now=now or datetime.now(KST)
    seen=set();valid=[]
    for r in sorted(rows,key=lambda v:v["date"]):
        day=datetime.strptime(r["date"],"%Y-%m-%d").date()
        if day>now.date() or (day==now.date() and (now.hour,now.minute)<(16,0)):continue
        if r["date"] in seen:raise ValueError("중복 거래일")
        seen.add(r["date"])
        o,h,l,c,v=[r[k] for k in ("open","high","low","close","volume")]
        if min(o,h,l,c)<=0 or v<0 or h<max(o,c) or l>min(o,c) or h<l:
            raise ValueError("OHLCV 논리 오류")
        valid.append(r)
    required=150 if history=="1y" else 650
    if len(valid)<required:raise ValueError(f"확정 일봉 부족({len(valid)}/{required})")
    age=(now.date()-datetime.strptime(valid[-1]["date"],"%Y-%m-%d").date()).days
    if not 0<=age<=7:raise ValueError(f"확정 일봉 {age}일 경과")
    return pd.DataFrame(valid).reset_index(drop=True)

def meta(df,symbol,market,endpoint):
    return dict(provider="네이버증권 비공식 일봉",source="NAVER",endpoint=endpoint,
                symbol=symbol,market=market,market_timezone="Asia/Seoul",
                fetched_at=datetime.now(timezone.utc).isoformat(),
                completed_daily_bar=str(df.iloc[-1]["date"]),quote_currency="KRW",
                adjustment_verified=False,
                disclaimer="네이버 비공식 확정 종가. 수정주가 및 기업행사 반영 여부 미검증.")

def fetch_naver_daily(symbol,market,*,history="1y",session=None):
    if market not in ("KOSPI","KOSDAQ") or not re.fullmatch(r"\d{6}",symbol):
        raise ValueError("국내 6자리 종목코드와 시장을 확인하세요.")
    if history not in ("1y","3y"):raise ValueError("조회 기간 오류")
    s=session or requests.Session();errors=[]
    try:
        r=s.get("https://fchart.stock.naver.com/sise.nhn",params={
            "symbol":symbol,"timeframe":"day","count":330 if history=="1y" else 1000,
            "requestType":0},headers=HEADERS,timeout=8)
        r.raise_for_status()
        df=validated(xml_rows(r.content),history)
        return df,meta(df,symbol,market,"NAVER_FCHART")
    except (requests.RequestException,ValueError,KeyError,ET.ParseError) as e:
        errors.append("FCHART:"+str(e)[:70])
    try:
        allrows=[]
        for page in range(1,5 if history=="1y" else 12):
            r=s.get(f"https://m.stock.naver.com/api/stock/{symbol}/price",
                    params={"page":page,"pageSize":100},headers=HEADERS,timeout=8)
            r.raise_for_status();chunk=mobile_rows(r.json())
            if not chunk:break
            allrows.extend(chunk)
            if len(chunk)<100:break
        df=validated(allrows,history)
        return df,meta(df,symbol,market,"NAVER_MOBILE")
    except (requests.RequestException,ValueError,TypeError,KeyError) as e:
        errors.append("MOBILE:"+str(e)[:70])
        raise ValueError("네이버 일봉 검증 실패. 국내 Yahoo 대체 조회 금지. "+"; ".join(errors))
