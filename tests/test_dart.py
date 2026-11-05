"""OpenDART transport tests: no key, network or live financial data required."""
from datetime import date
import io
import zipfile

import pytest
import requests

import dart_client as dart

KEY = 'DO_NOT_EXPOSE_TEST_KEY'
COMPANY = {'corp_code': '00126380', 'corp_name': '삼성전자', 'stock_code': '005930'}


class Response:
    def __init__(self, payload=None, content=b''):
        self.payload = payload
        self.content = content

    def raise_for_status(self):
        pass

    def json(self):
        if isinstance(self.payload, Exception):
            raise self.payload
        return self.payload


def registry_zip(xml=None):
    xml = xml or ('<result><list><corp_code>00126380</corp_code>'
                  '<corp_name>삼성전자</corp_name><stock_code>005930</stock_code>'
                  '</list></result>')
    content = io.BytesIO()
    with zipfile.ZipFile(content, 'w', zipfile.ZIP_DEFLATED) as archive:
        archive.writestr('CORPCODE.xml', xml)
    return content.getvalue()


@pytest.fixture(autouse=True)
def fixed_context(monkeypatch):
    dart._REGISTRY_CACHE.clear()
    monkeypatch.setattr(dart, '_today', lambda: date(2026, 11, 5))
    yield
    dart._REGISTRY_CACHE.clear()


def transport(monkeypatch, handler):
    calls = []

    def get(url, *, params, timeout):
        assert url.startswith(dart.BASE_URL)
        assert params['crtfc_key'] == KEY
        assert timeout == (5, 25)
        path = url.removeprefix(dart.BASE_URL)
        calls.append((path, dict(params)))
        if path == 'corpCode.xml':
            return Response(content=registry_zip())
        return handler(path, params)

    monkeypatch.setattr(dart._SESSION, 'get', get)
    return calls


def account(sheet='BS', **overrides):
    row = {'corp_code': COMPANY['corp_code'], 'bsns_year': '2025',
           'reprt_code': '11011', 'rcept_no': '20260316000001',
           'sj_div': sheet, 'account_id': 'ifrs-full_Assets',
           'account_nm': '자산총계', 'account_detail': '연결재무제표 [member]',
           'thstrm_nm': '제 57 기', 'thstrm_amount': '9,007,199,254,740,993',
           'thstrm_add_amount': '', 'frmtrm_nm': '제 56 기',
           'frmtrm_amount': '-123,456', 'bfefrmtrm_amount': '0', 'currency': 'KRW'}
    row.update(overrides)
    return row


def filing(index=0):
    return {'corp_code': COMPANY['corp_code'], 'corp_name': COMPANY['corp_name'],
            'stock_code': '005930', 'corp_cls': 'Y',
            'report_nm': '사업보고서 (2025.12)', 'rcept_no': f'20261105{index:06d}',
            'flr_nm': '삼성전자', 'rcept_dt': '20261105', 'rm': '유'}


def test_all_statement_sheets_both_bases_and_exact_values(monkeypatch):
    rows = [account(sheet) for sheet in ('BS', 'IS', 'CIS', 'CF', 'SCE')]
    calls = transport(monkeypatch, lambda path, params: Response({'status': '000', 'list': rows}))
    result = dart.fetch_dart_financials('005930', KEY, year=2025, report_code='11011')
    assert result['company'] == COMPANY
    assert result['complete']
    assert result['report_label'] == '사업보고서'
    assert [x['fs_div'] for x in result['statements']] == ['CFS', 'OFS']
    for statement in result['statements']:
        assert statement['status'] == 'available'
        assert {r['sj_div'] for r in statement['accounts']} == dart.SHEETS
        for row in statement['accounts']:
            assert row['fs_div'] == statement['fs_div']
            assert row['thstrm_amount'] == '9,007,199,254,740,993'
            assert row['frmtrm_amount'] == '-123,456'
            assert row['account_detail'] == '연결재무제표 [member]'
            assert row['link'].endswith('rcpNo=20260316000001')
    assert [c[0] for c in calls] == ['corpCode.xml', 'fnlttSinglAcntAll.json', 'fnlttSinglAcntAll.json']
    assert [c[1]['fs_div'] for c in calls[1:]] == ['CFS', 'OFS']
    assert KEY not in str(result)


