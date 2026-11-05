"""OpenDART full statements and paginated disclosures, without credential logging.

Amounts stay as reported strings: converting large KRW balances to browser floats
would lose precision. Each financial response keeps CFS and OFS separate.
"""
from __future__ import annotations

from datetime import date, datetime, timezone
from hashlib import sha256
import io
import re
import threading
import time
from zoneinfo import ZoneInfo
import zipfile
import xml.etree.ElementTree as ET

import requests

BASE_URL = 'https://opendart.fss.or.kr/api/'
REPORT_LABELS = {'11011': '사업보고서', '11012': '반기',
                 '11013': '1분기', '11014': '3분기'}
SHEETS = {'BS', 'IS', 'CIS', 'CF', 'SCE'}
_SESSION = requests.Session()
_REGISTRY_LOCK = threading.Lock()
_REGISTRY_CACHE: dict[str, tuple[float, dict]] = {}
_MAX_ZIP_BYTES = 32 * 1024 * 1024
_MAX_XML_BYTES = 100 * 1024 * 1024
_STATUS_MESSAGES = {
    '010': '등록되지 않은 인증키입니다.',
    '011': '사용할 수 없는 인증키입니다.',
    '012': '허용되지 않은 IP 주소입니다.',
    '014': '요청한 파일이 없습니다.',
    '020': '조회 한도를 초과했습니다. 나중에 다시 시도하세요.',
    '100': '요청 인자가 올바르지 않습니다.',
    '101': '잘못된 접근입니다.',
    '800': 'OpenDART 점검 중입니다.',
    '900': 'OpenDART 처리 오류입니다.',
    '901': '인증키 사용 기간이 만료되었습니다.',
}


class DartError(ValueError):
    """A safe public error, excluding provider URLs, key values and raw messages."""


def _today() -> date:
    return datetime.now(ZoneInfo('Asia/Seoul')).date()


def _validate_identity(symbol: str, api_key: str) -> tuple[str, str]:
    if not isinstance(symbol, str) or not re.fullmatch(r'\d{6}', symbol):
        raise DartError('국내 6자리 종목코드를 입력하세요.')
    if not isinstance(api_key, str) or not api_key.strip():
        raise DartError('DART_API_KEY를 환경 설정에 등록하세요.')
    return symbol, api_key.strip()


def _request(path: str, api_key: str, params: dict | None = None):
    try:
        response = _SESSION.get(BASE_URL + path,
                                params={'crtfc_key': api_key, **(params or {})},
                                timeout=(5, 25))
        response.raise_for_status()
        return response
    except requests.RequestException:
        # requests errors can include the complete URL, including crtfc_key.
        raise DartError('OpenDART 네트워크 요청에 실패했습니다. 접근 정책과 서비스 상태를 확인하세요.') from None


def _json_response(path: str, api_key: str, params: dict) -> dict:
    response = _request(path, api_key, params)
    try:
        payload = response.json()
    except (ValueError, TypeError):
        raise DartError('OpenDART JSON 응답 형식을 확인할 수 없습니다.') from None
    if not isinstance(payload, dict) or not isinstance(payload.get('status'), str):
        raise DartError('OpenDART 응답 상태가 누락되었습니다.')
    status = payload['status']
    if status not in ('000', '013'):
        message = _STATUS_MESSAGES.get(status, 'OpenDART가 요청을 완료하지 못했습니다.')
        # Do not expose arbitrary provider message/status strings.
        raise DartError(message)
    return payload


