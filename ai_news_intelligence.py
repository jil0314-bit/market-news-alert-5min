"""Source coverage and evidence-bound model/tool triage, without paid AI APIs."""
from __future__ import annotations
import datetime as dt
import re
from urllib.parse import urlencode

# Direct feeds were fetched and parsed on 2026-10-18. Other sites use labelled
# Google News site searches; indexing is not independent official confirmation.
DIRECT_FEEDS = (
    {"name":"OpenAI 공식 RSS", "url":"https://openai.com/news/rss.xml", "kind":"official", "region":"US"},
    {"name":"AI타임스", "url":"https://cdn.aitimes.com/rss/gn_rss_allArticle.xml", "kind":"specialist", "region":"KR"},
    {"name":"TechCrunch AI", "url":"https://techcrunch.com/category/artificial-intelligence/feed/", "kind":"specialist", "region":"US"},
)
SEARCH_TOPICS = (
    ("공식 Anthropic·Claude", 'site:anthropic.com (Claude OR model OR release OR features) when:1d', "en-US","US","US:en","official_search","US"),
    ("공식 xAI·Grok", 'site:x.ai (Grok OR release OR update OR integration) when:1d', "en-US","US","US:en","official_search","US"),
    ("공식 Google·Gemini", '(site:blog.google OR site:deepmind.google) (Gemini OR model OR release) when:1d', "en-US","US","US:en","official_search","US"),
    ("공식 Cursor·Suno", '(site:cursor.com OR site:suno.com) (release OR update OR changelog OR features OR integration) when:1d', "en-US","US","US:en","official_search","US"),
    ("국내 AI 전문매체", '(site:aitimes.kr OR site:zdnet.co.kr OR site:byline.network) (AI OR 인공지능 OR 클로드 OR 그록 OR 커서 OR 수노) when:1d', "ko","KR","KR:ko","specialist_search","KR"),
    ("미국 AI·IT 전문매체", '(site:venturebeat.com OR site:theverge.com OR site:arstechnica.com) (AI OR Claude OR Grok OR Gemini OR Cursor OR Suno) when:1d', "en-US","US","US:en","specialist_search","US"),
    ("주요 모델·툴 출시", '(Anthropic OR OpenAI OR Grok OR Gemini OR Cursor OR Suno) (launch OR release OR update OR preview OR integration) when:1d', "en-US","US","US:en","search","US"),
    ("중국 AI 전문매체", '(site:jiqizhixin.com OR site:qbitai.com OR site:36kr.com) (大模型 OR 人工智能 OR DeepSeek OR 通义 OR Kimi OR 智谱 OR MiniMax) when:1d', "zh-CN","CN","CN:zh-Hans","specialist_search","CN"),
    ("중국 모델 공식발표", '(site:deepseek.com OR site:api-docs.deepseek.com OR site:qwen.ai OR site:moonshot.ai OR site:z.ai OR site:minimax.io) (model OR release OR 模型 OR 发布 OR 更新) when:1d', "en-US","US","US:en","official_search","CN"),
    ("중국 모델·영상·코딩", '(DeepSeek OR Qwen OR Kimi OR GLM OR MiniMax OR 豆包 OR 可灵 OR 即梦) (发布 OR 更新 OR 模型 OR 编程 OR 视频 OR 开源) when:1d', "zh-CN","CN","CN:zh-Hans","search","CN"),
    ("중국 AI 해외보도", '(DeepSeek OR Qwen OR Kimi OR Zhipu OR MiniMax OR "Kling AI" OR "Seedance") (model OR release OR coding OR features) when:1d', "en-US","US","US:en","search","CN"),
    ("AI 툴·연동 활용", '(Cursor OR Suno OR Replit OR Windsurf OR v0 OR Lovable OR Runway OR Kling OR ElevenLabs OR NotebookLM) (AI OR integration OR workflow OR update OR release) when:1d', "en-US","US","US:en","search","GLOBAL"),
)
PRODUCTS = (
    ("Anthropic/Claude", r"anthropic|claude|앤트로픽|엔트로픽|클로드"),
    ("OpenAI/ChatGPT", r"openai|chatgpt|\bgpt[-\d]|오픈\s?ai|오픈에이아이|챗지피티"),
    ("xAI/Grok", r"\bxai\b|\bgrok\b|그록"),
    ("Google/Gemini", r"\bgemini\b|제미나이|제미니"),
    ("Cursor", r"\bcursor\b|커서"), ("Suno", r"\bsuno\b|수노"),
    ("DeepSeek", r"deepseek|딥시크|深度求索"), ("Qwen", r"\bqwen\b|通义|千问|큐웬"),
    ("Kimi/Moonshot", r"\bkimi\b|moonshot|月之暗面"),
    ("GLM/Zhipu", r"\bglm(?:[-\d\w]*)\b|zhipu|智谱|\bz\.ai\b"),
    ("MiniMax", r"minimax|미니맥스"), ("Doubao/Seed", r"doubao|豆包|seedance|seedream"),
    ("Kling", r"\bkling\b|可灵|클링"), ("Replit/Windsurf",r"replit|windsurf|리플릿|윈드서프"),
    ("Runway/ElevenLabs",r"\brunway\b|elevenlabs|런웨이|일레븐랩스"),
    ("NotebookLM/Lovable",r"notebooklm|lovable|노트북lm|러버블"),
)
CAPABILITIES = (
    ("코딩·개발", r"\bcod(?:e|ing|er)\b|programming|software engineer|코딩|코드|개발|编程|代码"),
    ("추론·수학", r"reasoning|\bmath|추론|수학|推理|数学"),
    ("음악·작곡", r"music|\bsong|음악|작곡|음원|노래|音乐|作曲"),
    ("영상 생성·편집", r"video|영상|동영상|视频"),
    ("이미지·디자인", r"image|design|이미지|디자인|图像|图像生成"),
    ("음성·전사", r"voice|speech|transcri|음성|전사|语音"),
    ("검색·자료 조사", r"search|research|browse|검색|자료 조사|리서치|搜索|研究"),
    ("문서·데이터 분석", r"spreadsheet|\bcsv\b|document|data analysis|엑셀|문서|데이터 분석|数据分析|文档"),
    ("에이전트·자동화", r"agent|automation|workflow|에이전트|자동화|워크플로|智能体|工作流"),
    ("긴 문맥·멀티모달", r"long.context|multimodal|긴 문맥|멀티모달|长上下文|多模态"),
)
RELEVANT = re.compile("|".join(pattern for _,pattern in PRODUCTS+CAPABILITIES) + r"|(?<![a-z])(?:ai|llm)(?![a-z])|인공지능|人工智能|大模型|模型", re.I)
RUMOR = re.compile(r"rumou?r|reportedly|leak|unconfirmed|소문|루머|유출|추정|传闻|据传|泄露",re.I)
UPCOMING = re.compile(r"upcoming|coming soon|expected to|plans? to|will (?:launch|release)|next (?:week|month)|출시 예정|공개 예정|출시예정|출시할|예고|即将|将推出|计划发布|尚未|not (?:yet )?(?:released|available)",re.I)
PREVIEW = re.compile(r"preview|\bbeta\b|early access|미리보기|프리뷰|베타|预览|测试版|内测",re.I)
RELEASE = re.compile(r"introducing|launch(?:es|ed)?|releases?|unveils?|now available|출시|공개|새 모델|更新|发布|推出|上线",re.I)
INTEGRATION = re.compile(r"integrat|connect|works? with|support for|available in|\bmcp\b|연동|결합|통합|지원|接入|集成|连接",re.I)


