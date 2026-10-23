"""Separate two-hour AI news digest. Existing market/report jobs are untouched."""
from __future__ import annotations
import argparse
import datetime as dt
from difflib import SequenceMatcher
import hashlib
import html
import json
import logging
import os
from pathlib import Path
import re
import tempfile
import time
import unicodedata
from urllib.parse import urlencode, urlsplit, urlunsplit, parse_qsl
from zoneinfo import ZoneInfo

import feedparser
import requests
import ai_news_intelligence as intel
from ai_news_translation import translated_record

UTC = dt.timezone.utc
KST = ZoneInfo("Asia/Seoul")
STATE_PATH = Path("data/ai_news_state.json")
MAX_ITEMS = 7                 # 회차당 최대 건수 (중요 기사만)
IMPORTANT_MAX_PRIORITY = 1    # intel.profile priority 0~1만 발송: 핵심 4사 소식, 모델·툴 출시·업데이트, 연동
PER_PRODUCT_LIMIT = 2         # 같은 회사·제품 기사는 회차당 최대 2건
PENDING_HOURS = 12            # 중요 기사라도 12시간 넘게 밀리면 버림
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


def story_signature(item):
    title = unicodedata.normalize("NFKC", clean(item["title"])).casefold()
    source = clean(item.get("source", "")).casefold()
    if source and title.endswith(" - " + source):
        title = title[:-(len(source) + 3)]
    title = re.sub(r"^\s*[\[【](?:속보|단독|종합|업데이트|breaking|updated)[\]】]\s*", "", title)
    words = re.findall(r"[a-z0-9가-힣\u3400-\u9fff]+", title)
    tokens = frozenset(words) - {"a", "an", "the", "to", "of", "in", "on", "for", "and", "with", "as", "by"}
    return " ".join(words), tokens, frozenset(re.findall(r"\d+(?:\.\d+)*", title))


def similar_story(left, right):
    a, aw, numbers_a = left
    b, bw, numbers_b = right
    # Model versions, prices and benchmark changes must remain separate news.
    if not a or not b or numbers_a != numbers_b:
        return False
    if a == b:
        return True
    shared = len(aw & bw)
    if shared >= 4 and shared / max(1, len(aw | bw)) >= .65:
        return True
    if min(len(a), len(b)) < 25 or min(len(a), len(b)) / max(len(a), len(b)) < .78:
        return False
    if shared < 2 and not (re.search(r"[가-힣\u3400-\u9fff]", a) and re.search(r"[가-힣\u3400-\u9fff]", b)):
        return False
    matcher = SequenceMatcher(None, a, b, autojunk=False)
    return matcher.quick_ratio() >= .88 and matcher.ratio() >= .88


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
    sent = set(state["sent"]) | set(state.get("skipped", {}))
    history = [(story_signature(item), item) for item in state.get("sent_stories", [])]
    for item in state["pending"] + items:
        keys = set(keys_for(item))
        signature = story_signature(item)
        overlap = [group for group in groups if group["keys"] & keys or
                   any(similar_story(signature, other) for other in group["signatures"])]
        members = [item]
        signatures = [signature]
        for group in overlap:
            keys.update(group["keys"])
            members.extend(group["items"])
            signatures.extend(group["signatures"])
            groups.remove(group)
        groups.append({"keys":keys, "items":members, "signatures":signatures})
    pending = []
    for group in groups:
        matches = [old for signature, old in history if any(
            similar_story(signature, candidate) for candidate in group["signatures"])]
        if group["keys"] & sent or matches:
            # Remember newly encountered aliases of an already delivered story.
            known = {**state.get("skipped", {}), **state["sent"]}
            at = max([known[key] for key in group["keys"] & sent] +
                     [old["sent_at"] for old in matches])
            state["sent"].update({key: at for key in group["keys"]})
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


MONEY = re.compile(r"\$\s?(\d+(?:\.\d+)?)\s?(B|bn|billion|M|mn|million|T|tn|trillion)\b", re.I)


def korean_money(match):
    value, unit = float(match.group(1)), match.group(2).lower()
    if unit.startswith("t"):
        return f"{value:g}조 달러"
    if unit.startswith("b"):
        return f"{value*10:g}억 달러"   # $30B -> 300억 달러
    return f"{value/100:g}억 달러" if value >= 100 else f"{value*100:g}만 달러"


