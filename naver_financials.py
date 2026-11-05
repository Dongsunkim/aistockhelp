"""Naver's aggregated earnings table; these are not independently verified filings."""
from __future__ import annotations

from calendar import monthrange
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation, localcontext
from html.parser import HTMLParser
import math
import re
from zoneinfo import ZoneInfo

import requests


KST = ZoneInfo("Asia/Seoul")
SOURCE = "네이버증권 기업실적분석"
PAGE = "https://finance.naver.com/item/main.naver"
HEADERS = {"User-Agent": "Mozilla/5.0 (StockDesk personal research)",
           "Referer": "https://finance.naver.com/"}
_ROWS = {"매출액": "매출액", "영업이익": "영업이익", "당기순이익": "순이익"}


def _compact(value):
    return re.sub(r"\s+", "", value)


@dataclass
class _Cell:
    tag: str
    colspan: int = 1
    rowspan: int = 1
    chunks: list[str] = field(default_factory=list)

    @property
    def text(self):
        return " ".join(self.chunks).strip()


@dataclass
class _Table:
    context: str
    headers: list[list[_Cell]] = field(default_factory=list)
    rows: list[list[_Cell]] = field(default_factory=list)
    caption: list[str] = field(default_factory=list)


class _EarningsParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.tables = []
        self.active = None
        self.row = None
        self.cell = None
        self.in_head = False
        self.in_caption = False
        self.ignored = 0
        self.context = ""

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self.ignored += 1
        if self.ignored:
            return
        attrs = dict(attrs)
        if tag == "table":
            if self.active is not None:
                raise ValueError("네이버 재무표에 중첩된 표가 있어 열을 확인할 수 없습니다.")
            if "tb_type1_ifrs" in attrs.get("class", "").split():
                self.active = _Table(self.context)
                self.tables.append(self.active)
        if self.active is None:
            return
        if tag == "thead":
            self.in_head = True
        elif tag == "caption":
            self.in_caption = True
        elif tag == "tr":
            if self.row is not None:
                raise ValueError("네이버 재무표 행 구조가 불명확합니다.")
            self.row = []
        elif tag in ("td", "th"):
            if self.row is None or self.cell is not None:
                raise ValueError("네이버 재무표 셀 구조가 불명확합니다.")
            spans = []
            for key in ("colspan", "rowspan"):
                raw = attrs.get(key, "1")
                if not re.fullmatch(r"[1-9]\d?", raw):
                    raise ValueError("네이버 재무표 병합 열/행 수를 확인할 수 없습니다.")
                spans.append(int(raw))
            self.cell = _Cell(tag, *spans)
        elif tag == "br" and self.cell is not None:
            self.cell.chunks.append(" ")

    def handle_endtag(self, tag):
        if tag in ("script", "style") and self.ignored:
            self.ignored -= 1
            return
        if self.ignored or self.active is None:
            return
        if tag in ("td", "th"):
            if self.cell is None or self.cell.tag != tag or self.row is None:
                raise ValueError("네이버 재무표 셀 종료 구조가 불명확합니다.")
            self.row.append(self.cell)
            self.cell = None
        elif tag == "tr":
            if self.row is None or self.cell is not None:
                raise ValueError("네이버 재무표 행 종료 구조가 불명확합니다.")
            (self.active.headers if self.in_head else self.active.rows).append(self.row)
            self.row = None
        elif tag == "thead":
            self.in_head = False
        elif tag == "caption":
            self.in_caption = False
        elif tag == "table":
            if self.row is not None or self.cell is not None:
                raise ValueError("네이버 재무표가 완전히 닫히지 않았습니다.")
            self.active = None

    def handle_data(self, data):
        if self.ignored:
            return
        self.context = (self.context + " " + data)[-2000:]
        if self.cell is not None:
            self.cell.chunks.append(data)
        if self.active is not None and self.in_caption:
            self.active.caption.append(data)


def _header_grid(rows):
    if len(rows) not in (2, 3):
        raise ValueError("네이버 재무표의 연간·분기 머리글을 확인할 수 없습니다.")
    occupied = {}
    for i, row in enumerate(rows):
        col = 0
        for cell in row:
            if cell.tag != "th" or i + cell.rowspan > len(rows):
                raise ValueError("네이버 재무표 머리글 병합 구조가 맞지 않습니다.")
            while (i, col) in occupied:
                col += 1
            for y in range(i, i + cell.rowspan):
                for x in range(col, col + cell.colspan):
                    if (y, x) in occupied:
                        raise ValueError("네이버 재무표 머리글 열이 겹칩니다.")
                    occupied[y, x] = cell
            col += cell.colspan
    width = max((col for _, col in occupied), default=-1) + 1
    if not 2 <= width <= 30 or any((y, x) not in occupied for y in range(len(rows)) for x in range(width)):
        raise ValueError("네이버 재무표 머리글 열 수가 맞지 않습니다.")
    return [[occupied[y, x] for x in range(width)] for y in range(len(rows))]


