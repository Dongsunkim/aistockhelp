"""Domestic + US conservative stock signal research web app. No trading endpoints."""
from __future__ import annotations
from pathlib import Path
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
import json, os, sqlite3, time, threading
from copy import deepcopy
from datetime import date, datetime, timezone
from typing import Literal
from zoneinfo import ZoneInfo
import requests
import anyio
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from dotenv import load_dotenv
from analysis_engine import analyze_bars
from evidence import build_evidence_pack
from sources import (fetch_daily,fetch_news,fetch_research_mentions,fetch_dart_filings,
                     fetch_sec_filings,fetch_financial_facts,summarize_with_openai,yahoo_symbol)

from quotes import configured as kis_configured, fetch_kis_quote
from backtest import backtest_technical
from naver_history import fetch_naver_quote
from data_quality import check as audit_data
from macro_data import fetch_macro_context
from stock_qa import answer_question
from dart_client import DartError, fetch_dart_financials, fetch_dart_disclosures

ROOT=Path(__file__).parent
ON_VERCEL=bool(os.getenv("VERCEL")) or bool(os.getenv("VERCEL_ENV"))
load_dotenv(ROOT/'.env')
DB=Path(os.environ.get('SIGNAL_DB',str(ROOT/'signals.sqlite3')))
app=FastAPI(title='보수적 AI 주식 가격알림 비서',version='0.4.4-results')
app.mount('/static',StaticFiles(directory=str(ROOT/'static')),name='static')


def getdb():
    con=sqlite3.connect(DB,timeout=15)
    con.row_factory=sqlite3.Row
    con.execute('PRAGMA busy_timeout=15000')
    return con


def init_db():
    with getdb() as con:
        con.execute('''CREATE TABLE IF NOT EXISTS watchlist (
        id INTEGER PRIMARY KEY AUTOINCREMENT, symbol TEXT NOT NULL,
        name TEXT NOT NULL, market TEXT NOT NULL,
        buy_below REAL, sell_above REAL,
        created_at TEXT NOT NULL,
        UNIQUE(symbol,market))''')
        con.execute('''CREATE TABLE IF NOT EXISTS sent_alerts (
        id INTEGER PRIMARY KEY AUTOINCREMENT, watch_id INTEGER NOT NULL,
        kind TEXT NOT NULL, daily_bar TEXT NOT NULL,
        message TEXT NOT NULL, sent_at TEXT NOT NULL,
        UNIQUE(watch_id,kind,daily_bar))''')

if not ON_VERCEL:
    init_db()


# Public, password-free: automatic AI summaries stay off on Vercel; user questions are rate-limited.
# Best-effort process-local abuse throttling; this is NOT a distributed quota.
_limiter_lock = threading.Lock()
_limiter = {}
_qa_limiter = {}
_analysis_contexts = {}
_analysis_context_lock = threading.Lock()
# A short, process-local cache only stores completed public analyses. Source timestamps
# stay unchanged, so a cache hit is never presented as a new provider observation.
_analysis_stream_cache = {}
_analysis_stream_cache_lock = threading.Lock()
_ANALYSIS_CACHE_SECONDS = 30
_ANALYSIS_CACHE_LIMIT = 32


def _context_key(symbol,market,name):
    return (symbol.strip().upper(),market,name.strip())


def _remember_context(bundle):
    key=_context_key(bundle['symbol'],bundle['market'],bundle['company_name'])
    with _analysis_context_lock:
        now=time.monotonic()
        expired=[k for k,(at,_) in _analysis_contexts.items() if now-at>90]
        for k in expired:_analysis_contexts.pop(k,None)
        if len(_analysis_contexts)>=32:_analysis_contexts.pop(next(iter(_analysis_contexts)))
        _analysis_contexts[key]=(now,deepcopy(bundle))


def _recent_context(symbol,market,name):
    with _analysis_context_lock:
        cached=_analysis_contexts.get(_context_key(symbol,market,name or symbol))
        return deepcopy(cached[1]) if cached and time.monotonic()-cached[0]<=90 else None


