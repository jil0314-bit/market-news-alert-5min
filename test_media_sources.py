"""새로 넣은 온라인 매체는 직접 RSS여야 하고, 주소가 겹치면 안 된다."""
from __future__ import annotations

import unittest
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent
DIRECT_MEDIA = (
    "연합뉴스-경제",
    "연합뉴스-마켓",
    "연합뉴스-산업",
    "연합뉴스-국제",
    "연합인포맥스",
    "이데일리",
    "서울경제-금융",
    "머니투데이",
    "머니투데이-증권",
    "아시아경제-증권",
    "아시아경제-경제",
    "파이낸셜뉴스-증권",
    "파이낸셜뉴스-경제",
    "조선비즈-마켓",
    "조선일보-경제",
    "뉴시스-경제",
    "뉴시스-속보",
    "인포스탁데일리",
    "매일경제-기업경영",
    "한국경제-IT",
    "동아일보-경제",
    "한겨레-경제",
    "전자신문-산업",
    "SBS-경제",
    "CNBC-금융",
    "CNBC-경제",
    "CNBC-톱뉴스",
    "MarketWatch-실시간",
    "MarketWatch-톱",
    "MarketWatch-마켓펄스",
    "Bloomberg-마켓",
    "WSJ-마켓",
    "BBC-비즈니스",
    "가디언-비즈니스",
    "NYT-비즈니스",
    "니케이아시아",
    "FXStreet",
    "ForexLive",
    "SeekingAlpha-속보",
    "야후파이낸스-시장",
    "인베스팅-경제",
    "인베스팅-외환",
)


class MediaSourceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        text = (ROOT / "config.github.yaml").read_text(encoding="utf-8")
        cls.config = yaml.safe_load(text)
        cls.feeds = cls.config["sources"]["rss"]["feeds"]

    def test_feed_names_are_unique(self) -> None:
        names = [str(feed["name"]) for feed in self.feeds]
        self.assertEqual(len(names), len(set(names)))

    def test_new_media_are_direct_feeds(self) -> None:
        by_name = {str(feed["name"]): feed for feed in self.feeds}
        for name in DIRECT_MEDIA:
            feed = by_name[name]
            url = str(feed["url"])
            self.assertTrue(url.startswith("http://") or url.startswith("https://"), name)
            self.assertNotIn("news.google.com", url, name)
            self.assertNotIn("publisher_watch", feed, name)
            self.assertGreaterEqual(int(feed["priority"]), 2)

    def test_existing_official_feeds_remain(self) -> None:
        names = {str(feed["name"]) for feed in self.feeds}
        for name in (
            "Federal Reserve-전체보도",
            "미국 재무부-보도자료",
            "SEC-보도자료",
            "한국경제-증권",
            "매일경제-헤드라인",
            "GoogleNews-연준인사발언",
        ):
            self.assertIn(name, names)


if __name__ == "__main__":
    unittest.main()