def _columns(grid):
    if any(_compact(row[0].text) not in ("주요재무정보", "주요재무정보(단위:억원)") for row in grid):
        raise ValueError("네이버 기업실적분석 표 머리글이 아닙니다.")
    columns = []
    seen = set()
    marker = r"\((?:[Ee]|추정|예상)\)"
    accounting = r"(?:IFRS|GAAP)(?:연결|별도)"
    for i in range(1, len(grid[0])):
        texts = list(dict.fromkeys(_compact(row[i].text) for row in grid))
        groups, periods, bases = [], [], []
        estimate = False
        for text in texts:
            if text in ("최근연간실적", "최근분기실적"):
                groups.append(text)
                continue
            # Naver also puts YYYY.MM, (E), and IFRS연결 in one th with br/em.
            match = re.fullmatch(r"(20\d{2})[./](\d{2})(" + marker + r")?(" + accounting + r")?(" + marker + r")?", text)
            if match:
                year, month, before, basis, after = match.groups()
                periods.append((int(year), int(month)))
                estimate = estimate or bool(before or after)
                if basis:
                    bases.append(basis)
                continue
            match = re.fullmatch(r"(" + accounting + r")(" + marker + r")?", text)
            if match:
                bases.append(match.group(1))
                estimate = estimate or bool(match.group(2))
            elif re.fullmatch(marker, text):
                estimate = True
            elif text:
                raise ValueError("네이버 재무표 기간·회계기준 머리글에 알 수 없는 내용이 있습니다.")
        if len(groups) != 1 or len(periods) != 1:
            raise ValueError("네이버 재무표의 연간·분기 또는 기간 열이 불명확합니다.")
        year, month = periods[0]
        if month not in (3, 6, 9, 12):
            raise ValueError("네이버 재무표의 결산 월이 분기 말과 일치하지 않습니다.")
        end = date(year, month, monthrange(year, month)[1])
        kind = "quarterly" if groups[0] == "최근분기실적" else "annual"
        key = (kind, end)
        if key in seen:
            raise ValueError("네이버 재무표에 같은 기간 열이 중복되어 있습니다.")
        seen.add(key)
        basis = list(dict.fromkeys(bases))
        if len(basis) > 1:
            raise ValueError("네이버 재무표의 연결·별도 기준이 충돌합니다.")
        columns.append({"index": i, "end": end, "kind": kind,
                        "estimate": estimate,
                        "basis": basis[0] if basis else None})
    return columns


def _earnings_rows(table, width):
    found = {}
    row_units = []
    for row in table.rows:
        if not row:
            continue
        label = _compact(row[0].text)
        match = re.fullmatch(r"(매출액|영업이익|당기순이익)(?:\((억원|백만원|천원|원)\))?", label)
        if not match:
            continue
        name, unit = match.groups()
        if name in found or row[0].tag != "th" or len(row) != width or any(c.colspan != 1 or c.rowspan != 1 for c in row):
            raise ValueError("네이버 핵심 재무계정의 행·열 수가 맞지 않습니다.")
        if any(c.tag != "td" for c in row[1:]):
            raise ValueError("네이버 핵심 재무계정의 값 열이 불명확합니다.")
        found[name] = row
        if unit:
            row_units.append(unit)
    if set(found) != set(_ROWS):
        raise ValueError("네이버 재무표의 매출액·영업이익·당기순이익 3개 행이 필요합니다.")
    context = table.context.rsplit("기업실적분석", 1)[-1] if "기업실적분석" in table.context else table.context[-500:]
    notes = [" ".join(table.caption)] + [c.text for row in table.headers for c in row] + [context]
    # Capture unsupported declarations too; silently ignoring '조원' would mis-scale.
    declared = [unit for note in notes
                for unit in re.findall(r"단위[:：]?([^,，/()%％;；]+)", _compact(note))]
    if any(unit != "억원" for unit in row_units + declared):
        raise ValueError("네이버 핵심 재무계정의 억원 단위를 확인할 수 없습니다.")
    # This exact Naver table schema uses 억원 for these three rows; EPS/BPS use 원.
    # Preserve that provenance instead of treating a nearby EPS unit as earnings scale.
    unit_basis = "표시된 억원 단위" if row_units or declared else "네이버 tb_type1_ifrs 표준 재무계정 단위: 억원"
    return found, unit_basis