def test_registry_reused_for_same_key(monkeypatch):
    calls = transport(monkeypatch, lambda *args: Response({'status': '013'}))
    for _ in range(2):
        result = dart.fetch_dart_financials('005930', KEY, year=2025,
                                           report_code='11011', fs_div='OFS')
        assert result['complete'] and result['statements'][0]['status'] == 'no_data'
    assert len([x for x in calls if x[0] == 'corpCode.xml']) == 1
    assert KEY not in str(dart._REGISTRY_CACHE)


def test_partial_failure_is_explicit_and_not_complete(monkeypatch):
    def response(path, params):
        return Response({'status': '000', 'list': [account()]}) if params['fs_div'] == 'CFS' else Response({'status': '020', 'message': KEY})
    transport(monkeypatch, response)
    result = dart.fetch_dart_financials('005930', KEY, year=2025, report_code='11011')
    assert not result['complete']
    assert result['statements'][0]['status'] == 'available'
    assert result['statements'][1]['status'] == 'error'
    assert '한도' in result['statements'][1]['message']
    assert KEY not in str(result)


@pytest.mark.parametrize('row', [account(fs_div='OFS'), account(bsns_year='2024'),
                                 account(corp_code='99999999'), account(sj_div='OTHER'),
                                 account(thstrm_amount=9007199254740993),
                                 account(rcept_no='<script>'), {}])
def test_reject_mixed_or_malformed_statement_rows(monkeypatch, row):
    transport(monkeypatch, lambda *args: Response({'status': '000', 'list': [row]}))
    with pytest.raises(dart.DartError):
        dart.fetch_dart_financials('005930', KEY, year=2025, report_code='11011', fs_div='CFS')


@pytest.mark.parametrize('field', ['corp_code', 'bsns_year', 'reprt_code', 'rcept_no'])
def test_statement_identity_period_and_receipt_are_required(monkeypatch, field):
    row = account()
    del row[field]
    transport(monkeypatch, lambda *args: Response({'status': '000', 'list': [row]}))
    with pytest.raises(dart.DartError):
        dart.fetch_dart_financials('005930', KEY, year=2025, report_code='11011', fs_div='CFS')


@pytest.mark.parametrize('payload', [{'status': '000'}, {'status': '000', 'list': []},
                                     {'status': '000', 'list': 'invalid'}, {}, [],
                                     ValueError('secret ' + KEY),
                                     {'status': '901', 'message': KEY}])
def test_bad_financial_payload_is_safe_failure(monkeypatch, payload):
    transport(monkeypatch, lambda *args: Response(payload))
    with pytest.raises(dart.DartError) as exc:
        dart.fetch_dart_financials('005930', KEY, year=2025, report_code='11011')
    assert KEY not in str(exc.value)


@pytest.mark.parametrize('kwargs', [dict(symbol='AAPL'), dict(api_key=''),
                                     dict(year=2014), dict(year=2027), dict(year=True),
                                     dict(report_code='bogus'), dict(report_code=[]),
                                     dict(fs_div='ALL')])
def test_financial_input_validation_precedes_network(monkeypatch, kwargs):
    def fail(*args, **kw):
        pytest.fail('Network must not run for invalid input')
    monkeypatch.setattr(dart._SESSION, 'get', fail)
    args = dict(symbol='005930', api_key=KEY, year=2025, report_code='11011')
    args.update(kwargs)
    with pytest.raises(dart.DartError):
        dart.fetch_dart_financials(**args)


def test_disclosure_pages_keep_all_categories_and_metadata(monkeypatch):
    def response(path, params):
        assert path == 'list.json'
        assert params['bgn_de'] == '20260101' and params['end_de'] == '20261105'
        assert params['page_count'] == 100
        assert 'pblntf_ty' not in params  # No category filter hides disclosures.
        page = params['page_no']
        rows = [filing(i) for i in range(100)] if page == 1 else [filing(100)]
        return Response({'status': '000', 'page_no': page, 'page_count': 100,
                         'total_count': 101, 'total_page': 2, 'list': rows})
    transport(monkeypatch, response)
    first = dart.fetch_dart_disclosures('005930', KEY, begin='2026-01-01', end='2026-11-05')
    second = dart.fetch_dart_disclosures('005930', KEY, begin='2026-01-01', end='2026-11-05', page=2)
    assert len(first['items']) == 100 and first['has_more'] and not first['complete']
    assert len(second['items']) == 1 and not second['has_more'] and second['complete']
    assert first['total_count'] == second['total_count'] == 101
    item = second['items'][0]
    assert item['title'] == item['report_nm']
    assert item['corp_cls'] == 'Y' and item['flr_nm'] == '삼성전자'
    assert item['rm'] == '유' and item['date'] == '2026-11-05'
    assert item['age_days'] == 0
    assert item['link'].endswith(item['rcept_no'])


