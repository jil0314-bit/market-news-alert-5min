"""Publish public briefing data in GitHub Actions; never send Telegram messages."""
from __future__ import annotations

import datetime as dt
import json
import math
import os
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

# The inherited collector requires these settings even for read-only collection.
# No real messaging credentials are supplied to this workflow.
os.environ["TELEGRAM_BOT_TOKEN"] = "unused"
os.environ["TELEGRAM_CHAT_ID"] = "unused"
import morning_us_market_report as m


class Collector(m.MorningUSMarketReport):
    def fetch_yahoo_quote(self, symbol, name):
        url = ("https://query1.finance.yahoo.com/v8/finance/chart/"
               + m.requests.utils.quote(symbol, safe="") + "?range=7d&interval=1d")
        res = self.session.get(url, timeout=15)
        res.raise_for_status()
        chart = (res.json().get("chart", {}).get("result") or [None])[0]
        if not chart:
            return None
        stamps = chart.get("timestamp") or []
        closes = list(chart.get("indicators", {}).get("quote", [{}])[0].get("close", []))
        meta = chart.get("meta") or {}
        live = meta.get("regularMarketPrice")
        live_stamp = meta.get("regularMarketTime")
        if stamps and closes and closes[-1] is None and live is not None and live_stamp:
            last_day = dt.datetime.fromtimestamp(stamps[-1], m.ET).date()
            live_day = dt.datetime.fromtimestamp(live_stamp, m.ET).date()
            if last_day == live_day:
                closes[-1] = live
        pairs = [(t, c) for t, c in zip(stamps, closes)
                 if isinstance(c, (int, float)) and math.isfinite(c) and c > 0]
        if len(pairs) < 2:
            return None
        (_, prev), (stamp, last) = pairs[-2:]
        price = float(last) / (10.0 if symbol == "^TNX" and float(last) > 20 else 1.0)
        return m.Quote(symbol=symbol, name=name, price=price,
                       change_pct=(float(last) / float(prev) - 1) * 100,
                       as_of=dt.datetime.fromtimestamp(stamp, m.UTC).astimezone(m.ET))

    def fetch_news(self):
        items = []
        for query, base in self.NEWS_QUERIES:
            url = ("https://news.google.com/rss/search?q=" + m.quote_plus(query)
                   + "&hl=en-US&gl=US&ceid=US:en")
            try:
                res = self.session.get(url, timeout=15)
                res.raise_for_status()
                for entry in m.feedparser.parse(res.content).entries[:20]:
                    title = m.clean_text(entry.get("title", ""))
                    source = m.clean_text((entry.get("source") or {}).get("title", ""))
                    if source and title.endswith(" - " + source):
                        title = title[:-(len(source) + 3)]
                    published = m.parse_date(entry.get("published", entry.get("updated")))
                    items.append(m.News(title=title, link=str(entry.get("link", "")),
                                        source=source, summary="", published_at=published,
                                        score=base + self.score_news(title, "", source, published)))
            except Exception:
                m.LOGGER.warning("News candidate source unavailable")
        now = dt.datetime.now(m.UTC)
        fresh = [n for n in items if n.published_at
                 and 0 <= (now - n.published_at).total_seconds() <= 30 * 3600]
        return self.dedupe_news(fresh)[:30]


def quote_json(x):
    return {"name": x.name, "symbol": x.symbol, "price": round(x.price, 2),
            "chg_pct": round(x.change_pct, 2), "as_of": x.as_of.date().isoformat()}


def build_data(r, indices, stocks, news):
    majors = [q for q in indices if q.symbol in ("^GSPC", "^IXIC", "^DJI")]
    if len({q.symbol for q in majors}) != 3 or len({q.as_of.date() for q in majors}) != 1:
        raise ValueError("All three major indices must have the same confirmed session")
    session = majors[0].as_of.date()
    indices = [q for q in indices if q.as_of.date() == session]
    stocks = [q for q in stocks if q.as_of.date() == session]
    sectors = [{"sector": s["sector"], "avg": round(s["average"], 2),
                "up": s["up"], "down": s["down"], "count": s["count"],
                "expected_count": len(r.SECTOR_WATCHLIST[s["sector"]]),
                "top": [f"{q.name} {q.change_pct:+.1f}%" for q in s["top"]],
                "worst": f"{s['bottom'].name} {s['bottom'].change_pct:+.1f}%"}
               for s in r.summarize_sectors(stocks)]
    label = lambda q: f"{q.name} {q.change_pct:+.1f}%({r.SYMBOL_SECTOR.get(q.symbol, '기타')})"
    return {
        "schema_version": 1,
        "generated_kst": dt.datetime.now(m.KST).strftime("%Y-%m-%d %H:%M"),
        "us_session_date": session.isoformat(),
        "indices": [quote_json(q) for q in indices], "sectors": sectors,
        "stocks": [quote_json(q) for q in stocks],
        "expected_stock_count": len(r.WATCHLIST),
        "missing_symbols": sorted((r.INDEX_SYMBOLS.keys() | r.WATCHLIST.keys())
                                  - {q.symbol for q in indices + stocks}),
        "big_movers": [label(q) for q in sorted(stocks, key=lambda q: q.change_pct, reverse=True)
                       if abs(q.change_pct) >= r.BIG_MOVE_PCT],
        "top_gainers": [label(q) for q in sorted(stocks, key=lambda q: q.change_pct, reverse=True)
                        if q.change_pct > 0][:5],
        "top_losers": [label(q) for q in sorted(stocks, key=lambda q: q.change_pct)
                       if q.change_pct < 0][:5],
        "news_candidates": [{"n": i + 1, "title_en": n.title, "source": n.source,
                             "score": n.score, "published_kst": n.published_at.astimezone(m.KST).strftime("%m/%d %H:%M"),
                             "link": n.link} for i, n in enumerate(news)],
        "earnings_calendar": r.fetch_earnings_calendar(),
        "korea_points_rule_based": r.build_korea_points({q.symbol: q for q in indices}, stocks, news),
    }


def collect_one(pair):
    r = Collector()
    try:
        return r.fetch_yahoo_quote(*pair)
    except Exception:
        return None
    finally:
        r.session.close()


def main(path):
    r = Collector()
    symbols = list(r.INDEX_SYMBOLS.items()) + list(r.WATCHLIST.items())
    with ThreadPoolExecutor(max_workers=6) as pool:
        quotes = [q for q in pool.map(collect_one, symbols) if q is not None]
    indices = [q for q in quotes if q.symbol in r.INDEX_SYMBOLS]
    stocks = [q for q in quotes if q.symbol in r.WATCHLIST]
    data = build_data(r, indices, stocks, r.fetch_news())
    output = Path(path)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(data, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
    print(f"Published data only: session={data['us_session_date']}, stocks={len(data['stocks'])}/{len(r.WATCHLIST)}")


if __name__ == "__main__":
    main(sys.argv[1])
