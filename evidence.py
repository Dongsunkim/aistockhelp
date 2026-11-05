"""Traceable citations for *available* financial evidence, not verified PDF or article interpretation."""
from __future__ import annotations
from datetime import datetime, timezone
from urllib.parse import quote


def _link(url):
    return url if isinstance(url, str) and url.startswith(('https://','http://')) else ''


def build_evidence_pack(payload):
    """Create a typed, clickable provenance index without inventing source facts.

    level specifies what the app really read, not what the document would show if opened.
    """
    market = payload['market']
    symbol = payload['symbol']
    ticker = symbol if market == 'US' else f'{symbol}.{ "KS" if market == "KOSPI" else "KQ"}'
    analysis = payload['analysis']
    meta = payload.get('meta') or {}
    docs = [{
        'id':'P1', 'kind':'가격 데이터', 'tier':'기술 데이터', 'title':f'{ticker} 확정 일봉 / OHLCV',
        'source':meta.get('provider') or '시세 제공원', 'published_at':str(analysis.get('date') or ''),
        'url':f'https://finance.yahoo.com/quote/{quote(ticker, safe="")}/history/',
        'level':'가격·거래량 계산에 사용',
        'why':'20·60·120일 이동평균선, RSI, ATR, 지지·저항, 매수 검토 구간의 수치적 입력값',
        'warning':'비공식 종가 데이터입니다. 체결가·배당조정·기업행사 적용 여부를 증권사와 대조해야 합니다.'
    }]
    for i,filing in enumerate(payload.get('filings',[])[:10],1):
        url=_link(filing.get('link'))
        if not url: continue
        docs.append({'id':f'F{i}', 'kind':'공식 공시', 'tier':'1차 공식 자료',
                     'title':filing.get('title') or '제목 미상', 'source':filing.get('source') or '공시기관',
                     'published_at':filing.get('date') or '', 'url':url,
                     'level':'공시 목록과 제출일 확인',
                     'why':'실적·경영위험·자금조달 등 매매 위험을 원문에서 대조할 자료',
                     'warning':'원문 내용을 앱이 열람·검증한 것은 아닙니다.'})
    for i,item in enumerate(payload.get('financials',[])[:10],1):
        url=_link(item.get('link'))
        if not url: continue
        docs.append({'id':f'A{i}', 'kind':'공식 재무 수치', 'tier':'1차 공식 자료',
                     'title':f'{item.get("name", "재무계정")} · {item.get("period", "기간 미상")}',
                     'source':item.get('source') or '공시기관',
                     'published_at':item.get('reported_at') or item.get('period') or '', 'url':url,
                     'level':'일부 표준화된 수치 조회',
                     'why':'최근 매출·영업이익 등 발표 수치를 평가할 참고자료(매수가 직접 산출에는 미반영)',
                     'warning':f'{item.get("basis") or "연결/별도와 기간 확인 필요"}; 계정 혼동 주의'})
    for i,item in enumerate(payload.get('news',[])[:10],1):
        url=_link(item.get('link'))
        if not url: continue
        docs.append({'id':f'N{i}', 'kind':'최신 뉴스', 'tier':'2차 보도',
                     'title':item.get('title') or '제목 미상', 'source':item.get('source') or '뉴스',
                     'published_at':item.get('published_at') or '', 'url':url,
                     'level':'제목·발행일·매체만 수집',
                     'why':'최근 사건·시장 분위기를 파악할 참고 링크; 부정적 제목은 보수적 경고 조건',
                     'warning':'뉴스 전문·진위·개별 종목 관련성을 검증하지 않았습니다.'})
    for i,item in enumerate(payload.get('research',[])[:8],1):
        url=_link(item.get('link'))
        if not url: continue
        docs.append({'id':f'R{i}', 'kind':'리포트 관련 보도', 'tier':'3차 참고 자료',
                     'title':item.get('title') or '제목 미상', 'source':item.get('source') or '보도',
                     'published_at':item.get('published_at') or '', 'url':url,
                     'level':'리포트 원문 미확인',
                     'why':'증권사 전망 및 목표주가 변화 여부를 별도 원문과 비교하기 위한 후보 자료',
                     'warning':'보고서 원본 PDF를 읽은 것으로 간주하지 않습니다. 목표가를 매수가로 사용하지 않습니다.'})
    levels=analysis['levels']
    passes=sum(bool(c['pass']) for c in analysis['conditions'])
    total=len(analysis['conditions'])
    primary=len([d for d in docs if d['kind'] in ('공식 공시','공식 재무 수치')])
    return {
        'generated_at':datetime.now(timezone.utc).isoformat(), 'documents':docs,
        'source_counts':{'price':1,'primary':primary,'news':len([d for d in docs if d['kind']=='최신 뉴스']),
                         'research_mentions':len([d for d in docs if d['kind']=='리포트 관련 보도'])},
        'price_reason':[
            f'확정 종가 기반 기술 조건 {passes}/{total}개 충족. {analysis["status"]} 상태입니다.',
            f'매수 관찰 구간 {levels["entry_low"]:,.2f}~{levels["entry_high"]:,.2f}은 20일 이동평균선과 ATR 변동성으로 계산한 가설입니다.',
            f'손절 가설 {levels["stop"]:,.2f}, 저항 가설 {levels["target_resistance"]:,.2f}; 기대 손익비 1:{levels["rr"]:,.2f}입니다.',
            f'공식 공시·재무 참고문서 {primary}건, 기사 {len([d for d in docs if d["kind"]=="최신 뉴스"])}건, 리포트 관련 보도 {len([d for d in docs if d["kind"]=="리포트 관련 보도"])}건을 연결했습니다.',
            '공시·뉴스·리포트 전문 검증과 기업 내재가치 평가는 아직 수행하지 않았습니다. 원문 확인 전 매매 확정 금지.'
        ],
        'methodology_url':'/static/evidence-guide.html',
        'limitations': ['관찰 가격은 기업의 적정가치가 아닌 기술적 산식 결과입니다.',
                        '뉴스·공시·보고서 제목과 공개 수치만 조회했으며 독립적으로 진실을 검증하지 않았습니다.',
                        '종목별 데이터 취득·발행시점이 다를 수 있고, 속보성 뉴스 및 실시간 체결가는 보장하지 않습니다.']
    }