def _registry(api_key: str) -> dict:
    cache_key = sha256(api_key.encode()).hexdigest()
    with _REGISTRY_LOCK:
        cached = _REGISTRY_CACHE.get(cache_key)
        if cached and time.monotonic() < cached[0]:
            return cached[1]
        response = _request('corpCode.xml', api_key)
        content = response.content
        if not isinstance(content, bytes) or len(content) > _MAX_ZIP_BYTES:
            raise DartError('OpenDART 기업 고유번호 파일 크기 또는 형식이 올바르지 않습니다.')
        # The ZIP endpoint returns an XML result instead of a ZIP for key/limit
        # errors. Keep these actionable without echoing its raw message.
        if (len(content) <= 65536 and content.lstrip().startswith(b'<')
                and b'<!DOCTYPE' not in content.upper() and b'<!ENTITY' not in content.upper()):
            try:
                status = ET.fromstring(content).findtext('status')
            except ET.ParseError:
                status = None
            if status and status != '000':
                raise DartError(_STATUS_MESSAGES.get(status, 'OpenDART 기업 고유번호 요청을 완료하지 못했습니다.'))
        try:
            with zipfile.ZipFile(io.BytesIO(content)) as archive:
                members = [x for x in archive.infolist()
                           if not x.is_dir() and x.filename.lower().endswith('.xml')]
                if len(members) != 1 or members[0].file_size > _MAX_XML_BYTES:
                    raise DartError('OpenDART 기업 고유번호 압축파일 구성이 올바르지 않습니다.')
                xml = archive.read(members[0])
            if b'<!DOCTYPE' in xml.upper() or b'<!ENTITY' in xml.upper():
                raise DartError('OpenDART 기업 고유번호 XML 구성이 올바르지 않습니다.')
            root = ET.fromstring(xml)
        except (zipfile.BadZipFile, zipfile.LargeZipFile, ET.ParseError,
                RuntimeError, OSError, ValueError):
            raise DartError('OpenDART 기업 고유번호 ZIP/XML을 읽을 수 없습니다.') from None
        companies = {}
        for item in root.findall('list'):
            stock_code = (item.findtext('stock_code') or '').strip()
            corp_code = (item.findtext('corp_code') or '').strip()
            corp_name = (item.findtext('corp_name') or '').strip()
            if re.fullmatch(r'\d{6}', stock_code) and re.fullmatch(r'\d{8}', corp_code) and corp_name:
                if stock_code in companies and companies[stock_code]['corp_code'] != corp_code:
                    raise DartError('OpenDART 기업 고유번호가 중복되어 종목을 확정할 수 없습니다.')
                companies[stock_code] = {'stock_code': stock_code,
                                         'corp_code': corp_code, 'corp_name': corp_name}
        if not companies:
            raise DartError('OpenDART 기업 고유번호 목록이 비어 있습니다.')
        if len(_REGISTRY_CACHE) >= 8:
            _REGISTRY_CACHE.clear()
        _REGISTRY_CACHE[cache_key] = (time.monotonic() + 86400, companies)
        return companies


def _company(symbol: str, api_key: str) -> dict:
    company = _registry(api_key).get(symbol)
    if not company:
        raise DartError('OpenDART 기업 고유번호에서 해당 종목을 찾지 못했습니다.')
    return dict(company)


def _financial_rows(payload: dict, company: dict, year: int,
                    report_code: str, fs_div: str) -> list[dict]:
    rows = payload.get('list')
    if not isinstance(rows, list) or not rows:
        raise DartError('OpenDART 전체 재무제표 계정 목록이 비어 있거나 올바르지 않습니다.')
    result = []
    for row in rows:
        if not isinstance(row, dict) or not all(isinstance(k, str) for k in row):
            raise DartError('OpenDART 재무계정 행 형식이 올바르지 않습니다.')
        if any(not isinstance(v, str) and v is not None for v in row.values()):
            raise DartError('OpenDART 재무계정은 원문 문자열로 제공되어야 합니다.')
        if row.get('sj_div') not in SHEETS or not row.get('account_nm'):
            raise DartError('OpenDART 재무제표 구분 또는 계정명이 올바르지 않습니다.')
        for field, expected in (('corp_code', company['corp_code']), ('bsns_year', str(year)),
                                ('reprt_code', report_code)):
            if row.get(field) != expected:
                raise DartError('OpenDART 요청과 응답의 기업·기간·연결/별도 기준이 다릅니다.')
        if 'fs_div' in row and row['fs_div'] != fs_div:
            raise DartError('OpenDART 요청과 응답의 연결/별도 기준이 다릅니다.')
        copied = dict(row)
        copied['fs_div'] = fs_div
        receipt = copied.get('rcept_no', '')
        if not isinstance(receipt, str) or not re.fullmatch(r'\d{14}', receipt):
            raise DartError('OpenDART 재무제표 접수번호 형식이 올바르지 않습니다.')
        copied['link'] = 'https://dart.fss.or.kr/dsaf001/main.do?rcpNo=' + receipt
        result.append(copied)
    return result