@app.middleware('http')
async def public_resource_guard(request: Request, call_next):
    if request.method=='POST' and request.url.path=='/api/ask':
        from fastapi.responses import JSONResponse
        client=request.client.host if request.client else 'unknown'
        now=time.monotonic()
        with _limiter_lock:
            for key in list(_qa_limiter):
                _qa_limiter[key]=[t for t in _qa_limiter[key] if now-t<60]
                if not _qa_limiter[key]:_qa_limiter.pop(key)
            personal=_qa_limiter.get(client,[])
            total=_qa_limiter.get('__total__',[])
            if len(personal)>=6 or len(total)>=24:
                return JSONResponse(status_code=429,content={'detail':'질문이 많습니다. 잠시 뒤 다시 질문해 주세요.'})
            _qa_limiter[client]=personal+[now]
            _qa_limiter['__total__']=total+[now]
    if ON_VERCEL and request.method == 'POST' and request.url.path in ('/api/analyze','/api/analyze/stream','/api/backtest','/api/dart/financials','/api/dart/disclosures'):
        from fastapi.responses import JSONResponse
        client = request.client.host if request.client else 'unknown'
        now = time.monotonic()
        with _limiter_lock:
            timestamps = [t for t in _limiter.get(client, []) if now - t < 60]
            if len(timestamps) >= 24:
                return JSONResponse(status_code=429, content={'detail':'분석 요청이 많습니다. 잠시 뒤 다시 시도하세요.'})
            timestamps.append(now)
            _limiter[client] = timestamps
            if len(_limiter) > 2000:
                _limiter.clear()
    return await call_next(request)


class AnalyzeReq(BaseModel):
    symbol:str=Field(min_length=1,max_length=18)
    name:str=Field(default='',max_length=80)
    market:str=Field(default='KOSPI')
    with_ai:bool=False
    refresh:bool=False


class WatchReq(BaseModel):
    symbol:str=Field(min_length=1,max_length=18)
    name:str=Field(min_length=1,max_length=80)
    market:str=Field(default='KOSPI')
    buy_below:float|None=Field(default=None,gt=0)
    sell_above:float|None=Field(default=None,gt=0)


def check_symbol(symbol,market):
    try: return yahoo_symbol(symbol,market)
    except ValueError as e: raise HTTPException(status_code=422,detail=str(e))


@app.get('/')
def home(): return FileResponse(ROOT/'static/index.html')


@app.get('/api/health')
def health():
    return {'status':'ok','version':'0.4.4-results','time_utc':datetime.now(timezone.utc).isoformat(),
            'serverless': ON_VERCEL, 'requires_password': False, 'public_no_login': True,
            'watchlist_storage': 'browser_local' if ON_VERCEL else 'sqlite',
            'configured':{'dart':bool(os.environ.get('DART_API_KEY') or os.environ.get('DART_API')),
                          'kis_quote':kis_configured(),
                          'sec':bool(os.environ.get('SEC_USER_AGENT')),
                          'ai':bool(os.environ.get('OPENAI_API_KEY')) and not ON_VERCEL,
                          'stock_qa':bool(os.environ.get('OPENAI_API_KEY')),
                          'telegram':bool(os.environ.get('TELEGRAM_BOT_TOKEN') and os.environ.get('TELEGRAM_CHAT_ID'))},
            'ai_note':'자동 AI 요약은 공개 앱에서 비활성입니다. 종목 질문은 OPENAI_API_KEY 연결 시 질문할 때만 호출하며, 미연결이면 자료 기반 안내를 제공합니다.',
            'warning':'국내 네이버 비공식 확정 일봉(검증 실패 시 대체 금지), 미국 Yahoo 비공식 종가. 기업가치 검증 전 매매 판단은 보류합니다.'}


