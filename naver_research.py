"""Naver company-research listings; report contents are never downloaded or read."""
from __future__ import annotations

from datetime import date, datetime
from html.parser import HTMLParser
import re
from urllib.parse import parse_qs, urljoin, urlsplit
from zoneinfo import ZoneInfo

import requests


LIST_URL = "https://finance.naver.com/research/company_list.naver"
HEADERS = {"User-Agent": "Mozilla/5.0 (StockDesk personal research)",
           "Referer": "https://stock.naver.com/"}


class _Tables(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.tables = []
        self.stack = []

    def handle_starttag(self, tag, attrs):
        if tag == "table":
            table = {"rows": [], "row": None, "cell": None, "anchor": None}
            self.tables.append(table)
            self.stack.append(table)
        if not self.stack:
            return
        table = self.stack[-1]
        if tag == "tr":
            table["row"] = []
            table["rows"].append(table["row"])
            table["cell"] = table["anchor"] = None
        elif tag in ("td", "th") and table["row"] is not None:
            cell = {"text": [], "anchors": []}
            table["row"].append(cell)
            table["cell"] = cell
            table["anchor"] = None
        elif tag == "a" and table["cell"] is not None:
            attrs = dict(attrs)
            anchor = {"href": attrs.get("href") or "", "text": [],
                      "title": attrs.get("title") or ""}
            table["cell"]["anchors"].append(anchor)
            table["anchor"] = anchor

    def handle_endtag(self, tag):
        if not self.stack:
            return
        table = self.stack[-1]
        if tag == "table":
            self.stack.pop()
        elif tag == "a":
            table["anchor"] = None
        elif tag in ("td", "th"):
            table["cell"] = table["anchor"] = None
        elif tag == "tr":
            table["row"] = table["cell"] = table["anchor"] = None

    def handle_data(self, text):
        if self.stack:
            table = self.stack[-1]
            if table["cell"] is not None:
                table["cell"]["text"].append(text)
            if table["anchor"] is not None:
                table["anchor"]["text"].append(text)


def _text(parts):
    return " ".join("".join(parts).split())


def _url(href, hosts):
    if not isinstance(href, str) or not href.strip() or re.search(r"[\x00-\x1f\x7f]", href):
        return None
    try:
        url = urljoin(LIST_URL, href.strip())
        parsed = urlsplit(url)
        if (parsed.scheme not in ("http", "https") or parsed.hostname not in hosts
                or parsed.username or parsed.password or parsed.port is not None):
            return None
        return url
    except ValueError:
        return None


def _date(text):
    match = re.fullmatch(r"(\d{2}|\d{4})[.\-/](\d{1,2})[.\-/](\d{1,2})\.?", text.strip())
    if not match:
        return None
    year, month, day = map(int, match.groups())
    # Explicit strptime-style century rule avoids interpreting 99 as 2099.
    if len(match.group(1)) == 2:
        year += 1900 if year >= 69 else 2000
    try:
        published = date(year, month, day)
    except ValueError:
        return None
    if published > datetime.now(ZoneInfo("Asia/Seoul")).date():
        return None
    return published.isoformat()


def _validate(symbol, limit):
    if not isinstance(symbol, str) or not re.fullmatch(r"[0-9]{6}", symbol):
        raise ValueError("네이버 종목 리서치는 국내 6자리 종목코드가 필요합니다.")
    if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 50:
        raise ValueError("리서치 목록 조회 건수는 1~50 사이여야 합니다.")


def parse_naver_research(html, symbol, limit=8):
    """Parse listing metadata, retaining dated historical reports without recommendations."""
    _validate(symbol, limit)
    parser = _Tables()
    parser.feed(html)
    listings = []
    for table in parser.tables:
        for row in table["rows"]:
            headings = [_text(cell["text"]).replace(" ", "") for cell in row]
            if all(label in headings for label in ("제목", "증권사", "작성일")):
                listings.append((table, {label: headings.index(label)
                                         for label in ("제목", "증권사", "작성일")}))
                break
    if not listings:
        raise ValueError("네이버 종목 리서치 목록 표를 확인할 수 없습니다. 응답 구조 확인이 필요합니다.")
    results, seen = [], set()
    for table, columns in listings:
        for row in table["rows"]:
            if len(row) <= max(columns.values()):
                continue
            anchors = [anchor for cell in row for anchor in cell["anchors"]]
            # The request filters itemCode; also reject mismatching stock links in each row.
            mismatched = False
            for anchor in anchors:
                url = _url(anchor["href"], {"finance.naver.com", "stock.naver.com"})
                if url:
                    parsed = urlsplit(url)
                    query = parse_qs(parsed.query, keep_blank_values=True)
                    for key in ("code", "itemCode"):
                        if key in query and query[key] != [symbol]:
                            mismatched = True
                    stock_path = re.match(r"/domestic/stock/([0-9]{6})(?:/|$)", parsed.path)
                    if stock_path and stock_path.group(1) != symbol:
                        mismatched = True
            if mismatched:
                continue
            detail, title, nid = None, None, None
            for anchor in row[columns["제목"]]["anchors"]:
                url = _url(anchor["href"], {"finance.naver.com"})
                if not url or urlsplit(url).path != "/research/company_read.naver":
                    continue
                ids = parse_qs(urlsplit(url).query).get("nid", [])
                if len(ids) != 1 or not re.fullmatch(r"[0-9]+", ids[0]):
                    continue
                label = _text(anchor["text"]) or anchor["title"].strip()
                if label:
                    detail, title, nid = url, label, ids[0]
                    break
            broker = _text(row[columns["증권사"]]["text"])
            published = _date(_text(row[columns["작성일"]]["text"]))
            if not detail or not broker or not published or nid in seen:
                continue
            item = {"symbol": symbol, "title": title, "source": "네이버증권 리서치",
                    "broker": broker, "date": published, "published_at": published,
                    "link": detail, "type": "research_report", "kind": "증권사 리포트 목록",
                    "verification": "목록 확인 · 원문 미확인",
                    "basis": "네이버증권의 제목·증권사·작성일·링크 목록만 확인; 보고서 원문 미확인"}
            for anchor in anchors:
                pdf = _url(anchor["href"], {"stock.pstatic.net"})
                if pdf and urlsplit(pdf).path.lower().endswith(".pdf"):
                    item["report_url"] = pdf
                    break
            results.append(item)
            seen.add(nid)
            if len(results) >= limit:
                return results
    return results


def fetch_naver_research(symbol, session=None, limit=8):
    """Read Naver's filtered company-report listing, without fetching PDFs or detail pages."""
    _validate(symbol, limit)
    own_session = session is None
    client = session or requests.Session()
    try:
        response = client.get(LIST_URL, params={"searchType": "itemCode", "itemCode": symbol},
                              headers=HEADERS, timeout=8)
        response.raise_for_status()
        content = response.content
        declared = re.search(rb"charset\s*=\s*[\"']?\s*([A-Za-z0-9_-]+)", content[:4096], re.I)
        encodings = [declared.group(1).decode("ascii")] if declared else ["utf-8", "euc-kr"]
        for encoding in encodings:
            try:
                html = content.decode(encoding)
                break
            except (UnicodeDecodeError, LookupError):
                continue
        else:
            raise ValueError("네이버 리서치 목록의 문자 인코딩을 확인할 수 없습니다.")
        return parse_naver_research(html, symbol, limit)
    finally:
        if own_session:
            client.close()