def clean_headline(title):
    """매체 꼬리표 제거: ' - 매체', ' | 매체', ' By 매체', '-36氪' 같은 끝부분."""
    title = re.sub(r"\s+(?:By|by)\s+[^\s].{0,30}$", "", title)
    title = re.sub(r"\s*[-|–—]\s*[^-|–—]{1,25}$", "", title) if len(title) > 40 else title
    title = re.sub(r"^\s*\[[^\]]{1,12}\]\s*", "", title)  # [더뉴론] 같은 머리말
    return title.strip(" -|·")


def machine_translate(text):
    """구글 -> (차단·실패 시) MyMemory 순서. 한국어가 들어간 결과만 인정."""
    try:
        from deep_translator import GoogleTranslator
        out = clean(GoogleTranslator(source="auto", target="ko").translate(text))
        if re.search(r"[가-힣]", out):
            return out
    except Exception:
        pass
    try:
        from deep_translator import MyMemoryTranslator
        source = "zh-CN" if re.search(r"[\u3400-\u9fff]", text) else "en-GB"
        out = clean(MyMemoryTranslator(source=source, target="ko-KR").translate(text[:480]))
        if re.search(r"[가-힣]", out):
            return out
    except Exception:
        pass
    return ""


def korean_title(item):
    """제목은 반드시 한국어로. 보존검사 번역 -> 백업 번역기 -> 그래도 실패하면 원문."""
    title = MONEY.sub(korean_money, clean_headline(item["title"]))
    if len(re.findall(r"[가-힣]", title)) >= 3 and not re.search(r"[\u3400-\u9fff]", title):
        return title
    record = translated_record({"title": title}, engine=machine_translate)
    if record["quality"] != "번역 미검수·원문 표시":
        return clean(record["title"])
    fallback = machine_translate(title)
    return re.sub(r"\$\s+", "$", fallback) if fallback else title


STATUS_SHORT = {"출시·업데이트 보도": "출시·업데이트", "출시 예정·예고": "예고",
                "미리보기·베타": "베타", "미확인 보도·전망": "미확인 보도"}


def important(item):
    return intel.profile(item)["priority"] <= IMPORTANT_MAX_PRIORITY


GENERIC = set("""ai model models new update updates updated launch launches launched launching release releases released
app apps feature features with the and for its to of in on at by from adds add gets get now users user more into after
says said will can could latest first own deal plan plans report reports reportedly working work about over amid
agent agents agentic tool tools company companies tech startup big its their this that what how why
인공지능 모델 공개 출시 발표 기능 업데이트 서비스 에이전트 새로운 신규 사용자 확대 추가 지원 도입 강화 선보여 선봬 관련 개발
""".split())
PRODUCT_WORDS = re.compile(r"openai|chatgpt|gpt|anthropic|claude|google|gemini|deepmind|xai|grok|cursor|suno|deepseek|qwen|"
                           r"오픈ai|챗gpt|앤트로픽|클로드|구글|제미나이|그록|딥시크|오픈에이아이", re.I)
FLUFF = re.compile(r"cheat sheet|how to|explained|\bvs\.?\b|alternatives|who owns|\bpicks\b|\btips\b|\bguide\b|"
                   r"what to know|newsletter|podcast|opinion|column|\[[^\]]*(?:서가|칼럼|기고|오피니언|레터|팟캐스트)[^\]]*\]|"
                   r"사용법|하는 법|총정리|비교해|\?\s*$|？\s*$|predicts?|price prediction|"
                   r"\b(?:bitcoin|btc|crypto|xrp|sui|solana|dogecoin|memecoin|token price)\b|코인|비트코인", re.I)
JUNK_SOURCES = re.compile(r"bitcoin|coin|crypto|99|benzinga|tradingview|yahoo finance|seeking alpha|motley fool|"
                          r"zacks|gurufocus|marketbeat|investorplace|ad hoc news", re.I)


def topic_words(text):
    words = set()
    for word in re.findall(r"[a-z0-9$.]+|[가-힣]+|[\u3400-\u9fff]+", clean(text).lower()):
        if re.match(r"[가-힣]", word):
            word = re.sub(r"(?:에서|으로|까지|부터|은|는|이|가|을|를|의|에|도|와|과|로)$", "", word)
        word = word.strip(".$")
        if len(word) >= 2 and word not in GENERIC and not PRODUCT_WORDS.fullmatch(word):
            words.add(word)
    return words


def shared_count(a, b):
    def same(x, y):
        return x == y or (re.match(r"[가-힣]", x) and re.match(r"[가-힣]", y) and min(len(x), len(y)) >= 2
                          and (x.startswith(y) or y.startswith(x)))
    return sum(1 for x in a if any(same(x, y) for y in b))


STRONG = set()   # 이번 회차의 '소식 고유 단어' (예: duet, mythos). pick_items가 채운다.


