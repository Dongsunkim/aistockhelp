"""Public-source adapters. No market orders. All credentials stay on the server."""
from __future__ import annotations
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from urllib.parse import urlencode
import os, re, io, zipfile, json, threading
import requests
import pandas as pd
import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime

BASE=os.path.dirname(__file__)
_SESSION=requests.Session()
_SESSION.headers.update({'User-Agent':'Mozilla/5.0 (personal educational stock signal app)'})
_registry_lock=threading.Lock()
_registry_cache={}


def yahoo_symbol(symbol: str, market: str) -> str:
    symbol=symbol.strip().upper()
    if market not in ('US','KOSPI','KOSDAQ'):
        raise ValueError('시장 선택은 US, KOSPI, KOSDAQ 중 하나여야 합니다.')
    if market=='US':
        if not re.fullmatch(r'[A-Z][A-Z0-9.\-]{0,11}', symbol):
            raise ValueError('미국 종목코드는 예: AAPL, MSFT')
        return symbol
    if not re.fullmatch(r'\d{6}',symbol):
        raise ValueError('한국 주식 종목코드는 6자리 숫자입니다. 예: 005930')
    return f'{symbol}.KS' if market=='KOSPI' else f'{symbol}.KQ'


def fetch_daily(symbol: str, market: str, *, history: str = "1y") -> tuple[pd.DataFrame,dict]:
    if history not in ("1y","3y"): raise ValueError("조회 기간은 1y 또는 3y입니다.")
    if market in ('KOSPI','KOSDAQ'):
        from naver_history import fetch_naver_daily
        return fetch_naver_daily(symbol,market,history=history)
    ticker=yahoo_symbol(symbol,market)
    url=f'https://query1.finance.yahoo.com/v8/finance/chart/{ticker}'
    response=_SESSION.get(url,params={'range':history,'interval':'1d','events':'div,splits'},timeout=12)
    response.raise_for_status()
    root=response.json().get('chart',{})
    if root.get('error'):
        raise ValueError(str(root['error']))
    raw=(root.get('result') or [None])[0]
    if not raw or not raw.get('timestamp'):
        raise ValueError('시세를 조회할 수 없습니다. 종목코드와 시장 구분을 확인하세요.')
    ts=raw['timestamp']
    quote=raw['indicators']['quote'][0]
    exchange_tz=ZoneInfo('America/New_York' if market=='US' else 'Asia/Seoul')
    local_now=datetime.now(exchange_tz)
    # Use only completed local daily bars. Deliberately exclude the current bar
    # until >30 minutes after the regular session close.
    cutoff=(16,30) if market=='US' else (16,0)
    last_today_allowed=(local_now.hour,local_now.minute)>=cutoff
    rows=[]
    for i,t in enumerate(ts):
        dt=datetime.fromtimestamp(t,exchange_tz)
        if dt.date()>local_now.date() or (dt.date()==local_now.date() and not last_today_allowed):
            continue
        try:
            row={'date':dt.date().isoformat(), 'timestamp':int(t),
                 'open':float(quote['open'][i]),'close':float(quote['close'][i]),'high':float(quote['high'][i]),
                 'low':float(quote['low'][i]),'volume':float(quote['volume'][i] or 0)}
            if all(pd.notna(row[k]) for k in ('open','close','high','low')):
                rows.append(row)
        except (TypeError,ValueError,KeyError,IndexError):
            continue
    if not rows:
        raise ValueError('확정된 일봉 데이터가 없습니다.')
    df=pd.DataFrame(rows).drop_duplicates('date').sort_values('date')
    meta={'provider':'Yahoo Finance 비공식 차트 API (미국만 사용, 지연 가능)',
          'symbol':ticker,'source':'YAHOO_US_ONLY','market_timezone':str(exchange_tz),
          'fetched_at':datetime.now(timezone.utc).isoformat(),
          'completed_daily_bar':df.iloc[-1]['date'],
          'quote_currency':'KRW' if market != 'US' else 'USD',
          'disclaimer':'틱 단위 실시간 시세가 아닙니다. 미확정 장중 일봉은 배제하며 가격과 시점은 증권사 화면에서 재확인하세요.'}
    return df,meta


