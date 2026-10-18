"""Separate two-hour AI news digest. Existing market/report jobs are untouched."""
from __future__ import annotations
import argparse
import datetime as dt
import hashlib
import html
import json
import logging
import os
from pathlib import Path
import re
import tempfile
import time
from urllib.parse import urlencode, urlsplit, urlunsplit, parse_qsl
from zoneinfo import ZoneInfo

import feedparser
import requests
import ai_news_intelligence as intel
from ai_news_translation import translated_record

UTC = dt.timezone.utc
KST = ZoneInfo("Asia/Seoul")
STATE_PATH = Path("data/ai_news_state.json")
MAX_ITEMS = 30
LOG = logging.getLogger("ai_news_digest")
TOPICS = (
    ("AI 전반", '(인공지능 OR "생성형 AI" OR "AI 에이전트") when:1d', "ko", "KR", "KR:ko"),
    ("AI 기업·서비스", '(OpenAI OR ChatGPT OR Anthropic OR Claude OR Gemini OR 딥시크) when:1d', "ko", "KR", "KR:ko"),
    ("AI 인프라", '("AI 반도체" OR "AI 데이터센터" OR "AI 로봇" OR "엔비디아 AI") when:1d', "ko", "KR", "KR:ko"),
    ("AI 정책·안전", '("AI 규제" OR "AI 안전" OR "AI 저작권" OR "AI 연구") when:1d', "ko", "KR", "KR:ko"),
    ("Global AI", '("artificial intelligence" OR "generative AI" OR "AI agents") when:1d', "en-US", "US", "US:en"),
    ("AI models", '(OpenAI OR Anthropic OR ChatGPT OR "Google Gemini" OR DeepSeek) when:1d', "en-US", "US", "US:en"),
    ("AI infrastructure", '("AI chips" OR "AI data centers" OR "AI robotics") when:1d', "en-US", "US", "US:en"),
    ("AI policy & research", '("AI regulation" OR "AI safety" OR "AI research" OR "AI copyright") when:1d', "en-US", "US", "US:en"),
)
AI_PATTERN = re.compile(r"(?i)(?<![a-z0-9])(?:ai|llm|gpt[-\d\w]*|chatgpt|openai|anthropic|claude|deepseek|gemini)(?![a-z0-9])|인공지능|생성형|에이전트|챗지피티|앤트로픽|클로드|제미나이|딥시크|artificial intelligence|machine learning")


def clean(value):
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]*>", " ", str(value or "")))).strip()


def parse_date(entry):
    parsed = entry.get("published_parsed") or entry.get("updated_parsed")
    if not parsed:
        return None
    try:
        return dt.datetime(*parsed[:6], tzinfo=UTC)
    except (TypeError, ValueError):
        return None


def safe_link(value):
    try:
        parts = urlsplit(str(value))
        if parts.scheme not in ("http", "https") or not parts.hostname or parts.username or parts.password:
            return ""
        return urlunsplit((parts.scheme, parts.netloc, parts.path, parts.query, ""))
    except ValueError:
        return ""


def keys_for(item):
    # Tracking-only URL changes and the same title from overlapping feeds dedupe.
    parts = urlsplit(item["link"])
    query = urlencode([(k, v) for k, v in parse_qsl(parts.query) if not k.lower().startswith("utm_")])
    url = urlunsplit((parts.scheme, parts.netloc, parts.path, query, ""))
    title = re.sub(r"[^a-z0-9가-힣]", "", item["title"].lower())
    keys = [hashlib.sha256(value.encode()).hexdigest() for value in ("url:"+url, "title:"+title)]
    return list(dict.fromkeys(keys + [key for key in item.get("duplicate_keys", [])
                                      if isinstance(key, str) and re.fullmatch(r"[0-9a-f]{64}", key)]))


def load_state(path):
    if not path.is_file():
        return {"version": 1, "sent": {}, "pending": [], "last_success_at": None}
    state = json.loads(path.read_text(encoding="utf-8"))
    if state.get("version") != 1 or not isinstance(state.get("sent"), dict) or not isinstance(state.get("pending"), list):
        raise ValueError("AI news state invalid; refusing to reset sent history")
    return state


