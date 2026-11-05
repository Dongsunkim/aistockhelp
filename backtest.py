"""Strictly illustrative technical-only backtest; excludes ALL news/filing gates.

Signal is measured at a completed closing bar. An entry can happen only at
next day's open *within* the precomputed entry range. Stops fill pessimistically.
"""
from __future__ import annotations
from datetime import datetime, time
import math
import pandas as pd
from analysis_engine import analyze_bars


def backtest_technical(bars:pd.DataFrame, *, market:str, horizon:int=10,
                       slippage_bps:float=10, round_trip_fees_bps:float=15)->dict:
    if horizon not in (5,10,20):raise ValueError('검증 기간은 5·10·20거래일 중 선택하세요.')
    if len(bars)<180:raise ValueError('백테스트에는 최소 180거래일이 필요합니다.')
    if 'open' not in bars.columns:raise ValueError('백테스트용 시가 데이터가 없습니다.')
    d=bars.reset_index(drop=True).copy()
    for col in ('open','high','low','close','volume'):
        d[col]=pd.to_numeric(d[col],errors='coerce')
    results=[]
    idx=150
    while idx<len(d)-horizon-1:
        known=d.iloc[:idx+1]
        if known[['open','high','low','close','volume']].tail(150).isna().any().any():
            idx+=1;continue
        asof=datetime.combine(pd.to_datetime(d.at[idx,'date']).date(),time(23,0))
        try:
            s=analyze_bars(known,market=market,news=[],filings=[],asof=asof)
        except (ValueError,TypeError,KeyError):
            idx+=1;continue
        # Do not fabricate historical news/filings: ONLY six technical checks.
        if not all(x['pass'] for x in s['conditions']):
            idx+=1;continue
        levels=s['levels']
        next_open=float(d.at[idx+1,'open'])
        if not (levels['entry_low']<=next_open<=levels['entry_high']):
            idx+=1;continue
        stop=levels['stop'];target=levels['target_resistance']
        buy=next_open*(1+slippage_bps/10000)
        if not (0<stop<buy<target):idx+=1;continue
        exit_price=None;exit_reason='기간 종료';exit_idx=min(idx+horizon,len(d)-1)
        for j in range(idx+1,exit_idx+1):
            op,low,high=(float(d.at[j,k]) for k in ('open','low','high'))
            # If both stop/target touched in one candle, take the stop first.
            if op<=stop:
                exit_price=op;exit_reason='손절 갭 하락';exit_idx=j;break
            if low<=stop:
                exit_price=stop;exit_reason='손절';exit_idx=j;break
            if high>=target:
                exit_price=target;exit_reason='저항 도달';exit_idx=j;break
        if exit_price is None:exit_price=float(d.at[exit_idx,'close'])
        sell=exit_price*(1-slippage_bps/10000)
        pnl_pct=((sell/buy)-1)*100-round_trip_fees_bps/100
        results.append({'signal_date':str(d.at[idx,'date']),
                        'entry_date':str(d.at[idx+1,'date']),
                        'exit_date':str(d.at[exit_idx,'date']),
                        'entry':round(buy,4),'exit':round(sell,4),
                        'net_return_pct':round(pnl_pct,3),'reason':exit_reason,
                        'signal_entry_low':round(levels['entry_low'],4),
                        'signal_entry_high':round(levels['entry_high'],4)})
        idx=exit_idx+1  # conservative one-at-a-time simulation
    n=len(results)
    equity=1.0;peak=1.0;drawdown=0.0
    for trade in results:
        equity*=max(0.0,1+trade['net_return_pct']/100)
        peak=max(peak,equity)
        drawdown=min(drawdown,(equity/peak-1)*100)
    return {'market':market,'horizon_sessions':horizon,'observations':len(d),
            'start_date':str(d.at[0,'date']),'end_date':str(d.at[len(d)-1,'date']),
            'total_trades':n,'win_rate_pct':round(100*sum(t['net_return_pct']>0 for t in results)/n,1) if n else None,
            'avg_return_pct':round(sum(t['net_return_pct'] for t in results)/n,2) if n else None,
            'cumulative_return_pct':round((equity-1)*100,2) if n else None,
            'max_drawdown_pct':round(drawdown,2) if n else None,
            'round_trip_fees_bps':round_trip_fees_bps,'slippage_per_side_bps':slippage_bps,
            'trades':results[-30:],
            'notes':['기술지표 6개 조건만 과거에 시험한 실험입니다. 뉴스·공시·보고서의 과거 시점 확인을 재구성하지 않았으므로 실전 매수 신호 전체의 백테스트가 아닙니다.',
                     '해당 날짜의 종가로 조건을 계산하고 다음 거래일 시가가 매수가 범위 안에 있을 때만 진입합니다. 손절·저항이 같은 봉에서 동시에 닿으면 손절을 우선합니다.',
                     '수수료(왕복 0.15%)와 슬리피지(매수·매도 각 0.10%)를 가정하며, 환율·세금·유동성·데이터 오류·기업행사 조정은 미반영입니다.',
                     '거래가 적거나 0건이면 유효성이 입증된 것으로 볼 수 없습니다. 과최적화와 표본선택 편향을 경계해야 합니다.']}
