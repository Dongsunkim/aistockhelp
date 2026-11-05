"""Domestic + US conservative stock signal research web app. No trading endpoints."""
from __future__ import annotations
from pathlib import Path
import os, sqlite3, time, threading
from datetime import datetime, timezone
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

ROOT=Path(__file__).parent
ON_VERCEL=bool(os.getenv("VERCEL")) or bool(os.getenv("VERCEL_ENV"))
load_dotenv(ROOT/'.env')
DB=Path(os.environ.get('SIGNAL_DB',str(ROOT/'signals.sqlite3')))
app=FastAPI(title='보수적 AI 주식 가격알림 비서',version='0.4.1-recovery')
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


# Public, password-free: paid LLM calls are disabled on Vercel to protect billing.
# Best-effort process-local abuse throttling; this is NOT a distributed quota.
_limiter_lock = threading.Lock()
_limiter = {}

@app.middleware('http')
async def public_resource_guard(request: Request, call_next):
    if ON_VERCEL and request.method == 'POST' and request.url.path in ('/api/analyze','/api/backtest'):
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
    return {'status':'ok','version':'0.4.1-recovery','time_utc':datetime.now(timezone.utc).isoformat(),
            'serverless': ON_VERCEL, 'requires_password': False, 'public_no_login': True,
            'watchlist_storage': 'browser_local' if ON_VERCEL else 'sqlite',
            'configured':{'dart':bool(os.environ.get('DART_API_KEY')),
                          'kis_quote':kis_configured(),
                          'sec':bool(os.environ.get('SEC_USER_AGENT')),
                          'ai':bool(os.environ.get('OPENAI_API_KEY')) and not ON_VERCEL,
                          'telegram':bool(os.environ.get('TELEGRAM_BOT_TOKEN') and os.environ.get('TELEGRAM_CHAT_ID'))},
            'ai_note':'공개 클라우드에서는 유료 AI 호출을 차단하고, 규칙 기반 근거 설명을 제공합니다.',
            'warning':'국내 네이버 비공식 확정 일봉(검증 실패 시 대체 금지), 미국 Yahoo 비공식 종가. 기업가치 검증 전 매매 판단은 보류합니다.'}


@app.get('/api/watchlist')
def list_watchlist():
    if ON_VERCEL: raise HTTPException(status_code=501, detail='Vercel은 브라우저 로컬 관심종목을 사용합니다.')
    with getdb() as con:
        return [dict(r) for r in con.execute('SELECT * FROM watchlist ORDER BY id DESC').fetchall()]


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
    bars,meta=fetch_daily(symbol,market)
    news,filings,research,financials=[],[],[],[]
    news_err,filings_err,research_err,financials_err=None,None,None,None
    try:
        news=fetch_news(name.strip() or symbol,market)
    except (requests.RequestException,ValueError,TypeError) as e:
        news_err='뉴스 수집 실패: '+str(e)[:160]
    try:
        research=fetch_research_mentions(name.strip() or symbol,market)
    except (requests.RequestException,ValueError,TypeError) as e:
        research_err='보고서 관련 보도 검색 실패: '+str(e)[:160]
    try:
        if market=='US':filings=fetch_sec_filings(symbol,os.environ.get('SEC_USER_AGENT',''))
        else: filings=fetch_dart_filings(symbol,os.environ.get('DART_API_KEY',''))
    except (requests.RequestException,ValueError,KeyError,TypeError,Exception) as e:
        filings_err='공시 확인 불가: '+str(e)[:160]
    try:
        financials=fetch_financial_facts(symbol,market,dart_key=os.environ.get('DART_API_KEY',''),
                                         sec_user_agent=os.environ.get('SEC_USER_AGENT',''))
    except (requests.RequestException,ValueError,KeyError,TypeError) as e:
        financials_err='재무계정 확인 불가: '+str(e)[:160]
    result=analyze_bars(bars,market=market,news=news,filings=filings,
                        news_error=news_err,filings_error=filings_err)
    bundle={'symbol':symbol.upper().strip(),'company_name':name or symbol,
            'market':market,'meta':meta,'analysis':result,'news':news,
            'filings':filings,'research':research,'financials':financials,
            'financials_error':financials_err,'news_error':news_err,'filings_error':filings_err,
            'research_error':research_err,
            'chart': [{'date':str(row['date']),'close':round(float(row['close']),4)}
                      for _,row in bars.tail(120).iterrows()]}
    if market in ('KOSPI','KOSDAQ'):
        try:
            bundle['quote']=fetch_naver_quote(symbol)
        except (requests.RequestException,ValueError,KeyError,TypeError) as e:
            bundle['quote_error']='네이버 현재가 미검증: '+str(e)[:150]
    elif kis_configured():
        try:bundle['quote']=fetch_kis_quote(symbol,market)
        except (requests.RequestException,ValueError,KeyError) as e:
            bundle['quote_error']='KIS 시세 조회 실패: '+str(e)[:170]
    else:
        bundle['quote_error']='시세 API 미연결: 확정 종가만 표시합니다.'
    audit=audit_data(market,meta,bars,result,financials,financials_err)
    bundle['data_quality']=audit
    if not audit['verified_for_trade']:
        result['status']=audit['status']
        result['confirmed']=False
    bundle['evidence_pack']=build_evidence_pack(bundle)
    if with_ai and not ON_VERCEL:
        try:bundle['ai_summary']=summarize_with_openai(bundle)
        except (requests.RequestException,ValueError,KeyError) as e:
            bundle['ai_error']='AI 요약 사용 불가: '+str(e)[:180]
    if ON_VERCEL:
        bundle['ai_error']='공개 웹앱에서는 비용 보호를 위해 유료 AI 요약을 비활성화했습니다. 아래 규칙 기반 근거 설명을 이용하세요.'
    return bundle


@app.post('/api/analyze')
def analyze(req:AnalyzeReq):
    check_symbol(req.symbol,req.market)
    try: return run_analysis(req.symbol,req.name,req.market,req.with_ai)
    except (requests.RequestException,ValueError,IndexError,KeyError) as e:
        raise HTTPException(status_code=502,detail='시세 분석 실패: '+str(e)[:250])


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