def fetch_news(search_term: str, market: str, limit: int = 12) -> list:
    # Google News RSS provides metadata/headlines, NOT verified article text.
    search_term=search_term.strip()
    if not search_term or len(search_term)>80:
        return []
    lang={'US':('en-US','US','US:en'), 'KOSPI':('ko','KR','KR:ko'),'KOSDAQ':('ko','KR','KR:ko')}[market]
    query=f'{search_term} when:7d'
    url='https://news.google.com/rss/search?'+urlencode({'q':query,'hl':lang[0],'gl':lang[1],'ceid':lang[2]})
    res=_SESSION.get(url,timeout=12)
    res.raise_for_status()
    root=ET.fromstring(res.content)
    now=datetime.now(timezone.utc)
    items=[]
    seen=set()
    for item in root.findall('.//channel/item'):
        title=(item.findtext('title') or '').strip()
        link=(item.findtext('link') or '').strip()
        date=(item.findtext('pubDate') or '').strip()
        source=(item.findtext('source') or '').strip()
        if not title or not link or title in seen:
            continue
        seen.add(title)
        age=None
        try:
            at=parsedate_to_datetime(date)
            if at.tzinfo is None: at=at.replace(tzinfo=timezone.utc)
            age=max(0,round((now-at.astimezone(timezone.utc)).total_seconds()/3600,1))
            date=at.astimezone(timezone.utc).isoformat()
        except Exception:
            date=''
        items.append({'title':title,'link':link,'source':source or '뉴스 원문',
                      'published_at':date,'age_hours':age,'basis':'제목·발행시점만 수집'})
        if len(items)>=limit:
            break
    return items


def fetch_research_mentions(search_term: str, market: str, limit: int = 8) -> list:
    """Research-related press coverage; NEVER present it as read research PDFs."""
    term=search_term.strip()
    if not term or len(term)>80:return []
    if market=='US':
        query=f'"{term}" (analyst OR research) (price target OR earnings) when:14d'
        lang=('en-US','US','US:en')
    else:
        query=f'"{term}" (증권사 리포트 OR 목표주가 OR 실적 전망) when:14d'
        lang=('ko','KR','KR:ko')
    url='https://news.google.com/rss/search?'+urlencode({'q':query,'hl':lang[0],'gl':lang[1],'ceid':lang[2]})
    res=_SESSION.get(url,timeout=12)
    res.raise_for_status()
    root=ET.fromstring(res.content)
    now=datetime.now(timezone.utc)
    items=[]
    for item in root.findall('.//channel/item'):
        title=(item.findtext('title') or '').strip()
        link=(item.findtext('link') or '').strip()
        source=(item.findtext('source') or '').strip()
        date=(item.findtext('pubDate') or '').strip()
        if not title or not link:continue
        try:
            when=parsedate_to_datetime(date)
            if when.tzinfo is None:when=when.replace(tzinfo=timezone.utc)
            date=when.astimezone(timezone.utc).isoformat()
        except Exception:date=''
        items.append({'title':title,'link':link,'source':source or '연구 관련 보도',
                      'published_at':date,'basis':'증권사 보고서 관련 보도 검색결과, 실제 보고서 전문 미확인',
                      'type':'research_mention'})
        if len(items)>=limit:break
    return items


