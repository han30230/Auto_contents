import html
import json
import os
import random
import re
import sys
import threading
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable
from urllib.request import Request, urlopen

from dotenv import load_dotenv
from openai import OpenAI
from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.action_chains import ActionChains
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from webdriver_manager.chrome import ChromeDriverManager


@dataclass
class NaverConfig:
    naver_id: str
    naver_pw: str
    openai_api_key: str
    manual_login: bool = False
    blog_action: str = "save"  # "save" 또는 "publish"


def load_config() -> NaverConfig:
    """
    .env 또는 환경변수에서 설정을 읽어옵니다.

    필요한 값:
      - NAVER_ID
      - NAVER_PW
      - OPENAI_API_KEY
    """
    load_dotenv()

    naver_id = os.getenv("NAVER_ID")
    naver_pw = os.getenv("NAVER_PW")
    openai_api_key = os.getenv("OPENAI_API_KEY")
    manual_login = os.getenv("MANUAL_LOGIN", "").strip().lower() in ("1", "true", "yes")
    blog_action = (os.getenv("BLOG_ACTION", "save") or "save").strip().lower()
    if blog_action not in ("save", "publish"):
        blog_action = "save"

    missing = [k for k, v in {
        "NAVER_ID": naver_id,
        "NAVER_PW": naver_pw,
        "OPENAI_API_KEY": openai_api_key,
    }.items() if not v]

    if missing:
        raise RuntimeError(
            f"환경변수(.env)에 {', '.join(missing)} 가(이) 설정되지 않았습니다."
        )

    return NaverConfig(
        naver_id=naver_id,
        naver_pw=naver_pw,
        openai_api_key=openai_api_key,
        manual_login=manual_login,
        blog_action=blog_action,
    )


def create_openai_client(api_key: str) -> OpenAI:
    """
    OpenAI 클라이언트를 생성합니다.
    """
    return OpenAI(api_key=api_key)


def _strip_double_asterisks(text: str) -> str:
    """본문에서 ** ... ** 만 제거합니다. 나머지 기호(소제목, 괄호 등)는 유지."""
    text = re.sub(r"\*\*(.+?)\*\*", r"\1", text)
    return text


def _strip_html_tags(text: str) -> str:
    """RSS description 등에서 HTML 태그 제거 + 공백 정리."""
    if not text:
        return ""
    text = html.unescape(text)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def _extract_first_image_src(html_text: str) -> str | None:
    """HTML 안에서 첫 번째 img src URL만 추출."""
    if not html_text:
        return None
    m = re.search(r'<img[^>]+src=["\'](.*?)["\']', html_text, re.IGNORECASE | re.DOTALL)
    if not m:
        return None
    return m.group(1).strip() or None


GOOGLE_TRENDS_RSS_URL = "https://trends.google.com/trending/rss?geo=KR"


def _parse_trends_rss() -> ET.Element | None:
    """Google Trends 한국 실시간 인기 RSS 파싱. 실패 시 None."""
    try:
        req = Request(
            GOOGLE_TRENDS_RSS_URL,
            headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"},
        )
        with urlopen(req, timeout=15) as resp:
            root = ET.fromstring(resp.read())
        return root
    except Exception:
        return None


def fetch_google_trending_keywords_kr(max_items: int = 35) -> list[str]:
    """
    한국 기준 Google 실시간 인기 검색어.
    https://trends.google.co.kr/trending?geo=KR RSS 사용.
    """
    out: list[str] = []
    root = _parse_trends_rss()
    if root is None:
        return out
    for item in root.iter():
        tag = item.tag.split("}")[-1] if "}" in item.tag else item.tag
        if tag != "item":
            continue
        for child in item:
            ctag = child.tag.split("}")[-1] if "}" in child.tag else child.tag
            if ctag == "title" and child.text and child.text.strip():
                s = child.text.strip()
                if s and s not in out:
                    out.append(s)
                if len(out) >= max_items:
                    return out[:max_items]
                break
    return out[:max_items]


def fetch_hot_news_headlines(max_items: int = 30) -> list[str]:
    """
    Google Trends RSS의 관련 기사 제목 — 트렌드와 연결된 핫한 뉴스.
    최신이 아닌 '인기·이슈' 위주.
    """
    headlines: list[str] = []
    root = _parse_trends_rss()
    if root is None:
        return headlines
    for item in root.iter():
        tag = item.tag.split("}")[-1] if "}" in item.tag else item.tag
        if tag != "item":
            continue
        for child in item:
            ctag = child.tag.split("}")[-1] if "}" in child.tag else child.tag
            if ctag == "news_item":
                for sub in child:
                    stag = sub.tag.split("}")[-1] if "}" in sub.tag else sub.tag
                    if stag == "news_item_title" and sub.text and sub.text.strip():
                        t = sub.text.strip()
                        if t and t not in headlines:
                            headlines.append(t)
                        if len(headlines) >= max_items:
                            return headlines[:max_items]
                        break
    return headlines[:max_items]


def fetch_rss_headlines(max_items: int = 25) -> list[str]:
    """한국 인기 뉴스 — 핫한 기사 우선 (Trends RSS 기반). 최신만 필요 시 Google News RSS 사용."""
    out = fetch_hot_news_headlines(max_items)
    if out:
        return out
    headlines: list[str] = []
    try:
        req = Request(
            "https://news.google.com/rss?hl=ko&gl=KR&ceid=KR:ko",
            headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"},
        )
        with urlopen(req, timeout=10) as resp:
            root = ET.fromstring(resp.read())
        for el in root.iter():
            tag = el.tag.split("}")[-1] if "}" in el.tag else el.tag
            if tag not in ("item", "entry"):
                continue
            if len(headlines) >= max_items:
                break
            for child in el:
                ctag = child.tag.split("}")[-1] if "}" in child.tag else child.tag
                if ctag == "title" and child.text and child.text.strip():
                    t = child.text.strip()
                    if t and t not in headlines:
                        headlines.append(t)
                    break
    except Exception:
        pass
    return headlines[:max_items]


TREND_FIELD_OPTIONS: tuple[str, ...] = (
    "전체",
    "경제·금융",
    "IT·테크·인터넷",
    "건강·의료",
    "연예·문화",
    "스포츠",
    "여행·맛집",
    "부동산",
    "자동차",
    "게임·e스포츠",
    "뷰티·패션",
    "교육·입시",
    "사회·이슈",
)


def _rss_state_path() -> Path:
    return Path(__file__).resolve().parent / "rss_state.json"


def load_rss_state() -> dict:
    """
    RSS 처리 이력을 로드합니다.
    구조 예시:
    {
      "sources": {
        "https://rss.blog.naver.com/xxx.xml": {
          "last_checked_at": "2026-03-18T10:20:00",
          "processed_ids": ["guid1", "guid2", ...]
        }
      }
    }
    """
    path = _rss_state_path()
    if not path.exists():
        return {"sources": {}}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if "sources" not in data or not isinstance(data["sources"], dict):
            data["sources"] = {}
        return data
    except Exception:
        return {"sources": {}}


def save_rss_state(state: dict) -> None:
    try:
        with open(_rss_state_path(), "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
    except Exception:
        # 상태 저장 실패는 치명적이지 않으므로 조용히 무시
        pass


def fetch_rss_feed(url: str) -> tuple[str | None, list[ET.Element]]:
    """
    단일 RSS/Atom 피드를 가져와 파싱합니다.

    반환:
      - channel_title (또는 blog/feed title)
      - item/entry Element 리스트
    """
    req = Request(
        url,
        headers={
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0 Safari/537.36"
            )
        },
    )
    with urlopen(req, timeout=15) as resp:
        data = resp.read()
    try:
        root = ET.fromstring(data)
    except ET.ParseError as e:
        raise RuntimeError(f"RSS XML 파싱 실패: {e}") from e

    # 네임스페이스가 있어도 태그 이름만 비교
    def _tag_name(el: ET.Element) -> str:
        return el.tag.split("}", 1)[-1] if "}" in el.tag else el.tag

    channel_title: str | None = None
    items: list[ET.Element] = []

    # RSS 2.0: <rss><channel><item>
    # Atom: <feed><entry>
    root_tag = _tag_name(root)
    if root_tag == "rss":
        channel = None
        for child in root:
            if _tag_name(child) == "channel":
                channel = child
                break
        if channel is not None:
            for child in channel:
                if _tag_name(child) == "title" and (child.text or "").strip():
                    channel_title = (child.text or "").strip()
                    break
            for child in channel:
                if _tag_name(child) == "item":
                    items.append(child)
    elif root_tag in ("feed",):
        for child in root:
            if _tag_name(child) == "title" and (child.text or "").strip():
                channel_title = (child.text or "").strip()
            if _tag_name(child) == "entry":
                items.append(child)
    else:
        # 기타 포맷: 전체 트리에서 item/entry 탐색
        for el in root.iter():
            if _tag_name(el) in ("item", "entry"):
                items.append(el)

    return channel_title, items


def extract_rss_item_data(item: ET.Element) -> dict:
    """
    단일 item/entry Element에서 필요한 정보를 dict로 추출합니다.
    - blog_title 은 fetch_rss_feed 에서 별도로 채웁니다.
    """

    def _tag_name(el: ET.Element) -> str:
        return el.tag.split("}", 1)[-1] if "}" in el.tag else el.tag

    data: dict = {
        "blog_title": None,
        "item_title": None,
        "link": None,
        "guid": None,
        "category": None,
        "pubDate": None,
        "description": None,
        "tags": [],
        "thumbnail_image": None,
        "id": None,
    }

    for child in item:
        tag = _tag_name(child)
        text = (child.text or "").strip()
        if not text and tag not in ("description", "content", "content:encoded", "summary"):
            continue
        if tag in ("title",):
            data["item_title"] = text
        elif tag in ("link",):
            # Atom 의 경우 <link href="..."> 인 경우도 있으므로 속성 우선
            href = child.attrib.get("href", "").strip()
            data["link"] = href or text or data["link"]
        elif tag == "guid":
            data["guid"] = text
        elif tag in ("category",):
            data["category"] = text
        elif tag in ("pubDate", "published", "updated"):
            data["pubDate"] = text
        elif tag in ("description", "content", "content:encoded", "summary"):
            # description 은 HTML 그대로 저장
            html_desc = ET.tostring(child, encoding="unicode", method="html")
            # 태그를 제외한 텍스트만 따로도 보유
            # 하지만 원문 재사용 금지를 위해 아래 clean_summary만 '아이디어 참고' 용도로 사용
            inner_text = text or html_desc
            data["description"] = inner_text
            thumb = _extract_first_image_src(inner_text)
            if thumb and not data["thumbnail_image"]:
                data["thumbnail_image"] = thumb
        elif tag in ("tag", "keyword", "keywords"):
            if text:
                data["tags"].append(text)

    # 고유 ID (guid 우선, 없으면 link)
    unique_id = data.get("guid") or data.get("link")
    data["id"] = unique_id
    return data


def is_new_rss_item(item_data: dict, state: dict, source_url: str) -> bool:
    """
    guid 또는 link 기준으로 이미 처리한 글인지 판단.
    """
    if not item_data.get("id"):
        return False
    src_state = state.setdefault("sources", {}).setdefault(
        source_url,
        {"last_checked_at": None, "processed_ids": []},
    )
    processed = src_state.setdefault("processed_ids", [])
    return item_data["id"] not in processed


def _default_rss_system_prompt() -> str:
    return (
        "당신은 한국어 네이버 블로그 전문 작가이자 SEO 컨설턴트입니다.\n"
        "입력으로 주어지는 RSS/원문 정보는 '참고용'일 뿐이며, 결과물은 반드시 완전히 새로운 원본 글이어야 합니다.\n"
        "다음 규칙을 반드시 지키세요.\n"
        "1) 원문의 표현, 문장 순서, 문단 구조, 소제목 스타일을 모방하지 마세요.\n"
        "2) 원문 문장을 그대로 복사하거나 일부만 치환하는 식으로 작성하지 마세요.\n"
        "3) 원문 소제목/목차 구조를 따라 쓰지 말고, 독창적인 목차와 흐름을 만드세요.\n"
        "4) 원문 제목과 유사한 제목을 만들지 말고, 다른 각도와 후킹 포인트로 새로운 제목을 만드세요.\n"
        "5) 이미지 URL 이 있어도 본문에 이미지를 자동 삽입하지 마세요. 필요하면 '대표 이미지 직접 추가 필요' 정도의 안내 문구만 사용할 수 있습니다.\n"
        "6) 이 글은 '출처 기반의 새로운 해설/분석/가이드 글'이어야 하며, 독자가 해당 주제를 더 잘 이해하도록 돕는 데 초점을 맞추세요.\n"
        "7) 한국어로 작성하며, 네이버 블로그에 어울리는 말투와 네이버 SEO 를 고려합니다.\n"
        "8) 최소 2,200자 이상, 가능하면 3,000자 내외 분량으로 충분한 정보, 사례, 체크리스트, 주의사항, FAQ 등을 포함할 수 있습니다.\n"
        "9) 과장/허위 사실은 금지하며, 불확실한 정보는 '추정', '가능성' 등의 표현으로 구분합니다.\n"
        "10) 출력 형식은 반드시 아래와 같이 합니다.\n"
        "===TITLE===\n"
        "제목 한 줄\n"
        "===CONTENT===\n"
        "본문 전체"
    )


def _default_rss_user_prompt_template() -> str:
    """UI/설정 비어 있을 때 쓰는 RSS 유저 프롬프트. {placeholder} 치환."""
    return (
        "다음은 참고용으로 제공되는 RSS 기반 원문 정보입니다.\n"
        "이 정보에서 '주제', '핵심 키워드', '독자가 궁금해할 질문', '주의할 점', '실생활 팁'만 추출하고, 완전히 새로운 구성과 관점으로 글을 작성하세요.\n\n"
        "source_blog_title: {source_blog_title}\n"
        "source_post_title: {source_post_title}\n"
        "source_link: {source_link}\n"
        "source_category: {source_category}\n"
        "source_tags: {source_tags}\n"
        "source_summary_clean: {source_summary_clean}\n"
        "inferred_topic: {inferred_topic}\n"
        "suggested_keywords: {suggested_keywords}\n\n"
        "위 정보를 참고하되, 글의 구조와 제목, 소제목, 문장을 모두 새로 만들어 주세요.\n"
        "특히 다음과 같은 구성을 고려해 정보성/분석형/가이드형 글로 확장해 주세요:\n"
        "- 왜 이 주제가 중요한지 (배경, 맥락)\n"
        "- 어떤 사람에게 특히 중요한 주제인지\n"
        "- 시작하기 전에 꼭 알아야 할 체크포인트\n"
        "- 실제 활용/적용 예시\n"
        "- 자주 하는 오해와 주의사항\n"
        "- 요약 및 마무리 조언\n\n"
        "출력 시에는 아래 형식을 엄격하게 지키세요.\n"
        "===TITLE===\n"
        "새로운 관점의 제목\n"
        "===CONTENT===\n"
        "완전히 새로운 구성의 본문 (소제목, 예시, 체크리스트, 주의사항, FAQ 등을 포함해도 좋습니다)."
    )


def _apply_rss_user_placeholders(template: str, ctx: dict[str, str]) -> str:
    out = template
    for k, v in ctx.items():
        out = out.replace("{" + k + "}", v)
    return out


