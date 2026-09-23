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
from dataclasses import dataclass, field, replace as dc_replace
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
    dup_count: int = 0                 # 같은 사건으로 묶여 생략된 기사 수
    dup_sources: tuple[str, ...] = ()  # 생략된 기사의 매체 이름


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
        self.translation_summary_chars = max(520, int(translation_conf.get("summary_max_chars", 520)))
        self.translation_open_page = bool(translation_conf.get("open_translated_page", True))
        self._translator = (
            GoogleTranslator(source="auto", target=self.translation_target)
            if self.translation_enabled
            else None
        )

        # 근접중복 판정 기준. config.filters 에서 조정한다.
        filters_conf = self.config.get("filters", {})
        self._dup_threshold = float(filters_conf.get("duplicate_title_similarity", 0.34))
        self._dup_containment = float(filters_conf.get("duplicate_containment_ratio", 0.75))
        self._dup_window_hours = int(filters_conf.get("duplicate_window_hours", 48))
        self._dup_retention_days = int(filters_conf.get("duplicate_retention_days", 30))
        self._dup_min_shared_words = int(filters_conf.get("duplicate_min_shared_words", 4))
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
            # 과거 발송분과 제목을 비교할 때 기간으로 먼저 걸러내기 위한 인덱스
            conn.execute("CREATE INDEX IF NOT EXISTS idx_seen_items_sent_at ON seen_items(sent_at)")
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
        max_per_topic = int(self.config["runtime"].get("max_items_per_topic", 2))
        max_per_source = int(self.config["runtime"].get("max_items_per_source", 2))
        scored = self._limit_per_topic(scored, max_per_topic, max_per_source)[:max_items]

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

        sectors, related = self.detect_sectors(text, item.title.lower())
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
            future_min = self.minutes_ahead(item.published_at)
            if future_min > 120:
                # 발행시각이 미래로 찍힌 피드는 신선도를 신뢰할 수 없다.
                score -= 10
            elif age_min > int(self.config["runtime"].get("max_news_age_minutes", 180)):
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

    def detect_sectors(self, text: str, title_text: str = "") -> tuple[list[str], list[str]]:
        """기사에 해당하는 섹터를 찾는다.

        제목만 본다. 요약까지 같이 보면 본문에 한 번 스친 단어로 엉뚱한 섹터가 붙는다.
        (예: 집값 기사의 요약에 "반도체 수출 호조"가 있어 반도체로 분류되던 문제)
        제목이 없을 때만 요약을 쓴다.
        """
        configured = self.config.get("sectors", {})

        def scan(haystack: str) -> tuple[list[str], list[str]]:
            names: list[str] = []
            stocks: list[str] = []
            for sector_name, info in configured.items():
                for kw in info.get("keywords", []):
                    if keyword_in_text(str(kw), haystack):
                        names.append(str(sector_name))
                        stocks.append(str(info.get("related", "")))
                        break
            return names, stocks

        sectors, related = scan(title_text) if title_text else scan(text)
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

        # 속보 알림이므로 오래된 기사는 점수가 아무리 높아도 내보내지 않는다.
        # 감점(-20)만으로는 가점이 큰 기사가 그대로 통과해 몇 달 전 뉴스가 나갔다.
        max_age = int(self.config["runtime"].get("max_news_age_minutes", 180))
        published = scored.item.published_at
        if max_age > 0 and published is not None and scored.item.item_type != "disclosure":
            if self.age_minutes(published) > max_age:
                return False

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
        """이미 보낸 기사인지 확인한다. 완전일치와 근접중복을 모두 본다.

        실행 주기가 1시간이라, 같은 사건이 몇 시간 뒤 다른 제목으로 다시 올라오는
        경우를 막으려면 과거 발송분과도 제목을 비교해야 한다.
        """
        item_hash = self.make_hash(scored.item)
        with sqlite3.connect(self.db_path) as conn:
            row = conn.execute("SELECT 1 FROM seen_items WHERE item_hash = ?", (item_hash,)).fetchone()
            if row is not None:
                return True
            if self._dup_window_hours <= 0:
                return False
            since = (
                dt.datetime.now(self.timezone) - dt.timedelta(hours=self._dup_window_hours)
            ).isoformat()
            past_titles = conn.execute(
                "SELECT title FROM seen_items WHERE sent_at >= ?", (since,)
            ).fetchall()

        fingerprint = title_fingerprint(scored.item.title)
        if not fingerprint["grams"]:
            return False
        for (past_title,) in past_titles:
            if is_near_duplicate(
                fingerprint,
                title_fingerprint(past_title or ""),
                self._dup_threshold,
                self._dup_containment,
                self._dup_min_shared_words,
            ):
                self.logger.info("과거 발송건과 중복이라 생략: %s", truncate(scored.item.title, 70))
                return True
        return False

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
            # 기록이 무한히 쌓이지 않도록 오래된 행은 정리한다.
            if self._dup_retention_days > 0:
                cutoff = (
                    dt.datetime.now(self.timezone) - dt.timedelta(days=self._dup_retention_days)
                ).isoformat()
                conn.execute("DELETE FROM seen_items WHERE sent_at < ?", (cutoff,))
            conn.commit()

    @staticmethod
    def make_hash(item: NewsItem) -> str:
        # 링크는 같은 기사라도 매체·추적파라미터마다 달라지므로 제목만으로 해시한다.
        key = normalize_for_hash(headline_core(item.title))
        return hashlib.sha256(key.encode("utf-8")).hexdigest()

    @staticmethod
    def _topic_key(scored: ScoredItem) -> str:
        """한 메시지가 한 주제로 도배되지 않도록 묶을 기준."""
        if scored.sectors:
            return "sector:" + scored.sectors[0]
        if scored.signals:
            return "signal:" + scored.signals[0]
        return "source:" + scored.item.source

    def _limit_per_topic(
        self, items: list[ScoredItem], max_per_topic: int, max_per_source: int = 0
    ) -> list[ScoredItem]:
        """한 주제나 한 피드가 메시지를 독차지하지 않게 건수를 제한한다.

        섹터만으로 묶으면 같은 사건이 여러 섹터로 흩어져 제한을 빠져나간다.
        (예: 이란 뉴스가 조선·에너지·금융으로 각각 분류되어 네 건이 모두 통과)
        그래서 피드 출처 기준 제한을 함께 건다.
        점수가 높은 순으로 먼저 채우고, 자리가 남으면 밀린 것을 뒤에 붙인다.
        """
        if max_per_topic <= 0 and max_per_source <= 0:
            return items
        topic_counts: dict[str, int] = {}
        source_counts: dict[str, int] = {}
        picked: list[ScoredItem] = []
        overflow: list[ScoredItem] = []
        for scored in items:
            topic_key = self._topic_key(scored)
            source_key = scored.item.source
            topic_full = 0 < max_per_topic <= topic_counts.get(topic_key, 0)
            source_full = 0 < max_per_source <= source_counts.get(source_key, 0)
            if topic_full or source_full:
                overflow.append(scored)
                continue
            topic_counts[topic_key] = topic_counts.get(topic_key, 0) + 1
            source_counts[source_key] = source_counts.get(source_key, 0) + 1
            picked.append(scored)
        return picked + overflow

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
        """같은 사건을 다룬 기사들을 한 건으로 묶는다.

        제목이 완전히 같은 경우만이 아니라, 매체마다 표현이 다른 근접중복까지 묶는다.
        대표 기사는 (소스 우선순위 -> 최신 -> 제목이 자세한 것) 순으로 고른다.
        """
        candidates = [x for x in items if x.title]
        ranked = sorted(
            candidates,
            key=lambda x: (
                x.priority,
                x.published_at or dt.datetime.min.replace(tzinfo=dt.timezone.utc),
                len(x.title),
            ),
            reverse=True,
        )

        clusters: list[dict[str, Any]] = []
        for item in ranked:
            fingerprint = title_fingerprint(item.title)
            if not fingerprint["grams"]:
                continue
            target = None
            for cluster in clusters:
                if is_near_duplicate(
                    fingerprint,
                    cluster["fp"],
                    self._dup_threshold,
                    self._dup_containment,
                    self._dup_min_shared_words,
                ):
                    target = cluster
                    break
            if target is None:
                clusters.append({"item": item, "fp": fingerprint, "dups": []})
            else:
                target["dups"].append(item)

        result: list[NewsItem] = []
        for cluster in clusters:
            base = cluster["item"]
            dups = cluster["dups"]
            if dups:
                sources = unique_keep_order([d.source for d in dups if d.source])
                base = dc_replace(base, dup_count=len(dups), dup_sources=tuple(sources[:4]))
            result.append(base)

        if len(result) < len(candidates):
            self.logger.info("근접중복 병합: %s건 -> %s건", len(candidates), len(result))
        return result

    def age_minutes(self, published_at: dt.datetime) -> int:
        now = dt.datetime.now(self.timezone)
        if published_at.tzinfo is None:
            published_at = published_at.replace(tzinfo=self.timezone)
        else:
            published_at = published_at.astimezone(self.timezone)
        delta = now - published_at
        return max(0, int(delta.total_seconds() // 60))

    def minutes_ahead(self, published_at: dt.datetime) -> int:
        """발행시각이 현재보다 얼마나 미래인지. 정상 기사는 0이다."""
        now = dt.datetime.now(self.timezone)
        if published_at.tzinfo is None:
            published_at = published_at.replace(tzinfo=self.timezone)
        else:
            published_at = published_at.astimezone(self.timezone)
        return max(0, int((published_at - now).total_seconds() // 60))

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
        """RSS 설명을 한국어로 바꾸고 최대 세 줄로 정리한다.

        본문이 없거나 제목 반복·안내문구뿐이면 빈 목록을 돌려준다.
        내용을 지어내지 않고, 메시지에서 그 줄을 아예 빼기 위해서다.
        """
        raw_summary = clean_text(item.summary)
        if not raw_summary or looks_like_junk_summary(raw_summary, item.title):
            return []
        translated = self.translate_to_korean(raw_summary)
        if normalize_for_hash(translated) == normalize_for_hash(translated_title):
            return []
        return split_summary_lines(translated, max_lines=3, max_chars=self.translation_summary_chars)

    def make_display_link(self, item: NewsItem) -> tuple[str, str | None]:
        """메시지에 넣을 링크를 정한다.

        구글뉴스 RSS 링크는 본문이 아니라 리다이렉트 주소라서 웹번역을 걸어도 동작하지
        않고 주소만 500자 넘게 길어진다. 그런 링크는 번역 래핑을 하지 않는다.
        """
        if not item.link:
            return "", None
        is_google_redirect = "news.google.com" in item.link
        wants_translation = self.translation_open_page and needs_korean_translation(
            f"{item.title} {item.summary}"
        )
        if wants_translation and not is_google_redirect:
            translated_url = (
                "https://translate.google.com/translate"
                f"?sl=auto&tl=ko&u={quote(item.link, safe='')}"
            )
            return translated_url, item.link
        return item.link, None

    def format_message(self, items: list[ScoredItem]) -> str:
        now = dt.datetime.now(self.timezone).strftime("%Y-%m-%d %H:%M")
        title = self.config["runtime"].get("mode_name", "시장영향 속보")
        # HTML 모드로 보내므로 링크를 뺀 모든 글자는 이스케이프한다.
        esc = html.escape
        lines = [f"🚨 [{esc(title)}] {now}", ""]

        for idx, scored in enumerate(items, start=1):
            item = scored.item
            translated_title = self.translate_to_korean(headline_core(item.title))
            summary_lines = self.make_korean_summary(item, translated_title)
            display_link, original_link = self.make_display_link(item)

            head = f"{idx}) {grade_icon(scored.grade)} {scored.grade} {scored.score}점"
            if scored.sectors:
                head += " · " + esc(", ".join(scored.sectors[:3]))
            lines.append(head)
            lines.append(f"<b>{esc(truncate(translated_title, 150))}</b>")
            lines.extend(f"   {esc(line)}" for line in summary_lines)

            origin = f"출처: {esc(item.source)}"
            if item.published_at:
                published = item.published_at.astimezone(self.timezone)
                today = dt.datetime.now(self.timezone).date()
                # 오늘 기사가 아니면 날짜까지 보여줘야 묵은 뉴스를 구분할 수 있다.
                stamp = published.strftime("%H:%M") if published.date() == today \
                    else published.strftime("%m/%d %H:%M")
                origin += " " + stamp
            if item.dup_count:
                origin += f" (같은 내용 {item.dup_count}건 생략)"
            lines.append(origin)

            detail = f"영향: {esc(scored.bias)}"
            if scored.related_stocks:
                detail += f" · 관련주: {esc(truncate(scored.related_stocks[0], 70))}"
            lines.append(detail)
            if scored.signals:
                lines.append(f"신호: {esc(', '.join(scored.signals[:2]))}")

            # 구글뉴스 주소는 500자가 넘어 그대로 쓰면 화면을 다 잡아먹는다.
            # 누르면 열리는 짧은 글자 링크로 바꾼다.
            if display_link:
                label = "한글로 열기" if original_link else "기사 원문 보기"
                lines.append(f'<a href="{esc(display_link, quote=True)}">▶ {label}</a>')
            lines.append("")

        lines.append("※ 자동 필터 알림. 매매 전 원문·차트·수급을 반드시 확인하세요.")
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
                "parse_mode": "HTML",
                "disable_web_page_preview": bool(telegram.get("disable_web_page_preview", True)),
            }
            timeout = int(self.config["runtime"].get("request_timeout_sec", 10))
            response = self.session.post(url, json=payload, timeout=timeout)
            if response.status_code >= 400:
                # 태그가 어긋나 HTML 해석이 실패하면 알림 자체가 끊긴다.
                # 그런 경우 태그를 걷어낸 평문으로 한 번 더 시도한다.
                self.logger.warning(
                    "HTML 모드 발송 실패(%s). 평문으로 재시도합니다: %s",
                    response.status_code, response.text[:200],
                )
                payload.pop("parse_mode", None)
                payload["text"] = strip_html_tags(chunk)
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


def split_summary_lines(text: str, max_lines: int = 4, max_chars: int = 520) -> list[str]:
    """번역문을 읽기 쉬운 최대 네 줄 요약으로 자른다."""
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


# 영문 키워드는 단어 경계를 지켜서 찾는다. 정규식은 한 번만 만들어 재사용한다.
_ASCII_KEYWORD_CACHE: dict[str, re.Pattern[str]] = {}


def keyword_in_text(keyword: str, lower_text: str) -> bool:
    """키워드 포함 여부. 대소문자와 공백 잡음을 완화한다.

    영문 키워드를 단순 부분일치로 찾으면 "war"가 "warning", "awarded", "warrant"에
    걸려 평범한 기사가 전쟁 뉴스로 분류된다. 그래서 영문은 단어 경계를 요구한다.
    한글은 조사가 붙어 형태가 변하므로 부분일치를 유지한다.
    """
    kw = keyword.strip().lower()
    if not kw:
        return False
    if not kw.isascii():
        return kw in lower_text
    pattern = _ASCII_KEYWORD_CACHE.get(kw)
    if pattern is None:
        pattern = re.compile(r"(?<![a-z0-9])" + re.escape(kw) + r"(?![a-z0-9])")
        _ASCII_KEYWORD_CACHE[kw] = pattern
    return pattern.search(lower_text) is not None


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


# ---------------------------------------------------------------------------
# 근접중복 판정
#
# 매체마다 같은 사건을 다른 제목으로 쓰기 때문에 제목 완전일치만으로는 중복이 걸러지지
# 않는다. 그렇다고 글자 유사도만 보면 "구리 가격 3개월 만에 최고치 경신"과
# "금 가격 3개월 만에 최고치 경신"처럼 핵심 단어 하나만 다른 기사가 0.78로 붙어버린다.
# 그래서 먼저 (1) 주체가 같은지, (2) 숫자가 충돌하지 않는지 확인하고,
# 그 관문을 통과한 것끼리만 (3) 글자 유사도로 비교한다.
# ---------------------------------------------------------------------------

# 구글뉴스 RSS 제목 끝의 " - Reuters" 같은 매체명 꼬리
_SOURCE_SUFFIX_RE = re.compile(r"\s+[-\u2013\u2014|]\s+[^-\u2013\u2014|]{2,30}$")
# 제목 앞의 [속보] <단독> 【표】 같은 말머리
_LEAD_TAG_RE = re.compile(r"^\s*[\[\(<\u3010][^\]\)>\u3011]{0,12}[\]\)>\u3011]\s*")
# 주체 판별에서 빼야 할 말머리성 단어
_HEAD_NOISE = {"속보", "단독", "긴급", "종합", "장중", "마감", "특징주", "표", "영상", "사진", "1보", "2보"}
# 조사 (긴 것부터 떼어낸다)
_JOSA = ("으로", "에서", "에게", "까지", "부터", "보다", "라고", "와의", "과의",
         "의", "은", "는", "이", "가", "을", "를", "에", "도", "로", "와", "과", "만")
_NUMBER_RE = re.compile(r"(\d[\d,.]*)\s*([%가-힣a-z]?)")


def strip_source_suffix(title: str) -> str:
    """제목 끝에 붙은 매체명 꼬리를 제거한다."""
    cleaned = clean_text(title)
    if not cleaned:
        return ""
    stripped = _SOURCE_SUFFIX_RE.sub("", cleaned).strip()
    return stripped or cleaned


def headline_core(title: str) -> str:
    """매체명 꼬리와 말머리를 떼어낸 제목 본문."""
    core = strip_source_suffix(title)
    for _ in range(3):
        trimmed = _LEAD_TAG_RE.sub("", core).strip()
        if trimmed == core:
            break
        core = trimmed
    return core or strip_source_suffix(title)


def _strip_josa(token: str) -> str:
    for josa in _JOSA:
        if len(token) > len(josa) + 1 and token.endswith(josa):
            return token[: -len(josa)]
    return token


def title_tokens(title: str) -> list[str]:
    """제목을 단어 단위로 자른다."""
    parts = re.split(r"[^0-9a-z가-힣]+", headline_core(title).lower())
    return [p for p in parts if p]


def head_tokens(title: str) -> list[str]:
    """제목의 주체로 볼 앞쪽 단어 두 개.

    한국어 기사 제목은 거의 항상 주체(기업·인물·품목)로 시작한다.
    이 값이 다르면 문장 구조가 비슷해도 다른 사건이다.
    """
    tokens = []
    for raw in title_tokens(title):
        token = _strip_josa(raw)
        if not token or token in _HEAD_NOISE:
            continue
        if len(token) == 1 and token.isascii():
            continue  # 영문 한 글자는 주체가 아니다 ("fed's" -> "fed", "s")
        tokens.append(token)
        if len(tokens) >= 2:
            break
    return tokens


# 어느 기사에나 흔히 나와서 사건을 구분해주지 못하는 단어들
_STOPWORD_TOKENS = {
    "관련", "오늘", "내일", "지난", "올해", "작년", "이번", "최근", "전날", "가운데",
    "대한", "위해", "따른", "따라", "대해", "통해", "함께", "우리", "모두", "다시",
    "하는", "한다", "했다", "있다", "없다", "된다", "예정", "기자", "종합", "속보",
    "the", "and", "for", "with", "from", "that", "this", "says", "said", "new",
    "after", "over", "into", "amid", "its", "his", "her", "their", "more", "than",
}


def content_tokens(title: str) -> set[str]:
    """사건을 구분해주는 내용어만 남긴 집합."""
    result = set()
    for raw in title_tokens(title):
        token = _strip_josa(raw)
        if len(token) < 2 or token in _STOPWORD_TOKENS:
            continue
        result.add(token)
    return result


# 어느 기사에나 붙는 상투어. 이 단어들이 겹치는 것은 같은 사건이라는 근거가 못 된다.
# ("엔비디아 3분기 실적 발표 임박"과 "엔비디아 3분기 실적 발표 결과"는 다른 기사다)
_GENERIC_TOKENS = {
    "실적", "발표", "전망", "예상", "기대", "상향", "하향", "목표주가", "주가", "종목",
    "상승", "하락", "급등", "급락", "강세", "약세", "돌파", "경신", "최고치", "최저치",
    "가격", "확대", "축소", "검토", "추진", "개최", "공개", "출시", "계획", "방침",
    "분석", "보고서", "리포트", "의견", "시장", "국내", "해외", "글로벌", "업계",
}


# 같은 기업의 비슷한 소식이라도 나라가 다르면 다른 사건이다.
# ("두산에너빌리티 체코 원전 수주"와 "두산에너빌리티 폴란드 원전 수주"는 별개 계약)
_COUNTRY_TOKENS = {
    "한국", "국내", "미국", "중국", "일본", "대만", "러시아", "우크라이나", "이란",
    "이스라엘", "사우디", "인도", "독일", "프랑스", "영국", "체코", "폴란드", "베트남",
    "호주", "캐나다", "멕시코", "브라질", "튀르키예", "터키", "북한", "유럽", "중동",
    "인도네시아", "태국", "말레이시아", "싱가포르", "네덜란드", "이탈리아", "스페인",
    "스웨덴", "노르웨이", "핀란드", "덴마크", "스위스", "오스트리아", "헝가리",
    "루마니아", "그리스", "포르투갈", "아일랜드", "벨기에", "카타르", "이라크",
    "시리아", "예멘", "리비아", "이집트", "나이지리아", "칠레", "페루", "아르헨티나",
    "콜롬비아", "필리핀", "아프리카", "남미", "동남아",
}


def countries_conflict(words_a: set[str], words_b: set[str]) -> bool:
    """두 제목이 서로 다른 나라만 언급하면 다른 사건으로 본다.

    한쪽에만 나라 이름이 있는 경우는 판단 근거로 쓰지 않는다.
    (한 기사는 나라를 적고 다른 기사는 생략했을 수 있다)
    """
    countries_a = words_a & _COUNTRY_TOKENS
    countries_b = words_b & _COUNTRY_TOKENS
    if not countries_a or not countries_b:
        return False
    return not (countries_a & countries_b)


def distinctive_tokens(title: str) -> set[str]:
    """사건을 실제로 구분해 주는 단어만 남긴다."""
    return {t for t in content_tokens(title) if t not in _GENERIC_TOKENS}


def count_shared_tokens(a: set[str], b: set[str]) -> int:
    """겹치는 단어 수. 한쪽이 다른 쪽의 앞부분이면 같은 단어로 센다.

    매체마다 "항공"과 "항공기", "9조"와 "9조원"처럼 표기가 조금씩 달라서,
    완전일치만 세면 같은 사건인데도 겹치는 단어가 거의 없는 것처럼 보인다.
    """
    matched = 0
    used: set[str] = set()
    for token_a in a:
        for token_b in b:
            if token_b in used:
                continue
            if _tokens_match(token_a, token_b):
                matched += 1
                used.add(token_b)
                break
    return matched


def _tokens_match(a: str, b: str) -> bool:
    if not a or not b:
        return False
    if a == b:
        return True
    if len(a) >= 2 and len(b) >= 2:
        return a.startswith(b) or b.startswith(a)
    return False


def heads_overlap(heads_a: list[str], heads_b: list[str]) -> bool:
    """두 제목의 주체가 겹치는지 본다."""
    if not heads_a or not heads_b:
        return False
    pairs = [(heads_a[0], heads_b[0])]
    if len(heads_b) > 1:
        pairs.append((heads_a[0], heads_b[1]))
    if len(heads_a) > 1:
        pairs.append((heads_a[1], heads_b[0]))
    return any(_tokens_match(x, y) for x, y in pairs)


def number_units(title: str) -> dict[str, set[str]]:
    """제목 속 숫자를 단위별로 모은다. 9조 -> {"조": {"9"}}"""
    found: dict[str, set[str]] = {}
    for digits, unit in _NUMBER_RE.findall(headline_core(title).lower()):
        found.setdefault(unit, set()).add(digits.replace(",", "").rstrip("."))
    return found


def numbers_conflict(a: dict[str, set[str]], b: dict[str, set[str]]) -> bool:
    """같은 단위인데 숫자가 전혀 겹치지 않으면 다른 사건으로 본다.

    "영업익 9조"와 "영업익 7조"를 갈라놓기 위한 장치다.
    한쪽에만 있는 단위는 판단 근거로 쓰지 않는다.
    """
    for unit, values_a in a.items():
        if not unit:
            continue
        values_b = b.get(unit)
        if values_b and not (values_a & values_b):
            return True
    return False


def bigrams_of_normalized(value: str) -> set[str]:
    """정규화된 문자열을 글자 두 개씩 잘라 집합으로 만든다.

    한국어는 띄어쓰기와 조사가 매체마다 달라 단어 단위 비교가 잘 맞지 않는다.
    글자 두 개 단위로 보면 표현이 달라도 같은 사건이면 겹치는 조각이 많다.
    """
    text = value or ""
    if len(text) < 2:
        return {text} if text else set()
    return {text[i : i + 2] for i in range(len(text) - 1)}


def jaccard_sim(a: set[str], b: set[str]) -> float:
    """두 집합이 겹치는 비율."""
    if not a or not b:
        return 0.0
    intersection = len(a & b)
    if not intersection:
        return 0.0
    return intersection / len(a | b)


def title_fingerprint(title: str) -> dict[str, Any]:
    """중복 비교에 쓰는 제목 지문."""
    return {
        "grams": bigrams_of_normalized(normalize_for_hash(headline_core(title))),
        "heads": head_tokens(title),
        "numbers": number_units(title),
        "words": distinctive_tokens(title),
    }


def is_near_duplicate(
    fp_a: dict[str, Any],
    fp_b: dict[str, Any],
    threshold: float,
    containment: float,
    min_shared_words: int = 4,
) -> bool:
    """두 제목이 같은 사건을 다루는지 판정한다.

    관문 1: 주체가 겹쳐야 한다.
    관문 2: 같은 단위의 숫자가 충돌하면 안 된다.
    관문 3: 언급된 나라가 서로 어긋나면 안 된다.
    본판정(셋 중 하나): 자카드 유사도 / 포함율 / 공유 내용어 수.
    공유 내용어 수는 문장 표현이 아주 달라도 같은 사건이면 핵심 단어가 여러 개
    겹친다는 점을 이용한다. 관문을 통과한 뒤에만 보므로 오판 위험이 낮다.
    """
    grams_a, grams_b = fp_a["grams"], fp_b["grams"]
    if not grams_a or not grams_b:
        return False
    if not heads_overlap(fp_a["heads"], fp_b["heads"]):
        return False
    if numbers_conflict(fp_a["numbers"], fp_b["numbers"]):
        return False
    if countries_conflict(fp_a.get("words", set()), fp_b.get("words", set())):
        return False

    shared = grams_a & grams_b
    if len(shared) < 4:
        return False
    if jaccard_sim(grams_a, grams_b) >= threshold:
        return True

    if count_shared_tokens(fp_a.get("words", set()), fp_b.get("words", set())) >= min_shared_words:
        return True

    smaller = grams_a if len(grams_a) <= len(grams_b) else grams_b
    if len(smaller) < 6:
        return False
    return len(shared) / len(smaller) >= containment


# 본문 대신 관련기사 목록·안내문구가 들어오는 피드를 걸러내기 위한 표시들
_JUNK_SUMMARY_MARKERS = (
    "view full coverage",
    "opens in a new window",
    "read more",
    "continue reading",
    "subscribe to",
    "sign up for",
    "전체 기사 보기",
    "자세히 보기",
    "무단전재",
    "재배포 금지",
)


def looks_like_junk_summary(summary: str, title: str) -> bool:
    """요약이 실제 본문이 아니라 잡동사니인지 판단한다."""
    body = clean_text(summary)
    if len(body) < 45:
        return True
    lowered = body.lower()
    if any(marker in lowered for marker in _JUNK_SUMMARY_MARKERS):
        return True
    norm_body = normalize_for_hash(body)
    norm_title = normalize_for_hash(title)
    if norm_title and norm_title in norm_body and len(norm_body) < len(norm_title) * 1.6:
        return True  # 제목만 그대로 반복한 요약은 쓸모가 없다
    return False


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


def strip_html_tags(message: str) -> str:
    """HTML 발송이 실패했을 때 쓰는 평문 변환. 링크는 주소를 그대로 드러낸다."""
    text = re.sub(r'<a href="([^"]+)">([^<]*)</a>', r"\2: \1", message)
    text = re.sub(r"</?b>", "", text)
    return html.unescape(text)


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
