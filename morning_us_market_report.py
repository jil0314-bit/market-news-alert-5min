"""미국장 마감 및 다음 한국장 영향 텔레그램 브리핑.

무료 공개 데이터와 RSS를 조합한 참고용 도구입니다.
- 가격 데이터는 무료 웹 데이터의 지연·누락·변경 가능성이 있습니다.
- 뉴스 번역·요약은 자동 처리이므로 원문 확인이 필요합니다.
- 매수·매도 추천이나 수익 보장 기능이 아닙니다.
"""
from __future__ import annotations

import datetime as dt
import html
import json
import logging
import os
import re
import sys
import time
from dataclasses import dataclass
from typing import Any, Iterable
from urllib.parse import quote_plus
from zoneinfo import ZoneInfo

import feedparser
import requests
from bs4 import BeautifulSoup
from dateutil import parser as date_parser
from deep_translator import GoogleTranslator

KST = ZoneInfo("Asia/Seoul")
ET = ZoneInfo("America/New_York")
UTC = dt.timezone.utc

LOGGER = logging.getLogger("morning_us_market_report")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)


@dataclass(frozen=True)
class Quote:
    symbol: str
    name: str
    price: float
    change_pct: float
    as_of: dt.datetime | None


@dataclass(frozen=True)
class News:
    title: str
    link: str
    source: str
    summary: str
    published_at: dt.datetime | None
    score: int