def _won(text):
    raw = _compact(text).replace("−", "-")
    if re.fullmatch(r"\(\d[\d,.]*\)", raw):
        raw = "-" + raw[1:-1]
    if len(raw) > 60 or not re.fullmatch(r"[+-]?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?", raw):
        raise ValueError("네이버 재무계정에 누락 또는 비정상 숫자가 있습니다.")
    try:
        with localcontext() as context:
            context.prec = 80
            scaled = Decimal(raw.replace(",", "")) * Decimal(100_000_000)
        value = int(scaled) if scaled == scaled.to_integral_value() else float(scaled)
        if not math.isfinite(value):
            raise ValueError("네이버 재무계정 숫자가 유한하지 않습니다.")
        return value
    except (InvalidOperation, OverflowError) as error:
        raise ValueError("네이버 재무계정 숫자 변환 실패") from error


def parse_naver_financials(html, symbol, *, asof=None):
    """Select one completed, recent actual column; never mix accounts or periods."""
    if not isinstance(symbol, str) or not re.fullmatch(r"\d{6}", symbol):
        raise ValueError("국내 6자리 종목코드가 필요합니다.")
    if not isinstance(html, str):
        raise ValueError("네이버 재무표 HTML 문자열이 필요합니다.")
    now = datetime.now(KST)
    today = asof.date() if isinstance(asof, datetime) else asof or now.date()
    if not isinstance(today, date):
        raise ValueError("재무표 기준일 형식 오류")
    parser = _EarningsParser()
    parser.feed(html)
    parser.close()
    if parser.active is not None or len(parser.tables) != 1:
        raise ValueError("네이버 기업실적분석 표가 없거나 여러 개여서 확인할 수 없습니다.")
    table = parser.tables[0]
    grid = _header_grid(table.headers)
    columns = _columns(grid)
    rows, unit_basis = _earnings_rows(table, len(grid[0]))
    for kind in ("quarterly", "annual"):
        candidates = sorted((c for c in columns if c["kind"] == kind and not c["estimate"]
                             and 0 < (today - c["end"]).days <= 240),
                            key=lambda c: c["end"], reverse=True)
        for column in candidates:
            try:
                values = {name: _won(row[column["index"]].text) for name, row in rows.items()}
            except ValueError:
                continue
            label = "분기 실적" if kind == "quarterly" else "연간 실적"
            basis = column["basis"] or "연결/별도 미확인"
            return [{"name": _ROWS[name], "value": values[name], "unit": "KRW",
                     "period": column["end"].isoformat(), "period_type": kind,
                     "basis": f"{label} · 네이버증권 표기 기준({basis})",
                     "basis_verified": column["basis"] is not None,
                     "source": SOURCE, "link": f"{PAGE}?code={symbol}",
                     "is_estimate": False, "data_origin": "aggregated",
                     "official_source": False, "reported_at": None,
                     "unit_basis": unit_basis, "fetched_at": now.astimezone(timezone.utc).isoformat()}
                    for name in _ROWS]
    raise ValueError("네이버 재무표에서 최근 완료된 실제 실적 3개 계정을 같은 기간으로 확인할 수 없습니다.")


def _response_html(response):
    content = response.content
    if not isinstance(content, bytes):
        raise ValueError("네이버 재무표 응답 형식 오류")
    charset = re.search(rb"charset\s*=\s*[\"']?([\w-]+)", content[:8192], re.I)
    encodings = [charset.group(1).decode("ascii")] if charset else ["utf-8", "cp949"]
    for encoding in encodings:
        try:
            return content.decode(encoding)
        except (LookupError, UnicodeDecodeError):
            continue
    raise ValueError("네이버 재무표 문자 인코딩을 확인할 수 없습니다.")


def fetch_naver_financials(symbol, session=None):
    """Fetch Naver's display values; no official-source or accounting audit claim."""
    if not isinstance(symbol, str) or not re.fullmatch(r"\d{6}", symbol):
        raise ValueError("국내 6자리 종목코드가 필요합니다.")
    client = session or requests.Session()
    response = client.get(PAGE, params={"code": symbol}, headers=HEADERS, timeout=10)
    response.raise_for_status()
    return parse_naver_financials(_response_html(response), symbol)
