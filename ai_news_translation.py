"""Contextual Korean auto-translation with terminology and integrity checks.

This validates omissions and changed identifiers, not human-level semantic quality.
No paid API, new credentials, or unsupported quality guarantee is introduced.
"""
from __future__ import annotations
from collections import Counter
import re

SEPARATOR = "§§§"
GLOSSARY = {
    "open weights":"가중치 공개", "open-weight":"가중치 공개",
    "open source":"오픈소스", "open-source":"오픈소스",
    "context window":"문맥 길이", "coding agent":"코딩 에이전트",
    "function calling":"함수 호출", "tool use":"도구 사용",
    "text-to-video":"텍스트 기반 영상 생성", "text-to-image":"텍스트 기반 이미지 생성",
    "reasoning":"추론", "fine-tuning":"미세 조정", "multimodal":"멀티모달",
}
NAMES = r"(?:Claude(?:\s+(?:Opus|Sonnet|Haiku|Fable))?|GPT|Grok|Gemini|DeepSeek|Qwen|Kimi|GLM|MiniMax)(?:[-\s]?\d+(?:\.\d+)*(?:[-\s](?:Pro|Flash|Thinking|Max|Coder|VL|Preview|Exp))?)*|Anthropic|OpenAI|ChatGPT|xAI|Cursor|Suno|Runway|ElevenLabs|NotebookLM|Windsurf|Replit|Lovable|Seedance|Seedream|Kling|深度求索|通义千问|通义|智谱|豆包|可灵|即梦"
NUMBERS = r"(?<![\w])(?:[$€¥₩]\s*)?\d[\d,.]*(?:\s?(?:%|ms|GB|TB|k|K|M|B))?"
KEEP = re.compile("|".join(re.escape(k) for k in sorted(GLOSSARY,key=len,reverse=True))+"|"+NAMES+"|"+NUMBERS,re.I)


def protect(text):
    replacements={}
    def replace(match):
        value=match.group()
        token=f"ZXQKEEP{len(replacements)}QXZ"
        replacements[token]=GLOSSARY.get(value.lower(),value)
        return token
    return KEEP.sub(replace,text),replacements


def default_engine(text):
    from deep_translator import GoogleTranslator
    return GoogleTranslator(source="auto",target="ko").translate(text)


def translated_record(item,engine=None):
    title=item["title"]
    excerpt=item.get("excerpt","")
    original=title+("\n"+SEPARATOR+"\n"+excerpt if excerpt else "")
    protected,replacements=protect(original)
    # Product names and technical terms alone are not grounds to retranslate an
    # already Korean sentence. Chinese text always requires translation.
    residual=re.sub(r"ZXQKEEP\d+QXZ","",protected)
    need=bool(re.search(r"[\u3400-\u9fff]",residual)) or len(re.findall(r"[A-Za-z]",residual))>max(5,len(re.findall(r"[가-힣]",residual)))
    if not need:
        return {"title":title,"excerpt":excerpt,"quality":"원문 한국어"}
    engine=engine or default_engine
    for _ in range(2):
        try:
            result=str(engine(protected) or "").strip()
            if excerpt and result.count(SEPARATOR)!=1:continue
            expected=Counter(replacements.keys())
            actual=Counter(re.findall(r"ZXQKEEP\d+QXZ",result))
            if actual!=expected:continue
            # Numbers inserted by the translation engine were not in the source.
            residual_result=re.sub(r"ZXQKEEP\d+QXZ","",result)
            if re.search(r"\d",residual_result):continue
            if re.search(r"[\u3400-\u9fff]",residual_result):continue
            if not re.search(r"[가-힣]",residual_result):continue
            if re.search(r"(?i)\b(?:not|cannot|can't|never|without)\b|不能|无法|不支持",residual):
                if not re.search(r"없|않|못|안\s|불가|미지원|없이",residual_result):continue
            # Retained long English clauses are not a completed Korean translation.
            if len(re.findall(r"[A-Za-z]{3,}",residual_result))>=5:continue
            if len(residual_result)<max(2,len(residual.strip())*.18):continue
            for token,value in replacements.items():result=result.replace(token,value)
            parts=result.split(SEPARATOR)
            ko_title=parts[0].strip()
            ko_excerpt=parts[1].strip() if excerpt else ""
            if not ko_title or (excerpt and not ko_excerpt):continue
            return {"title":ko_title,"excerpt":ko_excerpt,"quality":"자동번역·보존검사 통과"}
        except Exception:
            # Service failures/rate limits are not retried immediately. Format
            # validation failures may receive the second attempt above.
            break
    return {"title":title,"excerpt":excerpt,"quality":"번역 미검수·원문 표시"}