class MorningUSMarketReport:
    """미국장 마감 브리핑 생성기."""

    INDEX_SYMBOLS: dict[str, str] = {
        "^IXIC": "나스닥 종합",
        "^DJI": "다우",
        "^GSPC": "S&P500",
        "^SOX": "필라델피아 반도체",
        "^VIX": "VIX 공포지수",
        "^TNX": "미국 10년물",
        "DX-Y.NYB": "달러인덱스",
        "CL=F": "WTI 유가",
        "KRW=X": "원·달러",
    }

    WATCHLIST: dict[str, str] = {
        "NVDA": "엔비디아",
        "AMD": "AMD",
        "AVGO": "브로드컴",
        "MU": "마이크론",
        "TSM": "TSMC ADR",
        "ASML": "ASML",
        "QCOM": "퀄컴",
        "AAPL": "애플",
        "MSFT": "마이크로소프트",
        "GOOGL": "알파벳",
        "AMZN": "아마존",
        "META": "메타",
        "TSLA": "테슬라",
        "NFLX": "넷플릭스",
        "LLY": "일라이릴리",
        "MRNA": "모더나",
        "XOM": "엑슨모빌",
        "CVX": "셰브론",
        "BA": "보잉",
        "PLTR": "팔란티어",
    }

    NEWS_QUERIES: tuple[tuple[str, int], ...] = (
        ('US stocks Nasdaq Dow S&P 500 market close Reuters Bloomberg CNBC when:1d', 5),
        ('Federal Reserve Powell Warsh Waller Bowman Bessent speech outlook when:1d', 6),
        ('CPI PPI jobs Treasury yield dollar oil market when:1d', 6),
        ('Nvidia Micron AMD Broadcom TSMC semiconductor earnings AI when:1d', 5),
        ('Tesla Apple Microsoft Amazon Meta Google earnings guidance when:1d', 5),
        ('Wall Street biggest stock gainers losers earnings after hours when:1d', 4),
        ('Middle East Iran Israel Hormuz oil attack ceasefire Reuters when:1d', 7),
        ('Korea stocks Samsung SK Hynix US market impact when:1d', 6),
    )

    KEYWORD_SCORES: dict[str, int] = {
        "federal reserve": 5,
        "fed": 3,
        "powell": 5,
        "warsh": 6,
        "waller": 4,
        "bowman": 4,
        "bessent": 5,
        "cpi": 6,
        "ppi": 6,
        "inflation": 4,
        "jobs": 4,
        "payroll": 5,
        "treasury yield": 5,
        "tariff": 5,
        "sanction": 5,
        "attack": 6,
        "missile": 7,
        "oil": 3,
        "nvidia": 5,
        "micron": 5,
        "semiconductor": 4,
        "earnings": 4,
        "guidance": 5,
        "surge": 3,
        "plunge": 3,
        "record": 3,
    }

    def __init__(self) -> None:
        self.token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
        self.chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()
        if not self.token or not self.chat_id:
            raise RuntimeError("TELEGRAM_BOT_TOKEN 또는 TELEGRAM_CHAT_ID가 비어 있습니다.")
        self.session = requests.Session()
        self.session.headers.update(
            {
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 Chrome/131 Safari/537.36"
                ),
                "Accept-Language": "en-US,en;q=0.9,ko;q=0.8",
            }
        )
        self.translator = GoogleTranslator(source="auto", target="ko")
        self.translation_cache: dict[str, str] = {}

    def run(self) -> str:
        index_quotes = self.fetch_quotes(self.INDEX_SYMBOLS)
        stock_quotes = self.fetch_quotes(self.WATCHLIST)
        news = self.fetch_news()
        events = self.fetch_upcoming_economic_events()
        earnings = self.fetch_earnings_calendar()
        report = self.format_report(index_quotes, stock_quotes, news, events, earnings)
        self.send_telegram(report)
        return report

    def fetch_quotes(self, symbols: dict[str, str]) -> list[Quote]:
        quotes: list[Quote] = []
        for symbol, name in symbols.items():
            try:
                quote = self.fetch_yahoo_quote(symbol, name)
                if quote is not None:
                    quotes.append(quote)
            except Exception as exc:  # noqa: BLE001
                LOGGER.warning("시세 수집 실패 %s: %s", symbol, exc)
            time.sleep(0.08)
        return quotes

    def fetch_yahoo_quote(self, symbol: str, name: str) -> Quote | None:
        encoded = requests.utils.quote(symbol, safe="")
        url = (
            f"https://query1.finance.yahoo.com/v8/finance/chart/{encoded}"
            "?range=7d&interval=1d&events=div%2Csplits"
        )
        response = self.session.get(url, timeout=15)
        response.raise_for_status()
        payload = response.json()
        result = payload.get("chart", {}).get("result") or []
        if not result:
            return None
        chart = result[0]
        timestamps = chart.get("timestamp") or []
        closes = (
            chart.get("indicators", {})
            .get("quote", [{}])[0]
            .get("close", [])
        )
        pairs = [(ts, close) for ts, close in zip(timestamps, closes) if close is not None]
        if len(pairs) < 2:
            return None
        (prev_ts, prev_close), (last_ts, last_close) = pairs[-2], pairs[-1]
        if not prev_close:
            return None
        change_pct = (float(last_close) / float(prev_close) - 1.0) * 100.0
        price = float(last_close)
        if symbol == "^TNX" and price > 20:
            price /= 10.0
        return Quote(
            symbol=symbol,
            name=name,
            price=price,
            change_pct=change_pct,
            as_of=dt.datetime.fromtimestamp(last_ts, tz=UTC).astimezone(ET),
        )

    def fetch_news(self) -> list[News]:
        collected: list[News] = []
        for query, base_score in self.NEWS_QUERIES:
            url = (
                "https://news.google.com/rss/search?q="
                f"{quote_plus(query)}&hl=en-US&gl=US&ceid=US:en"
            )
            try:
                response = self.session.get(url, timeout=15)
                response.raise_for_status()
                parsed = feedparser.parse(response.content)
                for entry in parsed.entries[:20]:
                    title = clean_text(entry.get("title", ""))
                    summary = clean_text(entry.get("summary", entry.get("description", "")))
                    source = clean_text((entry.get("source") or {}).get("title", "Google News"))
                    published = parse_date(entry.get("published", entry.get("updated")))
                    score = base_score + self.score_news(title, summary, source, published)
                    collected.append(
                        News(
                            title=title,
                            link=str(entry.get("link", "")),
                            source=source,
                            summary=summary,
                            published_at=published,
                            score=score,
                        )
                    )
            except Exception as exc:  # noqa: BLE001
                LOGGER.warning("뉴스 수집 실패: %s", exc)
        return self.dedupe_news(collected)[:8]

    def score_news(
        self, title: str, summary: str, source: str, published: dt.datetime | None
    ) -> int:
        text = f"{title} {summary}".lower()
        score = 0
        for keyword, points in self.KEYWORD_SCORES.items():
            if keyword in text:
                score += points
        source_lower = source.lower()
        if any(x in source_lower for x in ("reuters", "bloomberg", "cnbc", "federal reserve")):
            score += 3
        if published:
            now = dt.datetime.now(UTC)
            stamp = published.astimezone(UTC) if published.tzinfo else published.replace(tzinfo=UTC)
            age_hours = (now - stamp).total_seconds() / 3600
            if 0 <= age_hours <= 8:
                score += 4
            elif age_hours > 30:
                score -= 8
        return score

    @staticmethod
    def dedupe_news(items: Iterable[News]) -> list[News]:
        seen: set[str] = set()
        unique: list[News] = []
        for item in sorted(
            items,
            key=lambda x: (x.score, x.published_at or dt.datetime.min.replace(tzinfo=UTC)),
            reverse=True,
        ):
            key = re.sub(r"[^a-z0-9가-힣]", "", item.title.lower())[:100]
            if not key or key in seen:
                continue
            seen.add(key)
            unique.append(item)
        return unique

    def fetch_upcoming_economic_events(self) -> list[str]:
        now_et = dt.datetime.now(ET)
        end_date = (now_et + dt.timedelta(days=2)).date()
        start_date = now_et.date()
        releases = {
            "소비자물가지수(CPI)": "https://www.bls.gov/schedule/news_release/cpi.htm",
            "생산자물가지수(PPI)": "https://www.bls.gov/schedule/news_release/ppi.htm",
            "고용보고서": "https://www.bls.gov/schedule/news_release/empsit.htm",
            "구인·이직(JOLTS)": "https://www.bls.gov/schedule/news_release/jolts.htm",
            "수출입물가": "https://www.bls.gov/schedule/news_release/ximpim.htm",
        }
        results: list[str] = []
        for label, url in releases.items():
            try:
                response = self.session.get(url, timeout=15)
                response.raise_for_status()
                soup = BeautifulSoup(response.text, "html.parser")
                for row in soup.select("tr"):
                    cells = [clean_text(cell.get_text(" ", strip=True)) for cell in row.select("th, td")]
                    if len(cells) < 2:
                        continue
                    row_text = " | ".join(cells)
                    event_date = parse_us_date(row_text, now_et.year)
                    if event_date and start_date <= event_date <= end_date:
                        time_match = re.search(r"\b(?:0?[1-9]|1[0-2]):[0-5]\d\s*(?:AM|PM)\b", row_text, re.I)
                        time_text = time_match.group(0).upper() if time_match else "시간 확인"
                        results.append(f"{event_date:%m/%d} {time_text} ET · {label}")
                        break
            except Exception as exc:  # noqa: BLE001
                LOGGER.warning("경제일정 수집 실패 %s: %s", label, exc)
        return unique_keep_order(results)[:6]

    def fetch_earnings_calendar(self) -> list[str]:
        today_et = dt.datetime.now(ET).date()
        dates = [today_et, today_et + dt.timedelta(days=1)]
        results: list[tuple[float, str]] = []
        for target_date in dates:
            url = f"https://api.nasdaq.com/api/calendar/earnings?date={target_date.isoformat()}"
            try:
                response = self.session.get(
                    url,
                    headers={
                        "Accept": "application/json, text/plain, */*",
                        "Origin": "https://www.nasdaq.com",
                        "Referer": "https://www.nasdaq.com/market-activity/earnings",
                    },
                    timeout=15,
                )
                response.raise_for_status()
                rows = ((response.json().get("data") or {}).get("rows") or [])
                for row in rows:
                    symbol = clean_text(row.get("symbol", ""))
                    name = clean_text(row.get("name", ""))
                    if not symbol:
                        continue
                    cap = parse_market_cap(row.get("marketCap", ""))
                    if symbol in self.WATCHLIST or cap >= 20_000_000_000:
                        timing = clean_text(row.get("time", "시간미정"))
                        results.append(
                            (
                                cap,
                                f"{target_date:%m/%d} · {symbol}({truncate(name, 34)}) · {timing}",
                            )
                        )
            except Exception as exc:  # noqa: BLE001
                LOGGER.warning("실적일정 수집 실패 %s: %s", target_date, exc)
        return [text for _, text in sorted(results, key=lambda x: x[0], reverse=True)[:7]]

    def translate(self, text: str) -> str:
        cleaned = clean_text(text)
        if not cleaned or not needs_translation(cleaned):
            return cleaned
        if cleaned in self.translation_cache:
            return self.translation_cache[cleaned]
        try:
            translated = clean_text(self.translator.translate(cleaned[:4500]))
        except Exception as exc:  # noqa: BLE001
            LOGGER.warning("번역 실패: %s", exc)
            translated = cleaned
        self.translation_cache[cleaned] = translated or cleaned
        return translated or cleaned

    def summarize_news(self, news: News) -> list[str]:
        text = clean_text(news.summary)
        if not text or normalize(text) == normalize(news.title):
            return [
                "기사 제목을 중심으로 시장 영향 키워드를 분류했습니다.",
                "세부 수치와 발언 문맥은 기사 원문에서 확인해야 합니다.",
            ]
        translated = self.translate(text)
        return split_sentences(translated, max_lines=2, max_chars=300)

    def format_report(
        self,
        indices: list[Quote],
        stocks: list[Quote],
        news: list[News],
        events: list[str],
        earnings: list[str],
    ) -> str:
        now = dt.datetime.now(KST)
        quote_map = {quote.symbol: quote for quote in indices}
        lines = [f"🌅 [미국장 마감·국내장 영향 브리핑] {now:%Y-%m-%d %H:%M}", ""]

        lines.append("📊 1. 미국 주요지수")
        for symbol in ("^IXIC", "^DJI", "^GSPC", "^SOX"):
            quote = quote_map.get(symbol)
            if quote:
                lines.append(f"- {quote.name}: {quote.change_pct:+.2f}% ({quote.price:,.2f})")
        if not any(symbol in quote_map for symbol in ("^IXIC", "^DJI", "^GSPC")):
            lines.append("- 무료 시세 데이터 응답 지연으로 지수 수치를 가져오지 못했습니다.")
        lines.append("")

        lines.append("💵 2. 금리·환율·유가·변동성")
        for symbol in ("^TNX", "DX-Y.NYB", "KRW=X", "CL=F", "^VIX"):
            quote = quote_map.get(symbol)
            if not quote:
                continue
            unit = "%" if symbol == "^TNX" else ""
            lines.append(
                f"- {quote.name}: {quote.price:,.2f}{unit} / 전일대비 {quote.change_pct:+.2f}%"
            )
        lines.append("")

        movers = sorted(stocks, key=lambda x: abs(x.change_pct), reverse=True)
        notable = [quote for quote in movers if abs(quote.change_pct) >= 1.5][:7]
        lines.append("🚀 3. 특이 급등락·한국 연관 종목")
        if notable:
            for quote in notable:
                direction = "급등" if quote.change_pct > 0 else "급락"
                lines.append(f"- {quote.name}({quote.symbol}): {quote.change_pct:+.2f}% · {direction}")
        else:
            lines.append("- 감시 종목 중 ±1.5% 이상 특이 변동을 확인하지 못했습니다.")
        lines.append("")

        lines.append("📰 4. 미국장을 움직인 핵심 이슈")
        if news:
            for item in news[:6]:
                title_ko = truncate(self.translate(item.title), 150)
                lines.append(f"- [{item.source}] {title_ko}")
                for summary_line in self.summarize_news(item):
                    lines.append(f"  · {summary_line}")
                if item.link:
                    lines.append(f"  · 링크: {item.link}")
        else:
            lines.append("- 최근 기사 피드에서 조건을 통과한 주요 이슈가 없었습니다.")
        lines.append("")

        lines.append("📅 5. 오늘 밤·다음 미국장 확인 일정")
        combined_schedule = events + earnings
        if combined_schedule:
            for event in combined_schedule[:10]:
                lines.append(f"- {event}")
        else:
            lines.append("- 공식 일정 응답이 없거나 주요 일정이 확인되지 않았습니다.")
        lines.append("")

        lines.append("🇰🇷 6. 다음 국내장 체크포인트")
        for point in self.build_korea_points(quote_map, stocks, news):
            lines.append(f"- {point}")
        lines.append("")
        lines.append("※ 무료 자동수집 자료입니다. 가격 지연·뉴스 누락·번역 오류가 있을 수 있으니 원문과 HTS 시세를 함께 확인하세요.")
        return "\n".join(lines)

    def build_korea_points(
        self, quote_map: dict[str, Quote], stocks: list[Quote], news: list[News]
    ) -> list[str]:
        points: list[str] = []
        sox = quote_map.get("^SOX")
        nasdaq = quote_map.get("^IXIC")
        oil = quote_map.get("CL=F")
        yield10 = quote_map.get("^TNX")
        vix = quote_map.get("^VIX")
        krw = quote_map.get("KRW=X")

        if sox:
            if sox.change_pct >= 1.0:
                points.append("필라델피아 반도체 강세: 삼성전자·SK하이닉스·반도체 장비주 수급 확인")
            elif sox.change_pct <= -1.0:
                points.append("필라델피아 반도체 약세: 국내 반도체 시가와 외국인 선물 방향 경계")
        if nasdaq and abs(nasdaq.change_pct) >= 1.0:
            points.append(
                "나스닥 변동 확대: 국내 성장주·코스닥·선물 갭 출발 가능성 점검"
            )
        if oil:
            if oil.change_pct >= 2.0:
                points.append("유가 급등: 정유·방산은 상대강도, 항공·운송·화학 원가 부담 확인")
            elif oil.change_pct <= -2.0:
                points.append("유가 급락: 항공·운송 비용 완화와 정유주 약세 가능성 동시 점검")
        if yield10 and abs(yield10.change_pct) >= 2.0:
            points.append("미국 10년물 변동 확대: 고PER 성장주와 원·달러 환율 민감도 확인")
        if vix and vix.price >= 20:
            points.append("VIX 20 이상: 지수선물 변동성 확대와 옵션 프리미엄 과열 경계")
        if krw and krw.change_pct >= 0.5:
            points.append("원·달러 상승: 외국인 현·선물 수급과 수입 원가 민감 업종 확인")

        stock_map = {quote.symbol: quote for quote in stocks}
        semi_moves = [stock_map[s].change_pct for s in ("NVDA", "MU", "AMD", "AVGO", "TSM") if s in stock_map]
        if semi_moves and sum(semi_moves) / len(semi_moves) >= 1.0:
            points.append("미국 AI·반도체 감시군 동반 강세: 국내 HBM·기판·장비 대장주 우선 관찰")
        elif semi_moves and sum(semi_moves) / len(semi_moves) <= -1.0:
            points.append("미국 AI·반도체 감시군 동반 약세: 국내 반도체 추격매수보다 지지 확인 우선")

        news_text = " ".join(item.title.lower() for item in news)
        if any(word in news_text for word in ("tariff", "sanction", "export control")):
            points.append("관세·제재·수출규제 뉴스: 반도체·자동차·2차전지 공급망 영향을 원문 확인")
        if not points:
            points.append("지수·금리·유가에 뚜렷한 단일 방향이 없어 국내장은 외국인 선물과 시가봉 확인 우선")
        return unique_keep_order(points)[:7]

    def send_telegram(self, message: str) -> None:
        url = f"https://api.telegram.org/bot{self.token}/sendMessage"
        for chunk in split_telegram(message):
            response = self.session.post(
                url,
                json={
                    "chat_id": self.chat_id,
                    "text": chunk,
                    "disable_web_page_preview": True,
                },
                timeout=20,
            )
            if response.status_code >= 400:
                raise RuntimeError(f"텔레그램 발송 실패: {response.status_code} {response.text[:300]}")
            time.sleep(0.5)