def fetch_dart_filings(symbol: str, api_key: str, limit: int = 7) -> list:
    if not api_key:
        raise ValueError('DART_API_KEY 미설정: https://opendart.fss.or.kr/ 에서 무료 API 키 발급 필요')
    if not re.fullmatch(r'\d{6}',symbol):
        raise ValueError('국내 주식코드는 6자리여야 합니다.')
    with _registry_lock:
        entry=_registry_cache.get(api_key)
        if not entry or (datetime.now(timezone.utc)-entry['at']).days>=1:
            rr=_SESSION.get('https://opendart.fss.or.kr/api/corpCode.xml',params={'crtfc_key':api_key},timeout=25)
            rr.raise_for_status()
            with zipfile.ZipFile(io.BytesIO(rr.content)) as z:
                content=z.read(z.namelist()[0])
            root=ET.fromstring(content)
            mapping={x.findtext('stock_code','').strip():x.findtext('corp_code','').strip()
                     for x in root.findall('list') if x.findtext('stock_code','').strip()}
            entry={'at':datetime.now(timezone.utc),'map':mapping}
            _registry_cache[api_key]=entry
    corp_code=entry['map'].get(symbol)
    if not corp_code:
        raise ValueError('DART 고유번호를 찾지 못했습니다.')
    today=datetime.now(ZoneInfo('Asia/Seoul')).date()
    since=today-timedelta(days=150)
    res=_SESSION.get('https://opendart.fss.or.kr/api/list.json',params={
        'crtfc_key':api_key,'corp_code':corp_code,
        'bgn_de':since.strftime('%Y%m%d'),'end_de':today.strftime('%Y%m%d'),
        'page_count':30,'sort':'date','sort_mth':'desc'},timeout=15)
    res.raise_for_status()
    data=res.json()
    if data.get('status')=='013': return []
    if data.get('status')!='000':
        raise ValueError('DART API 오류: '+str(data.get('message','')))
    results=[]
    for item in data.get('list',[])[:limit]:
        dt=datetime.strptime(item['rcept_dt'],'%Y%m%d').date()
        results.append({'title':item.get('report_nm',''),'date':str(dt),
                        'age_days':(today-dt).days,'source':'금융감독원 DART',
                        'link':'https://dart.fss.or.kr/dsaf001/main.do?rcpNo='+item['rcept_no'],
                        'basis':'원문 확인 필요'})
    return results


def fetch_sec_filings(symbol: str, user_agent: str, limit:int=7) -> list:
    if not user_agent or '@' not in user_agent:
        raise ValueError('SEC_USER_AGENT 미설정: SEC는 연락 가능한 식별 User-Agent를 요구합니다.')
    headers={'User-Agent':user_agent,'Accept-Encoding':'gzip, deflate'}
    mapping=_SESSION.get('https://www.sec.gov/files/company_tickers.json',headers=headers,timeout=12)
    mapping.raise_for_status()
    stock=symbol.upper()
    company=next((x for x in mapping.json().values() if x.get('ticker','').upper()==stock),None)
    if not company: raise ValueError('SEC 티커 매칭 실패: 해외 상장회사/ADR은 공시 체계가 다를 수 있습니다.')
    cik=int(company['cik_str'])
    res=_SESSION.get(f'https://data.sec.gov/submissions/CIK{cik:010d}.json',headers=headers,timeout=12)
    res.raise_for_status()
    filings=res.json()['filings']['recent']
    today=datetime.now(ZoneInfo('America/New_York')).date()
    out=[]
    for i,(form,date) in enumerate(zip(filings['form'],filings['filingDate'])):
        if form not in ('10-K','10-Q','8-K','20-F','6-K'):
            continue
        dt=datetime.strptime(date,'%Y-%m-%d').date()
        age=(today-dt).days
        if age>150: break
        accession=filings['accessionNumber'][i].replace('-','')
        filename=filings['primaryDocument'][i]
        # SEC returns primaryDocument and accessionNumber for the submitted filing
        link=f'https://www.sec.gov/Archives/edgar/data/{cik}/{accession}/{filename}'
        out.append({'title':f'{form} · {date}', 'date':date,'age_days':age,
                    'source':'미국 SEC EDGAR', 'link':link, 'basis':'원문 확인 필요'})
        if len(out)>=limit: break
    return out