def sources(legacy_topics):
    result = [dict(source) for source in DIRECT_FEEDS]
    for name,query,language,country,edition,*metadata in list(legacy_topics)+list(SEARCH_TOPICS):
        kind,region = metadata or ("search","GLOBAL")
        result.append({"name":name,"kind":kind,"region":region,
                       "url":"https://news.google.com/rss/search?"+urlencode({"q":query,"hl":language,"gl":country,"ceid":edition})})
    return result


def profile(item):
    # All labels are textual clues, not independently tested capabilities.
    title = item["title"]
    text = title + " " + item.get("excerpt", "")
    products = [name for name,pattern in PRODUCTS if re.search(pattern,text,re.I)]
    features = [name for name,pattern in CAPABILITIES if re.search(pattern,text,re.I)]
    if RUMOR.search(title):
        status = "미확인 보도·전망"
    elif UPCOMING.search(title):
        status = "출시 예정·예고"
    elif PREVIEW.search(title):
        status = "미리보기·베타"
    elif RELEASE.search(title) and products:
        status = "출시·업데이트 보도"
    else:
        status = "기능·활용/일반 보도"
    integration = bool(INTEGRATION.search(text)) and bool(products) and (
        len(products)>=2 or bool(re.search(r"\b(?:mcp|api|connector|plugin)\b|커넥터|플러그인|插件",text,re.I)))
    core = any(name in products for name,_ in PRODUCTS[:4])
    tool = any(name in products for name,_ in PRODUCTS[4:6]+PRODUCTS[12:])
    china = item.get("region")=="CN" or any(name in products for name,_ in PRODUCTS[6:13])
    release = status in ("출시·업데이트 보도","출시 예정·예고","미리보기·베타")
    priority = 0 if core and release else 1 if core or (products and release) or integration else 2 if features or products else 3
    if status=="미확인 보도·전망":priority=max(priority,2)
    kind=item.get("source_kind","search")
    source_rank={"official":0,"official_search":1,"specialist":2,"specialist_search":3,"search":4}.get(kind,4)
    return {"products":products,"features":features,"status":status,"integration":integration,
            "core":core,"tool":tool,"china":china,"priority":priority,"source_rank":source_rank}