def clean_text(value: Any) -> str:
    text = html.unescape(str(value or ""))
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def truncate(text: str, max_len: int) -> str:
    if len(text) <= max_len:
        return text
    return text[: max(1, max_len - 1)].rstrip() + "…"


def normalize(text: str) -> str:
    return re.sub(r"[^a-z0-9가-힣]", "", clean_text(text).lower())


def needs_translation(text: str) -> bool:
    latin = len(re.findall(r"[A-Za-z]", text))
    hangul = len(re.findall(r"[가-힣]", text))
    return latin >= 5 and latin > hangul * 1.3


def split_sentences(text: str, max_lines: int, max_chars: int) -> list[str]:
    cleaned = clean_text(text)
    sentences = [
        part.strip(" -•")
        for part in re.split(r"(?<=[.!?。])\s+|[\r\n]+", cleaned)
        if part.strip(" -•")
    ] or [cleaned]
    result: list[str] = []
    used = 0
    for sentence in sentences:
        if len(result) >= max_lines or used >= max_chars:
            break
        clipped = truncate(sentence, max(40, max_chars - used))
        result.append(clipped)
        used += len(clipped)
    return result


def parse_date(value: Any) -> dt.datetime | None:
    if not value:
        return None
    try:
        parsed = date_parser.parse(str(value))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
    except Exception:
        return None