def same_topic(a, b):
    """같은 소식 판정: 핵심 단어 2개 이상 공유, 또는 소식 고유 단어(제품·기능 이름 등) 1개 공유."""
    shared = shared_count(a, b)
    if shared >= 2 or (shared >= 1 and min(len(a), len(b)) <= 2):
        return True
    return shared == 1 and bool(a & b & STRONG)


def pick_items(pending, limit, now, recent=(), titles=None):
    """중요 소식만 고른다.
    1) 핵심 4사·모델/툴 출시 등 중요 기사만, 사용법·해설·칼럼은 제외
    2) 같은 회사 + 핵심 단어 2개 이상 공유 → 한 소식으로 묶고 '보도 매체 수'를 센다
    3) 많이 보도된 소식 → 출시 소식 → 공식·전문 매체 순으로 최대 limit건, 같은 회사 최대 PER_PRODUCT_LIMIT건
    4) 최근 12시간 안에 보낸 소식과 같은 소식은 다시 보내지 않는다."""
    titles = titles or {}
    words_of = lambda it: topic_words(it["title"]) | topic_words(titles.get(it["link"], ""))
    cutoff = (now - dt.timedelta(hours=12)).isoformat()
    sent_recent = []
    for old in recent:
        if old.get("sent_at", "") >= cutoff:
            prof = intel.profile({"title": old.get("title", "")})
            if prof["products"]:
                sent_recent.append((prof["products"][0], topic_words(old.get("title", "")) | topic_words(old.get("title_ko", ""))))
    candidates = [it for it in pending if important(it) and not FLUFF.search(it["title"])
                  and not JUNK_SOURCES.search(it.get("source", ""))]
    # 소식 고유 단어: 2건 이상에 나오지만 전체 후보의 15%(최소 3건) 이하에만 나오는 4글자 이상 단어
    freq = {}
    for it in candidates:
        for word in words_of(it):
            freq[word] = freq.get(word, 0) + 1
    ceiling = max(3, int(len(candidates) * 0.15))
    STRONG.clear()
    STRONG.update(w for w, n in freq.items() if 2 <= n <= ceiling and len(w) >= 4 and not w.isdigit())
    clusters = []
    for item in sorted(candidates, key=intel.order_key):
        product = (intel.profile(item)["products"] or ["기타"])[0]
        words = words_of(item)
        for cluster in clusters:
            if cluster["product"] == product and same_topic(words, cluster["words"]):
                cluster["items"].append(item)
                cluster["sources"].add(item.get("source", ""))
                break
        else:
            clusters.append({"product": product, "words": words, "items": [item], "sources": {item.get("source", "")}})
    dropped, chosen, per_product = [], [], {}
    for cluster in clusters:
        if any(prod == cluster["product"] and same_topic(cluster["words"], w) for prod, w in sent_recent):
            dropped.extend(cluster["items"])
            cluster["done"] = True
    # 한 매체만 보도한 소식은 '핵심 4사 출시 소식'이거나 공식·전문 매체일 때만
    live = [c for c in clusters if not c.get("done") and (
        len(c["sources"]) >= 2 or any(intel.profile(x)["priority"] == 0 or intel.profile(x)["source_rank"] <= 3
                                      for x in c["items"]))]
    live.sort(key=lambda c: (-min(len(c["sources"]), 6), intel.order_key(c["items"][0])))
    for cluster in live:
        if per_product.get(cluster["product"], 0) >= PER_PRODUCT_LIMIT:
            continue
        rep = min(cluster["items"], key=lambda x: (intel.profile(x)["source_rank"], intel.order_key(x)))
        rep = dict(rep, coverage=len(cluster["sources"]))
        chosen.append(rep)
        dropped.extend(it for it in cluster["items"] if it["link"] != rep["link"])
        per_product[cluster["product"]] = per_product.get(cluster["product"], 0) + 1
        if len(chosen) >= limit:
            break
    pick_items.dropped = dropped
    return chosen


def utf16_length(text):
    return len(text.encode("utf-16-le")) // 2