@app.get('/api/watchlist')
def list_watchlist():
    if ON_VERCEL: raise HTTPException(status_code=501, detail='Vercel은 브라우저 로컬 관심종목을 사용합니다.')
    with getdb() as con:
        return [dict(r) for r in con.execute('SELECT * FROM watchlist ORDER BY id DESC').fetchall()]


class DartFinancialReq(BaseModel):
    symbol: str = Field(pattern=r'^[0-9]{6}$')
    year: int = Field(ge=2015, le=datetime.now(ZoneInfo('Asia/Seoul')).year)
    report_code: Literal['11011','11012','11013','11014'] = '11011'
    fs_div: Literal['BOTH','CFS','OFS'] = 'BOTH'


class DartDisclosuresReq(BaseModel):
    symbol: str = Field(pattern=r'^[0-9]{6}$')
    begin: date
    end: date
    page: int = Field(default=1, ge=1, le=1000)


def dart_key():
    key = os.environ.get('DART_API_KEY') or os.environ.get('DART_API','')
    if not key:
        raise HTTPException(status_code=503, detail='DART 인증키가 없습니다. 환경 설정에 DART_API_KEY를 등록하세요. 키를 브라우저에 입력하지 마세요.')
    return key


@app.post('/api/dart/financials')
def dart_financials(req: DartFinancialReq):
    """Full accounts are separate from the three facts used by the price-quality gate."""
    key = dart_key()
    try:
        return fetch_dart_financials(req.symbol, key, year=req.year,
                                    report_code=req.report_code, fs_div=req.fs_div)
    except DartError as error:
        raise HTTPException(status_code=502, detail=str(error)) from None
    except (requests.RequestException, ValueError):
        # Never expose requests' credential-bearing query URLs or provider text.
        raise HTTPException(status_code=502, detail='DART 재무조회 실패: 인증키·API 한도·네트워크 허용 상태와 종목을 확인하세요.') from None


@app.post('/api/dart/disclosures')
def dart_disclosures(req: DartDisclosuresReq):
    if req.end < req.begin or (req.end - req.begin).days > 366:
        raise HTTPException(status_code=422, detail='공시 조회기간은 시작일부터 종료일까지 최대 366일입니다.')
    if req.end > datetime.now(ZoneInfo('Asia/Seoul')).date():
        raise HTTPException(status_code=422, detail='공시 종료일은 오늘 이후로 지정할 수 없습니다.')
    key = dart_key()
    try:
        return fetch_dart_disclosures(req.symbol, key, begin=req.begin.isoformat(),
                                     end=req.end.isoformat(), page=req.page)
    except DartError as error:
        raise HTTPException(status_code=502, detail=str(error)) from None
    except (requests.RequestException, ValueError):
        raise HTTPException(status_code=502, detail='DART 공시조회 실패: 인증키·API 한도·네트워크 허용 상태와 종목을 확인하세요.') from None


@app.post('/api/watchlist')
def add_watchlist(req:WatchReq):
    if ON_VERCEL: raise HTTPException(status_code=501, detail='Vercel은 브라우저 로컬 관심종목을 사용합니다.')
    check_symbol(req.symbol,req.market)
    symbol=req.symbol.upper().strip()
    with getdb() as con:
        con.execute('''INSERT INTO watchlist(symbol,name,market,buy_below,sell_above,created_at)
            VALUES(?,?,?,?,?,?) ON CONFLICT(symbol,market) DO UPDATE SET
            name=excluded.name,buy_below=excluded.buy_below,sell_above=excluded.sell_above''',
            (symbol,req.name.strip(),req.market,req.buy_below,req.sell_above,
             datetime.now(timezone.utc).isoformat()))
    return {'saved':True}


@app.delete('/api/watchlist/{item_id}')
def delete_watch(item_id:int):
    if ON_VERCEL: raise HTTPException(status_code=501, detail='Vercel은 브라우저 로컬 관심종목을 사용합니다.')
    with getdb() as con:
        row=con.execute('DELETE FROM watchlist WHERE id=?',(item_id,))
        if not row.rowcount: raise HTTPException(status_code=404,detail='관심종목이 없습니다.')
    return {'deleted':True}


