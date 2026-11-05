"""Naver domestic stock news titles and publication metadata, not article text."""
from datetime import datetime
from html.parser import HTMLParser
import re
from urllib.parse import parse_qs, urljoin, urlsplit
from zoneinfo import ZoneInfo

import requests


KST = ZoneInfo("Asia/Seoul")
BASE_URL = "https://finance.naver.com"
NEWS_URL = BASE_URL + "/item/news_news.naver"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (StockDesk personal research)",
    "Referer": BASE_URL + "/",
}


def _text(parts):
    return " ".join("".join(parts).split())


class _NewsTableParser(HTMLParser):
    """Keep separate contexts for nested related-news tables and every row."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack = []
        self.tables = []
        self.rows = []

    def _finish_row(self, table):
        if table["row"] is not None:
            self.rows.append(table["row"])
            table["row"] = None
            table["cell"] = None

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "table":
            typed = "type5" in attrs.get("class", "").split()
            news = typed or bool(self.stack and self.stack[-1]["news"])
            table = {"typed": typed, "news": news, "summary": attrs.get("summary", ""),
                     "headers": [], "row": None, "cell": None, "closed": False,
                     "news_cells": set()}
            self.stack.append(table)
            self.tables.append(table)
            return
        if not self.stack or not self.stack[-1]["news"]:
            return
        table = self.stack[-1]
        if tag == "tr":
            self._finish_row(table)
            table["row"] = []
            return
        if tag in ("td", "th") and table["row"] is not None:
            cell = {"classes": set(attrs.get("class", "").split()), "parts": [],
                    "anchors": [], "anchor": None, "header": tag == "th"}
            table["row"].append(cell)
            table["cell"] = cell
            return
        cell = table["cell"]
        if cell is not None and tag == "a":
            anchor = {"href": attrs.get("href", ""), "parts": []}
            cell["anchors"].append(anchor)
            cell["anchor"] = anchor
        elif cell is not None and tag == "br":
            self.handle_data(" ")

    def handle_data(self, data):
        if not self.stack or not self.stack[-1]["news"]:
            return
        cell = self.stack[-1]["cell"]
        if cell is not None:
            cell["parts"].append(data)
            if cell["anchor"] is not None:
                cell["anchor"]["parts"].append(data)

    def handle_endtag(self, tag):
        if not self.stack:
            return
        table = self.stack[-1]
        if tag == "table":
            self._finish_row(table)
            table["closed"] = True
            self.stack.pop()
            return
        cell = table["cell"]
        if tag == "a" and cell is not None:
            cell["anchor"] = None
        elif tag in ("td", "th") and cell is not None:
            if cell["header"]:
                table["headers"].append(_text(cell["parts"]))
            table["news_cells"].update(cell["classes"] & {"title", "info", "date"})
            table["cell"] = None
        elif tag == "tr":
            self._finish_row(table)


def _article_link(href, symbol):
    url = urljoin(BASE_URL, href)
    parts = urlsplit(url)
    if (parts.scheme != "https" or parts.netloc != "finance.naver.com" or
            parts.path != "/item/news_read.naver"):
        return None
    query = parse_qs(parts.query, keep_blank_values=True)
    if "code" in query and query["code"] != [symbol]:
        return None
    ids = [query.get(key, []) for key in ("office_id", "article_id")]
    if any(len(values) != 1 or not re.fullmatch(r"[0-9]+", values[0]) for values in ids):
        return None
    return url, (ids[0][0], ids[1][0])


def _response_html(response):
    """Honor declared HTTP/meta charsets; never silently replace broken bytes."""
    content = response.content
    charset = re.search(r"charset\s*=\s*[\"']?([a-zA-Z0-9._-]+)",
                        response.headers.get("Content-Type", ""), re.I)
    if not charset and isinstance(content, bytes):
        meta = re.search(rb"<meta\b[^>]*charset\s*=\s*[\"']?([a-zA-Z0-9._-]+)",
                         content[:16384], re.I)
        encoding = meta.group(1).decode("ascii") if meta else None
    else:
        encoding = charset.group(1) if charset else None
    if encoding and isinstance(content, bytes):
        try:
            return content.decode(encoding)
        except (LookupError, UnicodeDecodeError) as exc:
            raise ValueError("네이버 종목뉴스 문자 인코딩을 확인할 수 없습니다.") from exc
    return response.text


def fetch_naver_news(symbol, session=None, limit=12):
    """Read page 1 of Naver's ticker news list. No body or sentiment is inferred.

    Nested related stories are independent rows; source/date are never inherited
    from a parent story. Future dates retain negative ages and cannot qualify as
    fresh evidence. An unrecognized or broken table raises instead of reporting
    a successful empty result.
    """
    if not isinstance(symbol, str) or not re.fullmatch(r"[0-9]{6}", symbol):
        raise ValueError("국내 6자리 종목코드가 필요합니다.")
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
        raise ValueError("뉴스 조회 개수는 1~100개여야 합니다.")
    client = session or requests.Session()
    response = client.get(NEWS_URL, params={"code": symbol, "page": 1},
                          headers=HEADERS, timeout=8)
    response.raise_for_status()
    parser = _NewsTableParser()
    parser.feed(_response_html(response))
    parser.close()
    expected = [table for table in parser.tables if table["typed"] and
                ("뉴스" in table["summary"] or
                 {"제목", "정보제공", "날짜"}.issubset(table["headers"]) or
                 {"title", "info", "date"}.issubset(table["news_cells"]))]
    if not expected or any(not table["closed"] for table in expected):
        raise ValueError("네이버 종목뉴스 표 형식을 확인할 수 없습니다.")
    now = datetime.now(KST)
    items, seen = [], set()
    for row in parser.rows:
        titles = [cell for cell in row if "title" in cell["classes"]]
        sources = [cell for cell in row if "info" in cell["classes"]]
        dates = [cell for cell in row if "date" in cell["classes"]]
        for cell in titles:
            for anchor in cell["anchors"]:
                article = _article_link(anchor["href"], symbol)
                if article is None:
                    continue
                link, key = article
                title = _text(anchor["parts"])
                if not title or len(sources) != 1 or len(dates) != 1:
                    raise ValueError("네이버 종목뉴스의 제목·언론사·발행시점이 누락됐습니다.")
                source, stamp = _text(sources[0]["parts"]), _text(dates[0]["parts"])
                if not source or not re.fullmatch(r"[0-9]{4}\.[0-9]{2}\.[0-9]{2} [0-9]{2}:[0-9]{2}", stamp):
                    raise ValueError("네이버 종목뉴스의 언론사·발행시점을 검증할 수 없습니다.")
                try:
                    published = datetime.strptime(stamp, "%Y.%m.%d %H:%M").replace(tzinfo=KST)
                except ValueError as exc:
                    raise ValueError("네이버 종목뉴스 발행일이 올바르지 않습니다.") from exc
                if key in seen:
                    continue
                seen.add(key)
                hours = (now - published).total_seconds() / 3600
                age = min(-0.01, round(hours, 2)) if hours < 0 else round(hours, 2)
                items.append({"title": title, "source": source, "link": link,
                              "published_at": published.isoformat(), "age_hours": age,
                              "basis": "네이버 종목뉴스 제목·언론사·발행시점만 수집; 기사 원문 미확인"})
    return sorted(items, key=lambda item: item["published_at"], reverse=True)[:limit]