def save_state(path, state):
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, filename = tempfile.mkstemp(prefix=path.name+".", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as file:
            json.dump(state, file, ensure_ascii=False, indent=2, allow_nan=False)
        os.replace(filename, path)
    finally:
        Path(filename).unlink(missing_ok=True)


def collect(session, now, cutoff):
    found, successes, failures = [], 0, []
    for source_config in intel.sources(TOPICS):
        topic, url = source_config["name"], source_config["url"]
        try:
            response = session.get(url, timeout=20)
            response.raise_for_status()
            feed = feedparser.parse(response.content)
            if not feed.get("version") or (feed.get("bozo") and not feed.entries):
                raise ValueError("Invalid RSS")
            successes += 1
            for entry in feed.entries:
                published = parse_date(entry)
                if published is None or published < cutoff or published > now + dt.timedelta(minutes=5):
                    continue
                title = clean(entry.get("title"))
                source = clean(entry.get("source", {}).get("title", "")) or (
                    source_config["name"] if source_config["kind"] in ("official","specialist") else "")
                if source and title.endswith(" - " + source):
                    title = title[:-(len(source)+3)].strip()
                link = safe_link(entry.get("link", ""))
                excerpt = clean(entry.get("summary", ""))
                if source and excerpt.endswith(" "+source):
                    excerpt = excerpt[:-(len(source)+1)]
                if not title or not link or not (AI_PATTERN.search(title) or intel.RELEVANT.search(title)):
                    continue
                found.append({"title": title[:400], "source": source[:100] or "출처 미제공",
                              "link": link, "published_at": published.isoformat(), "topic": topic,
                              "excerpt": excerpt[:220] if excerpt and excerpt != title else "",
                              "source_kind": source_config["kind"], "region": source_config["region"]})
        except (requests.RequestException, ValueError, TypeError):
            failures.append(topic)
    if not successes:
        raise RuntimeError("All AI news feeds failed")
    return found, failures


def merge_pending(state, items):
    # Merge overlapping duplicate groups, including transitive title/link aliases.
    groups = []
    sent = set(state["sent"])
    for item in state["pending"] + items:
        keys = set(keys_for(item))
        overlap = [group for group in groups if group["keys"] & keys]
        members = [item]
        for group in overlap:
            keys.update(group["keys"])
            members.extend(group["items"])
            groups.remove(group)
        groups.append({"keys":keys, "items":members})
    pending = []
    for group in groups:
        if group["keys"] & sent:
            continue
        item = dict(min(group["items"], key=lambda x:(intel.profile(x)["source_rank"],
                                                     -len(x.get("excerpt","")), x["published_at"])))
        item["duplicate_keys"] = sorted(group["keys"])
        pending.append(item)
    state["pending"] = sorted(pending, key=lambda x:x["published_at"])


def translate_title(title):
    if len(re.findall(r"[가-힣]", title)) >= 3:
        return title
    try:
        from deep_translator import GoogleTranslator
        return clean(GoogleTranslator(source="auto", target="ko").translate(title)) or title
    except Exception:
        return title  # No invented summary on translation failure.


def utf16_length(text):
    return len(text.encode("utf-16-le")) // 2


def format_pages(items, now, backlog=0, failures=(), translate=None):
    header = "🤖 AI 뉴스 모음 · " + now.astimezone(KST).strftime("%m/%d %H:%M") + " 한국시간\n"
    note = "두 시간마다 별도 발송 · 공개 RSS 기준"
    if failures:
        note += f" · 피드 {len(failures)}개 조회 실패"
    if backlog:
        note += f" · 대기 {backlog}건은 다음 회차"
    prefix = header + note
    if not items:
        message = "새 AI 기사를 확인하지 못했습니다." if failures else "이번 회차 새 AI 뉴스가 없습니다."
        return [(prefix+"\n\n"+message, [])]
    pages, blocks, group = [], [], []
    translate = translate or (lambda title: title)
    for item in items:
        translated = translated_record(item) if translate is translate_title else {
            "title":translate(item["title"]), "excerpt":translate(item.get("excerpt","")), "quality":"검사용 번역"}
        title = clean(translated["title"])[:210]
        if translated["quality"]=="번역 미검수·원문 표시":
            title="번역 미검수 · "+(", ".join(intel.profile(item)["products"][:2]) or "AI")+" 관련 소식"
        published = dt.datetime.fromisoformat(item["published_at"]).astimezone(KST).strftime("%m/%d %H:%M")
        link = item["link"]
        # Abnormally long URLs remain archived but must not break all delivery.
        displayed_link = link if utf16_length(link) < 2200 else "원문 URL이 너무 길어 발송문에서 제외했습니다."
        details = "\n".join(intel.briefing_lines(item, lambda text:translated["excerpt"]))
        details += "\n번역: " + translated["quality"]
        if translated["title"]!=item["title"] or translated["quality"]=="번역 미검수·원문 표시":
            details += "\n원제: " + item["title"][:130]
        block = f"• {title}\n{details}\n{item['source']} · {published}\n{displayed_link}"
        if utf16_length(prefix+"\n\n"+block)>3800:
            # Preserve an intact URL when ordinary; very long evidence URLs stay
            # in the state file rather than crowding out useful briefing lines.
            displayed_link="긴 원문 URL은 발송 대기 기록에 보관됨"
            block=f"• {title}\n{details}\n{item['source']} · {published}\n{displayed_link}"
        candidate = prefix+"\n\n"+"\n\n".join(blocks+[block])
        if blocks and utf16_length(candidate) > 3800:
            pages.append((prefix+"\n\n"+"\n\n".join(blocks), group))
            blocks, group = [], []
        blocks.append(block); group.append(item)
    if blocks:
        pages.append((prefix+"\n\n"+"\n\n".join(blocks), group))
    return pages


def send(session, token, chat_id, message):
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    # Do not log exceptions that may embed the bot token in a request URL.
    try:
        response = session.post(url, json={"chat_id":chat_id, "text":message,
                                          "disable_web_page_preview":True}, timeout=25)
        result = response.json()
        if not response.ok or result.get("ok") is not True or not result.get("result", {}).get("message_id"):
            raise RuntimeError("Telegram did not confirm delivery")
    except (requests.RequestException, ValueError):
        raise RuntimeError("Telegram delivery failed; sent history unchanged") from None


def run(path=STATE_PATH, dry_run=False, session=None, now=None, translate=None):
    now = now or dt.datetime.now(UTC)
    session = session or requests.Session()
    session.headers.update({"User-Agent":"AINewsDigest/1.0 personal RSS reader"})
    state = load_state(path)
    previous = dt.datetime.fromisoformat(state["last_success_at"]) if state.get("last_success_at") else now-dt.timedelta(hours=2)
    cutoff = max(now-dt.timedelta(hours=24), previous-dt.timedelta(minutes=30))
    items, failures = collect(session, now, cutoff)
    merge_pending(state, items)
    selected = intel.select_items(state["pending"], MAX_ITEMS, now)
    pages = format_pages(selected, now, len(state["pending"])-len(selected), failures,
                         translate if translate is not None else translate_title)
    if dry_run:
        print(f"AI_DRY_RUN feeds_failed={len(failures)} new={len(items)} selected={len(selected)} pages={len(pages)} pending={len(state['pending'])}")
        return {"items":selected, "pages":len(pages), "failures":failures}
    token, chat_id = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip(), os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if not token or not chat_id:
        raise RuntimeError("Telegram secrets are missing")
    # Persist the backlog before sending. Checkpoint each accepted page so that
    # a later page failure does not forget earlier confirmed sends.
    save_state(path, state)
    for index, (message, group) in enumerate(pages):
        send(session, token, chat_id, message)
        sent_keys = {key for item in group for key in keys_for(item)}
        state["sent"].update({key:now.isoformat() for key in sent_keys})
        state["pending"] = [item for item in state["pending"] if not any(key in sent_keys for key in keys_for(item))]
        save_state(path, state)
        if index+1 < len(pages):
            time.sleep(1.1)
    if not failures:
        state["last_success_at"] = now.isoformat()
    retention = (now-dt.timedelta(days=14)).isoformat()
    state["sent"] = {key:value for key,value in state["sent"].items() if value >= retention}
    save_state(path, state)
    LOG.info("AI_NEWS_SENT items=%d pages=%d backlog=%d failed_feeds=%d",len(selected),len(pages),len(state["pending"]),len(failures))
    return {"sent":len(selected), "pages":len(pages), "pending":len(state["pending"]), "failed_feeds":len(failures)}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true", help="RSS 확인만 수행; 발송·상태 저장 없음")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO,format="%(levelname)s %(message)s")
    try:
        run(dry_run=args.dry_run)
        return 0
    except Exception as exc:
        LOG.error("AI 뉴스 실행 실패 (%s); 키·채팅번호·요청 URL은 기록하지 않음",type(exc).__name__)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