def fetch_financial_facts(symbol: str, market: str, *, dart_key: str = '', sec_user_agent: str = '') -> list:
    """Small set of *reported* financial facts, never inferred price targets.

    Domestic: OpenDART headline accounts from latest available official statement.
    US: SEC standardized companyfacts for a single reporting duration.
    Returns an empty list when the issuer has no suitable standardized fields.
    """
    if market!='US':
        if not dart_key: raise ValueError('OpenDART API 키가 없어 재무자료를 확인할 수 없습니다.')
        # Reuse the cached DART stock-code registry after calling the existing filing search.
        entry=_registry_cache.get(dart_key)
        if not entry or symbol not in entry['map']:
            fetch_dart_filings(symbol,dart_key,limit=1)
            entry=_registry_cache.get(dart_key)
        corp_code=(entry or {}).get('map',{}).get(symbol)
        if not corp_code:raise ValueError('OpenDART 기업 고유번호를 찾지 못했습니다.')
        today=datetime.now(ZoneInfo('Asia/Seoul')).date()
        candidates=[]
        if today.month>=11:candidates.append((today.year,'11014','3분기'))
        if today.month>=8:candidates.append((today.year,'11012','반기'))
        if today.month>=5:candidates.append((today.year,'11013','1분기'))
        candidates.append((today.year-1,'11011','사업보고서'))
        candidates.append((today.year-1,'11014','3분기'))
        for year,code,label in candidates:
            response=_SESSION.get('https://opendart.fss.or.kr/api/fnlttSinglAcnt.json',params={
                'crtfc_key':dart_key,'corp_code':corp_code,
                'bsns_year':str(year),'reprt_code':code},timeout=12)
            response.raise_for_status()
            data=response.json()
            if data.get('status')=='013':continue
            if data.get('status')!='000':
                raise ValueError('DART 재무조회 오류: '+str(data.get('message') or ''))
            rows=data.get('list') or []
            if not rows:continue
            # Prefer consolidated figures; do not mix consolidated and standalone.
            cfs=[x for x in rows if x.get('fs_div')=='CFS']
            basis_rows=cfs if cfs else [x for x in rows if x.get('fs_div')=='OFS']
            specs=[('매출액',['매출액','영업수익','매출총액']),
                   ('영업이익',['영업이익']),('순이익',['당기순이익'])]
            facts=[]
            for display,names in specs:
                row=next((r for r in basis_rows if r.get('account_nm') in names and
                           r.get('sj_div') in ('IS','CIS')),None)
                if not row:continue
                raw=(row.get('thstrm_amount') or row.get('thstrm_add_amount') or '').replace(',','').strip()
                if not raw or raw=='-':continue
                try:value=int(raw)
                except ValueError:continue
                facts.append({'name':display,'value':value,'unit':'KRW',
                              'period':f'{year}년 {label}',
                              'reported_at':None,'source':'OpenDART 주요 재무계정',
                              'basis':('연결' if cfs else '별도')+' 재무제표 · 분기/반기는 누적값일 수 있음',
                              'link':'https://dart.fss.or.kr/'})
            if facts:return facts
        return []
    if not sec_user_agent or '@' not in sec_user_agent:
        raise ValueError('SEC_USER_AGENT 미설정')
    headers={'User-Agent':sec_user_agent,'Accept-Encoding':'gzip, deflate'}
    mapping=_SESSION.get('https://www.sec.gov/files/company_tickers.json',headers=headers,timeout=12)
    mapping.raise_for_status()
    company=next((x for x in mapping.json().values() if x.get('ticker','').upper()==symbol.upper()),None)
    if not company:raise ValueError('SEC 상장기업 티커를 찾지 못했습니다.')
    cik=int(company['cik_str'])
    response=_SESSION.get(f'https://data.sec.gov/api/xbrl/companyfacts/CIK{cik:010d}.json',headers=headers,timeout=15)
    response.raise_for_status()
    concepts=(response.json().get('facts') or {}).get('us-gaap') or {}
    specs=[('매출액',['RevenueFromContractWithCustomerExcludingAssessedTax','Revenues','SalesRevenueNet']),
           ('영업이익',['OperatingIncomeLoss']),('순이익',['NetIncomeLoss'])]
    out=[]
    for display,tags in specs:
        selected=None
        for tag in tags:
            records=(concepts.get(tag) or {}).get('units',{}).get('USD') or []
            eligible=[]
            for x in records:
                if x.get('form') not in ('10-Q','10-K') or not x.get('start') or not x.get('end') or not x.get('filed'):
                    continue
                try:duration=(datetime.fromisoformat(x['end'])-datetime.fromisoformat(x['start'])).days
                except ValueError:continue
                if (70<=duration<=110 or 320<=duration<=410) and isinstance(x.get('val'),(int,float)):
                    eligible.append(x)
            if eligible:
                selected=max(eligible,key=lambda x:(x['filed'],x['end']))
                break
        if not selected:continue
        out.append({'name':display,'value':selected['val'],'unit':'USD',
                    'period':selected['start']+' ~ '+selected['end'],
                    'reported_at':selected['filed'],'source':'SEC XBRL companyfacts',
                    'basis':f'{selected["form"]} / 표준 US-GAAP 공시 수치; 회계기간 상이할 수 있음',
                    'link':f'https://www.sec.gov/edgar/browse/?CIK={cik}'})
    return out