_SOURCE_ORDER=('news','research','filings','financials','quote','macro')
# Bound the complete source collection, including every provider's fallbacks.
ANALYSIS_TIMEOUT_SECONDS=12
_SOURCE_LABELS={'news':'뉴스','research':'증권사 리서치','filings':'공시',
                'financials':'재무 자료','quote':'현재가','macro':'미국 경제 자료'}


def _start_analysis_jobs(symbol,name,market):
    pool=ThreadPoolExecutor(max_workers=7,thread_name_prefix='stockdesk-source')
    try:
        domestic=market in ('KOSPI','KOSDAQ')
        search_term=symbol if domestic else name.strip() or symbol
        dart_key=os.environ.get('DART_API_KEY') or os.environ.get('DART_API','')
        sec_agent=os.environ.get('SEC_USER_AGENT','')
        jobs={
            'daily':pool.submit(fetch_daily,symbol,market),
            'news':pool.submit(fetch_news,search_term,market),
            'research':pool.submit(fetch_research_mentions,search_term,market),
            'filings':(pool.submit(fetch_sec_filings,symbol,sec_agent) if market=='US'
                       else pool.submit(fetch_dart_filings,symbol,dart_key)),
            'financials':pool.submit(fetch_financial_facts,symbol,market,
                                     dart_key=dart_key,sec_user_agent=sec_agent),
            'quote':(pool.submit(fetch_naver_quote,symbol) if domestic
                     else pool.submit(fetch_kis_quote,symbol,market) if kis_configured() else None),
            'macro':pool.submit(fetch_macro_context),
        }
        return pool,jobs
    except BaseException:
        pool.shutdown(wait=False,cancel_futures=True)
        raise


def _analysis_source_state():
    return {'news':[],'research':[],'filings':[],'financials':[],
            'news_error':None,'research_error':None,'filings_error':None,'financials_error':None,
            'macro_error':None,'quote_configured':None,
            'quote_error':'현재가 확인 중입니다. 확정 종가를 먼저 표시합니다.',
            'macro':{'eligible':False,'summary':'미국 경제 자료를 확인하고 있습니다.','metrics':{},
                     'risk_flags':[],'warnings':['미국 금리·환율·물가·주요 종목 확인 중'],
                     'checked_at':None}}


def _read_analysis_source(kind,job,market,state,*,safe_errors=False):
    """Record one resolved provider. Pending sources never contribute verified evidence."""
    if job is None:
        state['quote_configured']=False
        state['quote_error']='시세 API 미연결: 확정 종가만 표시합니다.'
        return True
    if kind=='quote':state['quote_configured']=True
    try:
        value=job.result()
    except Exception as error:
        # Keep the existing non-streaming exception behavior and source messages.
        handled=(Exception if kind=='filings' else
                 (requests.RequestException,ValueError,TypeError) if kind in ('news','research') else
                 (requests.RequestException,ValueError,KeyError,TypeError))
        if not safe_errors and not isinstance(error,handled):
            raise
        message=str(error) if not safe_errors else '자료 제공원의 응답을 확인하지 못했습니다.'
        if kind=='news':state['news_error']='뉴스 수집 실패: '+message[:160]
        elif kind=='research':state['research_error']='보고서 관련 보도 검색 실패: '+message[:160]
        elif kind=='filings':
            state['filings_error']=('공시 확인 불가: OpenDART 인증키·조회 한도·네트워크 상태를 확인하세요.'
                                    if market!='US' else '공시 확인 불가: '+message[:160])
        elif kind=='financials':
            state['financials_error']=('재무계정 확인 불가: 네이버증권·OpenDART의 조회 상태를 확인하세요.'
                                       if market!='US' else '재무계정 확인 불가: '+message[:160])
        elif kind=='quote':
            prefix='네이버 현재가 미검증: ' if market in ('KOSPI','KOSDAQ') else 'KIS 시세 조회 실패: '
            state['quote_error']=prefix+message[:150 if market in ('KOSPI','KOSDAQ') else 170]
        elif kind=='macro':
            state['macro_error']='미국 경제 자료 조회 실패: '+message[:160]
            state['macro']={'eligible':False,'summary':'미국 경제 자료를 확인하지 못했습니다.','metrics':{},
                            'risk_flags':[],'warnings':['미국 금리·환율·물가·주요 종목 자료 조회 실패'],
                            'checked_at':datetime.now(timezone.utc).isoformat()}
        return False
    state[kind]=value
    if kind=='quote':state.pop('quote_error',None)
    return True


