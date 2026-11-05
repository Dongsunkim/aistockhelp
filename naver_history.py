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
    # FCHART declares EUC-KR. Expat cannot parse multibyte legacy encodings
    # directly, so decode according to the XML declaration before parsing.
    if isinstance(content, bytes):
        declaration=re.match(br'''\s*<\?xml\b[^>]*\bencoding\s*=\s*["']([^"']+)["']''',
                             content[:512],re.IGNORECASE)
        if declaration:
            try:
                content=content.decode(declaration.group(1).decode('ascii'))
            except (LookupError,UnicodeError):
                raise ValueError('네이버 XML 문자 인코딩을 해석할 수 없습니다.') from None
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
        # The mobile endpoint rejects oversized pages. Keep its small page size
        # while requesting enough pages for the same history as FCHART.
        page_size=20
        target_count=330 if history=="1y" else 1000
        page_count=(target_count+page_size-1)//page_size
        for page in range(1,page_count+1):
            r=s.get(f"https://m.stock.naver.com/api/stock/{symbol}/price",
                    params={"page":page,"pageSize":page_size},headers=HEADERS,timeout=8)
            r.raise_for_status();chunk=mobile_rows(r.json())
            if not chunk:break
            if len(chunk)>page_size:raise ValueError("모바일 응답 페이지 크기 오류")
            allrows.extend(chunk[:target_count-len(allrows)])
            if len(chunk)<page_size:break
        df=validated(allrows,history)
        return df,meta(df,symbol,market,"NAVER_MOBILE")
    except (requests.RequestException,ValueError,TypeError,KeyError) as e:
        errors.append("MOBILE:"+str(e)[:70])
        raise ValueError("네이버 일봉 검증 실패. 국내 Yahoo 대체 조회 금지. "+"; ".join(errors))


def fetch_naver_quote(symbol,session=None):
    """Unverified-timeliness Naver snapshot; never label it exchange-certified live."""
    if not re.fullmatch(r"\d{6}",symbol):
        raise ValueError("국내 6자리 종목코드 필요")
    s=session or requests.Session()
    r=s.get(f"https://polling.finance.naver.com/api/realtime/domestic/stock/{symbol}",
            headers=HEADERS,timeout=5)
    r.raise_for_status()
    obj=r.json()
    data=obj.get("datas") if isinstance(obj,dict) else None
    if not isinstance(data,list) or not data or not isinstance(data[0],dict):
        raise ValueError("네이버 현재가 응답 구조 확인 실패")
    q=data[0]
    returned=q.get("itemCode") or q.get("code")
    if returned and returned!=symbol:
        raise ValueError("요청 종목과 응답 종목이 다릅니다.")
    price=number(q.get("closePrice"))
    if price<=0:raise ValueError("비정상 현재가")
    return {"provider":"네이버증권 비공식 시세","price":price,
            "fetched_at":datetime.now(timezone.utc).isoformat(),
            "exchange_timestamp":q.get("localTradedAt"),
            "verified_live":False,
            "note":"실시간 체결 인증이 없는 네이버 참고 시세입니다."}
