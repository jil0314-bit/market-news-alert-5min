"""증권사 리포트 아침 텔레그램 요약.

공개된 리포트 목록 페이지에서 제목·종목·발행사·날짜·링크만 수집합니다.
PDF 원문을 내려받거나 재배포하지 않습니다.
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import logging
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup, Tag
from dateutil import tz

KST = tz.gettz("Asia/Seoul")
STATE_PATH = Path("data/seen_reports.json")
TIMEOUT = 18
MAX_ITEMS = 12
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 Chrome/126 Safari/537.36 PersonalReportMonitor/1.0"
)


@dataclass(frozen=True)
class ReportItem:
    source: str
    category: str
    company: str
    title: str
    broker: str
    published: str
    link: str
    opinion: str = ""
    target_price: str = ""

    @property
    def key(self) -> str:
        raw = f"{self.source}|{self.company}|{self.title}|{self.published}".lower()
        raw = re.sub(r"\s+", " ", raw).strip()
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()


class ReportDigest:
    def __init__(self) -> None:
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": USER_AGENT, "Accept-Language": "ko-KR,ko;q=0.9,en;q=0.7"})
        logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
        self.log = logging.getLogger("report_digest")

    def fetch_html(self, url: str) -> str:
        response = self.session.get(url, timeout=TIMEOUT)
        response.raise_for_status()
        # 국내 구형 금융 사이트의 EUC-KR/CP949 대응
        if response.encoding is None or response.encoding.lower() in {"iso-8859-1", "ascii"}:
            response.encoding = response.apparent_encoding or "utf-8"
        return response.text

    def collect(self) -> list[ReportItem]:
        collectors = [
            self.collect_naver_company,
            self.collect_naver_industry,
            self.collect_naver_market,
            self.collect_samsung,
            self.collect_mirae,
            self.collect_kiwoom,
        ]
        items: list[ReportItem] = []
        for collector in collectors:
            try:
                got = collector()
                self.log.info("%s: %d건", collector.__name__, len(got))
                items.extend(got)
            except Exception as exc:  # 개별 사이트 실패가 전체 실패로 번지지 않도록 처리
                self.log.warning("%s 실패: %s", collector.__name__, exc)
        return self.dedupe(items)

    def collect_naver_company(self) -> list[ReportItem]:
        return self._collect_naver(
            "https://finance.naver.com/research/company_list.naver",
            "기업",
            "company_read.naver",
        )

    def collect_naver_industry(self) -> list[ReportItem]:
        return self._collect_naver(
            "https://finance.naver.com/research/industry_list.naver",
            "산업",
            "industry_read.naver",
        )

    def collect_naver_market(self) -> list[ReportItem]:
        pages = [
            ("https://finance.naver.com/research/market_info_list.naver", "시장", "market_info_read.naver"),
            ("https://finance.naver.com/research/economy_list.naver", "경제", "economy_read.naver"),
        ]
        result: list[ReportItem] = []
        for url, category, pattern in pages:
            try:
                result.extend(self._collect_naver(url, category, pattern))
            except Exception as exc:
                self.log.warning("네이버 %s 수집 실패: %s", category, exc)
        return result

    def _collect_naver(self, url: str, category: str, href_pattern: str) -> list[ReportItem]:
        soup = BeautifulSoup(self.fetch_html(url), "html.parser")
        items: list[ReportItem] = []
        for anchor in soup.select("a[href]"):
            href = str(anchor.get("href", ""))
            if href_pattern not in href:
                continue
            title = clean(anchor.get_text(" ", strip=True))
            if len(title) < 4:
                continue
            row = anchor.find_parent("tr")
            cells = [clean(td.get_text(" ", strip=True)) for td in row.find_all("td")] if row else []
            company = cells[0] if category == "기업" and cells else ""
            broker = find_broker(cells)
            published = find_date(" ".join(cells))
            opinion = find_opinion(cells)
            target = find_target_price(cells)
            items.append(
                ReportItem(
                    source="네이버금융 리서치",
                    category=category,
                    company=company,
                    title=title,
                    broker=broker,
                    published=published,
                    link=urljoin(url, href),
                    opinion=opinion,
                    target_price=target,
                )
            )
        return recent_only(items)

    def collect_samsung(self) -> list[ReportItem]:
        url = "https://www.samsungpop.com/sscommon/jsp/search/research/research_pop.jsp"
        soup = BeautifulSoup(self.fetch_html(url), "html.parser")
        items: list[ReportItem] = []
        for row in soup.select("tr"):
            text = clean(row.get_text(" ", strip=True))
            published = find_date(text)
            if not published:
                continue
            links = [a for a in row.select("a[href]") if clean(a.get_text(" ", strip=True))]
            if not links:
                continue
            anchor = max(links, key=lambda a: len(clean(a.get_text(" ", strip=True))))
            title = clean(anchor.get_text(" ", strip=True))
            if len(title) < 6:
                continue
            cells = [clean(td.get_text(" ", strip=True)) for td in row.find_all(["td", "th"])]
            broker = "삼성증권"
            author = find_author(cells)
            category = classify_category(title)
            items.append(
                ReportItem(
                    source="삼성증권 리서치",
                    category=category,
                    company=guess_company(title),
                    title=title,
                    broker=f"{broker}{' / ' + author if author else ''}",
                    published=published,
                    link=urljoin(url, str(anchor.get("href", ""))),
                )
            )
        return recent_only(items)

    def collect_mirae(self) -> list[ReportItem]:
        url = "https://securities.miraeasset.com/bbs/board/message/list.do?categoryId=1800"
        soup = BeautifulSoup(self.fetch_html(url), "html.parser")
        return self._generic_broker_rows(soup, url, "미래에셋증권")

    def collect_kiwoom(self) -> list[ReportItem]:
        urls = [
            "https://www1.kiwoom.com/h/invest/research/VAnalCIView",
            "https://www.kiwoom.com/h/invest/research/VAnalCRView",
            "https://www.kiwoom.com/h/invest/research/VAnalSNView",
        ]
        result: list[ReportItem] = []
        for url in urls:
            try:
                soup = BeautifulSoup(self.fetch_html(url), "html.parser")
                result.extend(self._generic_broker_rows(soup, url, "키움증권"))
            except Exception as exc:
                self.log.warning("키움 페이지 실패 %s: %s", url, exc)
        return result

    def _generic_broker_rows(self, soup: BeautifulSoup, base_url: str, broker: str) -> list[ReportItem]:
        items: list[ReportItem] = []
        for row in soup.select("tr, li"):
            text = clean(row.get_text(" ", strip=True))
            published = find_date(text)
            if not published:
                continue
            candidates: list[Tag] = [a for a in row.select("a[href]") if len(clean(a.get_text(" ", strip=True))) >= 5]
            if not candidates:
                continue
            anchor = max(candidates, key=lambda a: len(clean(a.get_text(" ", strip=True))))
            title = clean(anchor.get_text(" ", strip=True))
            if is_navigation_text(title):
                continue
            company = guess_company(title)
            items.append(
                ReportItem(
                    source=f"{broker} 리서치",
                    category=classify_category(title),
                    company=company,
                    title=title,
                    broker=broker,
                    published=published,
                    link=urljoin(base_url, str(anchor.get("href", ""))),
                )
            )
        return recent_only(items)

    @staticmethod
    def dedupe(items: Iterable[ReportItem]) -> list[ReportItem]:
        seen: set[str] = set()
        result: list[ReportItem] = []
        for item in items:
            norm = normalize_title(item.title)
            if not norm or norm in seen:
                continue
            seen.add(norm)
            result.append(item)
        return result

    def new_items(self, items: list[ReportItem], force: bool = False) -> list[ReportItem]:
        seen = load_state()
        candidates = items if force else [item for item in items if item.key not in seen]
        ranked = sorted(candidates, key=rank_score, reverse=True)
        return ranked[:MAX_ITEMS]

    def save_seen(self, items: Iterable[ReportItem]) -> None:
        seen = load_state()
        seen.update(item.key for item in items)
        STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        # 크기가 무한히 늘지 않도록 최근 키만 유지
        STATE_PATH.write_text(json.dumps(list(seen)[-3000:], ensure_ascii=False, indent=2), encoding="utf-8")

    def format_digest(self, items: list[ReportItem]) -> str:
        now = dt.datetime.now(KST).strftime("%Y-%m-%d %H:%M")
        lines = [f"📚 [아침 증권사 리포트] {now}", ""]
        for idx, item in enumerate(items, 1):
            company = item.company or item.category
            lines.append(f"{idx}) [{item.category}] {company}")
            lines.append(f"제목: {item.title}")
            meta = " / ".join(x for x in [item.broker, item.published] if x)
            if meta:
                lines.append(f"발행: {meta}")
            extras = " / ".join(x for x in [item.opinion, item.target_price] if x)
            if extras:
                lines.append(f"의견: {extras}")
            lines.append(f"핵심: {summarize_title(item.title)}")
            lines.append(f"링크: {item.link}")
            lines.append("")
        lines.append("※ 제목·공개 목록만 요약합니다. 투자 전 원문과 목표주가 변경 근거를 확인하세요.")
        return "\n".join(lines).strip()

    def send_telegram(self, message: str) -> None:
        token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
        chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()
        if not token or not chat_id:
            raise RuntimeError("GitHub Secrets의 TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID가 비어 있습니다.")
        for chunk in split_message(message):
            response = self.session.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                json={"chat_id": chat_id, "text": chunk, "disable_web_page_preview": True},
                timeout=TIMEOUT,
            )
            if response.status_code >= 400:
                raise RuntimeError(f"텔레그램 발송 실패: {response.status_code} {response.text[:300]}")


def clean(value: str) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


def normalize_title(value: str) -> str:
    return re.sub(r"[^0-9a-z가-힣]", "", value.lower())


def find_date(text: str) -> str:
    patterns = [r"(20\d{2}[./-]\d{1,2}[./-]\d{1,2})", r"(\d{2}[./-]\d{2}[./-]\d{2})"]
    for pattern in patterns:
        match = re.search(pattern, text)
        if match:
            raw = match.group(1).replace("/", ".").replace("-", ".")
            parts = raw.split(".")
            if len(parts[0]) == 2:
                parts[0] = "20" + parts[0]
            try:
                return dt.date(int(parts[0]), int(parts[1]), int(parts[2])).isoformat()
            except ValueError:
                return ""
    return ""


def recent_only(items: list[ReportItem], days: int = 3) -> list[ReportItem]:
    today = dt.datetime.now(KST).date()
    result: list[ReportItem] = []
    for item in items:
        if not item.published:
            continue
        try:
            day = dt.date.fromisoformat(item.published)
        except ValueError:
            continue
        if dt.timedelta(days=-1) <= today - day <= dt.timedelta(days=days):
            result.append(item)
    return result


def find_broker(cells: list[str]) -> str:
    markers = ("증권", "투자", "리서치", "자산", "Capital", "Research")
    for cell in cells:
        if any(marker.lower() in cell.lower() for marker in markers) and len(cell) <= 30:
            return cell
    return ""


def find_author(cells: list[str]) -> str:
    for cell in cells:
        if re.fullmatch(r"[가-힣]{2,4}", cell):
            return cell
    return ""


def find_opinion(cells: list[str]) -> str:
    words = ("BUY", "매수", "보유", "HOLD", "중립", "매도", "OUTPERFORM")
    for cell in cells:
        if any(word.lower() in cell.lower() for word in words) and len(cell) <= 25:
            return cell
    return ""


def find_target_price(cells: list[str]) -> str:
    for cell in cells:
        compact = cell.replace(",", "").replace("원", "").strip()
        if re.fullmatch(r"\d{4,7}", compact):
            return f"목표 {int(compact):,}원"
    return ""


def guess_company(title: str) -> str:
    # '삼성전자(005930): ...', '삼성전자 - ...' 형태를 우선 인식
    match = re.match(r"\s*([가-힣A-Za-z0-9&. ]{2,25}?)(?:\s*\(\d{6}\)|\s*[:：-])", title)
    return clean(match.group(1)) if match else ""


def classify_category(title: str) -> str:
    lower = title.lower()
    if re.search(r"\(\d{6}\)", title) or any(x in lower for x in ["buy", "목표주가", "실적 리뷰", "실적 프리뷰"]):
        return "기업"
    if any(x in lower for x in ["산업", "sector", "weekly", "데이터북", "업종", "overweight"]):
        return "산업"
    if any(x in lower for x in ["경제", "macro", "금리", "환율", "채권", "cpi", "fomc"]):
        return "경제"
    return "시장"


def is_navigation_text(title: str) -> bool:
    bad = {"상세보기", "다운로드", "이전", "다음", "검색", "목록", "more", "view"}
    return title.lower() in bad or len(title) > 180


def summarize_title(title: str) -> str:
    rules = [
        (("목표주가 상향", "상향 조정", "raise"), "목표주가 상향 또는 실적 기대 강화"),
        (("목표주가 하향", "하향 조정", "lower"), "목표주가 하향 또는 실적 눈높이 조정"),
        (("수주", "계약"), "수주·계약이 실적에 미칠 영향 점검"),
        (("턴어라운드", "회복"), "실적 회복과 턴어라운드 가능성 점검"),
        (("서프라이즈", "호실적"), "예상보다 강한 실적과 지속 여부 점검"),
        (("부진", "둔화", "우려"), "실적 둔화 또는 위험 요인 점검"),
        (("ai", "hbm", "반도체"), "AI·반도체 수요와 업황 방향 점검"),
        (("배터리", "전기차", "리튬"), "배터리·전기차 수요와 수익성 방향 점검"),
        (("금리", "환율", "fomc"), "금리·환율 변화가 시장에 미칠 영향 점검"),
    ]
    lower = title.lower()
    for keys, summary in rules:
        if any(key in lower for key in keys):
            return summary
    return f"보고서 핵심 주장: {title[:75]}"


def rank_score(item: ReportItem) -> tuple[int, str]:
    score = {"기업": 4, "산업": 3, "시장": 2, "경제": 2}.get(item.category, 1)
    text = item.title.lower()
    for word in ["목표주가 상향", "목표주가 하향", "buy", "수주", "서프라이즈", "턴어라운드", "리스크", "실적"]:
        if word in text:
            score += 2
    return score, item.published


def load_state() -> set[str]:
    if not STATE_PATH.exists():
        return set()
    try:
        data = json.loads(STATE_PATH.read_text(encoding="utf-8"))
        return {str(x) for x in data if x}
    except Exception:
        return set()


def split_message(message: str, limit: int = 3900) -> list[str]:
    if len(message) <= limit:
        return [message]
    chunks: list[str] = []
    current = ""
    for block in message.split("\n\n"):
        candidate = f"{current}\n\n{block}".strip()
        if current and len(candidate) > limit:
            chunks.append(current)
            current = block
        else:
            current = candidate
    if current:
        chunks.append(current)
    return chunks


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run", action="store_true", help="새 리포트 수집·발송")
    parser.add_argument("--test", action="store_true", help="텔레그램 연결 시험")
    parser.add_argument("--force", action="store_true", help="중복 기록을 무시하고 최신 리포트 시험 발송")
    args = parser.parse_args()

    bot = ReportDigest()
    if args.test:
        bot.send_telegram("✅ [아침 증권사 리포트] 텔레그램 연결 시험 성공")
        return 0
    if args.run or args.force:
        items = bot.collect()
        selected = bot.new_items(items, force=args.force)
        if not selected:
            logging.info("새 리포트 없음")
            return 0
        bot.send_telegram(bot.format_digest(selected))
        bot.save_seen(selected)
        return 0
    parser.print_help()
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        logging.exception("실행 실패: %s", exc)
        raise
