"""Validated Naver Finance domestic daily bars. No Yahoo fallback for Korean stocks."""
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import datetime, timezone
from threading import Lock, local
from zoneinfo import ZoneInfo
import re, math, time
import xml.etree.ElementTree as ET
import requests, pandas as pd
KST=ZoneInfo("Asia/Seoul")
HEADERS={"User-Agent":"Mozilla/5.0 (StockDesk personal research)","Referer":"https://m.stock.naver.com/"}
DAILY_CACHE_SECONDS=30
DAILY_CACHE_SIZE=32
MOBILE_WORKERS=4
_daily_cache=OrderedDict()
_daily_cache_lock=Lock()

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

def _cached_daily(key,history):
    with _daily_cache_lock:
        cached=_daily_cache.get(key)
        if cached is None:return None
        age=time.monotonic()-cached["stored_at"]
        if not 0<=age<DAILY_CACHE_SECONDS:
            del _daily_cache[key]
            return None
        df=cached["data"].copy(deep=True)
        metadata=deepcopy(cached["metadata"])
        _daily_cache.move_to_end(key)
    # A short cache can still cross a date/freshness boundary. Recheck the
    # completed-bar rules rather than treating the retrieval time as market time.
    try:
        df=validated(df.to_dict("records"),history)
    except (ValueError,KeyError,TypeError):
        with _daily_cache_lock:
            if _daily_cache.get(key) is cached:del _daily_cache[key]
        return None
    metadata["cached_at"]=metadata["fetched_at"]
    metadata["cache_age_seconds"]=round(age,3)
    return df,metadata

def _store_daily(key,result):
    df,metadata=result
    with _daily_cache_lock:
        _daily_cache[key]={"stored_at":time.monotonic(),"data":df.copy(deep=True),
                           "metadata":deepcopy(metadata)}
        _daily_cache.move_to_end(key)
        while len(_daily_cache)>DAILY_CACHE_SIZE:_daily_cache.popitem(last=False)

def _mobile_page(client,symbol,page):
    response=client.get(f"https://m.stock.naver.com/api/stock/{symbol}/price",
                        params={"page":page,"pageSize":20},headers=HEADERS,timeout=8)
    response.raise_for_status()
    chunk=mobile_rows(response.json())
    if len(chunk)>20:raise ValueError("모바일 응답 페이지 크기 오류")
    return chunk

def _mobile_history(client,symbol,history,*,parallel):
    # Page 1 establishes that the endpoint works before starting bounded batches.
    # Production workers retain their own Session (and connection pool). An
    # explicitly injected session retains deterministic serial request behavior.
    target_count=330 if history=="1y" else 1000
    page_count=(target_count+19)//20
    rows=_mobile_page(client,symbol,1)
    if len(rows)<20:return rows
    if not parallel:
        for page in range(2,page_count+1):
            chunk=_mobile_page(client,symbol,page)
            rows.extend(chunk[:target_count-len(rows)])
            if len(chunk)<20:break
        return rows
    worker=local()
    clients=[]
    clients_lock=Lock()
    def page_rows(page):
        if not hasattr(worker,"client"):
            worker.client=requests.Session()
            with clients_lock:clients.append(worker.client)
        return _mobile_page(worker.client,symbol,page)
    try:
        with ThreadPoolExecutor(max_workers=MOBILE_WORKERS,thread_name_prefix="stockdesk-naver") as pool:
            for first in range(2,page_count+1,MOBILE_WORKERS):
                jobs=[pool.submit(page_rows,page)
                      for page in range(first,min(first+MOBILE_WORKERS,page_count+1))]
                # Consume in page order, irrespective of completion order. A
                # short page ends the history; later speculative pages are not
                # included or mistaken for a continuation of that history.
                for job in jobs:
                    chunk=job.result()
                    rows.extend(chunk[:target_count-len(rows)])
                    if len(chunk)<20:return rows
        return rows
    finally:
        for owned in clients:owned.close()

def fetch_naver_daily(symbol,market,*,history="1y",session=None):
    """Return validated bars; default HTTP sessions use a 30-second copy cache."""
    if market not in ("KOSPI","KOSDAQ") or not re.fullmatch(r"\d{6}",symbol):
        raise ValueError("국내 6자리 종목코드와 시장을 확인하세요.")
    if history not in ("1y","3y"):raise ValueError("조회 기간 오류")
    key=(market,symbol,history)
    if session is None:
        cached=_cached_daily(key,history)
        if cached is not None:return cached
    s=session if session is not None else requests.Session()
    try:
        result=_fetch_naver_daily(s,symbol,market,history,parallel=session is None)
        if session is None:_store_daily(key,result)
        return result
    finally:
        if session is None:s.close()

def _fetch_naver_daily(s,symbol,market,history,*,parallel):
    errors=[]
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
        # The mobile endpoint rejects oversized pages. Keep its small page size
        # while requesting enough pages for the same history as FCHART.
        allrows=_mobile_history(s,symbol,history,parallel=parallel)
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
