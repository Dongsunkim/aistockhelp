"""Conservative, explainable daily-bar signals. No order execution or return promises."""
from __future__ import annotations
from datetime import datetime
import math
import pandas as pd
import numpy as np


def _f(x):
    return round(float(x), 4) if pd.notna(x) and np.isfinite(float(x)) else None


def rsi(close: pd.Series, n: int = 14) -> pd.Series:
    delta = close.diff()
    gain = delta.clip(lower=0).ewm(alpha=1/n, adjust=False, min_periods=n).mean()
    loss = (-delta.clip(upper=0)).ewm(alpha=1/n, adjust=False, min_periods=n).mean()
    rel = gain / loss.replace(0, np.nan)
    out = 100 - 100 / (1+rel)
    out[(loss == 0) & (gain > 0)] = 100
    out[(gain == 0) & (loss > 0)] = 0
    return out


def analyze_bars(bars: pd.DataFrame, *, market: str, news: list | None = None,
                 filings: list | None = None, news_error: str | None = None,
                 filings_error: str | None = None, asof: datetime | None = None) -> dict:
    """Takes *completed* daily OHLCV bars, never an unclosed candle."""
    if len(bars) < 150:
        raise ValueError(f"최소 150개 거래일의 종가 데이터가 필요합니다. 현재 {len(bars)}개입니다.")
    for key in ('close','high','low','volume'):
        if key not in bars.columns:
            raise ValueError(f"가격 데이터에 '{key}' 열이 없습니다.")
    d = bars.copy().reset_index(drop=True)
    for key in ('close','high','low','volume'):
        d[key] = pd.to_numeric(d[key], errors='coerce')
    if d[['close','high','low','volume']].tail(120).isna().any().any() or (d['close'].tail(120)<=0).any():
        raise ValueError("최근 가격 데이터에 결측치 또는 비정상 값이 있습니다.")
    c,h,l,v = d['close'],d['high'],d['low'],d['volume']
    ma20,ma60,ma120 = [c.rolling(n).mean() for n in (20,60,120)]
    prevclose=c.shift(1)
    tr=pd.concat([(h-l), (h-prevclose).abs(), (l-prevclose).abs()],axis=1).max(axis=1)
    atr=tr.rolling(14).mean()
    rs=rsi(c)
    close, a, m20, m60, m120, r = (float(s.iloc[-1]) for s in (c,atr,ma20,ma60,ma120,rs))
    if not all(map(math.isfinite,[close,a,m20,m60,m120,r])) or a <= 0:
        raise ValueError('지표 계산에 필요한 데이터가 부족하거나 변동성이 비정상적입니다.')
    avgvol=float(v.iloc[-21:-1].mean())
    volratio=float(v.iloc[-1]/avgvol) if avgvol > 0 else 0.0
    atr_pct=a/close*100
    # These levels are *technical hypotheses* based on observed averages and true range.
    entry_low=max(0.01,m20-0.35*a)
    entry_high=m20+0.15*a
    stop=min(float(l.tail(10).min()),m60)-0.25*a
    resistance=float(h.tail(60).quantile(0.90))  # reduces single-wick outlier bias
    risk=entry_high-stop
    reward=resistance-entry_high
    rr=(reward/risk) if risk>0 and reward>0 else 0.0
    conditions=[
        {'id':'trend','name':'중기 상승 추세', 'pass':bool(m20>m60>m120 and close>m60), 'detail':f'MA20 {m20:,.2f} > MA60 {m60:,.2f} > MA120 {m120:,.2f}; 현재가 {close:,.2f}'},
        {'id':'pullback','name':'추격매수 방지 / 매수가 범위', 'pass':bool(entry_low<=close<=entry_high), 'detail':f'최근 종가가 진입 검토 구간 {entry_low:,.2f}~{entry_high:,.2f} 안에 있어야 함'},
        {'id':'rsi','name':'과열 회피', 'pass':bool(42<=r<=62), 'detail':f'RSI(14) {r:.1f} (허용 42~62)'},
        {'id':'volatility','name':'과도한 변동성 회피', 'pass':bool(atr_pct<=4.5), 'detail':f'ATR(14)/종가 {atr_pct:.2f}% (상한 4.5%)'},
        {'id':'volume','name':'이상 거래량 경계', 'pass':bool(0.8<=volratio<=2.5), 'detail':f'거래량 / 직전 20일 평균 {volratio:.2f}배 (0.8~2.5배)'},
        {'id':'rr','name':'최소 손익비 확보', 'pass':bool(stop>0 and rr>=2), 'detail':f'60일 고가 90분위 {resistance:,.2f}, 손절 추정 {stop:,.2f}, 손익비 1:{rr:.2f} (최소 1:2)'},
    ]
    now=asof or datetime.now().astimezone()
    # recent published evidence is required for a conservative confirmed watchlist signal
    recent_news=[]
    red_flags=[]
    suspicious=('횡령','배임','거래정지','상장폐지','영업정지','회계부정','어닝쇼크','실적쇼크','파산','유상증자','소송','수사',
                'bankrupt','fraud','sec probe','investigation','restatement','offering','accounting irregularity',
                'guidance cut','earnings miss','downgrade','recall','delisting')
    for item in news or []:
        hours=item.get('age_hours')
        if hours is not None and 0 <= hours <=72:
            recent_news.append(item)
            if any(word in (item.get('title') or '').lower() for word in suspicious):
                red_flags.append(item['title'])
    fresh_filings=[f for f in filings or [] if f.get('age_days') is not None and 0<=f['age_days']<=120]
    for item in fresh_filings:
        if any(word in (item.get('title') or '').lower() for word in suspicious):
            red_flags.append(item.get('title') or '')
    news_ok=not news_error and len(recent_news)>0
    filings_ok=not filings_error and len(fresh_filings)>0
    # The age is computed for the exchange's local market calendar; a five-calendar-day
    # ceiling tolerates weekends but not prolonged missing data.
    data_age_days=None
    if 'date' in d:
        try:
            data_age_days=max(0,(now.date()-pd.to_datetime(d['date'].iloc[-1]).date()).days)
        except Exception:
            pass
    freshness_ok=data_age_days is not None and data_age_days<=5
    technical_ok=all(item['pass'] for item in conditions)
    if not freshness_ok:
        status='데이터 확인 필요'
    elif not technical_ok:
        status='관망'
    elif red_flags or not news_ok or not filings_ok:
        status='근거 확인 전 보류'
    else:
        # A headline and a filing list are not verified *contents*. Do not claim confirmed buy.
        status='기술적 매수 관찰 · 원문 확인 필요'
    evidence=[
        {'name':'종가 데이터 최근 5일 이내', 'pass':bool(freshness_ok), 'detail':f'최종 확정 봉: {d["date"].iloc[-1] if "date" in d else "미상"}'},
        {'name':'최근 72시간 내 뉴스', 'pass':bool(news_ok), 'detail':news_error or f'{len(recent_news)}건 (제목만 확인; 기사 전문 미검증)'},
        {'name':'최근 120일 내 공식 공시', 'pass':bool(filings_ok), 'detail':filings_error or f'{len(fresh_filings)}건 (원문 내용 미검증)'},
        {'name':'위험 키워드 기사 없음', 'pass':not bool(red_flags) and bool(news_ok),
         'detail':f'위험 키워드 감지 {len(red_flags)}건; 오탐 가능성 있어 원문 확인 필요' if red_flags else '제목 기반 감지 — 실제 위험 부재를 보장하지 않음'},
    ]
    return {
      'status':status,'close':_f(close),'date':str(d['date'].iloc[-1]) if 'date' in d else '',
      'data_age_days':data_age_days,
      'levels':{'entry_low':_f(entry_low),'entry_high':_f(entry_high),'stop':_f(stop),
                'target_resistance':_f(resistance),'rr':_f(rr)},
      'indicators':{'ma20':_f(m20),'ma60':_f(m60),'ma120':_f(m120),'rsi14':_f(r),
                    'atr14':_f(a),'atr_pct':_f(atr_pct),'volume_ratio':_f(volratio)},
      'conditions':conditions,'evidence_checks':evidence,'red_flags':red_flags,
      'technical_ok':technical_ok,'confirmed':False,
      'method':{
        'entry':'진입 하단 = 20일 이동평균 − 0.35×14일 ATR, 상단 = 20일 이동평균 + 0.15×ATR',
        'stop':'손절 기준 = min(최근 10거래일 저점, 60일 이동평균) − 0.25×ATR',
        'target':'1차 저항 = 최근 60거래일 고가의 90분위수(단발성 급등 꼬리 영향 완화); 목표수익 보장 아님',
        'risk_reward':'손익비 = (60일 고가 90분위−진입 상단) / (진입 상단−손절 기준)',
        'caveat':'기술적 조건으로 산출한 관찰 구간일 뿐 내재가치·적정가치 추정치가 아닙니다. 수수료·세금·환율·슬리피지 미반영.'
      },
    }