def fetch_dart_financials(symbol: str, api_key: str, *, year: int,
                          report_code: str, fs_div: str = 'BOTH') -> dict:
    """Fetch all reported statement accounts for one period, keeping bases apart.

    ``complete`` means every requested basis was fetched successfully, including
    explicit status 013 (no data). A partial service failure is never called complete.
    """
    symbol, api_key = _validate_identity(symbol, api_key)
    if isinstance(year, bool) or not isinstance(year, int) or not 2015 <= year <= _today().year:
        raise DartError('사업연도는 2015년부터 올해까지 지정하세요.')
    if not isinstance(report_code, str) or report_code not in REPORT_LABELS:
        raise DartError('사업·반기·1분기·3분기 보고서 중 하나를 선택하세요.')
    if fs_div not in ('CFS', 'OFS', 'BOTH'):
        raise DartError('재무제표 기준은 CFS, OFS 또는 BOTH여야 합니다.')
    company = _company(symbol, api_key)
    statements = []
    for basis in (('CFS', 'OFS') if fs_div == 'BOTH' else (fs_div,)):
        entry = {'fs_div': basis, 'fs_name': '연결' if basis == 'CFS' else '별도',
                 'status': 'available', 'message': '', 'accounts': []}
        try:
            payload = _json_response('fnlttSinglAcntAll.json', api_key, {
                'corp_code': company['corp_code'], 'bsns_year': str(year),
                'reprt_code': report_code, 'fs_div': basis})
            if payload['status'] == '013':
                entry.update(status='no_data', message='해당 기간·기준의 공시 재무제표가 없습니다.')
            else:
                entry['accounts'] = _financial_rows(payload, company, year, report_code, basis)
        except DartError as error:
            entry.update(status='error', message=str(error))
        statements.append(entry)
    if all(x['status'] == 'error' for x in statements):
        raise DartError(statements[0]['message'])
    return {'company': company, 'year': year, 'report_code': report_code,
            'report_label': REPORT_LABELS[report_code], 'statements': statements,
            'complete': all(x['status'] != 'error' for x in statements),
            'source': 'OpenDART', 'fetched_at': datetime.now(timezone.utc).isoformat(),
            'note': '선택한 보고서의 전체 계정입니다. 연결·별도와 당기·전기·누적금액은 서로 합산하지 않습니다. 원문 주석·감사보고서 전체를 수집한 것은 아닙니다.'}


def _iso_date(value: str) -> date:
    if not isinstance(value, str) or not re.fullmatch(r'\d{4}-\d{2}-\d{2}', value):
        raise DartError('조회일은 YYYY-MM-DD 형식으로 입력하세요.')
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise DartError('유효한 조회일을 입력하세요.') from None


def _nonnegative_int(value, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, str)) or not re.fullmatch(r'\d+', str(value)):
        raise DartError(f'OpenDART {name} 형식이 올바르지 않습니다.')
    return int(value)