def summarize_with_openai(payload: dict) -> str:
    key=os.environ.get('OPENAI_API_KEY','').strip()
    if not key:
        raise ValueError('OPENAI_API_KEY가 설정되지 않았습니다. 규칙 기반 분석은 별도 키 없이 사용 가능합니다.')
    model=os.environ.get('OPENAI_MODEL','gpt-4.1-mini')
    relevant={'symbol':payload['symbol'],'status':payload['analysis']['status'],
        'levels':payload['analysis']['levels'],'indicators':payload['analysis']['indicators'],
        'checks':payload['analysis']['conditions']+payload['analysis']['evidence_checks'],
        'news':[{'id':f'N{i+1}','title':n['title'],'published_at':n['published_at'],'source':n['source']}
                 for i,n in enumerate(payload['news'][:8])],
        'financials':payload.get('financials',[]),
        'research':[{'id':f'R{i+1}','title':n['title'],'published_at':n.get('published_at'),'source':n['source']} for i,n in enumerate(payload.get('research',[])[:4])],
        'filings':[{'id':f'F{i+1}','title':f['title'],'date':f['date'],'source':f['source']}
                   for i,f in enumerate(payload['filings'][:5])]}
    instr=("한국어로 금융 투자 리서치 도우미처럼 답하라. 제공한 데이터만 이용한다. "
         "기사·공시·리포트 관련 보도는 제목/발행일만 수집했으며 원문을 읽었다고 주장하지 말 것. 공식 정량 재무계정은 표에 적힌 공시 기준으로만 해석하고 기간을 혼동하지 말 것. "
         "정량 알고리즘이 정한 상태 및 가격 구간은 변경하지 말 것. "
         "최종 판단 전 직접 확인해야 할 원문 목록을 [N1], [F1] 등으로 명시하고, "
         "보수적으로 3~5문장으로 정리할 것. 투자 수익 보장 또는 확정매수 표현 금지.")
    data={'model':model, 'instructions':instr,
          'input':json.dumps(relevant,ensure_ascii=False),'max_output_tokens':360}
    response=requests.post('https://api.openai.com/v1/responses',
        headers={'Authorization':f'Bearer {key}','Content-Type':'application/json'},
        json=data,timeout=30)
    response.raise_for_status()
    out=response.json()
    chunks=[]
    for item in out.get('output',[]):
        for content in item.get('content',[]):
            if content.get('type')=='output_text':chunks.append(content.get('text',''))
    return '\n'.join(chunks).strip() or 'AI가 요약 문장을 생성하지 못했습니다.'
