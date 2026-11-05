"""Naver aggregate values never masquerade as directly verified filing facts."""
import pytest
from data_quality import check
from tests.test_recovery_accuracy import quality_inputs


def naver_inputs():
    meta, bars, analysis, facts = quality_inputs('KOSPI')
    for fact in facts:
        fact.update(source='네이버증권 기업실적분석', basis_verified=True,
                    is_estimate=False, link='https://finance.naver.com/item/main.naver?code=005930')
    return meta, bars, analysis, facts


def test_actual_comparable_naver_values_are_eligible_but_not_trade_verified():
    meta, bars, analysis, facts = naver_inputs()
    result = check('KOSPI', meta, bars, analysis, facts)
    assert result['financials_eligible']
    assert result['price_levels_eligible']
    assert not result['verified_for_trade']


@pytest.mark.parametrize('field,value,reason', [
    ('basis_verified', False, '연결·별도'),
    ('is_estimate', True, '확정 실적'),
    ('is_estimate', None, '확정 실적'),
    ('value', float('nan'), '숫자'),
    ('unit', '억원', '통화'),
    ('source', 'OpenDART 주요 재무계정', '출처가 혼합'),
])
def test_uncomparable_naver_data_blocks_favorable_readiness_not_price_display(field, value, reason):
    meta, bars, analysis, facts = naver_inputs()
    facts[-1][field] = value
    result = check('KOSPI', meta, bars, analysis, facts)
    assert not result['financials_eligible']
    assert any(reason in blocker for blocker in result['financial_blockers'])
    assert result['price_levels_eligible']
    assert not result['verified_for_trade']
