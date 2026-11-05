"""Data-quality gate: never advertise technical levels as verified buy instructions."""
from datetime import date,datetime
from zoneinfo import ZoneInfo
from numbers import Real
import math,re


def _financial_period_end(period):
    match=re.fullmatch(r"(20\d{2})년 (사업보고서|3분기|반기|1분기)",period)
    if match:
        ends={"사업보고서":"12-31","3분기":"09-30","반기":"06-30","1분기":"03-31"}
        return date.fromisoformat(match.group(1)+"-"+ends[match.group(2)])
    try:
        parts=period.split(" ~ ")
        if len(parts)==1:return date.fromisoformat(period)
        if len(parts)!=2:return None
        start,end=map(date.fromisoformat,parts)
        return end if start<=end else None
    except ValueError:
        return None


def _financial_blockers(market,financials,error,now):
    blockers=[str(error)[:150]] if error else []
    required=("매출액","영업이익","순이익")
    records=[f for f in financials or [] if isinstance(f,dict) and f.get("name") in required]
    facts={f["name"]:f for f in records}
    if len(facts)!=3:
        blockers.append("재무제표 핵심 3개 계정 조회 미완료")
        return blockers
    if len(records)!=3:
        blockers.append("재무제표 핵심 계정이 중복되어 기준이 불명확합니다.")
    for fact in facts.values():
        value=fact.get("value")
        try:
            valid=isinstance(value,Real) and not isinstance(value,bool) and math.isfinite(value)
        except (ValueError,TypeError,OverflowError):
            valid=False
        if not valid:
            blockers.append("재무제표 핵심 3개 계정의 유효한 숫자 확인 미완료")
            break
    expected_source="SEC" if market=="US" else "OpenDART 또는 네이버증권"
    def supported_source(fact):
        source=fact.get("source")
        if not isinstance(source,str):return False
        if market=="US":return bool(re.match(r"^SEC(?:\s|$)",source.strip()))
        return source=="네이버증권 기업실적분석" or bool(re.match(r"^OpenDART(?:\s|$)",source.strip()))
    if market not in ("KOSPI","KOSDAQ","US") or not all(map(supported_source,facts.values())):
        blockers.append("재무제표 출처가 "+expected_source+"로 확인되지 않았습니다.")
    if any(f.get("source")=="네이버증권 기업실적분석" and f.get("basis_verified") is not True for f in facts.values()):
        blockers.append("네이버증권 재무제표의 연결·별도 기준 미확인")
    if any(f.get("source")=="네이버증권 기업실적분석" and f.get("is_estimate") is not False for f in facts.values()):
        blockers.append("네이버증권 재무제표의 확정 실적 여부 미확인")
    if len({f.get("source") for f in facts.values() if isinstance(f.get("source"),str)})>1:
        blockers.append("재무제표 출처가 혼합되어 같은 기준으로 비교할 수 없습니다.")
    periods=[f.get("period","") for f in facts.values()]
    bases=[f.get("basis","") for f in facts.values()]
    common_period=all(isinstance(p,str) and p.strip() for p in periods) and len({p.strip() for p in periods})==1
    common_basis=all(isinstance(b,str) and b.strip() for b in bases) and len({b.strip() for b in bases})==1
    if not common_period or not common_basis:
        blockers.append("재무제표 기간·연결/별도 기준 불일치 또는 누락")
    expected_unit="USD" if market=="US" else "KRW"
    if any(f.get("unit")!=expected_unit for f in facts.values()):
        blockers.append("재무제표 통화 불일치")
    end=_financial_period_end(periods[0].strip()) if common_period else None
    if end is None or not 0<=(now-end).days<=240:
        blockers.append("재무 회계기간이 오래됐거나 불명확합니다.")
    return blockers


def check(market,meta,bars,analysis,financials,error=None):
    # Only defects in the inputs to the technical calculation hide its output.
    # Missing financial/valuation evidence still prevents a verified trade signal.
    price_blockers=[];warnings=[];now=datetime.now(ZoneInfo("Asia/Seoul")).date()
    if market in ("KOSPI","KOSDAQ") and meta.get("source")!="NAVER":
        price_blockers.append("국내 데이터 출처가 네이버로 검증되지 않았습니다.")
    if market=="US" and meta.get("source")!="YAHOO_US_ONLY":
        price_blockers.append("해외 일봉 출처가 불명확합니다.")
    if market not in ("KOSPI","KOSDAQ","US"):
        price_blockers.append("가격 데이터의 시장 구분이 불명확합니다.")
    if len(bars)<150:price_blockers.append("가격 관측 수 부족")
    try:
        age=(now-date.fromisoformat(str(bars.iloc[-1]["date"]))).days
        if age<0 or age>7:price_blockers.append("최신 확정 종가가 아닙니다.")
    except (IndexError,ValueError,KeyError,TypeError):price_blockers.append("종가 기준일 검증 실패")
    try:
        l=analysis["levels"]
        lo,hi,stop,res=[float(l[k]) for k in ("entry_low","entry_high","stop","target_resistance")]
        if not all(map(math.isfinite,[lo,hi,stop,res])) or not 0<stop<lo<=hi<res:
            price_blockers.append("진입·손절·저항 가격관계 검증 실패")
        rr=float(l["rr"])
        if not math.isfinite(rr) or rr<0:
            price_blockers.append("손익비 계산값 검증 실패")
    except (ValueError,TypeError,KeyError,OverflowError):price_blockers.append("매매 가격 산식 누락 또는 계산값 오류")
    financial_blockers=_financial_blockers(market,financials,error,now)
    if market!="US" and meta.get("adjustment_verified") is not True:
        warnings.append("수정주가·기업행사 조정 여부 미검증")
    warnings.extend(dict.fromkeys(f["fallback_reason"] for f in financials or [] if isinstance(f,dict) and f.get("fallback_reason")))
    warnings.append("최신 가치평가 및 증권사 원문 교차검증 미완료")
    return {"source":meta.get("source","UNKNOWN"),"price_levels_eligible":not price_blockers,
            "price_level_blockers":price_blockers,
            "financials_eligible":not financial_blockers,"financial_blockers":financial_blockers,
            "verified_for_trade":False,"status":"데이터 검증 미완료 · 관망",
            "warnings":price_blockers+financial_blockers+warnings,
            "note":"계산된 기술적 구간은 적정가치나 검증된 매수가가 아닙니다."}