def format_pages(items, now, backlog=0, failures=(), translate=None):
    """번호 매긴 한 줄 요약형. 텔레그램 HTML(원문 링크는 '원문' 글자에 숨김)."""
    prefix = "🤖 <b>AI 핵심 뉴스</b> · " + now.astimezone(KST).strftime("%m/%d %H:%M")
    if failures:
        prefix += " · 일부 피드 조회 실패"
    if not items:
        message = "새 AI 기사를 확인하지 못했습니다." if failures else "이번 회차 새 AI 뉴스가 없습니다."
        return [(prefix+"\n\n"+message, [])]
    use_default = translate is translate_title
    translate = translate or (lambda title: title)
    pages, blocks, group = [], [], []
    for number, item in enumerate(items, 1):
        title = korean_title(item) if use_default else clean(translate(item["title"]))
        title = html.escape(re.sub(r"\s*[.。]$", "", title)[:160])
        p = intel.profile(item)
        meta = [", ".join(p["products"][:2])] if p["products"] else []
        if STATUS_SHORT.get(p["status"]):
            meta.append(STATUS_SHORT[p["status"]])
        published = dt.datetime.fromisoformat(item["published_at"]).astimezone(KST).strftime("%H:%M")
        meta.append(f"{html.escape(item['source'][:40])} {published}")
        if item.get("coverage", 1) >= 3:
            meta.append(f"{item['coverage']}개 매체 보도")
        link = html.escape(item["link"], quote=True)
        tail = f' <a href="{link}">원문</a>'
        block = f"{number}. <b>{title}</b>\n    " + " · ".join(meta)
        if utf16_length(prefix+"\n\n"+block+tail) > 3800:
            tail = ""  # 비정상적으로 긴 URL은 발송문에서 제외 (상태 파일에는 보관)
        block += tail
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
        response = session.post(url, json={"chat_id":chat_id, "text":message, "parse_mode":"HTML",
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
    # Do not drain yesterday's large backlog as if it were fresh news.
    state["pending"] = [item for item in state["pending"]
                        if dt.datetime.fromisoformat(item["published_at"]) >= now-dt.timedelta(hours=PENDING_HOURS)]
    state["skipped"] = {key: value for key, value in state.get("skipped", {}).items()
                        if value >= (now-dt.timedelta(days=2)).isoformat()}
    state["sent_stories"] = [item for item in state.get("sent_stories", [])
                             if dt.datetime.fromisoformat(item["sent_at"]) >= now-dt.timedelta(hours=48)]
    previous = dt.datetime.fromisoformat(state["last_success_at"]) if state.get("last_success_at") else now-dt.timedelta(hours=2)
    cutoff = max(now-dt.timedelta(hours=24), previous-dt.timedelta(minutes=30))
    items, failures = collect(session, now, cutoff)
    merge_pending(state, items)
    # 덜 중요한 기사는 보내지 않고 '건너뜀'으로 기록해 다음 회차에 다시 올라오지 않게 한다.
    minor = [item for item in state["pending"] if not important(item)]
    for item in minor:
        state["skipped"].update({key: now.isoformat() for key in keys_for(item)})
    state["pending"] = [item for item in state["pending"] if important(item)]
    default_translation = translate is None or translate is translate_title
    titles = {}
    selected = pick_items(state["pending"], MAX_ITEMS + 3, now, state["sent_stories"])
    dropped = list(pick_items.dropped)
    if default_translation:
        for item in selected:
            titles[item["link"]] = korean_title(item)
        # 언어가 달라 못 묶었던 같은 소식(한글 기사 vs 영문 기사)을 한국어 제목으로 다시 확인
        kept = []
        for item in selected:
            words = topic_words(item["title"]) | topic_words(titles[item["link"]])
            product = (intel.profile(item)["products"] or ["기타"])[0]
            twin = next((k for k in kept if (intel.profile(k)["products"] or ["기타"])[0] == product and
                         same_topic(words, topic_words(k["title"]) | topic_words(titles[k["link"]]))), None)
            if twin is not None:
                dropped.append(item)
            else:
                kept.append(item)
        selected = kept
    selected = selected[:MAX_ITEMS]
    for item in dropped:   # 이미 보낸·고른 소식의 다른 매체 기사
        state["skipped"].update({key: now.isoformat() for key in keys_for(item)})
    dropped_links = {item["link"] for item in dropped}
    state["pending"] = [item for item in state["pending"] if item["link"] not in dropped_links]
    pick_items.dropped = dropped
    if default_translation:
        translate = lambda title, _map={i["title"]: titles.get(i["link"]) for i in selected}: _map.get(title) or korean_title({"title": title})
    pages = format_pages(selected, now, len(state["pending"])-len(selected), failures, translate) if selected else []
    if dry_run:
        print(f"AI_DRY_RUN feeds_failed={len(failures)} new={len(items)} skipped={len(minor)} selected={len(selected)} pages={len(pages)} pending={len(state['pending'])}")
        for message, _ in pages:
            print(message)
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
        state["sent_stories"].extend({"title":item["title"], "title_ko":titles.get(item["link"], ""),
                                      "source":item.get("source", ""), "sent_at":now.isoformat()} for item in group)
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