def parse_us_date(text: str, default_year: int) -> dt.date | None:
    month_pattern = (
        r"(January|February|March|April|May|June|July|August|September|October|November|December)"
    )
    match = re.search(month_pattern + r"\s+(\d{1,2}),\s*(\d{4})", text, re.I)
    if not match:
        return None
    try:
        return dt.datetime.strptime(
            f"{match.group(1)} {match.group(2)} {match.group(3) or default_year}", "%B %d %Y"
        ).date()
    except Exception:
        return None


def parse_market_cap(value: Any) -> float:
    text = clean_text(value).replace("$", "").replace(",", "")
    match = re.match(r"([0-9.]+)\s*([KMBT]?)", text, re.I)
    if not match:
        return 0.0
    amount = float(match.group(1))
    factor = {"": 1.0, "K": 1e3, "M": 1e6, "B": 1e9, "T": 1e12}.get(match.group(2).upper(), 1.0)
    return amount * factor


def unique_keep_order(values: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if value and value not in seen:
            seen.add(value)
            result.append(value)
    return result


def split_telegram(message: str, max_len: int = 3900) -> list[str]:
    if len(message) <= max_len:
        return [message]
    chunks: list[str] = []
    current = ""
    for block in message.split("\n\n"):
        candidate = block if not current else current + "\n\n" + block
        if len(candidate) <= max_len:
            current = candidate
            continue
        if current:
            chunks.append(current)
        if len(block) <= max_len:
            current = block
        else:
            for start in range(0, len(block), max_len):
                chunks.append(block[start : start + max_len])
            current = ""
    if current:
        chunks.append(current)
    return chunks


if __name__ == "__main__":
    try:
        MorningUSMarketReport().run()
    except Exception as exc:  # noqa: BLE001
        LOGGER.exception("아침 미국시장 브리핑 실패: %s", exc)
        raise SystemExit(1) from exc