def test_disclosure_no_data(monkeypatch):
    transport(monkeypatch, lambda *args: Response({'status': '013', 'message': '조회된 데이타가 없습니다.'}))
    result = dart.fetch_dart_disclosures('005930', KEY, begin='2026-01-01', end='2026-11-05')
    assert result['items'] == [] and result['total_count'] == 0 and result['complete']
    with pytest.raises(dart.DartError, match='페이지'):
        dart.fetch_dart_disclosures('005930', KEY, begin='2026-01-01', end='2026-11-05', page=2)


@pytest.mark.parametrize('changes', [dict(total_count=2), dict(total_page=2),
                                      dict(page_no=2), dict(page_count=10),
                                      dict(total_count=-1), dict(list=[]),
                                      dict(list=[filing(0), filing(0)], total_count=2),
                                      dict(list=[{**filing(), 'corp_code': '99999999'}]),
                                      dict(list=[{**filing(), 'rcept_dt': '20251105'}]),
                                      dict(list=[{**filing(), 'rcept_no': 'bad'}])])
def test_disclosure_metadata_and_content_validation(monkeypatch, changes):
    payload = {'status': '000', 'page_no': 1, 'page_count': 100,
               'total_count': 1, 'total_page': 1, 'list': [filing()]}
    payload.update(changes)
    transport(monkeypatch, lambda *args: Response(payload))
    with pytest.raises(dart.DartError):
        dart.fetch_dart_disclosures('005930', KEY, begin='2026-01-01', end='2026-11-05')


@pytest.mark.parametrize('kwargs', [dict(begin='2026-11-06'), dict(begin='2024-01-01'),
                                     dict(begin='2026-02-30'), dict(begin='20260101'),
                                     dict(end='2026-11-06'), dict(page=0), dict(page=1001),
                                     dict(page=True)])
def test_disclosure_input_validation_precedes_network(monkeypatch, kwargs):
    def fail(*args, **kw):
        pytest.fail('Network must not run for invalid input')
    monkeypatch.setattr(dart._SESSION, 'get', fail)
    args = dict(begin='2026-01-01', end='2026-11-05')
    args.update(kwargs)
    with pytest.raises(dart.DartError):
        dart.fetch_dart_disclosures('005930', KEY, **args)


@pytest.mark.parametrize('content', [b'not a zip', registry_zip('<result/>'),
                                     registry_zip('<broken>'),
                                     registry_zip('<!DOCTYPE result [<!ENTITY x "bad">]><result/>')])
def test_invalid_registry_never_proceeds_to_financial_api(monkeypatch, content):
    def get(url, **kwargs):
        assert url.endswith('/corpCode.xml')
        return Response(content=content)
    monkeypatch.setattr(dart._SESSION, 'get', get)
    with pytest.raises(dart.DartError):
        dart.fetch_dart_financials('005930', KEY, year=2025, report_code='11011')


def test_network_errors_never_include_credential_url(monkeypatch):
    def get(*args, **kwargs):
        raise requests.HTTPError('https://opendart.fss.or.kr/api/corpCode.xml?crtfc_key=' + KEY)
    monkeypatch.setattr(dart._SESSION, 'get', get)
    with pytest.raises(dart.DartError) as exc:
        dart.fetch_dart_financials('005930', KEY, year=2025, report_code='11011')
    assert KEY not in str(exc.value) and 'crtfc_key' not in str(exc.value)


def test_registry_xml_auth_error_has_safe_actionable_message(monkeypatch):
    def get(*args, **kwargs):
        return Response(content=f'<result><status>010</status><message>{KEY}</message></result>'.encode())
    monkeypatch.setattr(dart._SESSION, 'get', get)
    with pytest.raises(dart.DartError, match='인증키') as exc:
        dart.fetch_dart_financials('005930', KEY, year=2025, report_code='11011')
    assert KEY not in str(exc.value)