def _assemble_analysis(symbol,name,market,bars,meta,state,*,with_ai=False):
    """Use the same calculations, audits and citations for full and progressive results."""
    news,filings,financials=state['news'],state['filings'],state['financials']
    macro=state['macro']
    result=analyze_bars(bars,market=market,news=news,filings=filings,
                        news_error=state['news_error'],filings_error=state['filings_error'])
    bundle={'symbol':symbol.upper().strip(),'company_name':name or symbol,
            'market':market,'meta':meta,'analysis':result,'news':news,'macro':macro,
            'filings':filings,'research':state['research'],'financials':financials,
            'financials_error':state['financials_error'],'news_error':state['news_error'],
            'filings_error':state['filings_error'],'research_error':state['research_error'],
            'macro_error':state['macro_error'],
            'chart': [{'date':str(row['date']),'close':round(float(row['close']),4)}
                      for _,row in bars.tail(120).iterrows()]}
    if 'quote' in state:bundle['quote']=state['quote']
    if 'quote_error' in state:bundle['quote_error']=state['quote_error']
    audit=audit_data(market,meta,bars,result,financials,state['financials_error'])
    bundle['data_quality']=audit
    if not audit['price_levels_eligible']:
        result['status']='가격 데이터 확인 필요'
    elif not audit['financials_eligible'] and result['technical_ok']:
        result['status']='재무 확인 전 판단 보류'
    elif not macro.get('eligible') and result['technical_ok']:
        result['status']='미국 경제 확인 전 판단 보류'
    result['confirmed']=False
    bundle['evidence_pack']=build_evidence_pack(bundle)
    if with_ai and not ON_VERCEL:
        try:bundle['ai_summary']=summarize_with_openai(bundle)
        except (requests.RequestException,ValueError,KeyError) as e:
            bundle['ai_error']='AI 요약 사용 불가: '+str(e)[:180]
    if ON_VERCEL:
        bundle['ai_error']='공개 웹앱에서는 비용 보호를 위해 유료 AI 요약을 비활성화했습니다. 아래 규칙 기반 근거 설명을 이용하세요.'
    return bundle


def _timeout_analysis_sources(pending,state):
    timed_out=list(pending)
    for kind,job in pending.items():
        job.cancel()
        message=f'{_SOURCE_LABELS[kind]} 조회 시간 초과: 이번 결과에는 반영하지 않았습니다.'
        state[kind+'_error']=message
        if kind=='macro':
            state['macro']={'eligible':False,'summary':'미국 경제 자료 조회 시간이 초과되었습니다.',
                            'metrics':{},'risk_flags':[],'warnings':[message],
                            'checked_at':datetime.now(timezone.utc).isoformat()}
    pending.clear()
    return timed_out