def fetch_dart_disclosures(symbol: str, api_key: str, *, begin: str,
                           end: str, page: int = 1) -> dict:
    """Fetch any disclosure category, 100 rows per page, without hidden truncation.

    ``has_more`` and ``complete`` describe whether more pages follow this one;
    callers accumulate pages when they need the entire selected date range.
    """
    symbol, api_key = _validate_identity(symbol, api_key)
    start_date, end_date = _iso_date(begin), _iso_date(end)
    if start_date > end_date or (end_date - start_date).days > 366:
        raise DartError('공시 조회기간은 시작일 이후 최대 366일로 지정하세요.')
    if end_date > _today():
        raise DartError('공시 조회 종료일은 오늘 이후로 지정할 수 없습니다.')
    if isinstance(page, bool) or not isinstance(page, int) or not 1 <= page <= 1000:
        raise DartError('공시 페이지는 1부터 1000까지 지정하세요.')
    company = _company(symbol, api_key)
    payload = _json_response('list.json', api_key, {
        'corp_code': company['corp_code'], 'bgn_de': start_date.strftime('%Y%m%d'),
        'end_de': end_date.strftime('%Y%m%d'), 'page_no': page, 'page_count': 100,
        'sort': 'date', 'sort_mth': 'desc'})
    result = {'company': company, 'begin': begin, 'end': end, 'page': page,
              'page_size': 100, 'total_count': 0, 'total_pages': 0,
              'items': [], 'has_more': False, 'complete': True,
              'source': 'OpenDART', 'fetched_at': datetime.now(timezone.utc).isoformat()}
    if payload['status'] == '013':
        if page != 1:
            raise DartError('요청한 공시 페이지가 없습니다. 첫 페이지부터 다시 조회하세요.')
        return result
    total = _nonnegative_int(payload.get('total_count'), '공시 건수')
    pages = _nonnegative_int(payload.get('total_page'), '공시 페이지 수')
    returned_page = _nonnegative_int(payload.get('page_no'), '현재 페이지')
    page_size = _nonnegative_int(payload.get('page_count'), '페이지 크기')
    if page_size != 100 or returned_page != page or pages != (total + 99) // 100:
        raise DartError('OpenDART 공시 페이지 메타데이터가 일치하지 않습니다.')
    if page > max(pages, 1):
        raise DartError('요청한 공시 페이지가 전체 페이지 범위를 벗어났습니다.')
    rows = payload.get('list')
    expected_count = min(100, max(0, total - (page - 1) * 100))
    if not isinstance(rows, list) or len(rows) != expected_count:
        raise DartError('OpenDART 공시 목록과 전체 건수가 일치하지 않습니다.')
    items, seen = [], set()
    for row in rows:
        if not isinstance(row, dict) or any(not isinstance(v, str) for v in row.values()):
            raise DartError('OpenDART 공시 행 형식이 올바르지 않습니다.')
        receipt = row.get('rcept_no', '')
        if not re.fullmatch(r'\d{14}', receipt) or receipt in seen:
            raise DartError('OpenDART 공시 접수번호가 올바르지 않거나 중복되었습니다.')
        seen.add(receipt)
        if row.get('corp_code') != company['corp_code'] or not row.get('report_nm'):
            raise DartError('OpenDART 공시의 기업 또는 보고서명을 확인할 수 없습니다.')
        raw_date = row.get('rcept_dt', '')
        try:
            filed_date = datetime.strptime(raw_date, '%Y%m%d').date()
        except (ValueError, TypeError):
            raise DartError('OpenDART 공시 접수일이 올바르지 않습니다.') from None
        if not start_date <= filed_date <= end_date:
            raise DartError('OpenDART 공시 접수일이 요청 기간을 벗어났습니다.')
        item = dict(row)
        item.update(title=row['report_nm'], date=filed_date.isoformat(),
                    age_days=(_today() - filed_date).days, source='금융감독원 DART',
                    link='https://dart.fss.or.kr/dsaf001/main.do?rcpNo=' + receipt,
                    basis='공시 목록 메타데이터 · 원문 확인 필요')
        items.append(item)
    result.update(total_count=total, total_pages=pages, items=items,
                  has_more=page < pages, complete=page >= pages)
    return result
