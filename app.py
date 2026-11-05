"""Domestic + US conservative stock signal research web app. No trading endpoints."""
from __future__ import annotations
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
import os, sqlite3, time, threading
from copy import deepcopy
from datetime import date, datetime, timezone
from typing import Literal
from zoneinfo import ZoneInfo
import requests
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse
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
app=FastAPI(title='보수적 AI 주식 가격알림 비서',version='0.4.2-timing')
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
    if ON_VERCEL and request.method == 'POST' and request.url.path in ('/api/analyze','/api/backtest','/api/dart/financials','/api/dart/disclosures'):
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
    return {'status':'ok','version':'0.4.2-timing','time_utc':datetime.now(timezone.utc).isoformat(),
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


def run_analysis(symbol:str,name:str,market:str,with_ai:bool=False):
    news,filings,research,financials=[],[],[],[]
    news_err,filings_err,research_err,financials_err=None,None,None,None
    quote={}
    pool=ThreadPoolExecutor(max_workers=7,thread_name_prefix='stockdesk-source')
    try:
        # Independent network requests overlap; calculation still waits for their results.
        daily_job=pool.submit(fetch_daily,symbol,market)
        domestic=market in ('KOSPI','KOSDAQ')
        search_term=symbol if domestic else name.strip() or symbol
        news_job=pool.submit(fetch_news,search_term,market)
        research_job=pool.submit(fetch_research_mentions,search_term,market)
        dart_key=os.environ.get('DART_API_KEY') or os.environ.get('DART_API','')
        sec_agent=os.environ.get('SEC_USER_AGENT','')
        filings_job=(pool.submit(fetch_sec_filings,symbol,sec_agent) if market=='US'
                     else pool.submit(fetch_dart_filings,symbol,dart_key))
        financials_job=pool.submit(fetch_financial_facts,symbol,market,
                                   dart_key=dart_key,sec_user_agent=sec_agent)
        quote_job=(pool.submit(fetch_naver_quote,symbol) if domestic
                   else pool.submit(fetch_kis_quote,symbol,market) if kis_configured() else None)
        macro_job=pool.submit(fetch_macro_context)
        # A daily-bar failure remains fatal and reaches the existing API 502 handler.
        bars,meta=daily_job.result()
        try:news=news_job.result()
        except (requests.RequestException,ValueError,TypeError) as e:
            news_err='뉴스 수집 실패: '+str(e)[:160]
        try:research=research_job.result()
        except (requests.RequestException,ValueError,TypeError) as e:
            research_err='보고서 관련 보도 검색 실패: '+str(e)[:160]
        try:filings=filings_job.result()
        except Exception as e:
            filings_err=('공시 확인 불가: OpenDART 인증키·조회 한도·네트워크 상태를 확인하세요.'
                         if market!='US' else '공시 확인 불가: '+str(e)[:160])
        try:financials=financials_job.result()
        except (requests.RequestException,ValueError,KeyError,TypeError) as e:
            financials_err=('재무계정 확인 불가: 네이버증권·OpenDART의 조회 상태를 확인하세요.'
                            if market!='US' else '재무계정 확인 불가: '+str(e)[:160])
        if quote_job is None:
            quote['quote_error']='시세 API 미연결: 확정 종가만 표시합니다.'
        else:
            try:quote['quote']=quote_job.result()
            except (requests.RequestException,ValueError,KeyError,TypeError) as e:
                prefix='네이버 현재가 미검증: ' if domestic else 'KIS 시세 조회 실패: '
                quote['quote_error']=prefix+str(e)[:150 if domestic else 170]
        try:macro=macro_job.result()
        except (requests.RequestException,ValueError,KeyError,TypeError):
            macro={'eligible':False,'summary':'미국 경제 자료를 확인하지 못했습니다.','metrics':{},
                   'risk_flags':[],'warnings':['미국 금리·환율·물가·주요 종목 자료 조회 실패'],
                   'checked_at':datetime.now(timezone.utc).isoformat()}
    finally:
        # On a fatal daily error, do not hold the response for unrelated providers.
        # Running requests retain their existing network timeouts; queued jobs cancel.
        pool.shutdown(wait=False,cancel_futures=True)
    result=analyze_bars(bars,market=market,news=news,filings=filings,
                        news_error=news_err,filings_error=filings_err)
    bundle={'symbol':symbol.upper().strip(),'company_name':name or symbol,
            'market':market,'meta':meta,'analysis':result,'news':news,'macro':macro,
            'filings':filings,'research':research,'financials':financials,
            'financials_error':financials_err,'news_error':news_err,'filings_error':filings_err,
            'research_error':research_err,
            'chart': [{'date':str(row['date']),'close':round(float(row['close']),4)}
                      for _,row in bars.tail(120).iterrows()]}
    bundle.update(quote)
    audit=audit_data(market,meta,bars,result,financials,financials_err)
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
    _remember_context(bundle)
    return bundle


@app.post('/api/analyze')
def analyze(req:AnalyzeReq):
    check_symbol(req.symbol,req.market)
    try: return run_analysis(req.symbol,req.name,req.market,req.with_ai)
    except (requests.RequestException,ValueError,IndexError,KeyError) as e:
        raise HTTPException(status_code=502,detail='시세 분석 실패: '+str(e)[:250])


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