def _analysis_completion(state,timed_out=(),*,pending_sources=()):
    pending=bool(pending_sources)
    # An optional quote API with no configuration made no request that could fail.
    # Its visible quote_error remains, while valid core results may still be reused.
    unavailable=[kind for kind in _SOURCE_ORDER if kind not in pending_sources
                 and not (kind=='quote' and state.get('quote_configured') is False)
                 and (state.get(kind+'_error')
                      or (kind=='macro' and not state['macro'].get('eligible')))]
    if pending:
        message='가격 자료를 먼저 표시합니다. 추가 판단 근거를 확인하고 있습니다.'
    elif unavailable:
        labels='·'.join(_SOURCE_LABELS[kind] for kind in unavailable)
        message=f'확인된 자료로 결과를 표시했습니다. {labels} 자료를 확인하지 못했습니다.'
    else:
        message='자료 조회를 마쳤습니다. 각 자료의 검증 상태를 확인해 주세요.'
    return {'status':'partial' if pending or unavailable else 'complete',
            'timed_out_sources':list(timed_out),'unavailable_sources':unavailable,
            'message':message}


def _resolve_analysis_sources(pending,done,market,state,*,safe_errors=False):
    for kind in list(pending):
        if pending[kind] in done:
            _read_analysis_source(kind,pending.pop(kind),market,state,safe_errors=safe_errors)


def _analysis_daily(jobs,cancelled,deadline):
    if not _analysis_done((jobs['daily'],),cancelled,deadline):
        jobs['daily'].cancel()
        raise _AnalysisDailyTimeout('가격 자료 조회 시간 초과: 확인된 일봉이 없어 분석 결과를 만들 수 없습니다. 잠시 뒤 다시 시도해 주세요.')
    return jobs['daily'].result()


def run_analysis(symbol:str,name:str,market:str,with_ai:bool=False):
    deadline=time.monotonic()+ANALYSIS_TIMEOUT_SECONDS
    pool,jobs=_start_analysis_jobs(symbol,name,market)
    state=_analysis_source_state()
    timed_out=[]
    try:
        bars,meta=_analysis_daily(jobs,None,deadline)
        pending={kind:jobs[kind] for kind in _SOURCE_ORDER if jobs[kind] is not None}
        if jobs['quote'] is None:_read_analysis_source('quote',None,market,state)
        while pending:
            done=_analysis_done(tuple(pending.values()),None,deadline)
            if not done:
                timed_out=_timeout_analysis_sources(pending,state)
                break
            _resolve_analysis_sources(pending,done,market,state)
    finally:
        pool.shutdown(wait=False,cancel_futures=True)
    bundle=_assemble_analysis(symbol,name,market,bars,meta,state,with_ai=with_ai)
    bundle['completion']=_analysis_completion(state,timed_out)
    _remember_context(bundle)
    return bundle


def _stream_cache_key(symbol,name,market):
    canonical=symbol.strip().upper()
    return (canonical,market,name.strip() or canonical)


def _cached_stream_analysis(key):
    with _analysis_stream_cache_lock:
        now=time.monotonic()
        for old in list(_analysis_stream_cache):
            if now-_analysis_stream_cache[old][0]>=_ANALYSIS_CACHE_SECONDS:
                _analysis_stream_cache.pop(old,None)
        cached=_analysis_stream_cache.get(key)
        if cached is None:return None
        at,stored_at,bundle=cached
        result=deepcopy(bundle)
        result['cache']={'hit':True,'age_seconds':round(max(0,now-at),3),'stored_at':stored_at}
        return result


def _store_stream_analysis(key,bundle):
    with _analysis_stream_cache_lock:
        now=time.monotonic()
        stored_at=datetime.now(timezone.utc).isoformat()
        for old in list(_analysis_stream_cache):
            if now-_analysis_stream_cache[old][0]>=_ANALYSIS_CACHE_SECONDS:
                _analysis_stream_cache.pop(old,None)
        _analysis_stream_cache.pop(key,None)
        while len(_analysis_stream_cache)>=_ANALYSIS_CACHE_LIMIT:
            _analysis_stream_cache.pop(next(iter(_analysis_stream_cache)))
        _analysis_stream_cache[key]=(now,stored_at,deepcopy(bundle))
    return {'hit':False,'age_seconds':0,'stored_at':stored_at}


class _AnalysisCancelled(Exception):
    pass


class _AnalysisDailyTimeout(requests.Timeout):
    pass


