"""Optional KIS REST quotation snapshots. This module NEVER places orders.

An HTTP quote response does not necessarily certify a live exchange tick.
We deliberately mark any unverified timestamp or US quote as *not alert safe*.
"""
from __future__ import annotations
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
import os, threading, requests

_BASE='https://openapi.koreainvestment.com:9443'
_auth_lock=threading.Lock()
_cached={'access_token':None,'expires_at':datetime.min.replace(tzinfo=timezone.utc)}


def configured() -> bool:
    return bool(os.getenv('KIS_APP_KEY') and os.getenv('KIS_APP_SECRET'))


def _token() -> str:
    if not configured():
        raise ValueError('KIS_APP_KEY와 KIS_APP_SECRET 설정 필요')
    with _auth_lock:
        now=datetime.now(timezone.utc)
        if _cached['access_token'] and now+timedelta(minutes=15)<_cached['expires_at']:
            return _cached['access_token']
        response=requests.post(_BASE+'/oauth2/tokenP', json={
            'grant_type':'client_credentials', 'appkey':os.environ['KIS_APP_KEY'],
            'appsecret':os.environ['KIS_APP_SECRET']},timeout=12)
        response.raise_for_status()
        data=response.json()
        if not data.get('access_token'):
            raise ValueError('KIS 인증 실패: '+str(data.get('msg1') or data.get('error_description') or '응답에 토큰 없음'))
        _cached['access_token']=data['access_token']
        seconds=int(data.get('expires_in') or 86400)
        _cached['expires_at']=now+timedelta(seconds=max(600,seconds))
        return _cached['access_token']


def _get(path:str,tr_id:str,params:dict)->dict:
    response=requests.get(_BASE+path,params=params,headers={
        'content-type':'application/json;charset=utf-8','authorization':'Bearer '+_token(),
        'appkey':os.environ['KIS_APP_KEY'],'appsecret':os.environ['KIS_APP_SECRET'],'tr_id':tr_id,
        'custtype':'P'},timeout=12)
    response.raise_for_status()
    data=response.json()
    if str(data.get('rt_cd'))!='0':
        raise ValueError('KIS 시세 조회 실패: '+str(data.get('msg1') or data.get('msg_cd') or '알 수 없는 오류'))
    return data.get('output') or {}


def _as_float(v):
    try:
        p=float(v)
        return p if p>0 else None
    except (TypeError,ValueError):
        return None


def domestic_timestamp_valid(output:dict, now:datetime|None=None)->tuple[str|None,bool]:
    """Conservatively refuse intraday alarms absent independently dated quote time."""
    now=now or datetime.now(ZoneInfo('Asia/Seoul'))
    day=str(output.get('stck_bsop_date') or '')
    clock=str(output.get('stck_cntg_hour') or '').zfill(6)
    if not (len(day)==8 and day.isdigit() and len(clock)==6 and clock.isdigit()):
        return None,False
    try:
        stamp=datetime.strptime(day+clock,'%Y%m%d%H%M%S').replace(tzinfo=ZoneInfo('Asia/Seoul'))
    except ValueError:
        return None,False
    session=stamp.weekday()<5 and (9,0)<=(stamp.hour,stamp.minute)<=(15,30)
    age=(now-stamp).total_seconds()
    fresh=session and (9,0)<=(now.hour,now.minute)<=(15,30) and 0<=age<=300
    return stamp.isoformat(),fresh


def fetch_kis_quote(symbol:str,market:str)->dict:
    if market in ('KOSPI','KOSDAQ'):
        output=_get('/uapi/domestic-stock/v1/quotations/inquire-price','FHKST01010100',
                    {'FID_COND_MRKT_DIV_CODE':'J','FID_INPUT_ISCD':symbol})
        price=_as_float(output.get('stck_prpr'))
        if price is None: raise ValueError('KIS 국내 시세의 현재가 값이 없습니다.')
        stamped,verified=domestic_timestamp_valid(output)
        return {'price':price,'currency':'KRW','provider':'한국투자증권 KIS REST',
                'fetched_at':datetime.now(timezone.utc).isoformat(), 'exchange_timestamp':stamped,
                'verified_live':verified,'is_delayed':None,
                'note':'REST 조회 시점은 체결 시점과 다릅니다. 거래일·체결시각이 모두 검증되고 5분 이내일 때만 장중 감시 기준으로 사용합니다.'}
    if market!='US': raise ValueError('지원하지 않는 시장입니다.')
    # Exchange depends on listing. Query choices; never substitute a quote from a different symbol.
    last_error=None
    for exchange in ('NAS','NYS','AMS'):
        try:
            output=_get('/uapi/overseas-price/v1/quotations/price','HHDFS00000300',
                        {'AUTH':'','EXCD':exchange,'SYMB':symbol})
            price=_as_float(output.get('last'))
            if price is None: continue
            return {'price':price,'currency':'USD','provider':'한국투자증권 KIS 해외시세 REST',
                    'fetched_at':datetime.now(timezone.utc).isoformat(),'exchange_timestamp':None,
                    'verified_live':False,'is_delayed':True,'exchange':exchange,
                    'note':'미국 주식 REST 응답은 실시간 체결가로 검증되지 않았으며 지연될 수 있습니다. 장중 자동 가격 도달 알림의 근거로 사용하지 않습니다.'}
        except (requests.RequestException,ValueError) as e:
            last_error=e
    raise ValueError('KIS 해외 시세를 찾지 못했습니다: '+str(last_error or '해당 거래소 심볼 없음'))