def build_rss_based_prompt(
    source_data: dict,
    include_source_link: bool,
    custom_system: str | None = None,
    custom_user_template: str | None = None,
) -> tuple[str, str]:
    """
    RSS 기반 글 생성을 위한 system / user 프롬프트를 생성합니다.
    - 원문 문장/문단/소제목/이미지 구조 재사용 금지
    custom_system / custom_user_template 이 비어 있지 않으면 UI·설정값 사용.
    유저 템플릿에는 {source_blog_title} 등 placeholder 사용 가능.
    """
    system_msg = (custom_system or "").strip() or _default_rss_system_prompt()
    summary_clean = _strip_html_tags(source_data.get("description") or source_data.get("source_summary_clean") or "")

    inferred_topic = source_data.get("inferred_topic") or ""
    suggested_keywords = source_data.get("suggested_keywords") or []
    if isinstance(suggested_keywords, str):
        keywords_str = suggested_keywords
    else:
        keywords_str = ", ".join(str(k) for k in suggested_keywords if k)

    ctx: dict[str, str] = {
        "source_blog_title": str(source_data.get("source_blog_title") or source_data.get("blog_title") or ""),
        "source_post_title": str(source_data.get("source_post_title") or source_data.get("item_title") or ""),
        "source_link": str(source_data.get("source_link") or source_data.get("link") or ""),
        "source_category": str(source_data.get("source_category") or source_data.get("category") or ""),
        "source_tags": ", ".join(source_data.get("source_tags") or source_data.get("tags") or []),
        "source_summary_clean": summary_clean,
        "inferred_topic": str(inferred_topic),
        "suggested_keywords": keywords_str,
    }

    tpl = (custom_user_template or "").strip()
    if tpl:
        user_msg = _apply_rss_user_placeholders(tpl, ctx)
    else:
        user_msg = _apply_rss_user_placeholders(_default_rss_user_prompt_template(), ctx)

    if include_source_link:
        user_msg += (
            "\n\n본문 마지막 단락 바로 아래에, 다음 문장을 1줄로 추가할 수 있도록 자연스럽게 마무리 문단을 구성해 주세요.\n"
            "참고: 관련 주제를 바탕으로 재구성한 글이며, 원문 참고 링크: {source_link}"
        )
        user_msg = user_msg.replace("{source_link}", ctx["source_link"])

    return system_msg, user_msg


def generate_post_from_rss(
    client: OpenAI,
    source_data: dict,
    include_source_link: bool = False,
    custom_system: str | None = None,
    custom_user_template: str | None = None,
) -> tuple[str, str]:
    """
    RSS 기반으로 완전히 새로운 블로그 글을 생성합니다.
    반환: (title, content)
    """
    system_msg, user_msg = build_rss_based_prompt(
        source_data,
        include_source_link,
        custom_system=custom_system,
        custom_user_template=custom_user_template,
    )
    completion = client.chat.completions.create(
        model="gpt-4.1-mini",
        messages=[
            {"role": "system", "content": system_msg},
            {"role": "user", "content": user_msg},
        ],
        temperature=0.7,
    )
    text = completion.choices[0].message.content or ""

    title = ""
    content = ""
    if "===TITLE===" in text and "===CONTENT===" in text:
        _, rest = text.split("===TITLE===", 1)
        title_part, content_part = rest.split("===CONTENT===", 1)
        title = (title_part or "").strip()
        content = (content_part or "").strip()
    else:
        lines = [line for line in text.splitlines() if line.strip()]
        if lines:
            title = lines[0].strip()
            content = "\n".join(lines[1:]).strip()
        else:
            title = "RSS 기반 자동 생성 글"
            content = text.strip()

    title = _strip_double_asterisks(title)
    content = _strip_double_asterisks(content)

    # 출처 링크 표기 옵션이 켜져 있으면 마지막에 1줄 추가
    if include_source_link:
        source_link = source_data.get("source_link") or source_data.get("link") or ""
        if source_link:
            footer = (
                f"\n\n참고: 관련 주제를 바탕으로 재구성한 글이며, 원문 참고 링크: {source_link}"
            )
            content = content.rstrip() + footer

    return title, content


def default_prompt_hot_system() -> str:
    return (
        "당신은 네이버 블로그·네이버 SEO에 특화된 한국어 콘텐츠 전문가입니다. "
        "다음 규칙을 반드시 지키세요.\n"
        "1) 제목: 사람이 꼭 클릭하고 싶어 하는 '후킹' 제목. 궁금함·유익함·경고·숫자·질문을 활용해 클릭률을 높이세요. 검색 키워드 포함, 25~35자 권장.\n"
        "2) 본문: 최소 1,800자 이상, 권장 2,500~3,500자. '도움이 되는' 글이 되게: 핵심 정보 요약, 단계별 정리, 주의점·팁, 정리·마무리를 넣으세요. 소제목은 1. 2. 3. 또는 질문형으로 구분.\n"
        "3) 네이버 SEO: 핵심 키워드를 제목·도입·소제목·본문에 자연스럽게 2~3회 이상. 소제목·번호·괄호·물음표 등은 자유롭게 사용 가능.\n"
        "4) 말투: 친근하고 신뢰감 있게. 정보는 정확하게, 숫자·근거 포함.\n"
        "5) 형식: 별표 두 개(**텍스트**)만 절대 사용하지 마세요. 그 외 소제목(1. 2. 3.), 괄호(), 물음표?, 이모지 등은 사용해도 됩니다."
    )


def default_prompt_hot_user_suffix() -> str:
    return (
        "아래 형식으로만 답변하세요. 본문에 ** 두 개로 감싼 강조는 넣지 마세요. 소제목·번호·괄호 등은 사용 가능.\n\n"
        "===TITLE===\n"
        "여기에 후킹되는 블로그 글 제목 (클릭하고 싶게)\n"
        "===CONTENT===\n"
        "여기에 본문 전체. **만 쓰지 말고, 1. 2. 3. 소제목·괄호·질문 등으로 읽기 쉽고 도움 되게. 최소 1,800자 이상."
    )


def generate_hot_issue_post(
    client: OpenAI,
    topic: str | None = None,
    topic_mode: str = "news_rss",
    trend_field: str = "전체",
    system_override: str | None = None,
    user_suffix_override: str | None = None,
) -> tuple[str, str]:
    """
    topic_mode:
      - manual: topic 문자열 그대로 사용
      - google_trends: 한국 Google 실검 키워드 목록 + trend_field(분야)에 맞게 GPT가 하나 선택
      - news_rss: Google 뉴스 RSS 헤드라인에서 선택
    """
    now = datetime.now()
    current_date_str = f"{now.year}년 {now.month}월 {now.day}일"
    field = (trend_field or "전체").strip() or "전체"

    if topic_mode == "manual" and topic and topic.strip():
        topic_instruction = (
            f"오늘 날짜는 {current_date_str}입니다. "
            f"주제는 반드시 다음을 중심으로 작성해 주세요: '{topic.strip()}'. "
            "가능한 한 최신 동향을 반영하고, 읽는 사람에게 도움이 되게 써 주세요."
        )
    elif topic_mode == "google_trends":
        kws = fetch_google_trending_keywords_kr(40)
        if not kws:
            kws = fetch_rss_headlines(28)
            src_note = "(Google 실검 조회 실패 → 인기 뉴스 제목으로 대체한 후보입니다.)"
        else:
            src_note = "(한국 기준 Google 검색 급상승·인기 키워드 후보입니다.)"
        block = "\n".join(f"- {k}" for k in kws[:30])
        if field == "전체":
            field_line = "독자·블로그 관심 분야: 제한 없음(전체). 목록에서 독자에게 가장 유익한 키워드 하나를 고르세요."
        else:
            field_line = (
                f"독자·블로그 관심 분야: 【{field}】. "
                "반드시 아래 목록에서 이 분야와 가장 잘 맞는 키워드(또는 그 키워드와 직접 연결되는 이슈) 하나만 골라 글을 쓰세요. "
                "분야와 거리가 멀어도 목록 중 그나마 가장 가까운 하나를 선택하세요. 목록 밖 주제는 금지입니다."
            )
        topic_instruction = (
            f"오늘 날짜는 {current_date_str}입니다.\n"
            f"{field_line}\n"
            f"{src_note}\n\n"
            "[후보 키워드·제목 목록]\n"
            f"{block}\n\n"
            "위 목록에서 선택한 하나만을 중심으로, 네이버 블로그 독자에게 도움이 되는 글을 작성하세요."
        )
    else:
        headlines = fetch_rss_headlines(max_items=28)
        if field != "전체" and headlines:
            topic_instruction = (
                f"오늘 날짜는 {current_date_str}입니다.\n"
                f"블로그·독자 관심 분야: 【{field}】.\n"
                "아래는 지금 한국에서 인기 있는 뉴스 제목(RSS)입니다. "
                "이 분야와 가장 관련 깊은 제목 하나를 골라 그 이슈를 바탕으로 글을 작성하세요. "
                "목록에 없는 임의 주제는 쓰지 마세요.\n\n"
                "[인기 뉴스 제목]\n"
                + "\n".join(f"- {h}" for h in headlines[:22])
                + "\n\n선택한 이슈를 읽는 사람에게 유익하고 클릭하고 싶게 만드는 글을 써 주세요."
            )
        elif headlines:
            headline_block = "\n".join(f"- {h}" for h in headlines[:22])
            topic_instruction = (
                f"오늘 날짜는 {current_date_str}입니다. "
                "아래는 지금 한국에서 실제로 인기 있는 뉴스/이슈 제목들(RSS 기준)입니다. "
                "반드시 이 목록에서 하나를 골라 그 기사·이슈를 바탕으로 블로그 글을 작성하세요.\n\n"
                "[최신 인기 뉴스 제목]\n"
                f"{headline_block}\n\n"
                "위 제목 중 하나를 선택해, 그 주제를 읽은 사람에게 도움이 되고 클릭하고 싶게 만드는 글을 써 주세요."
            )
        else:
            topic_instruction = (
                f"오늘 날짜는 {current_date_str}입니다. "
                "한국에서 최근 1~2일 이내 실제로 화제인 뉴스·이슈 하나를 골라 그 주제로 글을 작성해 주세요. "
                "과거 이슈가 아닌 최신 이슈만 선택하세요."
            )

    system_msg = (system_override or "").strip() or default_prompt_hot_system()
    user_tail = (user_suffix_override or "").strip() or default_prompt_hot_user_suffix()
    user_msg = f"{topic_instruction}\n\n{user_tail}"

    completion = client.chat.completions.create(
        model="gpt-4.1-mini",
        messages=[
            {"role": "system", "content": system_msg},
            {"role": "user", "content": user_msg},
        ],
        temperature=0.7,
    )

    text = completion.choices[0].message.content or ""

    # TITLE / CONTENT 파싱
    title = ""
    content = ""

    if "===TITLE===" in text and "===CONTENT===" in text:
        _, rest = text.split("===TITLE===", 1)
        title_part, content_part = rest.split("===CONTENT===", 1)
        title = title_part.strip()
        content = content_part.strip()
    else:
        lines = [line for line in text.splitlines() if line.strip()]
        if lines:
            title = lines[0].strip()
            content = "\n".join(lines[1:]).strip()
        else:
            title = "자동 생성된 블로그 글"
            content = text.strip()

    # ** 두 개 강조만 제거 (나머지 기호는 유지)
    title = _strip_double_asterisks(title)
    content = _strip_double_asterisks(content)

    return title, content


def create_driver() -> webdriver.Chrome:
    """
    Chrome WebDriver를 생성합니다.

    - 크롬이 설치되어 있어야 합니다.
    - webdriver_manager가 자동으로 드라이버를 내려받습니다.
    - 크래시 방지를 위해 GPU/샌드박스 등 옵션을 추가했습니다.
    """
    chrome_options = webdriver.ChromeOptions()
    chrome_options.add_argument("--start-maximized")
    # 크래시·불안정 방지 (Windows에서 드라이버 오류 시 도움)
    chrome_options.add_argument("--disable-gpu")
    chrome_options.add_argument("--no-sandbox")
    chrome_options.add_argument("--disable-dev-shm-usage")
    chrome_options.add_argument("--disable-extensions")
    chrome_options.add_argument("--disable-background-networking")
    chrome_options.add_experimental_option("excludeSwitches", ["enable-logging"])

    service = Service(ChromeDriverManager().install())
    driver = webdriver.Chrome(service=service, options=chrome_options)
    return driver


def naver_login(
    driver: webdriver.Chrome,
    cfg: NaverConfig,
    manual: bool = False,
    manual_login_event: threading.Event | None = None,
) -> None:
    """
    네이버에 로그인합니다.
    manual=True이고 manual_login_event가 있으면 이벤트가 set될 때까지 대기 (GUI용).
    """
    driver.get("https://nid.naver.com/nidlogin.login")
    wait = WebDriverWait(driver, 20)

    if manual:
        if manual_login_event is not None:
            manual_login_event.wait()
        else:
            input("브라우저에서 직접 로그인(캡차·2단계 인증 포함)을 완료한 뒤 Enter 키를 눌러 주세요...")
        time.sleep(2)
        return

    # 아이디/비밀번호 입력창 찾기
    id_input = wait.until(EC.presence_of_element_located((By.ID, "id")))
    pw_input = wait.until(EC.presence_of_element_located((By.ID, "pw")))

    id_input.clear()
    pw_input.clear()
    time.sleep(0.35)
    # 네이버가 send_keys를 감지해 차단할 수 있어, JS로 값 설정 + ID/PW 사이 텀
    try:
        driver.execute_script("arguments[0].value = arguments[1];", id_input, cfg.naver_id)
        time.sleep(0.45)
        driver.execute_script("arguments[0].value = arguments[1];", pw_input, cfg.naver_pw)
        time.sleep(0.35)
        pw_input.send_keys(Keys.RETURN)
    except Exception:
        id_input.send_keys(cfg.naver_id)
        time.sleep(0.5)
        pw_input.send_keys(cfg.naver_pw)
        time.sleep(0.25)
        pw_input.send_keys(Keys.RETURN)

    time.sleep(5)


def open_blog_write_page(driver: webdriver.Chrome, blog_id: str) -> None:
    """
    네이버 블로그 글쓰기 페이지를 엽니다.

    blog_id: 블로그 주소가 https://blog.naver.com/xxx 인 경우 xxx 부분
             (일반적으로 NAVER_ID 와 같지만, 다른 경우 직접 지정 필요)
    """
    # 글쓰기 폼 직접 URL 시도 (로그인 상태면 바로 에디터로 이동)
    driver.get(f"https://blog.naver.com/{blog_id}/postwrite")
    time.sleep(3)
    current = driver.current_url
    # postwrite가 없거나 리다이렉트되면 블로그 메인에서 '글쓰기' 클릭
    if "/postwrite" not in current and "PostWrite" not in current:
        driver.get(f"https://blog.naver.com/{blog_id}")
        wait = WebDriverWait(driver, 20)
        try:
            write_btn = wait.until(
                EC.element_to_be_clickable((By.LINK_TEXT, "글쓰기"))
            )
            write_btn.click()
        except Exception:
            try:
                write_btn = wait.until(
                    EC.element_to_be_clickable((By.CSS_SELECTOR, "a.btn_write, a[href*='PostWrite'], a[href*='postwrite']"))
                )
                write_btn.click()
            except Exception:
                pass
    # 글쓰기 에디터 로딩 대기
    time.sleep(8)


def _type_with_action_chains(
    driver: webdriver.Chrome, element, text: str, interval: float = 0.03, click_first: bool = True
) -> None:
    """
    ActionChains를 사용해 한 글자씩 interval 간격으로 입력합니다. (폴백용)
    """
    if click_first and element:
        ActionChains(driver).click(element).perform()
    for char in text:
        ActionChains(driver).send_keys(char).perform()
        time.sleep(interval)


def _paste_text_into_focused_editor(driver: webdriver.Chrome, element, text: str) -> None:
    """클립보드 복사 후 Ctrl+V로 붙여넣기 (한글·긴 본문에 적합)."""
    text = text or ""
    try:
        import pyperclip
    except ImportError:
        _type_with_action_chains(driver, element, text, interval=0.02)
        return
    mod = Keys.COMMAND if sys.platform == "darwin" else Keys.CONTROL
    prev = None
    try:
        prev = pyperclip.paste()
    except Exception:
        pass
    try:
        pyperclip.copy(text)
    except Exception:
        _type_with_action_chains(driver, element, text, interval=0.02)
        return
    try:
        element.click()
        time.sleep(0.28)
        ActionChains(driver).key_down(mod).send_keys("a").key_up(mod).perform()
        time.sleep(0.12)
        ActionChains(driver).key_down(mod).send_keys("v").key_up(mod).perform()
        time.sleep(0.45)
    except Exception:
        try:
            element.clear()
        except Exception:
            pass
        _type_with_action_chains(driver, element, text, interval=0.02)
    finally:
        if prev is not None:
            try:
                pyperclip.copy(prev)
            except Exception:
                pass