def _analysis_done(jobs,cancelled,deadline=None):
    while cancelled is None or not cancelled.is_set():
        # Keep observations which already arrived, even at the budget boundary.
        done={job for job in jobs if job.done()}
        if done:return done
        remaining=None if deadline is None else deadline-time.monotonic()
        if remaining is not None and remaining<=0:return set()
        timeout=(min(0.1,remaining) if remaining is not None else 0.1) if cancelled is not None else remaining
        done,_=wait(jobs,timeout=timeout,return_when=FIRST_COMPLETED)
        if done:return done
    raise _AnalysisCancelled()


def iter_analysis_events(symbol,name,market,with_ai=False,refresh=False,*,cancelled=None):
    """Yield the price result before slow independent evidence, then merge arrivals.

    The response owns all provider futures until completion or iterator closure. It
    does not rely on post-response background tasks in a serverless runtime.
    """
    pool=None
    try:
        key=_stream_cache_key(symbol,name,market)
        if not with_ai and not refresh:
            cached=_cached_stream_analysis(key)
            if cached is not None:
                _remember_context(cached)
                yield {'stage':'complete','result':cached,'pending_sources':[]}
                return
        deadline=time.monotonic()+ANALYSIS_TIMEOUT_SECONDS
        pool,jobs=_start_analysis_jobs(symbol,name,market)
        state=_analysis_source_state()
        bars,meta=_analysis_daily(jobs,cancelled,deadline)
        pending={kind:jobs[kind] for kind in _SOURCE_ORDER if jobs[kind] is not None}
        if jobs['quote'] is None:_read_analysis_source('quote',None,market,state)
        # This first yield never waits for news, filings, financials, quote or macro.
        bundle=_assemble_analysis(symbol,name,market,bars,meta,state)
        bundle['cache']={'hit':False,'age_seconds':0,'stored_at':None}
        bundle['completion']=_analysis_completion(state,pending_sources=pending)
        yield {'stage':'partial','result':bundle,'pending_sources':list(pending)}
        timed_out=[]
        while pending:
            done=_analysis_done(tuple(pending.values()),cancelled,deadline)
            if done:
                _resolve_analysis_sources(pending,done,market,state,safe_errors=True)
            else:
                timed_out=_timeout_analysis_sources(pending,state)
            final=not pending
            bundle=_assemble_analysis(symbol,name,market,bars,meta,state,with_ai=with_ai and final)
            bundle['cache']={'hit':False,'age_seconds':0,'stored_at':None}
            bundle['completion']=_analysis_completion(state,timed_out,pending_sources=pending)
            if final:
                # Failed providers and incomplete macro observations are deliberately
                # retried on the next request, instead of being hidden by the cache.
                if (bundle['completion']['status']=='complete' and not with_ai
                        and bundle['data_quality']['price_levels_eligible']
                        and state['macro'].get('eligible') is True):
                    bundle['cache']=_store_stream_analysis(key,bundle)
                _remember_context(bundle)
            yield {'stage':'complete' if final else 'partial','result':bundle,
                   'pending_sources':list(pending)}
    except _AnalysisCancelled:
        return
    except _AnalysisDailyTimeout as error:
        yield {'stage':'error','result':None,'pending_sources':[],'detail':str(error)}
    except Exception:
        # Provider exception text may contain URLs, credentials or response bodies.
        yield {'stage':'error','result':None,'pending_sources':[],
               'detail':'시세 분석에 필요한 가격 자료를 확인하지 못했습니다. 잠시 뒤 다시 시도해 주세요.'}
    finally:
        if pool is not None:pool.shutdown(wait=False,cancel_futures=True)


def _analysis_ndjson(events):
    try:
        for event in events:
            try:
                line=json.dumps(event,ensure_ascii=False,allow_nan=False,separators=(',',':'))
            except (ValueError,TypeError,OverflowError):
                yield json.dumps({'stage':'error','result':None,'pending_sources':[],
                                  'detail':'분석 자료의 형식을 확인하지 못했습니다. 잠시 뒤 다시 시도해 주세요.'},
                                 ensure_ascii=False)+'\n'
                return
            yield line+'\n'
    finally:
        events.close()


