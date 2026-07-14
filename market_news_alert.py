"""주식시장 영향 속보 텔레그램 알림 봇.

이 프로그램은 공식 API/RSS를 주기적으로 확인하고, 주식시장에 영향이 큰 뉴스/공시만
점수화해서 텔레그램으로 보냅니다.

주의:
- 투자 판단을 보조하는 알림 도구입니다. 매수/매도 추천기가 아닙니다.
- 뉴스 API/RSS 지연, 누락, 중복, 오보 가능성이 있습니다.
- 로이터 등 유료/라이선스 뉴스는 무단 크롤링하지 않습니다.
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import html
import json
import logging
import os
import re
import sqlite3
import sys
import time
from dataclasses import dataclass, field
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any, Iterable, Mapping
from urllib.parse import quote, quote_plus

import feedparser
import requests
import yaml
from deep_translator import GoogleTranslator
from dateutil import parser as date_parser
from dateutil import tz


@dataclass(frozen=True)
class NewsItem:
    """뉴스/공시 1건."""

    source: str
    title: str
    link: str
    published_at: dt.datetime | None = None
    summary: str = ""
    raw: dict[str, Any] = field(default_factory=dict)
    priority: int = 1
    item_type: str = "news"  # news, disclosure


@dataclass
class ScoredItem:
    """점수화된 알림 후보."""

    item: NewsItem
    score: int
    grade: str
    matched_keywords: list[str]
    sectors: list[str]
    related_stocks: list[str]
    bias: str
    reason: str
    signals: list[str] = field(default_factory=list)


class ConfigError(RuntimeError):
    """설정 오류."""


class MarketNewsAlertBot:
    """시장 영향 뉴스/공시 알림 엔진."""

    def __init__(self, config_path: Path) -> None:
        self.config_path = config_path
        self.config = self._load_config(config_path)
        self.timezone = tz.gettz(self.config["runtime"].get("timezone", "Asia/Seoul"))
        if self.timezone is None:
            raise ConfigError("runtime.timezone 값이 올바르지 않습니다.")
        self.logger = self._setup_logger()
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": self.config["runtime"].get("user_agent", "MarketNewsAlertBot/1.0")})
        self.db_path = Path(self.config["runtime"].get("sqlite_path", "data/seen_news.sqlite3"))
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._translation_cache: dict[str, str] = {}
        translation_conf = self.config.get("translation", {})
        self.translation_enabled = bool(translation_conf.get("enabled", True))
        self.translation_target = str(translation_conf.get("target_language", "ko"))
        self.translation_summary_chars = int(translation_conf.get("summary_max_chars", 260))
        self.translation_open_page = bool(translation_conf.get("open_translated_page", True))
        self._translator = (
            GoogleTranslator(source="auto", target=self.translation_target)
            if self.translation_enabled
            else None
        )
        self._init_db()

    @staticmethod
    def _load_config(path: Path) -> dict[str, Any]:
        if not path.exists():
            raise ConfigError(f"설정파일이 없습니다: {path}")
        with path.open("r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        required = ["telegram", "runtime", "sources", "keyword_scores", "sectors"]
        missing = [key for key in required if key not in data]
        if missing:
            raise ConfigError(f"설정파일에 필수 항목이 없습니다: {missing}")

        # GitHub Actions에서는 비밀값을 저장소 파일에 넣지 않고 Secrets → 환경변수로 주입합니다.
        telegram = data.setdefault("telegram", {})
        env_token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
        env_chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()
        if env_token:
            telegram["bot_token"] = env_token
        if env_chat_id:
            telegram["chat_id"] = env_chat_id
        if env_token and env_chat_id:
            telegram["enabled"] = True

        naver = data.setdefault("sources", {}).setdefault("naver", {})
        if os.getenv("NAVER_CLIENT_ID", "").strip():
            naver["client_id"] = os.getenv("NAVER_CLIENT_ID", "").strip()
        if os.getenv("NAVER_CLIENT_SECRET", "").strip():
            naver["client_secret"] = os.getenv("NAVER_CLIENT_SECRET", "").strip()
        if naver.get("client_id") and naver.get("client_secret"):
            naver["enabled"] = True

        dart = data.setdefault("sources", {}).setdefault("dart", {})
        if os.getenv("DART_API_KEY", "").strip():
            dart["api_key"] = os.getenv("DART_API_KEY", "").strip()
            dart["enabled"] = True
        return data

    def _setup_logger(self) -> logging.Logger:
        log_path = Path(self.config["runtime"].get("log_path", "logs/market_news_alert.log"))
        log_path.parent.mkdir(parents=True, exist_ok=True)
        logger = logging.getLogger("market_news_alert")
        logger.setLevel(logging.INFO)
        logger.handlers.clear()

        formatter = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
        file_handler = RotatingFileHandler(log_path, maxBytes=2_000_000, backupCount=5, encoding="utf-8")
        file_handler.setFormatter(formatter)
        logger.addHandler(file_handler)

        console_handler = logging.StreamHandler(sys.stdout)
        console_handler.setFormatter(formatter)
        logger.addHandler(console_handler)
        return logger

    def _init_db(self) -> None:
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS seen_items (
                    item_hash TEXT PRIMARY KEY,
                    title TEXT NOT NULL,
                    link TEXT NOT NULL,
                    source TEXT NOT NULL,
                    sent_at TEXT NOT NULL,
                    score INTEGER NOT NULL
                )
                """
            )
            conn.commit()

    def run_once(self, dry_run: bool = False, force_send: bool = False) -> list[ScoredItem]:
        """한 번 실행한다."""
        self.logger.info("뉴스/공시 수집 시작")
        items = self.fetch_all_items()
        self.logger.info("수집 완료: %s건", len(items))

        scored = [self.score_item(item) for item in items]
        scored = [s for s in scored if self.should_send(s, force_send=force_send)]
        scored = self._sort_items(scored)

        max_items = int(self.config["runtime"].get("max_items_per_message", 5))
        scored = scored[:max_items]

        if not scored:
            self.logger.info("발송 대상 없음")
            if dry_run:
                print("발송 대상 없음: 조건을 통과한 뉴스/공시가 없습니다.")
            return []

        message = self.format_message(scored)
        print(message)
        if not dry_run:
            self.send_telegram(message)
            self.mark_as_seen(scored)
        else:
            self.logger.info("dry-run 모드: 텔레그램 발송 생략")
        return scored

    def run_live(self) -> None:
        """계속 실행한다."""
        self.logger.info("실시간 모드 시작. Ctrl+C로 종료합니다.")
        while True:
            try:
                self.run_once(dry_run=False)
            except KeyboardInterrupt:
                self.logger.info("사용자 종료")
                raise
            except Exception as exc:  # noqa: BLE001 - 장시간 실행 안정성 우선
                self.logger.exception("실행 중 오류: %s", exc)

            interval = self.get_polling_interval_sec()
            self.logger.info("다음 확인까지 %s초 대기", interval)
            time.sleep(interval)

    def get_polling_interval_sec(self) -> int:
        """현재 시간대별 확인 주기."""
        polling = self.config.get("polling", {})
        now = dt.datetime.now(self.timezone)
        if now.weekday() >= 5:
            return int(polling.get("weekend_interval_sec", 900))

        open_t = self._parse_hhmm(polling.get("market_open", "08:30"))
        close_t = self._parse_hhmm(polling.get("market_close", "15:45"))
        now_t = now.time()
        if open_t <= now_t <= close_t:
            return int(polling.get("market_interval_sec", 60))
        return int(polling.get("normal_interval_sec", 300))

    @staticmethod
    def _parse_hhmm(value: str) -> dt.time:
        hour, minute = value.split(":")
        return dt.time(int(hour), int(minute))

    def fetch_all_items(self) -> list[NewsItem]:
        """모든 소스 수집."""
        items: list[NewsItem] = []
        sources = self.config.get("sources", {})

        if sources.get("rss", {}).get("enabled", False):
            items.extend(self.fetch_rss_items())
        if sources.get("naver", {}).get("enabled", False):
            items.extend(self.fetch_naver_items())
        if sources.get("dart", {}).get("enabled", False):
            items.extend(self.fetch_dart_items())

        return self._dedupe_in_memory(items)

    def fetch_rss_items(self) -> list[NewsItem]:
        """RSS 피드 수집."""
        rss_conf = self.config["sources"].get("rss", {})
        timeout = int(self.config["runtime"].get("request_timeout_sec", 10))
        items: list[NewsItem] = []
        for feed in rss_conf.get("feeds", []):
            name = str(feed.get("name", "RSS"))
            url = str(feed.get("url", ""))
            priority = int(feed.get("priority", 1))
            if not url:
                continue
            try:
                response = self.session.get(url, timeout=timeout)
                response.raise_for_status()
                parsed = feedparser.parse(response.content)
                for entry in parsed.entries:
                    title = clean_text(entry.get("title", ""))
                    link = str(entry.get("link", ""))
                    summary = clean_text(entry.get("summary", entry.get("description", "")))
                    published_at = parse_entry_time(entry)
                    items.append(
                        NewsItem(
                            source=name,
                            title=title,
                            link=link,
                            published_at=published_at,
                            summary=summary,
                            raw=dict(entry),
                            priority=priority,
                        )
                    )
            except Exception as exc:  # noqa: BLE001
                self.logger.warning("RSS 수집 실패: %s / %s", name, exc)
        return items

    def fetch_naver_items(self) -> list[NewsItem]:
        """네이버 뉴스 검색 API 수집."""
        conf = self.config["sources"].get("naver", {})
        client_id = conf.get("client_id", "")
        client_secret = conf.get("client_secret", "")
        if not client_id or not client_secret:
            self.logger.warning("네이버 API 키가 없어 네이버 수집 생략")
            return []

        display = int(conf.get("display", 20))
        sort = str(conf.get("sort", "date"))
        timeout = int(self.config["runtime"].get("request_timeout_sec", 10))
        headers = {
            "X-Naver-Client-Id": client_id,
            "X-Naver-Client-Secret": client_secret,
        }
        items: list[NewsItem] = []
        for query in conf.get("queries", []):
            try:
                url = (
                    "https://openapi.naver.com/v1/search/news.json"
                    f"?query={quote_plus(str(query))}&display={display}&sort={sort}"
                )
                response = self.session.get(url, headers=headers, timeout=timeout)
                response.raise_for_status()
                data = response.json()
                for row in data.get("items", []):
                    title = clean_text(row.get("title", ""))
                    summary = clean_text(row.get("description", ""))
                    link = row.get("originallink") or row.get("link") or ""
                    published_at = parse_datetime_safely(row.get("pubDate"))
                    items.append(
                        NewsItem(
                            source=f"네이버뉴스:{query}",
                            title=title,
                            link=link,
                            published_at=published_at,
                            summary=summary,
                            raw=row,
                            priority=3,
                        )
                    )
            except Exception as exc:  # noqa: BLE001
                self.logger.warning("네이버 수집 실패: %s / %s", query, exc)
        return items

    def fetch_dart_items(self) -> list[NewsItem]:
        """OpenDART 공시 목록 수집."""
        conf = self.config["sources"].get("dart", {})
        api_key = conf.get("api_key", "")
        if not api_key:
            self.logger.warning("DART API 키가 없어 공시 수집 생략")
            return []

        now = dt.datetime.now(self.timezone)
        days_back = int(conf.get("days_back", 1))
        bgn_de = (now - dt.timedelta(days=days_back)).strftime("%Y%m%d")
        end_de = now.strftime("%Y%m%d")
        params: dict[str, str | int] = {
            "crtfc_key": api_key,
            "bgn_de": bgn_de,
            "end_de": end_de,
            "page_count": 100,
        }
        corp_cls = str(conf.get("corp_cls", "")).strip()
        if corp_cls:
            params["corp_cls"] = corp_cls

        timeout = int(self.config["runtime"].get("request_timeout_sec", 10))
        try:
            response = self.session.get("https://opendart.fss.or.kr/api/list.json", params=params, timeout=timeout)
            response.raise_for_status()
            data = response.json()
        except Exception as exc:  # noqa: BLE001
            self.logger.warning("DART 수집 실패: %s", exc)
            return []

        if data.get("status") not in ("000", None):
            self.logger.warning("DART 응답 오류: %s / %s", data.get("status"), data.get("message"))
            return []

        items: list[NewsItem] = []
        for row in data.get("list", []) or []:
            report_nm = clean_text(row.get("report_nm", ""))
            corp_name = clean_text(row.get("corp_name", ""))
            title = f"{corp_name} - {report_nm}" if corp_name else report_nm
            rcept_no = row.get("rcept_no", "")
            link = f"https://dart.fss.or.kr/dsaf001/main.do?rcpNo={rcept_no}" if rcept_no else ""
            published_at = parse_dart_time(row.get("rcept_dt"), self.timezone)
            items.append(
                NewsItem(
                    source="DART공시",
                    title=title,
                    link=link,
                    published_at=published_at,
                    summary=report_nm,
                    raw=row,
                    priority=5,
                    item_type="disclosure",
                )
            )
        return items

    def score_item(self, item: NewsItem) -> ScoredItem:
        """뉴스/공시 점수화."""
        text = f"{item.title} {item.summary}".lower()
        score = int(item.priority)
        matched: list[str] = []
        signals: list[str] = []
        bias_points = {"good": 0, "bad": 0, "critical": 0, "market": 0}

        # 신뢰도 높은 공식/주요 매체 경유 소스는 소폭 가산합니다.
        source_lower = item.source.lower()
        for source_word, bonus in self.config.get("source_bonuses", {}).items():
            if str(source_word).lower() in source_lower:
                score += int(bonus)
                matched.append(str(source_word))

        for group, rule in self.config.get("keyword_scores", {}).items():
            group_score = int(rule.get("score", 0))
            for word in rule.get("words", []):
                if keyword_in_text(str(word), text):
                    matched.append(str(word))
                    score += group_score
                    if group in bias_points:
                        bias_points[group] += group_score

        # 단순 이름 언급이 아니라, 주요 인사/기관 + 발언·전망 표현이 함께 있을 때만 강하게 가산합니다.
        for rule_name, rule in self.config.get("combination_rules", {}).items():
            subjects = [str(x) for x in rule.get("subjects", []) if keyword_in_text(str(x), text)]
            triggers = [str(x) for x in rule.get("triggers", []) if keyword_in_text(str(x), text)]
            if subjects and triggers:
                score += int(rule.get("score", 0))
                label = str(rule.get("label", rule_name))
                signals.append(label)
                matched.extend(subjects[:2])
                matched.extend(triggers[:2])

        # 공시는 뉴스보다 액션성이 높으므로 추가점수
        if item.item_type == "disclosure":
            score += 4
            matched.append("공시")

        sectors, related = self.detect_sectors(text)
        if sectors:
            score += min(5, len(sectors) * 2)

        # 제목에 속보/긴급/단독이 있으면 우선순위 상승. 단, 단독은 과열 기사도 많아서 낮은 점수.
        for headline_word, add_score in {"속보": 4, "긴급": 4, "단독": 2, "장중": 2}.items():
            if headline_word in item.title:
                score += add_score
                matched.append(headline_word)

        # 너무 오래된 것은 점수 감점
        if item.published_at is not None:
            age_min = self.age_minutes(item.published_at)
            if age_min > int(self.config["runtime"].get("max_news_age_minutes", 180)):
                score -= 20
            elif age_min <= 30:
                score += 2

        if bias_points["critical"] > 0 or score >= int(self.config["filters"].get("urgent_score", 14)):
            grade = "A급 긴급"
        elif score >= int(self.config["filters"].get("min_score_to_send", 8)):
            grade = "B급 중요"
        else:
            grade = "관찰"

        bias = self.determine_bias(bias_points)
        reason = self.make_reason(matched, sectors, bias, item.item_type, signals)
        return ScoredItem(
            item=item,
            score=score,
            grade=grade,
            matched_keywords=unique_keep_order(matched),
            sectors=sectors,
            related_stocks=related,
            bias=bias,
            reason=reason,
            signals=unique_keep_order(signals),
        )

    def detect_sectors(self, text: str) -> tuple[list[str], list[str]]:
        sectors: list[str] = []
        related: list[str] = []
        for sector_name, info in self.config.get("sectors", {}).items():
            for kw in info.get("keywords", []):
                if keyword_in_text(str(kw), text):
                    sectors.append(str(sector_name))
                    related.append(str(info.get("related", "")))
                    break
        return unique_keep_order(sectors), [x for x in unique_keep_order(related) if x]

    @staticmethod
    def determine_bias(points: Mapping[str, int]) -> str:
        if points.get("critical", 0) > 0:
            return "위험/방어 우선"
        good = points.get("good", 0)
        bad = points.get("bad", 0)
        market = points.get("market", 0)
        if bad >= good + 4:
            return "부정 가능성"
        if good >= bad + 4:
            return "긍정 가능성"
        if market > 0:
            return "시장변수 확인"
        return "중립/확인 필요"

    @staticmethod
    def make_reason(
        matched: list[str], sectors: list[str], bias: str, item_type: str, signals: list[str]
    ) -> str:
        if item_type == "disclosure":
            return "공식 공시 기반이므로 뉴스보다 우선 확인"
        if signals:
            return f"{', '.join(unique_keep_order(signals)[:2])}: 원문 발언과 시장 반응 확인"
        if sectors and matched:
            return f"{', '.join(sectors[:2])} 관련 핵심어 감지"
        if matched:
            return "시장 민감 키워드 감지"
        return bias

    def should_send(self, scored: ScoredItem, force_send: bool = False) -> bool:
        """점수·관련성·중복 여부를 함께 검사한다.

        매체 이름만으로 점수가 올라 일반 기사까지 발송되는 일을 막기 위해,
        기본적으로 키워드·조합신호·섹터·공시 중 하나가 있어야 한다.
        """
        if force_send:
            return not self.is_seen(scored)
        if scored.score < int(self.config["filters"].get("min_score_to_send", 8)):
            return False
        require_signal = bool(self.config["filters"].get("require_relevance_signal", True))
        has_signal = bool(
            scored.matched_keywords
            or scored.signals
            or scored.sectors
            or scored.item.item_type == "disclosure"
        )
        if require_signal and not has_signal:
            return False
        if self.is_seen(scored):
            return False
        return True

    def is_seen(self, scored: ScoredItem) -> bool:
        item_hash = self.make_hash(scored.item)
        with sqlite3.connect(self.db_path) as conn:
            row = conn.execute("SELECT 1 FROM seen_items WHERE item_hash = ?", (item_hash,)).fetchone()
        return row is not None

    def mark_as_seen(self, scored_items: Iterable[ScoredItem]) -> None:
        now = dt.datetime.now(self.timezone).isoformat()
        with sqlite3.connect(self.db_path) as conn:
            for scored in scored_items:
                item_hash = self.make_hash(scored.item)
                conn.execute(
                    """
                    INSERT OR IGNORE INTO seen_items(item_hash, title, link, source, sent_at, score)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (item_hash, scored.item.title, scored.item.link, scored.item.source, now, scored.score),
                )
            conn.commit()

    @staticmethod
    def make_hash(item: NewsItem) -> str:
        key = normalize_for_hash(f"{item.title}|{item.link}")
        return hashlib.sha256(key.encode("utf-8")).hexdigest()

    def _sort_items(self, items: list[ScoredItem]) -> list[ScoredItem]:
        return sorted(
            items,
            key=lambda x: (
                x.score,
                x.item.published_at or dt.datetime.min.replace(tzinfo=dt.timezone.utc),
            ),
            reverse=True,
        )

    def _dedupe_in_memory(self, items: list[NewsItem]) -> list[NewsItem]:
        seen: set[str] = set()
        result: list[NewsItem] = []
        for item in items:
            if not item.title:
                continue
            key = normalize_for_hash(item.title)
            if key in seen:
                continue
            seen.add(key)
            result.append(item)
        return result

    def age_minutes(self, published_at: dt.datetime) -> int:
        now = dt.datetime.now(self.timezone)
        if published_at.tzinfo is None:
            published_at = published_at.replace(tzinfo=self.timezone)
        else:
            published_at = published_at.astimezone(self.timezone)
        delta = now - published_at
        return max(0, int(delta.total_seconds() // 60))

    def translate_to_korean(self, text: str) -> str:
        """영문 텍스트를 한국어로 번역한다. 실패 시 원문을 반환한다."""
        cleaned = clean_text(text)
        if not cleaned or not self.translation_enabled or not needs_korean_translation(cleaned):
            return cleaned
        cached = self._translation_cache.get(cleaned)
        if cached is not None:
            return cached
        if self._translator is None:
            return cleaned
        try:
            # 번역 서비스의 단일 요청 길이 제한을 피하기 위해 짧게 자릅니다.
            source_text = cleaned[:4500]
            translated = clean_text(self._translator.translate(source_text))
            result = translated or cleaned
        except Exception as exc:  # noqa: BLE001 - 번역 실패가 전체 알림을 막으면 안 됨
            self.logger.warning("한글 번역 실패: %s", exc)
            result = cleaned
        self._translation_cache[cleaned] = result
        return result

    def make_korean_summary(self, item: NewsItem, translated_title: str) -> list[str]:
        """RSS 설명을 한국어로 바꾸고 최대 두 줄로 정리한다.

        원문에 설명이 없으면 내용을 지어내지 않고 제목 기반 안내문을 반환한다.
        """
        raw_summary = clean_text(item.summary)
        if raw_summary and normalize_for_hash(raw_summary) != normalize_for_hash(item.title):
            translated = self.translate_to_korean(raw_summary)
            lines = split_summary_lines(translated, max_lines=2, max_chars=self.translation_summary_chars)
            if lines:
                return lines
        return [
            f"{truncate(translated_title, 170)} 관련 기사입니다.",
            "원문 요약을 제공하지 않는 피드이므로 세부 내용은 링크에서 확인하세요.",
        ]

    def make_display_link(self, item: NewsItem) -> tuple[str, str | None]:
        """영문 기사면 한국어 웹번역 링크와 원문 링크를 함께 반환한다."""
        if not item.link:
            return "", None
        if self.translation_open_page and needs_korean_translation(f"{item.title} {item.summary}"):
            translated_url = (
                "https://translate.google.com/translate"
                f"?sl=auto&tl=ko&u={quote(item.link, safe='')}"
            )
            return translated_url, item.link
        return item.link, None

    def format_message(self, items: list[ScoredItem]) -> str:
        now = dt.datetime.now(self.timezone).strftime("%Y-%m-%d %H:%M")
        title = self.config["runtime"].get("mode_name", "시장영향 속보")
        lines = [f"🚨 [{title}] {now}", ""]

        for idx, scored in enumerate(items, start=1):
            item = scored.item
            time_text = "시간미상"
            if item.published_at:
                time_text = item.published_at.astimezone(self.timezone).strftime("%H:%M")
            sector_text = ", ".join(scored.sectors) if scored.sectors else "미분류"
            related_text = " / ".join(scored.related_stocks[:2]) if scored.related_stocks else "직접 확인"
            keyword_text = ", ".join(scored.matched_keywords[:6]) if scored.matched_keywords else "없음"
            translated_title = self.translate_to_korean(item.title)
            clean_title = truncate(translated_title, 150)
            summary_lines = self.make_korean_summary(item, translated_title)
            display_link, original_link = self.make_display_link(item)

            lines.extend(
                [
                    f"{idx}) {grade_icon(scored.grade)} {scored.grade} / 점수 {scored.score}",
                    f"제목(한글): {clean_title}",
                    "한글 요약:",
                    *[f"- {line}" for line in summary_lines],
                    f"출처: {item.source} / {time_text}",
                    f"영향: {scored.bias}",
                    f"섹터: {sector_text}",
                    f"관련: {related_text}",
                    f"핵심신호: {', '.join(scored.signals) if scored.signals else '일반 시장뉴스'}",
                    f"키워드: {keyword_text}",
                    f"해석: {scored.reason}",
                ]
            )
            if display_link:
                link_label = "한글로 열기" if original_link else "링크"
                lines.append(f"{link_label}: {display_link}")
            if original_link:
                lines.append(f"원문: {original_link}")
            lines.append("")

        lines.append("※ 자동 필터링 알림입니다. 매수/매도 결정 전 원문·차트·수급을 반드시 확인하세요.")
        return "\n".join(lines).strip()

    def send_telegram(self, message: str) -> None:
        telegram = self.config.get("telegram", {})
        if not telegram.get("enabled", False):
            self.logger.info("telegram.enabled=false: 발송 생략")
            return
        token = telegram.get("bot_token", "")
        chat_id = telegram.get("chat_id", "")
        if not token or not chat_id:
            raise ConfigError("텔레그램 bot_token/chat_id가 비어 있습니다.")

        chunks = split_telegram_message(message)
        for chunk in chunks:
            url = f"https://api.telegram.org/bot{token}/sendMessage"
            payload = {
                "chat_id": chat_id,
                "text": chunk,
                "disable_web_page_preview": bool(telegram.get("disable_web_page_preview", True)),
            }
            timeout = int(self.config["runtime"].get("request_timeout_sec", 10))
            response = self.session.post(url, json=payload, timeout=timeout)
            if response.status_code >= 400:
                raise RuntimeError(f"텔레그램 발송 실패: {response.status_code} {response.text[:300]}")
            time.sleep(0.4)

    def send_test_message(self) -> None:
        message = "✅ [시장영향 속보] 텔레그램 연결 테스트 성공\n이 메시지가 보이면 토큰과 chat_id가 정상입니다."
        self.send_telegram(message)


def clean_text(value: Any) -> str:
    """HTML 태그/엔티티를 제거한다."""
    text = html.unescape(str(value or ""))
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def needs_korean_translation(text: str) -> bool:
    """한글보다 영문 비중이 큰 텍스트인지 판단한다."""
    cleaned = clean_text(text)
    if not cleaned:
        return False
    hangul_count = len(re.findall(r"[가-힣]", cleaned))
    latin_count = len(re.findall(r"[A-Za-z]", cleaned))
    return latin_count >= 5 and latin_count > hangul_count * 1.5


def split_summary_lines(text: str, max_lines: int = 2, max_chars: int = 260) -> list[str]:
    """번역문을 읽기 쉬운 최대 두 줄 요약으로 자른다."""
    cleaned = clean_text(text)
    if not cleaned:
        return []
    sentences = [
        part.strip(" -•")
        for part in re.split(r"(?<=[.!?。])\s+|[\r\n]+", cleaned)
        if part.strip(" -•")
    ]
    if not sentences:
        sentences = [cleaned]
    result: list[str] = []
    used = 0
    for sentence in sentences:
        if len(result) >= max_lines or used >= max_chars:
            break
        remaining = max_chars - used
        clipped = truncate(sentence, max(30, remaining))
        result.append(clipped)
        used += len(clipped)
    if len(result) == 1 and len(result[0]) > 150:
        midpoint = min(len(result[0]) // 2, 130)
        split_at = result[0].rfind(" ", 50, midpoint + 20)
        if split_at > 50:
            first = result[0][:split_at].strip()
            second = result[0][split_at:].strip()
            result = [first, truncate(second, max_chars - len(first))]
    return result[:max_lines]


def keyword_in_text(keyword: str, lower_text: str) -> bool:
    """키워드 포함 여부. 대소문자와 공백 잡음을 완화한다."""
    kw = keyword.strip().lower()
    if not kw:
        return False
    return kw in lower_text


def parse_entry_time(entry: Any) -> dt.datetime | None:
    """feedparser entry에서 시간 추출."""
    for key in ("published", "updated", "created"):
        if entry.get(key):
            parsed = parse_datetime_safely(entry.get(key))
            if parsed:
                return parsed
    return None


def parse_datetime_safely(value: Any) -> dt.datetime | None:
    if not value:
        return None
    try:
        parsed = date_parser.parse(str(value))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=dt.timezone.utc)
        return parsed
    except Exception:
        return None


def parse_dart_time(value: Any, timezone: dt.tzinfo | None) -> dt.datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return dt.datetime.strptime(text, "%Y%m%d").replace(tzinfo=timezone)
    except ValueError:
        return None


def normalize_for_hash(value: str) -> str:
    text = clean_text(value).lower()
    text = re.sub(r"https?://\S+", "", text)
    text = re.sub(r"[^0-9a-z가-힣]+", "", text)
    return text


def unique_keep_order(values: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if value not in seen:
            result.append(value)
            seen.add(value)
    return result


def truncate(value: str, max_len: int) -> str:
    return value if len(value) <= max_len else value[: max_len - 1] + "…"


def grade_icon(grade: str) -> str:
    if "A급" in grade:
        return "🔴"
    if "B급" in grade:
        return "🟠"
    return "⚪"


def split_telegram_message(message: str, max_len: int = 3900) -> list[str]:
    if len(message) <= max_len:
        return [message]
    chunks: list[str] = []
    current: list[str] = []
    current_len = 0
    for line in message.splitlines():
        line_len = len(line) + 1
        if current and current_len + line_len > max_len:
            chunks.append("\n".join(current))
            current = []
            current_len = 0
        current.append(line)
        current_len += line_len
    if current:
        chunks.append("\n".join(current))
    return chunks


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="주식시장 영향 속보 텔레그램 알림 봇")
    parser.add_argument("--config", default="config.yaml", help="설정파일 경로")
    parser.add_argument("--once", action="store_true", help="한 번만 실행")
    parser.add_argument("--live", action="store_true", help="계속 실행")
    parser.add_argument("--dry-run", action="store_true", help="텔레그램 발송 없이 화면에만 출력")
    parser.add_argument("--force", action="store_true", help="점수 조건은 보되 중복이 아니면 강제 후보 처리")
    parser.add_argument("--test-telegram", action="store_true", help="텔레그램 테스트 메시지 발송")
    parser.add_argument("--show-config", action="store_true", help="설정파일 로딩 확인")
    return parser


def main() -> int:
    parser = build_arg_parser()
    args = parser.parse_args()
    try:
        bot = MarketNewsAlertBot(Path(args.config))
        if args.show_config:
            safe_config = json.loads(json.dumps(bot.config, ensure_ascii=False))
            if "telegram" in safe_config:
                safe_config["telegram"]["bot_token"] = "***SET***" if bot.config.get("telegram", {}).get("bot_token") else "***EMPTY***"
                safe_config["telegram"]["chat_id"] = "***SET***" if bot.config.get("telegram", {}).get("chat_id") else "***EMPTY***"
            if "sources" in safe_config:
                safe_config["sources"].get("naver", {})["client_id"] = "***"
                safe_config["sources"].get("naver", {})["client_secret"] = "***"
                safe_config["sources"].get("dart", {})["api_key"] = "***"
            print(yaml.safe_dump(safe_config, allow_unicode=True, sort_keys=False))
            return 0
        if args.test_telegram:
            bot.send_test_message()
            return 0
        if args.live:
            bot.run_live()
            return 0
        # 기본은 한 번 실행. 실수로 무한실행되는 것을 막기 위함.
        bot.run_once(dry_run=args.dry_run, force_send=args.force)
        return 0
    except KeyboardInterrupt:
        return 130
    except Exception as exc:  # noqa: BLE001
        print(f"오류: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