def fill_post_and_publish(
    driver: webdriver.Chrome, title: str, content: str, action: str = "save"
) -> None:
    """
    에디터에 제목/본문을 채우고 저장 또는 발행 버튼을 클릭합니다.

    action: "save" → 저장 버튼 클릭, "publish" → 발행 버튼 클릭
    """
    wait = WebDriverWait(driver, 25)

    # 1. iframe 전환 (mainFrame 우선, 없으면 다른 에디터 iframe 시도)
    driver.switch_to.default_content()
    iframe_selectors = [
        "iframe#mainFrame",
        "iframe[id*='se2_iframe']",
        "iframe[id*='se_canvas']",
        "iframe[id*='smartEditor']",
    ]
    frame_switched = False
    for sel in iframe_selectors:
        try:
            iframe = WebDriverWait(driver, 10).until(
                EC.presence_of_element_located((By.CSS_SELECTOR, sel))
            )
            driver.switch_to.frame(iframe)
            frame_switched = True
            break
        except Exception:
            driver.switch_to.default_content()
            continue
    if not frame_switched:
        # iframe 없이 에디터가 페이지에 직접 있는 경우(default_content 유지)
        pass

    # 2. 팝업/도움말 닫기: 취소 버튼 있으면 클릭, 없으면 도움말 패널 닫기
    try:
        cancel_btn = driver.find_element(By.CSS_SELECTOR, ".se-popup-button-cancel")
        cancel_btn.click()
        time.sleep(0.3)

    except Exception:
        # 취소 버튼 없으면 도움말 패널(.se-help-panel-close-button) 닫기
        try:
            close_btn = driver.find_element(By.CSS_SELECTOR, ".se-help-panel-close-button")
            close_btn.click()
            time.sleep(0.3)
        except Exception:
            pass

    # 3. 제목: 클립보드 붙여넣기
    title_area = wait.until(EC.element_to_be_clickable((By.CSS_SELECTOR, ".se-section-documentTitle")))
    _paste_text_into_focused_editor(driver, title_area, title or "")

    # 4. 본문: 전체를 한 번에 붙여넣기 (줄바꿈 유지)
    body_area = wait.until(EC.element_to_be_clickable((By.CSS_SELECTOR, ".se-section-text")))
    _paste_text_into_focused_editor(driver, body_area, content or "")

    time.sleep(0.6)

    # 5. 저장 또는 발행 버튼 클릭
    driver.switch_to.default_content()
    do_publish = action.strip().lower() == "publish"

    if do_publish:
        # 1단계: 발행 버튼 (에디터 상단 등)
        publish_selectors = [
            "button.publish_btn__m9KHH",
            "button[data-click-area='tpb.publish']",
            "button.se_publish",
            "button[aria-label*='발행']",
            "button.publish_btn__Y5zDv",
            "a.publish_btn__Y5zDv",
            "strong.publish_btn__Y5zDv",
            "button[type='submit']",
        ]
        btn = None
        for sel in publish_selectors:
            try:
                btn = wait.until(EC.element_to_be_clickable((By.CSS_SELECTOR, sel)))
                break
            except Exception:
                continue
        if not btn:
            try:
                btn = wait.until(EC.element_to_be_clickable((By.XPATH, "//button[contains(., '발행')]")))
            except Exception:
                try:
                    btn = wait.until(EC.element_to_be_clickable((By.XPATH, "//a[contains(., '발행')]")))
                except Exception:
                    btn = None
        if not btn:
            raise RuntimeError("발행 버튼을 찾지 못했습니다. F12로 selector를 확인해 주세요.")
        btn.click()
        print("발행 버튼을 클릭했습니다.")
        time.sleep(1.5)
        # 2단계: 확인 발행 버튼 (팝업/모달)
        confirm_selectors = [
            "button[data-testid='seOnePublishBtn']",
            "button.confirm_btn__WEaBq",
            "button[data-click-area='tpb*i.publish']",
        ]
        confirm_btn = None
        for sel in confirm_selectors:
            try:
                confirm_btn = WebDriverWait(driver, 10).until(
                    EC.element_to_be_clickable((By.CSS_SELECTOR, sel))
                )
                break
            except Exception:
                continue
        if not confirm_btn:
            try:
                confirm_btn = WebDriverWait(driver, 5).until(
                    EC.element_to_be_clickable((By.XPATH, "//button[contains(@class,'confirm') and contains(., '발행')]"))
                )
            except Exception:
                confirm_btn = None
        if confirm_btn:
            confirm_btn.click()
            print("확인 발행 버튼을 클릭했습니다.")
        # 2단계 버튼이 없으면 1단계만으로 완료된 경우로 간주
    else:
        try:
            save_btn = wait.until(EC.element_to_be_clickable((By.ID, "save_btn_bcz58")))
        except Exception:
            save_btn = wait.until(EC.element_to_be_clickable((By.CSS_SELECTOR, ".save_btn_bcz58")))
        save_btn.click()
        print("저장 버튼을 클릭했습니다.")

    time.sleep(5)


def main():
    """
    네이버 블로그 핫이슈 글 자동 생성 + 발행 진입점.
    """
    cfg = load_config()
    client = create_openai_client(cfg.openai_api_key)

    # 필요하면 topic 을 직접 지정 가능 (예: "테슬라 주가 급등 이슈")
    # topic = "원하는 이슈 직접 지정"
    topic = None

    print("GPT로 블로그 글을 생성 중입니다...")
    title, content = generate_hot_issue_post(client, topic=topic)
    print("제목:", title)
    print("본문 일부 미리보기:\n", content[:200], "...\n")

    driver = create_driver()
    try:
        if cfg.manual_login:
            print("수동 로그인 모드: 브라우저에서 직접 로그인해 주세요.")
        else:
            print("네이버에 로그인 중입니다...")
        naver_login(driver, cfg, manual=cfg.manual_login)

        # blog_id 가 NAVER_ID 와 같지 않은 경우 아래를 수정
        blog_id = cfg.naver_id

        print("블로그 글쓰기 페이지를 여는 중입니다...")
        open_blog_write_page(driver, blog_id=blog_id)

        action_label = "발행" if cfg.blog_action == "publish" else "저장"
        print(f"제목/본문을 입력하고 {action_label}까지 시도합니다...")
        fill_post_and_publish(driver, title=title, content=content, action=cfg.blog_action)

        print("작업이 완료되었습니다. 브라우저 상태를 확인해 주세요.")
        input("브라우저를 닫으려면 Enter 키를 누르세요...")
    finally:
        try:
            driver.quit()
        except Exception:
            pass


def _sanitize_error_for_gui(err: str) -> str:
    """GUI/반환용: 긴 백트레이스·크래시 메시지를 짧게 정리합니다."""
    if not err or len(err) < 500:
        return (err or "알 수 없는 오류").strip()
    lower = err.lower()
    if "symbols not available" in lower or "backtrace" in lower or "0x" in err[:200]:
        return (
            "Chrome 또는 드라이버에서 오류가 났습니다. "
            "Chrome을 최신 버전으로 업데이트하고, PC를 재시작한 뒤 다시 시도해 보세요. "
            "계속되면 수동 로그인 옵션을 켜서 실행해 보세요."
        )
    first_line = err.strip().split("\n")[0].strip()
    return first_line[:300] if first_line else "오류가 발생했습니다. 로그를 확인하세요."


def run_blog_workflow(
    cfg: NaverConfig,
    topic: str | None,
    log_fn: Callable[[str], None] | None = None,
    manual_login_event: threading.Event | None = None,
    hot_system: str | None = None,
    hot_user_suffix: str | None = None,
    profile: str | None = None,
) -> tuple[webdriver.Chrome | None, str | None]:
    """블로그 글 생성 ~ 저장/발행. 반환: (None, None) 성공(브라우저는 이미 종료), (None, error_msg) 실패."""
    def log(msg: str) -> None:
        if log_fn:
            log_fn(msg)
        else:
            print(msg)

    try:
        gc = load_gui_config(profile)
        if hot_system is None and hot_user_suffix is None:
            sys_ov = (gc.get("prompt_hot_system") or "").strip() or None
            suf_ov = (gc.get("prompt_hot_user_suffix") or "").strip() or None
        else:
            sys_ov = (hot_system or "").strip() or None
            suf_ov = (hot_user_suffix or "").strip() or None

        client = create_openai_client(cfg.openai_api_key)
        topic_trimmed = topic.strip() if topic else None
        topic_source = (gc.get("topic_source") or "google_trends").strip().lower()
        trend_field = (gc.get("trend_field") or "전체").strip() or "전체"
        if topic_source not in ("manual", "google_trends", "news_rss", "trend_1", "news_1"):
            topic_source = "google_trends"
        if topic_source == "trend_1":
            kws = fetch_google_trending_keywords_kr(1)
            topic_trimmed = kws[0] if kws else None
            if topic_trimmed:
                mode = "manual"
                log(f"주제: 실검 1위 — {topic_trimmed}")
            else:
                mode = "google_trends"
                log("실검 1위 조회 실패 → Google 실검 전체 모드로 진행합니다.")
        elif topic_source == "news_1":
            news = fetch_hot_news_headlines(1)
            topic_trimmed = news[0] if news else None
            if topic_trimmed:
                mode = "manual"
                log(f"주제: 인기뉴스 1위 — {topic_trimmed[:60]}...")
            else:
                mode = "news_rss"
                log("인기뉴스 1위 조회 실패 → 인기 뉴스 전체 모드로 진행합니다.")
        elif topic_source == "manual" and topic_trimmed:
            mode = "manual"
            log(f"주제: 직접 입력 — {topic_trimmed[:60]}...")
        elif topic_source == "google_trends":
            mode = "google_trends"
            log(f"주제: Google 실검 키워드 (분야: {trend_field})")
        elif topic_source == "manual" and not topic_trimmed:
            mode = "google_trends"
            log("직접 주제가 비어 있어 Google 트렌드 키워드 모드로 진행합니다.")
        else:
            mode = "news_rss"
            log(f"주제: 인기 뉴스 RSS (분야 필터: {trend_field})")

        log("GPT로 블로그 글을 생성 중입니다...")
        title, content = generate_hot_issue_post(
            client,
            topic=topic_trimmed if mode == "manual" else None,
            topic_mode=mode,
            trend_field=trend_field,
            system_override=sys_ov,
            user_suffix_override=suf_ov,
        )
        log(f"제목: {title}")
        log(f"본문 일부: {content[:200]}...")
        driver = create_driver()
        try:
            if cfg.manual_login:
                log("수동 로그인 모드: 브라우저에서 로그인 후 '로그인 완료' 버튼을 누르세요.")
            else:
                log("네이버에 로그인 중입니다...")
            naver_login(driver, cfg, manual=cfg.manual_login, manual_login_event=manual_login_event)
            blog_id = cfg.naver_id
            log("블로그 글쓰기 페이지를 여는 중입니다...")
            open_blog_write_page(driver, blog_id=blog_id)
            action_label = "발행" if cfg.blog_action == "publish" else "저장"
            log(f"제목/본문 입력 후 {action_label} 시도 중...")
            fill_post_and_publish(driver, title=title, content=content, action=cfg.blog_action)
            log("작업이 완료되었습니다.")
            # 같은 스레드에서 바로 브라우저 종료 → GUI로 드라이버 넘기지 않음 (마지막 Chrome 오류 방지)
            try:
                driver.quit()
            except Exception:
                pass
            return (None, None)
        except Exception as e:
            try:
                driver.quit()
            except Exception:
                pass
            raise
    except Exception as e:
        return (None, _sanitize_error_for_gui(str(e)))


_PROFILES_META_FILENAME = "gui_profiles.json"
_DEFAULT_PROFILE = "default"


def _config_dir() -> Path:
    return Path(__file__).resolve().parent


def _sanitize_profile_id(name: str) -> str:
    """프로필 이름을 파일명에 쓸 수 있게 정리."""
    s = (name or "").strip()
    if not s:
        return _DEFAULT_PROFILE
    for c in r'/\:*?"<>|':
        s = s.replace(c, "_")
    return s or _DEFAULT_PROFILE


def _gui_config_path(profile: str | None = None) -> Path:
    """프로필별 설정 파일 경로. profile=None이면 기본(legacy) 경로."""
    base = _config_dir()
    if profile is None or profile == _DEFAULT_PROFILE:
        return base / "gui_config.json"
    return base / f"gui_config_{_sanitize_profile_id(profile)}.json"


def _profiles_meta_path() -> Path:
    return _config_dir() / _PROFILES_META_FILENAME