def _next_analysis_line(lines,cancelled):
    try:
        return next(lines,None)
    finally:
        # Cancellation can also arrive during calculation or JSON encoding, after
        # the future wait has finished. The owning worker must close that iterator.
        if cancelled.is_set():lines.close()


class _AnalysisStreamingResponse(StreamingResponse):
    """Explicitly release source work on client disconnect, including ASGI 2.4."""
    def __init__(self,events,cancelled):
        self._lines=_analysis_ndjson(events)
        self._cancelled=cancelled
        super().__init__(self._body(),media_type='application/x-ndjson',
                         headers={'Cache-Control':'no-store','X-Accel-Buffering':'no'})

    async def _body(self):
        while True:
            # A disconnect must not wait for a shielded synchronous next() call.
            line=await anyio.to_thread.run_sync(_next_analysis_line,self._lines,self._cancelled,abandon_on_cancel=True)
            if line is None:return
            yield line

    async def __call__(self,scope,receive,send):
        try:
            await super().__call__(scope,receive,send)
        finally:
            self._cancelled.set()
            try:self._lines.close()
            except ValueError:
                # An abandoned worker currently owns next(); its cancellation-aware
                # future wait returns within 100ms and runs the same finally block.
                pass


@app.post('/api/analyze')
def analyze(req:AnalyzeReq):
    check_symbol(req.symbol,req.market)
    try: return run_analysis(req.symbol,req.name,req.market,req.with_ai)
    except (requests.RequestException,ValueError,IndexError,KeyError) as e:
        raise HTTPException(status_code=502,detail='시세 분석 실패: '+str(e)[:250])


@app.post('/api/analyze/stream')
def analyze_stream(req:AnalyzeReq):
    check_symbol(req.symbol,req.market)
    cancelled=threading.Event()
    events=iter_analysis_events(req.symbol,req.name,req.market,req.with_ai,req.refresh,cancelled=cancelled)
    return _AnalysisStreamingResponse(events,cancelled)


class AskReq(BaseModel):
    symbol:str=Field(min_length=1,max_length=18)
    market:str=Field(default='KOSPI')
    name:str=Field(default='',max_length=80)
    question:str=Field(min_length=2,max_length=600)


@app.post('/api/ask')
def ask_stock(req:AskReq):
    check_symbol(req.symbol,req.market)
    if len(req.question.strip())<2:
        raise HTTPException(status_code=422,detail='궁금한 점을 두 글자 이상 입력해 주세요.')
    context=_recent_context(req.symbol,req.market,req.name)
    if context is None:
        try:context=run_analysis(req.symbol,req.name,req.market)
        except (requests.RequestException,ValueError,IndexError,KeyError) as error:
            raise HTTPException(status_code=502,detail='답변에 필요한 최신 종목자료를 조회하지 못했습니다.') from error
    return answer_question(req.question.strip(),context)


class BacktestReq(BaseModel):
    symbol:str=Field(min_length=1,max_length=18)
    market:str=Field(default='KOSPI')
    horizon:int=Field(default=10)


@app.post('/api/backtest')
def backtest(req:BacktestReq):
    check_symbol(req.symbol,req.market)
    if req.horizon not in (5,10,20):
        raise HTTPException(status_code=422,detail='보유 기간은 5, 10, 20거래일 중 하나여야 합니다.')
    try:
        bars,meta=fetch_daily(req.symbol,req.market,history='3y')
        return {'meta':meta,'backtest':backtest_technical(bars,market=req.market,horizon=req.horizon)}
    except (requests.RequestException,ValueError,KeyError,IndexError) as e:
        raise HTTPException(status_code=502,detail='과거 검증 불가: '+str(e)[:220])
