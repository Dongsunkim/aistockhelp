"""Data-quality gate: never advertise technical levels as verified buy instructions."""
from datetime import date,datetime
from zoneinfo import ZoneInfo
import math,re

def check(market,meta,bars,analysis,financials,error=None):
    errors=[];now=datetime.now(ZoneInfo("Asia/Seoul")).date()
    if market in ("KOSPI","KOSDAQ") and meta.get("source")!="NAVER":
        errors.append("국내 데이터 출처가 네이버로 검증되지 않았습니다.")
    if market=="US" and meta.get("source")!="YAHOO_US_ONLY":
        errors.append("해외 일봉 출처가 불명확합니다.")
    if len(bars)<150:errors.append("가격 관측 수 부족")
    try:
        age=(now-date.fromisoformat(str(bars.iloc[-1]["date"]))).days
        if age<0 or age>7:errors.append("최신 확정 종가가 아닙니다.")
    except (IndexError,ValueError,KeyError):errors.append("종가 기준일 검증 실패")
    try:
        l=analysis["levels"]
        lo,hi,stop,res=[float(l[k]) for k in ("entry_low","entry_high","stop","target_resistance")]
        if not all(map(math.isfinite,[lo,hi,stop,res])) or not 0<stop<lo<=hi<res:
            errors.append("진입·손절·저항 가격관계 검증 실패")
    except (ValueError,TypeError,KeyError):errors.append("매매 가격 산식 누락")
    if error:errors.append(str(error)[:150])
    facts={f.get("name"):f for f in financials or [] if f.get("name") in ("매출액","영업이익","순이익")}
    if len(facts)!=3:errors.append("재무제표 핵심 3개 계정 조회 미완료")
    else:
        if len({f.get("period") for f in facts.values()})!=1 or len({f.get("basis") for f in facts.values()})!=1:
            errors.append("재무제표 기간·연결/별도 기준 불일치")
        if {f.get("unit") for f in facts.values()}!={"KRW" if market!="US" else "USD"}:
            errors.append("재무제표 통화 불일치")
        periods=list(facts.values())[0].get("period","")
        m=re.search(r"(20\d{2})년 (사업보고서|3분기|반기|1분기)",periods)
        if m:
            ends={"사업보고서":"12-31","3분기":"09-30","반기":"06-30","1분기":"03-31"}
            end=date.fromisoformat(m.group(1)+"-"+ends[m.group(2)])
        else:
            try:end=date.fromisoformat(periods.rsplit(" ~ ",1)[-1])
            except ValueError:end=None
        if end is None or (now-end).days>240 or (now-end).days<0:
            errors.append("재무 회계기간이 오래됐거나 불명확합니다.")
    if market!="US" and meta.get("adjustment_verified") is not True:
        errors.append("수정주가·기업행사 조정 여부 미검증")
    errors.append("최신 가치평가 및 증권사 원문 교차검증 미완료")
    eligible=not any(("진입·손절" in x or "종가" in x or "일봉" in x or "재무" in x or "가격 관측" in x) for x in errors)
    return {"source":meta.get("source","UNKNOWN"),"price_levels_eligible":eligible,
            "verified_for_trade":False,"status":"데이터 검증 미완료 · 관망",
            "warnings":errors,
            "note":"계산된 기술적 구간은 적정가치나 검증된 매수가가 아닙니다."}