def order_key(item):
    p=profile(item)
    return (p["priority"],p["source_rank"],-dt.datetime.fromisoformat(item["published_at"]).timestamp())


def select_items(pending, limit, now):
    ranked=sorted(pending,key=order_key)
    selected=[]
    def take(predicate, count):
        for item in ranked:
            if len(selected)>=limit or count<=0:break
            if item not in selected and predicate(item):
                selected.append(item);count-=1
    take(lambda item:profile(item)["core"] and profile(item)["priority"]==0,10)
    take(lambda item:profile(item)["china"],5)
    take(lambda item:profile(item)["tool"],4)
    take(lambda item:item.get("source_kind") in ("specialist","specialist_search"),3)
    take(lambda item:(now-dt.datetime.fromisoformat(item["published_at"])).total_seconds()>=43200,2)
    take(lambda item:True,limit)
    return sorted(selected,key=order_key)


def usage_idea(features):
    if "코딩·개발" in features:return "기존 코드 검토·수정 보조를 작은 예제로 비교해 보기"
    if "음악·작곡" in features:return "직접 쓴 가사를 짧은 데모로 만들어 보고 사용권 확인"
    if "문서·데이터 분석" in features:return "뉴스·CSV·문서 정리에 적용해 보고 원자료와 대조"
    if "검색·자료 조사" in features:return "모델별 공식 발표와 기능 비교표 만들기에 시험 적용"
    if "영상 생성·편집" in features or "이미지·디자인" in features:return "발표·영상용 짧은 시안을 만들어 편집 가능 범위 비교"
    if "음성·전사" in features:return "음성 메모의 한글 전사·요약 정확도를 비교"
    if "에이전트·자동화" in features:return "뉴스 정리 같은 반복 작업을 샘플 자료로 시험"
    return "공식 기능·사용 조건을 확인한 뒤 현재 도구와 작은 과제로 비교"


def briefing_lines(item, translate=lambda text:text):
    p=profile(item)
    source_label={"official":"공식 RSS","official_search":"공식 사이트 검색·원문 확인 필요",
                  "specialist":"전문매체 RSS","specialist_search":"전문매체 검색"}.get(item.get("source_kind"),"뉴스 검색")
    lines=[f"[{p['status']}] {', '.join(p['products'][:3]) or 'AI 관련'} · {source_label}"]
    # No model names, version numbers, release dates or benchmark numbers are
    # invented. The original headline and link remain evidence anchors.
    lines.append("기능 단서(제목·RSS): "+(", ".join(p["features"][:4]) or "구체 기능 미확인"))
    if item.get("excerpt"):
        translated=translate(item["excerpt"])
        lines.append("보도 발췌: "+translated[:180])
    if p["integration"]:
        lines.append("연동 언급: "+", ".join(p["products"][:3])+" · 실제 지원·요금·조건은 원문 확인")
    lines.append("활용 검토(추정): "+usage_idea(p["features"]))
    if p["status"] in ("미확인 보도·전망","출시 예정·예고"):
        lines.append("출시일·이용 가능 여부: 원문 확인 전 미확정")
    return lines