def _ensure_profiles_meta() -> dict:
    """프로필 메타 초기화. 기존 gui_config.json이 있으면 default로 등록."""
    meta_path = _profiles_meta_path()
    legacy_path = _config_dir() / "gui_config.json"
    if meta_path.exists():
        try:
            with open(meta_path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    profiles = [_DEFAULT_PROFILE]
    if legacy_path.exists():
        profiles = [_DEFAULT_PROFILE]
    meta = {"current": _DEFAULT_PROFILE, "profiles": profiles}
    try:
        with open(meta_path, "w", encoding="utf-8") as f:
            json.dump(meta, f, ensure_ascii=False, indent=2)
    except Exception:
        pass
    return meta


def load_profiles_meta() -> dict:
    meta = _ensure_profiles_meta()
    if "profiles" not in meta or not meta["profiles"]:
        meta["profiles"] = [_DEFAULT_PROFILE]
    if meta.get("current") not in meta["profiles"]:
        meta["current"] = meta["profiles"][0]
    return meta


def save_profiles_meta(meta: dict) -> None:
    with open(_profiles_meta_path(), "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)


def add_profile(name: str) -> str:
    """새 프로필 추가. 반환: 실제 사용할 프로필 ID."""
    name = (name or "").strip()
    if not name:
        return _DEFAULT_PROFILE
    pid = _sanitize_profile_id(name)
    if pid == _DEFAULT_PROFILE:
        return _DEFAULT_PROFILE
    meta = load_profiles_meta()
    if pid not in meta["profiles"]:
        meta["profiles"].append(pid)
        save_profiles_meta(meta)
        path = _gui_config_path(pid)
        if not path.exists():
            default_data = {}
            try:
                legacy = _config_dir() / "gui_config.json"
                if legacy.exists():
                    with open(legacy, "r", encoding="utf-8") as f:
                        default_data = json.load(f)
            except Exception:
                pass
            default_data["naver_id"] = ""
            default_data["naver_pw"] = ""
            default_data["topic"] = ""
            try:
                with open(path, "w", encoding="utf-8") as f:
                    json.dump(default_data, f, ensure_ascii=False, indent=2)
            except Exception:
                pass
    return pid


def delete_profile(profile_id: str) -> bool:
    """프로필 삭제. default는 삭제 불가."""
    if not profile_id or profile_id == _DEFAULT_PROFILE:
        return False
    meta = load_profiles_meta()
    if profile_id not in meta["profiles"]:
        return False
    meta["profiles"] = [p for p in meta["profiles"] if p != profile_id]
    if meta["current"] == profile_id:
        meta["current"] = meta["profiles"][0] if meta["profiles"] else _DEFAULT_PROFILE
    save_profiles_meta(meta)
    path = _gui_config_path(profile_id)
    if path.exists():
        try:
            path.unlink()
        except Exception:
            pass
    return True


def load_gui_config(profile: str | None = None) -> dict:
    """프로필별 설정 로드. profile=None이면 현재 프로필(메타 기준)."""
    if profile is None:
        meta = load_profiles_meta()
        profile = meta.get("current") or _DEFAULT_PROFILE
    path = _gui_config_path(profile)
    if path.exists():
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def save_gui_config(data: dict, profile: str | None = None) -> None:
    """프로필별 설정 저장. profile=None이면 현재 프로필."""
    if profile is None:
        meta = load_profiles_meta()
        profile = meta.get("current") or _DEFAULT_PROFILE
    path = _gui_config_path(profile)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def main_gui() -> None:
    import sys
    import time
    import tkinter as tk
    from tkinter import scrolledtext, messagebox, simpledialog

    APP_VER = "v1.0.1"
    # === 상용툴급 프리미엄 테마 (Indigo + Emerald) ===
    BG_MAIN = "#F1F5F9"           # 차분한 라이트 그레이
    BG_CARD = "#FFFFFF"           # 순백 카드
    BG_HEADER = "#1E1B4B"        # 딥 인디고 헤더 (프리미엄)
    BG_FOOTER = "#1E1B4B"        # 딥 인디고 푸터
    TEXT_MAIN = "#0F172A"        # 진한 텍스트 (가독성↑)
    TEXT_MUTED = "#64748B"       # 보조 텍스트
    BORDER = "#E2E8F0"
    BORDER_ACCENT = "#A5B4FC"    # 인디고 톤 보더
    ACCENT = "#6366F1"           # 인디고 500 (가시성·프리미엄)
    ACCENT_DARK = "#4F46E5"      # 인디고 600 (호버)
    GREEN_BTN = "#10B981"        # 에메랄드 (성공/시작)
    GREEN_DARK = "#059669"
    ORANGE_BTN = "#F59E0B"       # 앰버 (경고/중지)
    RED_BTN = "#EF4444"          # 레드 (위험/중지)
    SLATE_BTN = "#475569"        # 슬레이트 (보조 버튼)
    INPUT_BG = "#F8FAFC"         # 입력 필드 배경
    LOG_BG = "#0F172A"           # 로그 다크 배경
    LOG_FG = "#E2E8F0"           # 로그 밝은 텍스트

    root = tk.Tk()
    root.title(f"네이버 블로그 자동 글쓰기 {APP_VER}")
    root.withdraw()
    root.geometry("1360x900")
    root.minsize(1100, 720)
    root.configure(bg=BG_MAIN)

    _icon_path = Path(__file__).resolve().parent / "assets" / "app_icon.ico"
    if _icon_path.is_file():
        try:
            root.iconbitmap(str(_icon_path))
        except Exception:
            pass

    # ttkbootstrap 제거 — 커스텀 테마 적용을 위해 기본 ttk 사용
    spinbox_boot = None
    try:
        from tkinter import ttk as _ttk

        _st = _ttk.Style()
        try:
            _st.theme_use("clam")
        except Exception:
            pass
    except Exception:
        pass
    root.configure(bg=BG_MAIN)  # ttk 이후에도 배경색 유지

    splash_closed = False
    splash_start = time.perf_counter()
    splash = tk.Toplevel(root)
    splash.overrideredirect(True)
    splash.configure(bg=BG_HEADER)
    try:
        splash.attributes("-topmost", True)
    except Exception:
        pass
    _sw, _sh = splash.winfo_screenwidth(), splash.winfo_screenheight()
    _sw_w, _sw_h = 520, 360
    splash.geometry(f"{_sw_w}x{_sw_h}+{(_sw - _sw_w) // 2}+{(_sh - _sw_h) // 2}")
    _sp = tk.Frame(splash, bg=BG_HEADER)
    _sp.pack(expand=True, fill=tk.BOTH)
    tk.Frame(_sp, height=4, bg=ACCENT).pack(fill=tk.X)
    tk.Label(_sp, text="◆", font=("Malgun Gothic", 36), fg="#A5B4FC", bg=BG_HEADER).pack(pady=(40, 8))
    tk.Label(
        _sp,
        text="네이버 블로그 자동 글쓰기",
        font=("Malgun Gothic", 22, "bold"),
        fg="#F8FAFC",
        bg=BG_HEADER,
    ).pack()
    tk.Label(_sp, text=APP_VER, font=("Malgun Gothic", 11), fg="#A5B4FC", bg=BG_HEADER).pack(pady=(8, 2))
    tk.Label(_sp, text="GPT 생성 · 예약 업로드 · RSS 자동 감시", font=("Malgun Gothic", 11), fg="#A5B4FC", bg=BG_HEADER).pack()
    tk.Label(
        _sp,
        text="준비 중…  ·  클릭 또는 Esc 로 바로 시작",
        font=("Malgun Gothic", 10),
        fg="#64748B",
        bg=BG_HEADER,
    ).pack(pady=(40, 50))
    splash.update_idletasks()

    def _close_splash() -> None:
        nonlocal splash_closed
        if splash_closed:
            return
        splash_closed = True
        try:
            splash.destroy()
        except Exception:
            pass
        root.deiconify()
        try:
            root.attributes("-topmost", True)
            root.after(250, lambda: root.attributes("-topmost", False))
        except Exception:
            pass
        root.lift()
        try:
            root.focus_force()
        except Exception:
            pass

    splash.bind("<Button-1>", lambda _e: _close_splash())
    splash.bind("<Escape>", lambda _e: _close_splash())
    splash.focus_set()

    _meta = load_profiles_meta()
    var_current_profile = tk.StringVar(value=_meta.get("current") or _DEFAULT_PROFILE)
    var_naver_id = tk.StringVar()
    var_naver_pw = tk.StringVar()
    var_api_key = tk.StringVar()
    var_manual = tk.BooleanVar(value=False)
    var_action = tk.StringVar(value="save")
    var_topic = tk.StringVar()
    cfg = load_gui_config(var_current_profile.get())
    _ts = (cfg.get("topic_source") or "google_trends").strip().lower()
    if _ts not in ("manual", "google_trends", "news_rss", "trend_1", "news_1"):
        _ts = "google_trends"
    var_topic_source = tk.StringVar(value=_ts)
    _tf = cfg.get("trend_field") or "전체"
    var_trend_field = tk.StringVar(value=_tf if _tf in TREND_FIELD_OPTIONS else "전체")
    var_save_login = tk.BooleanVar(value=True)
    var_save_api = tk.BooleanVar(value=False)
    var_start_date = tk.StringVar()
    var_end_date = tk.StringVar()
    var_time_start = tk.StringVar(value="09:00")
    var_time_end = tk.StringVar(value="21:00")
    var_runs_per_day = tk.StringVar(value="2")
    # RSS 관련 GUI 상태
    var_rss_url_input = tk.StringVar()
    var_rss_check_interval = tk.StringVar(value="3")  # 시간 단위
    var_rss_auto_enabled = tk.BooleanVar(value=False)
    var_rss_include_source_link = tk.BooleanVar(value=True)
    var_rss_image_memo_only = tk.BooleanVar(value=True)
    var_rss_publish_mode = tk.StringVar(value="save")  # save / publish / queue
    var_rss_last_checked = tk.StringVar(value="-")

    schedule_cancel_event: threading.Event | None = None
    rss_monitor_stop_event: threading.Event | None = None
    rss_monitor_thread: threading.Thread | None = None
    trend_auto_stop_event: threading.Event | None = None
    trend_auto_thread: threading.Thread | None = None
    rss_state_lock = threading.Lock()
    rss_one_shot_lock = threading.Lock()

    var_naver_id.set(cfg.get("naver_id", ""))
    var_naver_pw.set(cfg.get("naver_pw", ""))
    var_api_key.set(cfg.get("api_key", ""))
    var_manual.set(cfg.get("manual_login", False))
    var_action.set("publish" if cfg.get("blog_action") == "publish" else "save")
    var_topic.set(cfg.get("topic", ""))
    var_save_login.set(cfg.get("save_login", True))
    var_save_api.set(cfg.get("save_api_key", False))
    _today = datetime.now().date().isoformat()
    _end_default = (datetime.now().date() + timedelta(days=30)).isoformat()
    var_start_date.set(cfg.get("schedule_start") or _today)
    var_end_date.set(cfg.get("schedule_end") or _end_default)
    var_time_start.set(cfg.get("schedule_time_start", "09:00"))
    var_time_end.set(cfg.get("schedule_time_end", "21:00"))
    var_runs_per_day.set(str(cfg.get("schedule_runs_per_day", 2)))
    var_rss_check_interval.set(str(cfg.get("rss_interval_hours", 3)))
    var_rss_auto_enabled.set(cfg.get("rss_auto_enabled", False))
    var_rss_include_source_link.set(cfg.get("rss_include_source_link", False))
    var_rss_image_memo_only.set(cfg.get("rss_image_memo_only", True))
    var_rss_publish_mode.set(cfg.get("rss_publish_mode", "save"))

    current_driver: webdriver.Chrome | None = None
    manual_login_event: threading.Event | None = None

    top_frame = tk.Frame(root, bg=BG_HEADER, pady=18, padx=24)
    top_frame.pack(fill=tk.X)
    tk.Frame(top_frame, height=3, bg=ACCENT).pack(fill=tk.X, pady=(0, 14))
    tk.Label(
        top_frame,
        text=f"네이버 블로그 자동 글쓰기  {APP_VER}",
        fg="#F8FAFC",
        bg=BG_HEADER,
        font=("Malgun Gothic", 22, "bold"),
    ).pack(anchor=tk.W)
    tk.Label(
        top_frame,
        text="GPT로 글을 쓰고 네이버 블로그에 저장·발행 · 예약 업로드 · RSS 자동 감시",
        fg="#A5B4FC",
        bg=BG_HEADER,
        font=("Malgun Gothic", 11),
    ).pack(anchor=tk.W, pady=(8, 0))

    btn_frame = tk.Frame(root, bg=BG_FOOTER, pady=18, padx=24)
    btn_frame.pack(side=tk.BOTTOM, fill=tk.X)
    tk.Frame(btn_frame, height=1, bg="#4338CA").pack(fill=tk.X, side=tk.TOP)

    paned = tk.PanedWindow(root, orient=tk.HORIZONTAL, bg=BG_MAIN, sashwidth=10, sashrelief=tk.FLAT)
    paned.pack(fill=tk.BOTH, expand=True, padx=16, pady=(14, 0))
    left_inner = tk.Frame(paned, bg=BG_MAIN)
    paned.add(left_inner, minsize=280, width=340)

    left_vp = tk.PanedWindow(left_inner, orient=tk.VERTICAL, bg=BG_MAIN, sashwidth=6)
    left_vp.pack(fill=tk.BOTH, expand=True)
    left_top = tk.Frame(left_vp, bg=BG_MAIN)
    left_vp.add(left_top, minsize=400)
    left_bot = tk.Frame(left_vp, bg=BG_MAIN)
    left_vp.add(left_bot, minsize=300)

    def section(parent: tk.Widget, title: str) -> tk.LabelFrame:
        return tk.LabelFrame(
            parent,
            text=f"  {title}  ",
            fg=ACCENT,
            bg=BG_CARD,
            font=("Malgun Gothic", 10, "bold"),
            padx=16,
            pady=14,
            relief=tk.FLAT,
            highlightthickness=2,
            highlightbackground=BORDER,
            highlightcolor=BORDER_ACCENT,
        )

    _entry_kw = dict(font=("Malgun Gothic", 10), relief=tk.FLAT, highlightthickness=2, highlightbackground=BORDER, highlightcolor=ACCENT, bg=INPUT_BG, insertbackground=TEXT_MAIN)
    chk_kw = dict(bg=BG_CARD, font=("Malgun Gothic", 9), activebackground=BG_CARD, selectcolor="#C7D2FE", fg=TEXT_MAIN, activeforeground=TEXT_MAIN)

    def _get_profile() -> str:
        return var_current_profile.get() or _DEFAULT_PROFILE

    def _apply_profile_to_form(profile: str) -> None:
        c = load_gui_config(profile)
        var_naver_id.set(c.get("naver_id", ""))
        var_naver_pw.set(c.get("naver_pw", ""))
        var_api_key.set(c.get("api_key", ""))
        var_manual.set(c.get("manual_login", False))
        var_action.set("publish" if c.get("blog_action") == "publish" else "save")
        var_topic.set(c.get("topic", ""))
        _ts = (c.get("topic_source") or "google_trends").strip().lower()
        if _ts not in ("manual", "google_trends", "news_rss", "trend_1", "news_1"):
            _ts = "google_trends"
        var_topic_source.set(_ts)
        _tf = c.get("trend_field") or "전체"
        var_trend_field.set(_tf if _tf in TREND_FIELD_OPTIONS else "전체")
        var_save_login.set(c.get("save_login", True))
        var_save_api.set(c.get("save_api_key", False))
        _today = datetime.now().date().isoformat()
        _end_default = (datetime.now().date() + timedelta(days=30)).isoformat()
        var_start_date.set(c.get("schedule_start") or _today)
        var_end_date.set(c.get("schedule_end") or _end_default)
        var_time_start.set(c.get("schedule_time_start", "09:00"))
        var_time_end.set(c.get("schedule_time_end", "21:00"))
        var_runs_per_day.set(str(c.get("schedule_runs_per_day", 2)))
        var_rss_check_interval.set(str(c.get("rss_interval_hours", 3)))
        var_rss_auto_enabled.set(c.get("rss_auto_enabled", False))
        var_rss_include_source_link.set(c.get("rss_include_source_link", False))
        var_rss_image_memo_only.set(c.get("rss_image_memo_only", True))
        var_rss_publish_mode.set(c.get("rss_publish_mode", "save"))
        rss_urls = c.get("rss_urls", [])
        rss_urls_listbox.delete(0, tk.END)
        for u in rss_urls:
            if isinstance(u, str) and u.strip():
                rss_urls_listbox.insert(tk.END, u)
        try:
            _load_prompt_widgets_from_config()
        except Exception:
            pass

    def _save_form_to_profile(profile: str) -> None:
        data = load_gui_config(profile)
        data["naver_id"] = var_naver_id.get().strip()
        data["naver_pw"] = var_naver_pw.get()
        data["manual_login"] = var_manual.get()
        data["blog_action"] = var_action.get()
        data["topic"] = var_topic.get().strip()
        data["topic_source"] = var_topic_source.get()
        data["trend_field"] = var_trend_field.get()
        data["save_login"] = var_save_login.get()
        data["save_api_key"] = var_save_api.get()
        data["api_key"] = var_api_key.get().strip() if var_save_api.get() else data.get("api_key", "")
        data["rss_urls"] = [rss_urls_listbox.get(i) for i in range(rss_urls_listbox.size())]
        data["rss_interval_hours"] = int(var_rss_check_interval.get().strip() or "3")
        data["rss_auto_enabled"] = bool(var_rss_auto_enabled.get())
        data["rss_include_source_link"] = bool(var_rss_include_source_link.get())
        data["rss_image_memo_only"] = bool(var_rss_image_memo_only.get())
        data["rss_publish_mode"] = var_rss_publish_mode.get()
        data["prompt_hot_system"] = txt_prompt_hot_system.get("1.0", tk.END).rstrip("\n")
        data["prompt_hot_user_suffix"] = txt_prompt_hot_user.get("1.0", tk.END).rstrip("\n")
        data["prompt_rss_system"] = txt_prompt_rss_system.get("1.0", tk.END).rstrip("\n")
        data["prompt_rss_user"] = txt_prompt_rss_user.get("1.0", tk.END).rstrip("\n")
        save_gui_config(data, profile)

    def _refresh_profile_combo() -> None:
        meta = load_profiles_meta()
        profile_combo["values"] = meta.get("profiles", [_DEFAULT_PROFILE])
        var_current_profile.set(meta.get("current") or _DEFAULT_PROFILE)

    def _on_profile_change(*args: object) -> None:
        new_prof = var_current_profile.get()
        if not new_prof:
            return
        meta = load_profiles_meta()
        if new_prof not in meta.get("profiles", []):
            return
        old_prof = meta.get("current") or _DEFAULT_PROFILE
        if old_prof == new_prof:
            return
        _save_form_to_profile(old_prof)
        meta["current"] = new_prof
        save_profiles_meta(meta)
        _apply_profile_to_form(new_prof)

    def _on_add_profile() -> None:
        name = simpledialog.askstring("프로필 추가", "프로필 이름 (예: 계정2, 계정3):", parent=root)
        if not name or not name.strip():
            return
        _save_form_to_profile(_get_profile())
        pid = add_profile(name.strip())
        _refresh_profile_combo()
        var_current_profile.set(pid)
        meta = load_profiles_meta()
        meta["current"] = pid
        save_profiles_meta(meta)
        _apply_profile_to_form(pid)
        log_msg(f"프로필 '{pid}' 추가됨.")

    def _on_delete_profile() -> None:
        prof = _get_profile()
        if prof == _DEFAULT_PROFILE:
            messagebox.showinfo("프로필", "기본 프로필은 삭제할 수 없습니다.")
            return
        if not messagebox.askyesno("프로필 삭제", f"프로필 '{prof}'를 삭제하시겠습니까?"):
            return
        _save_form_to_profile(prof)
        delete_profile(prof)
        _refresh_profile_combo()
        _apply_profile_to_form(_get_profile())
        log_msg(f"프로필 '{prof}' 삭제됨.")

    var_current_profile.trace_add("write", _on_profile_change)

    api_f = section(left_top, "OpenAI API")
    api_f.pack(fill=tk.X, pady=(0, 10))
    tk.Label(api_f, text="API 키", bg=BG_CARD, fg=TEXT_MUTED, font=("Malgun Gothic", 9)).pack(anchor=tk.W)
    tk.Entry(api_f, textvariable=var_api_key, show="*", **_entry_kw).pack(fill=tk.X, pady=(4, 8), ipady=8)

    def open_api() -> None:
        import webbrowser
        webbrowser.open("https://platform.openai.com/api-keys")

    tk.Button(api_f, text="키 발급 페이지 열기", fg="white", bg=ACCENT, activebackground=ACCENT_DARK, activeforeground="white", font=("Malgun Gothic", 9, "bold"), relief=tk.FLAT, padx=14, pady=8, cursor="hand2", command=open_api).pack(anchor=tk.W)

    login_f = section(left_top, "네이버 로그인")
    login_f.pack(fill=tk.X, pady=(0, 10))
    prof_row = tk.Frame(login_f, bg=BG_CARD)
    prof_row.pack(fill=tk.X, pady=(0, 6))
    tk.Label(prof_row, text="계정", bg=BG_CARD, fg=TEXT_MUTED, font=("Malgun Gothic", 9)).pack(side=tk.LEFT, padx=(0, 6))
    profile_combo = tk.ttk.Combobox(prof_row, textvariable=var_current_profile, values=_meta.get("profiles", [_DEFAULT_PROFILE]), state="readonly", width=10, font=("Malgun Gothic", 9))
    profile_combo.pack(side=tk.LEFT, padx=(0, 4))
    tk.Button(prof_row, text="+", fg="white", bg=ACCENT, activebackground=ACCENT_DARK, font=("Malgun Gothic", 8), relief=tk.FLAT, padx=6, pady=2, cursor="hand2", command=_on_add_profile).pack(side=tk.LEFT, padx=(0, 2))
    tk.Button(prof_row, text="−", fg="white", bg=SLATE_BTN, activebackground="#334155", font=("Malgun Gothic", 8), relief=tk.FLAT, padx=6, pady=2, cursor="hand2", command=_on_delete_profile).pack(side=tk.LEFT)
    tk.Label(login_f, text="아이디", bg=BG_CARD, fg=TEXT_MUTED, font=("Malgun Gothic", 9)).pack(anchor=tk.W)
    tk.Entry(login_f, textvariable=var_naver_id, **_entry_kw).pack(fill=tk.X, pady=(4, 8), ipady=8)
    tk.Label(login_f, text="비밀번호", bg=BG_CARD, fg=TEXT_MUTED, font=("Malgun Gothic", 9)).pack(anchor=tk.W)
    tk.Entry(login_f, textvariable=var_naver_pw, show="*", **_entry_kw).pack(fill=tk.X, pady=(4, 8), ipady=8)

    save_f = section(left_top, "설정 저장")
    save_f.pack(fill=tk.X, pady=(0, 10))
    tk.Checkbutton(save_f, text="다음 실행 시 로그인 정보 자동 불러오기", variable=var_save_login, **chk_kw).pack(anchor=tk.W)
    tk.Checkbutton(save_f, text="API 키도 파일에 저장", variable=var_save_api, **chk_kw).pack(anchor=tk.W, pady=(4, 0))

    write_f = section(left_top, "글쓰기 · 주제")
    write_f.pack(fill=tk.X, pady=(0, 10))
    tk.Label(
        write_f,
        text="주제 정하는 방식",
        bg=BG_CARD,
        fg=TEXT_MAIN,
        font=("Malgun Gothic", 9, "bold"),
    ).pack(anchor=tk.W, pady=(0, 4))
    ts_row = tk.Frame(write_f, bg=BG_CARD)
    ts_row.pack(anchor=tk.W, fill=tk.X)
    for val, lab in (
        ("manual", "직접 입력"),
        ("google_trends", "Google 실검"),
        ("news_rss", "인기 뉴스"),
        ("trend_1", "실검 1위"),
        ("news_1", "인기뉴스 1위"),
    ):
        tk.Radiobutton(
            ts_row,
            text=lab,
            variable=var_topic_source,
            value=val,
            bg=BG_CARD,
            font=("Malgun Gothic", 8),
            activebackground=BG_CARD,
            fg=TEXT_MAIN,
        ).pack(side=tk.LEFT, padx=(0, 10))
    tk.Label(
        write_f,
        text="분야 (실검·뉴스 모두 GPT가 이에 맞는 키워드/기사를 고름)",
        bg=BG_CARD,
        fg=TEXT_MUTED,
        font=("Malgun Gothic", 8),
        wraplength=320,
        justify=tk.LEFT,
    ).pack(anchor=tk.W, pady=(6, 2))
    _om_tf = tk.OptionMenu(write_f, var_trend_field, *TREND_FIELD_OPTIONS)
    _om_tf.config(font=("Malgun Gothic", 9), bg=BG_CARD, highlightthickness=0)
    _om_tf.pack(anchor=tk.W, pady=(0, 6))
    tk.Label(write_f, text="직접 주제 (위에서 '직접 입력' 선택 시 필수)", bg=BG_CARD, fg=TEXT_MUTED, font=("Malgun Gothic", 9)).pack(anchor=tk.W)
    tk.Entry(write_f, textvariable=var_topic, **_entry_kw).pack(fill=tk.X, pady=(4, 8), ipady=8)
    tk.Checkbutton(write_f, text="수동 로그인 (캡차·2단계 인증 시) — 브라우저에서 직접 로그인 후 하단 초록 버튼", variable=var_manual, **chk_kw).pack(anchor=tk.W, pady=(0, 8))
    tk.Label(write_f, text="작성 후", bg=BG_CARD, fg=TEXT_MUTED, font=("Malgun Gothic", 9)).pack(anchor=tk.W)
    act_inner = tk.Frame(write_f, bg=BG_CARD)
    act_inner.pack(anchor=tk.W, pady=(4, 0))
    tk.Radiobutton(act_inner, text="임시저장", variable=var_action, value="save", bg=BG_CARD, font=("Malgun Gothic", 9), activebackground=BG_CARD, fg=TEXT_MAIN).pack(side=tk.LEFT, padx=(0, 20))
    tk.Radiobutton(act_inner, text="바로 발행", variable=var_action, value="publish", bg=BG_CARD, font=("Malgun Gothic", 9), activebackground=BG_CARD, fg=TEXT_MAIN).pack(side=tk.LEFT)

    sched_f = section(left_bot, "예약 업로드")
    sched_f.pack(fill=tk.BOTH, expand=True, pady=(0, 0))
    tk.Label(
        sched_f,
        text="기간 안에서 매일 지정 횟수만큼, 시간대 안에서 무작위 시각에 글이 올라갑니다. 아래 [예약 시작]으로 실행합니다.",
        bg=BG_CARD,
        fg=TEXT_MUTED,
        font=("Malgun Gothic", 9),
        wraplength=300,
        justify=tk.LEFT,
    ).pack(anchor=tk.W, pady=(0, 10))
    tk.Label(sched_f, text="기간 (YYYY-MM-DD)", bg=BG_CARD, fg=TEXT_MAIN, font=("Malgun Gothic", 9, "bold")).pack(anchor=tk.W)
    sched_date_f = tk.Frame(sched_f, bg=BG_CARD)
    sched_date_f.pack(fill=tk.X, pady=(6, 12))
    tk.Entry(sched_date_f, textvariable=var_start_date, width=11, **_entry_kw).pack(side=tk.LEFT, padx=(0, 6), ipady=6)
    tk.Label(sched_date_f, text="~", bg=BG_CARD, fg=TEXT_MUTED).pack(side=tk.LEFT, padx=4)
    tk.Entry(sched_date_f, textvariable=var_end_date, width=11, **_entry_kw).pack(side=tk.LEFT, ipady=6)
    tk.Label(sched_f, text="업로드 시간대 (HH:MM)", bg=BG_CARD, fg=TEXT_MAIN, font=("Malgun Gothic", 9, "bold")).pack(anchor=tk.W)
    sched_time_f = tk.Frame(sched_f, bg=BG_CARD)
    sched_time_f.pack(fill=tk.X, pady=(6, 12))
    tk.Entry(sched_time_f, textvariable=var_time_start, width=7, **_entry_kw).pack(side=tk.LEFT, padx=(0, 6), ipady=6)
    tk.Label(sched_time_f, text="~", bg=BG_CARD, fg=TEXT_MUTED).pack(side=tk.LEFT, padx=4)
    tk.Entry(sched_time_f, textvariable=var_time_end, width=7, **_entry_kw).pack(side=tk.LEFT, ipady=6)
    tk.Label(sched_f, text="하루 업로드 횟수", bg=BG_CARD, fg=TEXT_MAIN, font=("Malgun Gothic", 9, "bold")).pack(anchor=tk.W)
    if spinbox_boot:
        spinbox_boot(sched_f, textvariable=var_runs_per_day, from_=1, to=10, width=5, bootstyle="primary").pack(anchor=tk.W, pady=(8, 0))
    else:
        tk.Spinbox(
            sched_f,
            textvariable=var_runs_per_day,
            from_=1,
            to=10,
            width=6,
            font=("Malgun Gothic", 10),
            relief=tk.FLAT,
            highlightthickness=2,
            highlightbackground=BORDER,
            highlightcolor=ACCENT,
            bg=INPUT_BG,
        ).pack(anchor=tk.W, pady=(8, 0), ipady=4)

    # ——— 가운데: 위=프롬프트 · 아래=실행 로그 ———
    center_col = tk.Frame(paned, bg=BG_MAIN)
    paned.add(center_col, minsize=380)
    center_paned = tk.PanedWindow(center_col, orient=tk.VERTICAL, bg=BG_MAIN, sashwidth=6)
    center_paned.pack(fill=tk.BOTH, expand=True, padx=2, pady=2)

    hot_prompt_outer = tk.Frame(center_paned, bg=BG_MAIN)
    center_paned.add(hot_prompt_outer, minsize=300)
    prompt_card = tk.Frame(hot_prompt_outer, bg=BG_CARD, highlightthickness=2, highlightbackground=BORDER)
    prompt_card.pack(fill=tk.BOTH, expand=True)
    tk.Label(
        prompt_card,
        text="핫이슈 · 주제 글 프롬프트",
        fg=TEXT_MAIN,
        bg=BG_CARD,
        font=("Malgun Gothic", 12, "bold"),
    ).pack(anchor=tk.W, padx=14, pady=(14, 6))
    tk.Label(prompt_card, text="시스템 역할", bg=BG_CARD, fg=TEXT_MUTED, font=("Malgun Gothic", 9)).pack(anchor=tk.W, padx=12)
    txt_prompt_hot_system = scrolledtext.ScrolledText(prompt_card, height=5, wrap=tk.WORD, font=("Malgun Gothic", 9), bg=INPUT_BG, relief=tk.FLAT, padx=10, pady=10)
    txt_prompt_hot_system.pack(fill=tk.BOTH, expand=True, padx=12, pady=(4, 8))
    tk.Label(
        prompt_card,
        text="유저 지시 (날짜·주제 뒤에 붙음) · 반드시 ===TITLE=== / ===CONTENT=== 형식 유지",
        bg=BG_CARD,
        fg=TEXT_MUTED,
        font=("Malgun Gothic", 9),
        wraplength=520,
        justify=tk.LEFT,
    ).pack(anchor=tk.W, padx=12)
    txt_prompt_hot_user = scrolledtext.ScrolledText(prompt_card, height=5, wrap=tk.WORD, font=("Malgun Gothic", 9), bg=INPUT_BG, relief=tk.FLAT, padx=10, pady=10)
    txt_prompt_hot_user.pack(fill=tk.BOTH, expand=True, padx=12, pady=(4, 8))

    prompt_btn_f = tk.Frame(prompt_card, bg=BG_CARD)
    prompt_btn_f.pack(fill=tk.X, padx=12, pady=(0, 12))

    log_outer = tk.Frame(center_paned, bg=BG_MAIN)
    center_paned.add(log_outer, minsize=140)
    log_card = tk.Frame(log_outer, bg=BG_CARD, highlightthickness=2, highlightbackground=BORDER)
    log_card.pack(fill=tk.BOTH, expand=True)
    tk.Label(log_card, text="실행 로그", fg=TEXT_MAIN, bg=BG_CARD, font=("Malgun Gothic", 12, "bold")).pack(anchor=tk.W, padx=14, pady=(12, 8))
    log_text = scrolledtext.ScrolledText(
        log_card, height=12, state=tk.DISABLED, wrap=tk.WORD, font=("Consolas", 10), bg=LOG_BG, fg=LOG_FG, insertbackground="white", relief=tk.FLAT, padx=12, pady=12
    )
    log_text.pack(fill=tk.BOTH, expand=True, padx=12, pady=(0, 12))

    # ——— 오른쪽: RSS 설정 + RSS 프롬프트 ———
    right_rss_col = tk.Frame(paned, bg=BG_MAIN)
    paned.add(right_rss_col, minsize=360, width=480)
    right_rss_paned = tk.PanedWindow(right_rss_col, orient=tk.VERTICAL, bg=BG_MAIN, sashwidth=6)
    right_rss_paned.pack(fill=tk.BOTH, expand=True, padx=2, pady=2)

    rss_f = section(right_rss_paned, "RSS 자동 감시")
    right_rss_paned.add(rss_f, minsize=240)

    tk.Label(rss_f, text="피드 URL 붙여넣기 후 추가", bg=BG_CARD, fg=TEXT_MUTED, font=("Malgun Gothic", 9)).pack(anchor=tk.W)
    rss_input_f = tk.Frame(rss_f, bg=BG_CARD)
    rss_input_f.pack(fill=tk.X, pady=(6, 6))
    tk.Entry(rss_input_f, textvariable=var_rss_url_input, **_entry_kw).pack(side=tk.LEFT, fill=tk.X, expand=True, ipady=6)

    rss_urls_listbox = tk.Listbox(rss_f, height=6, font=("Malgun Gothic", 9), bg=INPUT_BG, relief=tk.FLAT, highlightthickness=2, highlightbackground=BORDER, selectbackground=ACCENT, selectforeground="white")
    rss_urls_listbox.pack(fill=tk.BOTH, expand=True, pady=(0, 8))

    def _load_rss_urls_to_listbox() -> None:
        rss_urls = cfg.get("rss_urls", [])
        rss_urls_listbox.delete(0, tk.END)
        for u in rss_urls:
            rss_urls_listbox.insert(tk.END, u)

    _load_rss_urls_to_listbox()

    def add_rss_url() -> None:
        url = var_rss_url_input.get().strip()
        if not url:
            return
        data = load_gui_config(_get_profile())
        urls = data.get("rss_urls", [])
        if url in urls:
            messagebox.showinfo("중복 URL", "이미 등록된 RSS URL 입니다.")
            return
        urls.append(url)
        data["rss_urls"] = urls
        save_gui_config(data, _get_profile())
        rss_urls_listbox.insert(tk.END, url)
        var_rss_url_input.set("")

    def remove_rss_url() -> None:
        sel = rss_urls_listbox.curselection()
        if not sel:
            return
        index = sel[0]
        url = rss_urls_listbox.get(index)
        data = load_gui_config(_get_profile())
        urls = data.get("rss_urls", [])
        urls = [u for u in urls if u != url]
        data["rss_urls"] = urls
        save_gui_config(data, _get_profile())
        rss_urls_listbox.delete(index)

    btn_rss_add = tk.Button(rss_input_f, text="추가", fg="white", bg=ACCENT, activebackground=ACCENT_DARK, font=("Malgun Gothic", 9), relief=tk.FLAT, padx=10, pady=4, cursor="hand2", command=add_rss_url)
    btn_rss_add.pack(side=tk.LEFT, padx=(8, 0))
    btn_rss_del = tk.Button(rss_input_f, text="삭제", fg="white", bg=RED_BTN, activebackground="#B91C1C", font=("Malgun Gothic", 9), relief=tk.FLAT, padx=10, pady=4, cursor="hand2", command=remove_rss_url)
    btn_rss_del.pack(side=tk.LEFT, padx=(6, 0))

    interval_f = tk.Frame(rss_f, bg=BG_CARD)
    interval_f.pack(fill=tk.X, pady=(4, 4))
    tk.Label(interval_f, text="확인 주기(시간)", bg=BG_CARD, fg=TEXT_MUTED, font=("Malgun Gothic", 9)).pack(side=tk.LEFT)
    if spinbox_boot:
        spinbox_boot(interval_f, textvariable=var_rss_check_interval, from_=1, to=24, width=4, bootstyle="primary").pack(side=tk.LEFT, padx=(8, 0))
    else:
        tk.Spinbox(
            interval_f,
            textvariable=var_rss_check_interval,
            from_=1,
            to=24,
            width=4,
            font=("Malgun Gothic", 10),
            relief=tk.FLAT,
            highlightthickness=2,
            highlightbackground=BORDER,
            bg=INPUT_BG,
        ).pack(side=tk.LEFT, padx=(8, 0), ipady=2)

    chk_rss = dict(bg=BG_CARD, font=("Malgun Gothic", 9), activebackground=BG_CARD, selectcolor="#C7D2FE", fg=TEXT_MAIN, activeforeground=TEXT_MAIN)
    tk.Checkbutton(rss_f, text="감시 옵션 저장(시작은 하단 파란 버튼)", variable=var_rss_auto_enabled, **chk_rss).pack(anchor=tk.W, pady=(4, 0))
    tk.Checkbutton(rss_f, text="본문 말미 출처 링크 한 줄", variable=var_rss_include_source_link, **chk_rss).pack(anchor=tk.W, pady=(2, 0))
    tk.Checkbutton(rss_f, text="이미지는 본문에 넣지 않고 로그만", variable=var_rss_image_memo_only, **chk_rss).pack(anchor=tk.W, pady=(2, 4))

    tk.Label(rss_f, text="새 글 발견 시", bg=BG_CARD, fg=TEXT_MAIN, font=("Malgun Gothic", 9, "bold")).pack(anchor=tk.W)
    for val, lab in (("save", "네이버 임시저장"), ("publish", "네이버 즉시 발행"), ("queue", "예약 큐에만 적재(JSON)")):
        tk.Radiobutton(rss_f, text=lab, variable=var_rss_publish_mode, value=val, bg=BG_CARD, font=("Malgun Gothic", 9), anchor=tk.W, activebackground=BG_CARD, fg=TEXT_MAIN).pack(anchor=tk.W)

    last_chk_f = tk.Frame(rss_f, bg=BG_CARD)
    last_chk_f.pack(fill=tk.X, pady=(8, 0))
    tk.Label(last_chk_f, text="마지막 확인", bg=BG_CARD, fg=TEXT_MUTED, font=("Malgun Gothic", 9)).pack(side=tk.LEFT)
    tk.Label(last_chk_f, textvariable=var_rss_last_checked, bg=BG_CARD, fg=ACCENT, font=("Malgun Gothic", 9, "bold")).pack(side=tk.LEFT, padx=(8, 0))

    rss_prompt_outer = tk.Frame(right_rss_paned, bg=BG_MAIN)
    right_rss_paned.add(rss_prompt_outer, minsize=240)
    rss_prompt_card = tk.Frame(rss_prompt_outer, bg=BG_CARD, highlightthickness=2, highlightbackground=BORDER)
    rss_prompt_card.pack(fill=tk.BOTH, expand=True)
    tk.Label(
        rss_prompt_card,
        text="RSS 전용 프롬프트",
        fg=TEXT_MAIN,
        bg=BG_CARD,
        font=("Malgun Gothic", 12, "bold"),
    ).pack(anchor=tk.W, padx=14, pady=(14, 6))
    tk.Label(rss_prompt_card, text="시스템", bg=BG_CARD, fg=TEXT_MUTED, font=("Malgun Gothic", 9)).pack(anchor=tk.W, padx=12)
    txt_prompt_rss_system = scrolledtext.ScrolledText(rss_prompt_card, height=4, wrap=tk.WORD, font=("Malgun Gothic", 9), bg=INPUT_BG, relief=tk.FLAT, padx=10, pady=10)
    txt_prompt_rss_system.pack(fill=tk.BOTH, expand=True, padx=12, pady=(4, 6))
    tk.Label(
        rss_prompt_card,
        text="유저 템플릿 변수: {source_blog_title} {source_post_title} {source_link} …",
        bg=BG_CARD,
        font=("Malgun Gothic", 8),
        wraplength=360,
        justify=tk.LEFT,
        fg=TEXT_MUTED,
    ).pack(anchor=tk.W, padx=12)
    txt_prompt_rss_user = scrolledtext.ScrolledText(rss_prompt_card, height=6, wrap=tk.WORD, font=("Malgun Gothic", 9), bg=INPUT_BG, relief=tk.FLAT, padx=10, pady=10)
    txt_prompt_rss_user.pack(fill=tk.BOTH, expand=True, padx=12, pady=(4, 8))

    # ——— 맨 오른쪽: 실시간 트렌드 & 뉴스 ———
    trends_col = tk.Frame(paned, bg=BG_MAIN)
    paned.add(trends_col, minsize=300, width=380)
    trends_f = section(trends_col, "실시간 트렌드 & 뉴스")
    trends_f.pack(fill=tk.BOTH, expand=True)
    var_trends_source = tk.StringVar(value="google_trends")
    tk.Label(trends_f, text="글 주제 참고용 — 클릭 후 아래 버튼으로 주제에 넣기", bg=BG_CARD, fg=TEXT_MUTED, font=("Malgun Gothic", 8), wraplength=280).pack(anchor=tk.W)
    ts_radio_f = tk.Frame(trends_f, bg=BG_CARD)
    ts_radio_f.pack(anchor=tk.W, pady=(6, 4))
    for val, lab in (("google_trends", "Google 실검"), ("hot_news", "인기 뉴스")):
        tk.Radiobutton(ts_radio_f, text=lab, variable=var_trends_source, value=val, bg=BG_CARD, font=("Malgun Gothic", 9), activebackground=BG_CARD, fg=TEXT_MAIN).pack(side=tk.LEFT, padx=(0, 12))
    var_trends_source.trace_add("write", lambda *_: _refresh_trends())
    trends_btn_f = tk.Frame(trends_f, bg=BG_CARD)
    trends_btn_f.pack(fill=tk.X, pady=(4, 6))
    trends_listbox = tk.Listbox(trends_f, height=14, font=("Malgun Gothic", 9), bg=INPUT_BG, relief=tk.FLAT, highlightthickness=2, highlightbackground=BORDER, selectmode=tk.SINGLE, selectbackground=ACCENT, selectforeground="white")
    trends_listbox.pack(fill=tk.BOTH, expand=True, pady=(0, 8))
    trends_full_items: list[str] = []

    def _refresh_trends() -> None:
        def _fetch() -> None:
            src = var_trends_source.get()
            try:
                if src == "google_trends":
                    items = fetch_google_trending_keywords_kr(40)
                else:
                    items = fetch_rss_headlines(30)
            except Exception:
                items = []
            def _update() -> None:
                nonlocal trends_full_items
                trends_full_items = items
                trends_listbox.delete(0, tk.END)
                for x in items:
                    trends_listbox.insert(tk.END, (x[:80] + "…") if len(x) > 80 else x)
                if not items:
                    trends_listbox.insert(tk.END, "(데이터 없음 — 새로고침 또는 네트워크 확인)")
            root.after(0, _update)
        threading.Thread(target=_fetch, daemon=True).start()

    def _use_trend_as_topic() -> None:
        sel = trends_listbox.curselection()
        if not sel:
            messagebox.showinfo("트렌드", "목록에서 항목을 선택한 뒤 버튼을 누르세요.")
            return
        idx = sel[0]
        if idx >= len(trends_full_items):
            return
        text = trends_full_items[idx]
        if not text or "(데이터 없음" in text:
            return
        var_topic.set(text)
        var_topic_source.set("manual")
        log_msg(f"주제에 반영: {text[:50]}...")

    tk.Button(trends_btn_f, text="새로고침", fg="white", bg=ACCENT, activebackground=ACCENT_DARK, font=("Malgun Gothic", 9), relief=tk.FLAT, padx=10, pady=4, cursor="hand2", command=_refresh_trends).pack(side=tk.LEFT, padx=(0, 6))
    tk.Button(trends_btn_f, text="이 주제로 글쓰기", fg="white", bg=GREEN_BTN, activebackground=GREEN_DARK, font=("Malgun Gothic", 9), relief=tk.FLAT, padx=10, pady=4, cursor="hand2", command=_use_trend_as_topic).pack(side=tk.LEFT)
    _refresh_trends()

    # ——— 트렌드 자동 글쓰기 (정해진/랜덤 간격으로 실검 1위·인기뉴스 1위 기반 글 작성) ———
    trend_auto_f = tk.LabelFrame(trends_f, text="  트렌드 자동 글쓰기  ", fg=ACCENT, bg=BG_CARD, font=("Malgun Gothic", 9, "bold"), padx=12, pady=10, relief=tk.FLAT, highlightthickness=2, highlightbackground=BORDER)
    trend_auto_f.pack(fill=tk.X, pady=(12, 0))
    var_trend_auto_source = tk.StringVar(value="news_1")
    var_trend_auto_interval = tk.StringVar(value="3")
    var_trend_auto_random = tk.BooleanVar(value=True)
    tk.Label(trend_auto_f, text="주제", bg=BG_CARD, fg=TEXT_MUTED, font=("Malgun Gothic", 8)).pack(anchor=tk.W)
    ta_src_f = tk.Frame(trend_auto_f, bg=BG_CARD)
    ta_src_f.pack(anchor=tk.W)
    for val, lab in (("trend_1", "실검 1위"), ("news_1", "인기뉴스 1위")):
        tk.Radiobutton(ta_src_f, text=lab, variable=var_trend_auto_source, value=val, bg=BG_CARD, font=("Malgun Gothic", 8), activebackground=BG_CARD, fg=TEXT_MAIN).pack(side=tk.LEFT, padx=(0, 10))
    ta_int_f = tk.Frame(trend_auto_f, bg=BG_CARD)
    ta_int_f.pack(anchor=tk.W, pady=(4, 2))
    tk.Label(ta_int_f, text="간격(시간)", bg=BG_CARD, fg=TEXT_MUTED, font=("Malgun Gothic", 8)).pack(side=tk.LEFT)
    tk.Entry(ta_int_f, textvariable=var_trend_auto_interval, width=4, **_entry_kw).pack(side=tk.LEFT, padx=(6, 8))
    tk.Checkbutton(trend_auto_f, text="랜덤(2~6시간)", variable=var_trend_auto_random, bg=BG_CARD, font=("Malgun Gothic", 8), activebackground=BG_CARD, fg=TEXT_MAIN).pack(anchor=tk.W, pady=(0, 6))

    def trend_auto_worker(ta_profile: str | None = None) -> None:
        nonlocal trend_auto_stop_event
        prof = ta_profile or _get_profile()
        stop_ev = trend_auto_stop_event
        log = log_msg
        naver_id = var_naver_id.get().strip()
        naver_pw = var_naver_pw.get()
        api_key = var_api_key.get().strip()
        if not naver_id or not naver_pw or not api_key:
            root.after(0, lambda: messagebox.showwarning("입력 오류", "네이버 아이디, 비밀번호, API 키를 입력하세요."))
            return
        cfg = NaverConfig(naver_id=naver_id, naver_pw=naver_pw, openai_api_key=api_key, manual_login=False, blog_action=var_action.get())
        src = var_trend_auto_source.get()
        use_random = var_trend_auto_random.get()
        try:
            interval_h = int(var_trend_auto_interval.get().strip() or "3")
        except ValueError:
            interval_h = 3
        if interval_h < 1:
            interval_h = 1
        run_count = 0
        try:
            while stop_ev is not None and not stop_ev.is_set():
                run_count += 1
                topic = None
                if src == "trend_1":
                    kws = fetch_google_trending_keywords_kr(1)
                    topic = kws[0] if kws else None
                    log(f"[트렌드 자동 #{run_count}] 실검 1위: {topic or '(없음)'}")
                else:
                    news = fetch_hot_news_headlines(1)
                    topic = news[0] if news else None
                    log(f"[트렌드 자동 #{run_count}] 인기뉴스 1위: {topic[:50] if topic else '(없음)'}...")
                if topic:
                    try:
                        client = create_openai_client(api_key)
                        gc = load_gui_config(prof)
                        sys_ov = (gc.get("prompt_hot_system") or "").strip() or None
                        suf_ov = (gc.get("prompt_hot_user_suffix") or "").strip() or None
                        title, content = generate_hot_issue_post(client, topic=topic, topic_mode="manual", trend_field="전체", system_override=sys_ov, user_suffix_override=suf_ov)
                        _post_to_blog_with_generated_content(cfg, title, content, log)
                        log(f"[트렌드 자동 #{run_count}] 글 작성·게시 완료.")
                    except Exception as e:
                        log(f"[트렌드 자동 #{run_count}] 오류: {_sanitize_error_for_gui(str(e))}")
                else:
                    log(f"[트렌드 자동 #{run_count}] 주제 없음 — 건너뜀.")
                if stop_ev is None or stop_ev.is_set():
                    break
                sleep_sec = (random.randint(2, 6) * 3600) if use_random else (interval_h * 3600)
                log(f"[트렌드 자동] 다음 실행까지 약 {sleep_sec // 3600}시간 대기...")
                while sleep_sec > 0 and stop_ev is not None and not stop_ev.is_set():
                    time.sleep(min(60, sleep_sec))
                    sleep_sec -= 60
        finally:
            log("[트렌드 자동] 종료되었습니다.")

    def trend_auto_start() -> None:
        nonlocal trend_auto_stop_event, trend_auto_thread
        if trend_auto_thread and trend_auto_thread.is_alive():
            messagebox.showinfo("트렌드 자동", "이미 실행 중입니다.")
            return
        trend_auto_stop_event = threading.Event()
        trend_auto_thread = threading.Thread(target=lambda: trend_auto_worker(_get_profile()), daemon=True)
        trend_auto_thread.start()
        log_msg("[트렌드 자동] ON — 간격마다 실검/인기뉴스 1위로 글 작성합니다. 끄려면 [트렌드 자동 OFF]를 누르세요.")

    def trend_auto_stop() -> None:
        nonlocal trend_auto_stop_event
        if trend_auto_stop_event and not trend_auto_stop_event.is_set():
            trend_auto_stop_event.set()
            log_msg("[트렌드 자동] OFF — 곧 종료됩니다.")
        else:
            log_msg("[트렌드 자동] 실행 중인 작업이 없습니다.")

    ta_btn_f = tk.Frame(trend_auto_f, bg=BG_CARD)
    ta_btn_f.pack(fill=tk.X)
    tk.Button(ta_btn_f, text="트렌드 자동 ON", fg="white", bg="#7C3AED", activebackground="#6D28D9", font=("Malgun Gothic", 9), relief=tk.FLAT, padx=10, pady=4, cursor="hand2", command=trend_auto_start).pack(side=tk.LEFT, padx=(0, 6))
    tk.Button(ta_btn_f, text="트렌드 자동 OFF", fg="white", bg=SLATE_BTN, activebackground="#334155", font=("Malgun Gothic", 9), relief=tk.FLAT, padx=10, pady=4, cursor="hand2", command=trend_auto_stop).pack(side=tk.LEFT)

    prompt_btn_rss_f = tk.Frame(rss_prompt_card, bg=BG_CARD)
    prompt_btn_rss_f.pack(fill=tk.X, padx=12, pady=(0, 12))

    def _load_prompt_widgets_from_config() -> None:
        c = load_gui_config(_get_profile())
        hs = (c.get("prompt_hot_system") or "").strip()
        hu = (c.get("prompt_hot_user_suffix") or "").strip()
        rs = (c.get("prompt_rss_system") or "").strip()
        ru = (c.get("prompt_rss_user") or "").strip()
        txt_prompt_hot_system.delete("1.0", tk.END)
        txt_prompt_hot_system.insert(tk.END, hs or default_prompt_hot_system())
        txt_prompt_hot_user.delete("1.0", tk.END)
        txt_prompt_hot_user.insert(tk.END, hu or default_prompt_hot_user_suffix())
        txt_prompt_rss_system.delete("1.0", tk.END)
        txt_prompt_rss_system.insert(tk.END, rs or _default_rss_system_prompt())
        txt_prompt_rss_user.delete("1.0", tk.END)
        txt_prompt_rss_user.insert(tk.END, ru or _default_rss_user_prompt_template())

    def _persist_prompts_silent() -> None:
        data = load_gui_config(_get_profile())
        data["prompt_hot_system"] = txt_prompt_hot_system.get("1.0", tk.END).rstrip("\n")
        data["prompt_hot_user_suffix"] = txt_prompt_hot_user.get("1.0", tk.END).rstrip("\n")
        data["prompt_rss_system"] = txt_prompt_rss_system.get("1.0", tk.END).rstrip("\n")
        data["prompt_rss_user"] = txt_prompt_rss_user.get("1.0", tk.END).rstrip("\n")
        save_gui_config(data, _get_profile())

    def save_prompts_to_config() -> None:
        _persist_prompts_silent()
        log_msg("프롬프트를 gui_config.json 에 저장했습니다.")
        messagebox.showinfo("프롬프트", "저장되었습니다. 예약·RSS 감시는 저장된 내용을 사용합니다.")

    def reset_prompts_to_defaults() -> None:
        txt_prompt_hot_system.delete("1.0", tk.END)
        txt_prompt_hot_system.insert(tk.END, default_prompt_hot_system())
        txt_prompt_hot_user.delete("1.0", tk.END)
        txt_prompt_hot_user.insert(tk.END, default_prompt_hot_user_suffix())
        txt_prompt_rss_system.delete("1.0", tk.END)
        txt_prompt_rss_system.insert(tk.END, _default_rss_system_prompt())
        txt_prompt_rss_user.delete("1.0", tk.END)
        txt_prompt_rss_user.insert(tk.END, _default_rss_user_prompt_template())
        log_msg("프롬프트를 기본값으로 채웠습니다. 적용하려면 '프롬프트 저장'을 누르세요.")

    tk.Button(
        prompt_btn_f,
        text="프롬프트 저장",
        fg="white",
        bg=ACCENT,
        activebackground=ACCENT_DARK,
        font=("Malgun Gothic", 9, "bold"),
        relief=tk.FLAT,
        padx=14,
        pady=8,
        cursor="hand2",
        command=save_prompts_to_config,
    ).pack(side=tk.LEFT, padx=(0, 8))
    tk.Button(
        prompt_btn_f,
        text="기본 문구로 되돌리기",
        fg="white",
        bg=SLATE_BTN,
        activebackground="#334155",
        font=("Malgun Gothic", 9),
        relief=tk.FLAT,
        padx=12,
        pady=8,
        cursor="hand2",
        command=reset_prompts_to_defaults,
    ).pack(side=tk.LEFT)

    tk.Label(prompt_btn_rss_f, text="전체 저장", bg=BG_CARD, font=("Malgun Gothic", 8), fg=TEXT_MUTED).pack(side=tk.LEFT, padx=(0, 8))
    tk.Button(
        prompt_btn_rss_f,
        text="저장",
        fg="white",
        bg=ACCENT,
        activebackground=ACCENT_DARK,
        font=("Malgun Gothic", 9, "bold"),
        relief=tk.FLAT,
        padx=12,
        pady=6,
        cursor="hand2",
        command=save_prompts_to_config,
    ).pack(side=tk.LEFT, padx=(0, 6))
    tk.Button(
        prompt_btn_rss_f,
        text="기본값",
        fg="white",
        bg=SLATE_BTN,
        activebackground="#334155",
        font=("Malgun Gothic", 9),
        relief=tk.FLAT,
        padx=10,
        pady=6,
        cursor="hand2",
        command=reset_prompts_to_defaults,
    ).pack(side=tk.LEFT)

    _load_prompt_widgets_from_config()

    def log_msg(msg: str) -> None:
        def _() -> None:
            log_text.configure(state=tk.NORMAL)
            log_text.insert(tk.END, msg + "\n")
            log_text.see(tk.END)
            log_text.configure(state=tk.DISABLED)
        root.after(0, _)

    def _get_rss_urls_from_config(profile: str | None = None) -> list[str]:
        data = load_gui_config(profile)
        urls = data.get("rss_urls", [])
        return [u for u in urls if isinstance(u, str) and u.strip()]

    def _save_rss_gui_options() -> None:
        data = load_gui_config(_get_profile())
        data["rss_interval_hours"] = int(var_rss_check_interval.get().strip() or "3")
        data["rss_auto_enabled"] = bool(var_rss_auto_enabled.get())
        data["rss_include_source_link"] = bool(var_rss_include_source_link.get())
        data["rss_image_memo_only"] = bool(var_rss_image_memo_only.get())
        data["rss_publish_mode"] = var_rss_publish_mode.get()
        save_gui_config(data, _get_profile())

    def run_click() -> None:
        nonlocal current_driver, manual_login_event
        naver_id = var_naver_id.get().strip()
        naver_pw = var_naver_pw.get()
        api_key = var_api_key.get().strip()
        if not naver_id or not naver_pw or not api_key:
            messagebox.showwarning("입력 오류", "네이버 아이디, 비밀번호, OpenAI API 키를 모두 입력하세요.")
            return
        if var_topic_source.get() == "manual" and not var_topic.get().strip():
            messagebox.showwarning(
                "주제",
                "'직접 입력'을 쓰려면 주제 칸에 키워드를 입력하세요.\n"
                "또는 Google 실검 / 인기 뉴스 / 실검 1위 / 인기뉴스 1위 중 하나를 선택하세요.",
            )
            return
        data = load_gui_config()
        data["naver_id"] = naver_id
        data["naver_pw"] = naver_pw
        data["manual_login"] = var_manual.get()
        data["blog_action"] = var_action.get()
        data["topic"] = var_topic.get().strip()
        data["topic_source"] = var_topic_source.get()
        data["trend_field"] = var_trend_field.get()
        data["save_login"] = var_save_login.get()
        data["save_api_key"] = var_save_api.get()
        if var_save_api.get():
            data["api_key"] = api_key
        else:
            data["api_key"] = data.get("api_key", "")
        data["rss_urls"] = [rss_urls_listbox.get(i) for i in range(rss_urls_listbox.size())]
        data["rss_interval_hours"] = int(var_rss_check_interval.get().strip() or "3")
        data["rss_auto_enabled"] = bool(var_rss_auto_enabled.get())
        data["rss_include_source_link"] = bool(var_rss_include_source_link.get())
        data["rss_image_memo_only"] = bool(var_rss_image_memo_only.get())
        data["rss_publish_mode"] = var_rss_publish_mode.get()
        save_gui_config(data, _get_profile())
        cfg = NaverConfig(naver_id=naver_id, naver_pw=naver_pw, openai_api_key=api_key, manual_login=var_manual.get(), blog_action=var_action.get())
        topic = var_topic.get().strip() or None
        current_profile = _get_profile()
        manual_login_event = threading.Event()
        if cfg.manual_login:
            btn_manual_ok.configure(
                state=tk.NORMAL,
                bg="#10B981",
                activebackground=GREEN_DARK,
                fg="white",
                disabledforeground="#E2E8F0",
                text="  로그인 끝났습니다 → 여기를 눌러 글쓰기 계속  ",
                font=("Malgun Gothic", 10, "bold"),
            )

        def run() -> None:
            nonlocal current_driver
            hs = txt_prompt_hot_system.get("1.0", tk.END).strip()
            hu = txt_prompt_hot_user.get("1.0", tk.END).strip()
            driver, err = run_blog_workflow(
                cfg,
                topic,
                log_fn=log_msg,
                manual_login_event=manual_login_event,
                hot_system=hs,
                hot_user_suffix=hu,
                profile=current_profile,
            )
            def after_run() -> None:
                try:
                    if err:
                        messagebox.showerror("오류", _sanitize_error_for_gui(err))
                        return
                    log_msg("작업이 완료되었습니다.")
                except Exception as e:
                    messagebox.showerror("오류", _sanitize_error_for_gui(str(e)))
            root.after(0, after_run)
        threading.Thread(target=run, daemon=True).start()
        log_msg("실행을 시작합니다...")

    def _post_to_blog_with_generated_content(
        cfg: NaverConfig,
        title: str,
        content: str,
        log: Callable[[str], None],
    ) -> None:
        driver = create_driver()
        try:
            if cfg.manual_login:
                log("RSS 기반 자동 모드에서는 수동 로그인을 지원하지 않습니다. 환경설정에서 수동 로그인 옵션을 끄세요.")
                raise RuntimeError("RSS 자동 모드에서 수동 로그인은 사용할 수 없습니다.")
            log("네이버에 로그인 중입니다...(RSS 자동)")
            naver_login(driver, cfg, manual=False)
            blog_id = cfg.naver_id
            log("블로그 글쓰기 페이지를 여는 중입니다...(RSS 자동)")
            open_blog_write_page(driver, blog_id=blog_id)
            action_label = "발행" if cfg.blog_action == "publish" else "저장"
            log(f"[RSS] 제목/본문 입력 후 {action_label} 시도 중...")
            fill_post_and_publish(driver, title=title, content=content, action=cfg.blog_action)
            log("[RSS] 글 게시/저장이 완료되었습니다.")
        finally:
            try:
                driver.quit()
            except Exception:
                pass

    def parse_schedule_params() -> tuple[datetime, datetime, tuple[int,int], tuple[int,int], int] | None:
        """(start_dt, end_dt, (h1,m1), (h2,m2), runs_per_day) 또는 None."""
        try:
            s = var_start_date.get().strip()
            e = var_end_date.get().strip()
            if not s or not e:
                messagebox.showwarning("입력 오류", "예약 기간(시작일, 종료일)을 입력하세요. (예: 2025-02-12)")
                return None
            start_d = datetime.strptime(s, "%Y-%m-%d").date()
            end_d = datetime.strptime(e, "%Y-%m-%d").date()
            if start_d > end_d:
                messagebox.showwarning("입력 오류", "시작일이 종료일보다 늦을 수 없습니다.")
                return None
            t1 = var_time_start.get().strip()
            t2 = var_time_end.get().strip()
            for t, name in [(t1, "시작 시간"), (t2, "종료 시간")]:
                if len(t) < 4 or ":" not in t:
                    messagebox.showwarning("입력 오류", f"{name}을 HH:MM 형식으로 입력하세요. (예: 09:00)")
                    return None
            parts1 = t1.split(":")
            parts2 = t2.split(":")
            h1, m1 = int(parts1[0].strip()), int(parts1[1].strip()) if len(parts1) > 1 else 0
            h2, m2 = int(parts2[0].strip()), int(parts2[1].strip()) if len(parts2) > 1 else 0
            runs = int(var_runs_per_day.get().strip() or "2")
            if runs < 1 or runs > 10:
                runs = 2
            start_dt = datetime.combine(start_d, datetime.min.time())
            end_dt = datetime.combine(end_d, datetime.min.time())
            return (start_dt, end_dt, (h1, m1), (h2, m2), runs)
        except ValueError as ex:
            messagebox.showwarning("입력 오류", "날짜는 YYYY-MM-DD, 시간은 HH:MM 형식으로 입력하세요.")
            return None

    def schedule_worker(schedule_profile: str) -> None:
        nonlocal schedule_cancel_event
        params = parse_schedule_params()
        if not params:
            return
        start_dt, end_dt, (h1, m1), (h2, m2), runs_per_day = params
        naver_id = var_naver_id.get().strip()
        naver_pw = var_naver_pw.get()
        api_key = var_api_key.get().strip()
        if not naver_id or not naver_pw or not api_key:
            root.after(0, lambda: messagebox.showwarning("입력 오류", "네이버 아이디, 비밀번호, OpenAI API 키를 입력하세요."))
            return
        cfg = NaverConfig(naver_id=naver_id, naver_pw=naver_pw, openai_api_key=api_key, manual_login=False, blog_action=var_action.get())
        topic = var_topic.get().strip() or None
        run_times: list[datetime] = []
        h_lo, h_hi = min(h1, h2), max(h1, h2)
        d = start_dt.date()
        while d <= end_dt.date():
            for _ in range(runs_per_day):
                hour = random.randint(h_lo, h_hi)
                minute = random.randint(0, 59)
                run_times.append(datetime.combine(d, datetime.min.replace(hour=hour, minute=minute).time()))
            d += timedelta(days=1)
        run_times.sort()
        root.after(0, lambda: log_msg(f"예약: 총 {len(run_times)}회 (기간 내 랜덤 시간)"))
        for run_at in run_times:
            if schedule_cancel_event and schedule_cancel_event.is_set():
                root.after(0, lambda: log_msg("예약이 중지되었습니다."))
                return
            now = datetime.now()
            if run_at > now:
                delay = (run_at - now).total_seconds()
                while delay > 0 and (not schedule_cancel_event or not schedule_cancel_event.is_set()):
                    time.sleep(min(60, delay))
                    delay -= 60
                    if schedule_cancel_event and schedule_cancel_event.is_set():
                        root.after(0, lambda: log_msg("예약이 중지되었습니다."))
                        return
            # 실행 시점 도달
            root.after(0, lambda t=run_at: log_msg(f"예약 실행: {t.strftime('%Y-%m-%d %H:%M')}"))

            # 1순위: RSS 예약 큐에 쌓여 있는 글이 있으면 그것부터 소모
            queued_item = _pop_from_rss_queue()
            if queued_item:
                gen_title = queued_item.get("generated_title") or "RSS 예약 글"
                gen_content = queued_item.get("generated_content") or ""
                root.after(0, lambda title=gen_title: log_msg(f"[RSS 예약 큐] '{title[:40]}...' 글을 발행/저장합니다."))
                try:
                    _post_to_blog_with_generated_content(cfg, gen_title, gen_content, log_msg)
                except Exception as e:
                    root.after(0, lambda e=e: log_msg(f"[RSS 예약 큐] 네이버 게시 실패: {_sanitize_error_for_gui(str(e))}"))
                continue

            # 2순위: 큐가 비어 있으면 기존 topic 기반 자동 생성 실행
            manual_ev = threading.Event()
            driver, err = run_blog_workflow(cfg, topic, log_fn=log_msg, manual_login_event=manual_ev, profile=schedule_profile)
            if driver:
                try:
                    driver.quit()
                except Exception:
                    pass
            if err:
                root.after(0, lambda e=err: log_msg(f"실패: {_sanitize_error_for_gui(e)}"))
        root.after(0, lambda: log_msg("예약 업로드가 모두 완료되었습니다."))

    def schedule_start_click() -> None:
        nonlocal schedule_cancel_event
        if var_topic_source.get() == "manual" and not var_topic.get().strip():
            messagebox.showwarning(
                "주제",
                "예약 실행도 '직접 입력'이면 주제 칸을 채워 주세요.\n"
                "또는 Google 실검 / 인기 뉴스 / 실검 1위 / 인기뉴스 1위를 선택하세요.",
            )
            return
        if schedule_cancel_event and schedule_cancel_event.is_set():
            schedule_cancel_event = None
        schedule_cancel_event = threading.Event()
        data = load_gui_config(_get_profile())
        data["schedule_start"] = var_start_date.get().strip()
        data["schedule_end"] = var_end_date.get().strip()
        data["schedule_time_start"] = var_time_start.get().strip()
        data["schedule_time_end"] = var_time_end.get().strip()
        try:
            data["schedule_runs_per_day"] = int(var_runs_per_day.get().strip() or "2")
        except ValueError:
            data["schedule_runs_per_day"] = 2
        data["topic_source"] = var_topic_source.get()
        data["trend_field"] = var_trend_field.get()
        data["topic"] = var_topic.get().strip()
        data["rss_urls"] = [rss_urls_listbox.get(i) for i in range(rss_urls_listbox.size())]
        data["rss_interval_hours"] = int(var_rss_check_interval.get().strip() or "3")
        data["rss_auto_enabled"] = bool(var_rss_auto_enabled.get())
        data["rss_include_source_link"] = bool(var_rss_include_source_link.get())
        data["rss_image_memo_only"] = bool(var_rss_image_memo_only.get())
        data["rss_publish_mode"] = var_rss_publish_mode.get()
        save_gui_config(data, _get_profile())
        _persist_prompts_silent()
        threading.Thread(target=lambda: schedule_worker(_get_profile()), daemon=True).start()
        log_msg("예약을 시작합니다. 중지하려면 '예약 중지'를 누르세요.")

    def schedule_stop_click() -> None:
        nonlocal schedule_cancel_event
        if schedule_cancel_event:
            schedule_cancel_event.set()
        log_msg("예약 중지 요청했습니다.")

    def _rss_queue_path() -> Path:
        return Path(__file__).resolve().parent / "rss_queue.json"

    def _append_to_rss_queue(item_payload: dict) -> None:
        path = _rss_queue_path()
        try:
            if path.exists():
                with open(path, "r", encoding="utf-8") as f:
                    data = json.load(f)
            else:
                data = []
        except Exception:
            data = []
        data.append(item_payload)
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except Exception:
            pass

    def _pop_from_rss_queue() -> dict | None:
        """
        예약 큐에서 가장 오래된 항목 하나를 꺼내 반환합니다.
        항목이 없으면 None.
        구조 예시:
        {
          "source_url": "...",
          "detected_title": "...",
          "generated_title": "...",
          "generated_content": "...",
          "detected_at": "2026-03-18 10:20:00"
        }
        """
        path = _rss_queue_path()
        try:
            if not path.exists():
                return None
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, list) or not data:
                return None
            item = data.pop(0)
            with open(path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            return item
        except Exception:
            return None

    def _rss_should_stop(honor_global: bool) -> bool:
        if not honor_global:
            return False
        ev = rss_monitor_stop_event
        return ev is None or ev.is_set()

    def _rss_run_single_scan_cycle(*, honor_global_stop: bool, log_cycle_done: bool, rss_profile: str | None = None) -> None:
        """한 번의 피드 스캔 + 새 글 처리. 연속 감시 시 honor_global_stop=True 로 OFF 반영."""
        log = log_msg
        client: OpenAI | None = None

        urls = _get_rss_urls_from_config(rss_profile)
        if not urls:
            log("[RSS] 등록된 RSS URL 이 없습니다.")
            return

        interval_hours = 3
        try:
            interval_hours = int(var_rss_check_interval.get().strip() or "3")
        except ValueError:
            interval_hours = 3
        if interval_hours < 1:
            interval_hours = 1

        include_source_link = bool(var_rss_include_source_link.get())
        image_memo_only = bool(var_rss_image_memo_only.get())
        publish_mode = var_rss_publish_mode.get()

        state = load_rss_state()
        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        for url in urls:
            if _rss_should_stop(honor_global_stop):
                log("[RSS] OFF 요청: 피드 확인을 중단합니다.")
                return
            try:
                log(f"[RSS] 피드 확인 중: {url}")
                channel_title, items = fetch_rss_feed(url)
                if not items:
                    log(f"[RSS] 항목이 없습니다: {url}")
                    continue

                new_items: list[dict] = []
                for item in items:
                    if _rss_should_stop(honor_global_stop):
                        log("[RSS] OFF 요청: 새 글 목록 수집을 중단합니다.")
                        var_rss_last_checked.set(now_str)
                        return
                    data = extract_rss_item_data(item)
                    data["blog_title"] = channel_title
                    data["source_blog_title"] = channel_title
                    data["source_post_title"] = data.get("item_title")
                    data["source_link"] = data.get("link")
                    data["source_category"] = data.get("category")
                    data["source_tags"] = data.get("tags") or []
                    data["source_summary_clean"] = _strip_html_tags(data.get("description") or "")
                    data["inferred_topic"] = data.get("item_title") or channel_title or ""
                    data["suggested_keywords"] = []

                    with rss_state_lock:
                        is_new = is_new_rss_item(data, state, url)
                        if is_new:
                            src_state = state.setdefault("sources", {}).setdefault(
                                url, {"last_checked_at": None, "processed_ids": []}
                            )
                            processed = src_state.setdefault("processed_ids", [])
                            processed.append(data["id"])
                            src_state["last_checked_at"] = now_str
                    if is_new:
                        new_items.append(data)

                if not new_items:
                    log(f"[RSS] 새 글이 없습니다: {url}")
                    continue

                with rss_state_lock:
                    save_rss_state(state)

                for data in new_items:
                    if _rss_should_stop(honor_global_stop):
                        log("[RSS] OFF 요청: 남은 새 글 처리를 건너뜁니다. (다음 ON 때 이어짐)")
                        var_rss_last_checked.set(now_str)
                        return

                    title_raw = data.get("item_title") or ""
                    log(f"[RSS] 새 글 감지: {title_raw}")
                    thumb_url = data.get("thumbnail_image")
                    if thumb_url and image_memo_only:
                        log(f"[RSS] 대표 이미지 후보 감지됨: {thumb_url}")

                    if client is None:
                        api_key = var_api_key.get().strip()
                        if not api_key:
                            log("[RSS] OpenAI API 키가 없어 RSS 기반 글 생성을 건너뜁니다.")
                            return
                        client = create_openai_client(api_key)

                    if _rss_should_stop(honor_global_stop):
                        log("[RSS] OFF 요청: GPT 호출 전에 중단합니다.")
                        return

                    try:
                        gconf = load_gui_config(rss_profile)
                        rss_sys = (gconf.get("prompt_rss_system") or "").strip() or None
                        rss_usr = (gconf.get("prompt_rss_user") or "").strip() or None
                        gen_title, gen_content = generate_post_from_rss(
                            client,
                            data,
                            include_source_link=include_source_link,
                            custom_system=rss_sys,
                            custom_user_template=rss_usr,
                        )
                    except Exception as e:
                        log(f"[RSS] GPT 글 생성 실패: {_sanitize_error_for_gui(str(e))}")
                        continue

                    if _rss_should_stop(honor_global_stop):
                        log("[RSS] OFF 요청: 게시 직전에 중단합니다.")
                        return

                    if publish_mode == "queue":
                        payload = {
                            "source_url": url,
                            "detected_title": title_raw,
                            "generated_title": gen_title,
                            "generated_content": gen_content,
                            "detected_at": now_str,
                        }
                        _append_to_rss_queue(payload)
                        log(f"[RSS] 새 글을 예약 큐에 추가했습니다. (제목: {gen_title[:40]}...)")
                    else:
                        naver_id = var_naver_id.get().strip()
                        naver_pw = var_naver_pw.get()
                        api_key = var_api_key.get().strip()
                        if not naver_id or not naver_pw or not api_key:
                            log("[RSS] 네이버 로그인 정보 또는 API 키가 없어 자동 게시를 건너뜁니다.")
                            continue
                        cfg_obj = NaverConfig(
                            naver_id=naver_id,
                            naver_pw=naver_pw,
                            openai_api_key=api_key,
                            manual_login=False,
                            blog_action="publish" if publish_mode == "publish" else "save",
                        )
                        try:
                            _post_to_blog_with_generated_content(
                                cfg_obj,
                                gen_title,
                                gen_content,
                                log,
                            )
                        except Exception as e:
                            log(f"[RSS] 네이버 게시 실패: {_sanitize_error_for_gui(str(e))}")

            except Exception as e:
                log(f"[RSS] 피드 처리 중 오류: {_sanitize_error_for_gui(str(e))}")

        var_rss_last_checked.set(now_str)
        if log_cycle_done:
            log("[RSS] 이번 피드 확인(1회)을 마쳤습니다.")

    def rss_monitor_worker(single_pass: bool = False, rss_profile: str | None = None) -> None:
        """single_pass=True 이면 피드 1회만 확인 후 종료 (지금 RSS 확인용, OFF 상태에서도 동작)."""
        stop_ev = rss_monitor_stop_event
        prof = rss_profile or _get_profile()
        log = log_msg
        try:
            if single_pass:
                if not rss_one_shot_lock.acquire(blocking=False):
                    log("[RSS] 이미 '지금 확인'이 실행 중입니다. 잠시 후 다시 시도하세요.")
                    return
                try:
                    _rss_run_single_scan_cycle(honor_global_stop=False, log_cycle_done=True, rss_profile=prof)
                finally:
                    rss_one_shot_lock.release()
                return
            while True:
                if stop_ev is None or stop_ev.is_set():
                    break
                _rss_run_single_scan_cycle(honor_global_stop=True, log_cycle_done=False, rss_profile=prof)
                if stop_ev is None or stop_ev.is_set():
                    log("[RSS] 감시가 중지되어 대기 루프에 들어가지 않습니다.")
                    break
                interval_hours = 3
                try:
                    interval_hours = int(var_rss_check_interval.get().strip() or "3")
                except ValueError:
                    interval_hours = 3
                if interval_hours < 1:
                    interval_hours = 1
                sleep_sec = interval_hours * 3600
                log(f"[RSS] 다음 확인까지 약 {interval_hours}시간 대기 (RSS OFF 시 즉시 깨어남).")
                while sleep_sec > 0 and stop_ev is not None and not stop_ev.is_set():
                    step = min(30, sleep_sec)
                    time.sleep(step)
                    sleep_sec -= step
        finally:
            log("[RSS] RSS 감시 스레드가 완전히 종료되었습니다.")

    def rss_start_monitor() -> None:
        nonlocal rss_monitor_stop_event, rss_monitor_thread
        if rss_monitor_thread and rss_monitor_thread.is_alive():
            messagebox.showinfo("RSS", "이미 RSS 자동 감시가 실행 중입니다.")
            return
        _save_rss_gui_options()
        _persist_prompts_silent()
        prof = _get_profile()
        urls = _get_rss_urls_from_config(prof)
        if not urls:
            messagebox.showwarning("RSS", "먼저 RSS URL 을 하나 이상 등록하세요.")
            return
        rss_monitor_stop_event = threading.Event()
        rss_monitor_thread = threading.Thread(
            target=lambda: rss_monitor_worker(single_pass=False, rss_profile=prof), daemon=True
        )
        rss_monitor_thread.start()
        try:
            gc = load_gui_config(prof)
            gc["rss_auto_enabled"] = True
            save_gui_config(gc, prof)
            var_rss_auto_enabled.set(True)
        except Exception:
            pass
        log_msg("[RSS] 감시 ON — 주기마다 확인합니다. 끄려면 [RSS OFF]를 누르세요.")

    def rss_stop_monitor() -> None:
        nonlocal rss_monitor_stop_event
        if rss_monitor_stop_event and not rss_monitor_stop_event.is_set():
            rss_monitor_stop_event.set()
            try:
                data = load_gui_config(_get_profile())
                data["rss_auto_enabled"] = False
                save_gui_config(data, _get_profile())
                var_rss_auto_enabled.set(False)
            except Exception:
                pass
            log_msg("[RSS] OFF — 감시 스레드가 곧 종료됩니다. (지금 글 작성 중이면 그 작업만 끝난 뒤 멈춤)")
        elif rss_monitor_thread and rss_monitor_thread.is_alive():
            rss_monitor_stop_event = rss_monitor_stop_event or threading.Event()
            rss_monitor_stop_event.set()
            log_msg("[RSS] 중지 신호를 보냈습니다.")
        else:
            log_msg("[RSS] 실행 중인 감시가 없습니다.")

    def rss_check_now() -> None:
        """연속 감시와 무관하게 피드만 1회 확인 (긴 대기 없음)."""
        threading.Thread(target=lambda: rss_monitor_worker(single_pass=True, rss_profile=_get_profile()), daemon=True).start()
        log_msg("[RSS] 피드 1회 확인을 시작했습니다…")

    foot_btn = dict(relief=tk.FLAT, cursor="hand2", font=("Malgun Gothic", 10, "bold"), padx=18, pady=10, bd=0, activeforeground="white")
    tk.Button(btn_frame, text="▶ 자동화 시작", fg="white", bg=ACCENT, activebackground=ACCENT_DARK, command=run_click, **foot_btn).pack(side=tk.LEFT, padx=(0, 8))
    tk.Button(btn_frame, text="예약 시작", fg="white", bg=GREEN_BTN, activebackground=GREEN_DARK, command=schedule_start_click, **foot_btn).pack(side=tk.LEFT, padx=(0, 8))
    tk.Button(btn_frame, text="예약 중지", fg="white", bg=ORANGE_BTN, activebackground="#D97706", command=schedule_stop_click, **foot_btn).pack(side=tk.LEFT, padx=(0, 8))
    tk.Frame(btn_frame, width=1, bg="#4338CA").pack(side=tk.LEFT, fill=tk.Y, padx=12, pady=8)
    tk.Button(btn_frame, text="RSS 감시 ON", fg="white", bg="#8B5CF6", activebackground="#7C3AED", command=rss_start_monitor, **foot_btn).pack(side=tk.LEFT, padx=(0, 8))
    tk.Button(btn_frame, text="RSS OFF", fg="white", bg=SLATE_BTN, activebackground="#334155", command=rss_stop_monitor, **foot_btn).pack(side=tk.LEFT, padx=(0, 8))
    tk.Button(btn_frame, text="RSS 지금 확인", fg="white", bg="#0D9488", activebackground="#0F766E", command=rss_check_now, **foot_btn).pack(side=tk.LEFT, padx=(0, 8))
    tk.Frame(btn_frame, width=1, bg="#4338CA").pack(side=tk.LEFT, fill=tk.Y, padx=12, pady=8)

    def do_stop() -> None:
        nonlocal current_driver
        if current_driver:
            try:
                current_driver.quit()
            except Exception:
                pass
            current_driver = None
            log_msg("중지되었습니다. 브라우저를 닫았습니다.")

    tk.Button(btn_frame, text="브라우저 중지", fg="white", bg=RED_BTN, activebackground="#DC2626", command=do_stop, **foot_btn).pack(side=tk.LEFT, padx=(0, 12))

    manual_wrap = tk.Frame(btn_frame, bg=BG_FOOTER)
    manual_wrap.pack(side=tk.LEFT, padx=(8, 0))
    tk.Label(
        manual_wrap,
        text="수동 로그인 모드일 때만 켜짐 →",
        fg="#A5B4FC",
        bg=BG_FOOTER,
        font=("Malgun Gothic", 9),
    ).pack(side=tk.LEFT, padx=(0, 8))
    btn_manual_ok = tk.Button(
        manual_wrap,
        text="  수동 로그인 완료 후 이 버튼  ",
        state=tk.DISABLED,
        fg="#CBD5E1",
        bg="#334155",
        activebackground="#10B981",
        disabledforeground="#94A3B8",
        font=("Malgun Gothic", 10, "bold"),
        relief=tk.FLAT,
        padx=18,
        pady=10,
        cursor="hand2",
    )

    def do_manual_ok() -> None:
        if manual_login_event:
            manual_login_event.set()
        btn_manual_ok.configure(
            state=tk.DISABLED,
            bg="#334155",
            fg="#CBD5E1",
            text="  수동 로그인 완료 후 이 버튼  ",
            font=("Malgun Gothic", 10, "bold"),
        )

    btn_manual_ok.configure(command=do_manual_ok)
    btn_manual_ok.pack(side=tk.LEFT)

    def load_saved_if_needed() -> None:
        c = load_gui_config(_get_profile())
        if c.get("save_login", True):
            var_naver_id.set(c.get("naver_id", ""))
            var_naver_pw.set(c.get("naver_pw", ""))
        if c.get("save_api_key", False):
            var_api_key.set(c.get("api_key", ""))
    load_saved_if_needed()

    def _auto_close_splash() -> None:
        elapsed = time.perf_counter() - splash_start
        delay_ms = max(120, int((1.55 - elapsed) * 1000))
        root.after(delay_ms, _close_splash)

    root.after_idle(_auto_close_splash)
    root.mainloop()


if __name__ == "__main__":
    main_gui()
