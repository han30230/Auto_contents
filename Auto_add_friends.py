"""
네이버 서로이웃 자동화봇 v1.0.0
검색어로 블로그를 찾아 서로이웃 신청을 자동으로 수행합니다.
"""
import json
import random
import re
import sys
import threading
import time
from pathlib import Path
from urllib.parse import quote

import tkinter as tk
from tkinter import messagebox, scrolledtext

try:
    import pyperclip
except ImportError:
    pyperclip = None

try:
    from selenium import webdriver
    from selenium.webdriver.chrome.service import Service
    from selenium.webdriver.common.action_chains import ActionChains
    from selenium.webdriver.common.by import By
    from selenium.webdriver.common.keys import Keys
    from selenium.webdriver.support.ui import WebDriverWait
    from selenium.webdriver.support import expected_conditions as EC
    from selenium.common.exceptions import (
        StaleElementReferenceException,
        ElementClickInterceptedException,
        TimeoutException,
        ElementNotInteractableException,
    )
    from webdriver_manager.chrome import ChromeDriverManager
except ImportError:
    webdriver = None
    ActionChains = None
    StaleElementReferenceException = Exception
    ElementClickInterceptedException = Exception
    TimeoutException = Exception
    ElementNotInteractableException = Exception

# Gemini API 키 발급
GEMINI_API_URL = "https://aistudio.google.com/apikey"

# 설정 저장 경로
def _get_base_path():
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).parent


CONFIG_PATH = _get_base_path() / "add_friends_config.json"
COOKIES_PATH = _get_base_path() / "naver_session_cookies.json"


def load_config():
    if CONFIG_PATH.exists():
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {"save_login": True, "save_api": False}


def save_config(data: dict):
    try:
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


def create_driver():
    """Chrome WebDriver 생성. Chrome 미설치/버전 오류 시 예외 발생."""
    chrome_options = webdriver.ChromeOptions()
    chrome_options.add_argument("--start-maximized")
    try:
        service = Service(ChromeDriverManager().install())
        return webdriver.Chrome(service=service, options=chrome_options)
    except Exception as e:
        err = str(e).split("\n")[0] if "\n" in str(e) else str(e)
        raise RuntimeError(f"Chrome 브라우저를 시작할 수 없습니다. Chrome 설치 및 버전을 확인해 주세요. ({err})") from e


def _paste_text(driver, element, text: str):
    """클립보드 복붙 (Ctrl+V)"""
    if pyperclip:
        pyperclip.copy(text)
    else:
        return False
    element.click()
    time.sleep(0.2)
    ActionChains(driver).key_down(Keys.CONTROL).send_keys("v").key_up(Keys.CONTROL).perform()
    return True


def _try_load_session(driver) -> bool:
    """저장된 세션(쿠키) 로드 후 로그인 여부 확인. 실제 로그인 상태를 한 번 더 검사함."""
    if not COOKIES_PATH.exists():
        return False
    try:
        with open(COOKIES_PATH, "r", encoding="utf-8") as f:
            cookies = json.load(f)
        driver.get("https://www.naver.com")
        time.sleep(2)
        for c in cookies:
            try:
                driver.add_cookie(c)
            except Exception:
                pass
        driver.refresh()
        time.sleep(2)
        # 1) 네이버 메인에서 로그인 여부 확인
        try:
            driver.find_element(By.CSS_SELECTOR, "a[href*='nidlogout'], .my_nickname, #account")
        except Exception:
            return False
        # 2) 블로그에서 재확인 (세션 만료 시 로그인 페이지로 리다이렉트됨)
        driver.get("https://blog.naver.com/")
        time.sleep(2)
        try:
            current = driver.current_url.lower()
            if "nidlogin" in current or ("nid.naver" in current and "login" in current):
                return False
        except Exception:
            pass
        return True
    except Exception:
        return False


def _save_session(driver):
    """현재 세션(쿠키) 저장"""
    try:
        driver.get("https://www.naver.com")
        time.sleep(2)
        cookies = driver.get_cookies()
        with open(COOKIES_PATH, "w", encoding="utf-8") as f:
            json.dump(cookies, f, ensure_ascii=False)
    except Exception:
        pass


def naver_login(driver, naver_id: str, naver_pw: str, use_saved_session: bool = True, save_session: bool = True, manual_login: bool = False, log_cb=None, wait_for_manual_cb=None):
    """네이버 로그인 (저장 세션 시도 → 수동/자동 + 로그인 후 세션 저장)."""
    # 저장된 세션으로 로그인 시도 (수동 로그인 아닐 때만)
    if use_saved_session and not manual_login and COOKIES_PATH.exists():
        if log_cb:
            log_cb("저장된 로그인 정보로 시도 중...\n")
        if _try_load_session(driver):
            if log_cb:
                log_cb("저장된 세션으로 로그인되었습니다.\n")
            return
        if log_cb:
            log_cb("저장된 세션 만료 또는 실패. 로그인 페이지로 진행합니다.\n")

    driver.get("https://nid.naver.com/nidlogin.login")
    time.sleep(2)

    if manual_login:
        if log_cb:
            log_cb("수동 로그인: 브라우저에서 로그인(캡차/자동감시문자 포함)을 완료해 주세요.\n")
        if wait_for_manual_cb:
            wait_for_manual_cb()
        time.sleep(2)
        if save_session:
            _save_session(driver)
        return

    wait = WebDriverWait(driver, 20)
    id_input = wait.until(EC.presence_of_element_located((By.ID, "id")))
    pw_input = wait.until(EC.presence_of_element_located((By.ID, "pw")))
    id_input.clear()
    pw_input.clear()
    time.sleep(0.5)

    if _paste_text(driver, id_input, naver_id):
        pass
    else:
        id_input.send_keys(naver_id)
    time.sleep(random.uniform(1.5, 2.5))  # ID 입력 후 쉬기

    if _paste_text(driver, pw_input, naver_pw):
        pass
    else:
        pw_input.send_keys(naver_pw)
    time.sleep(random.uniform(1.0, 1.8))  # PW 입력 후 쉬기

    ActionChains(driver).send_keys(Keys.RETURN).perform()
    time.sleep(5)

    if save_session:
        _save_session(driver)


def collect_blog_ids_from_search(driver, keyword: str, max_count: int) -> list[str]:
    """검색어로 블로그 ID 수집"""
    url = f"https://search.naver.com/search.naver?ssc=tab.blog.all&sm=tab_jum&query={quote(keyword)}"
    driver.get(url)
    time.sleep(3)

    blog_ids = []
    seen = set()
    # blog.naver.com/ID 형태 링크 추출
    pattern = re.compile(r"blog\.naver\.com/([^/?]+)(?:/|$)")
    links = driver.find_elements(By.CSS_SELECTOR, "a[href*='blog.naver.com']")
    for link in links:
        if len(blog_ids) >= max_count:
            break
        href = link.get_attribute("href") or ""
        m = pattern.search(href)
        if m:
            bid = m.group(1)
            if bid not in seen and not bid.startswith("PostList") and bid != "SectionSearch":
                seen.add(bid)
                blog_ids.append(bid)
    return blog_ids[:max_count]


def _dismiss_alert(driver) -> bool:
    """알림창이 있으면 닫고 True 반환, 없으면 False"""
    try:
        alert = driver.switch_to.alert
        alert.accept()
        return True
    except Exception:
        return False


def _dismiss_alert_get_text(driver) -> tuple:
    """알림창이 있으면 (True, 문구) 반환 후 닫기, 없으면 (False, '')"""
    try:
        alert = driver.switch_to.alert
        text = (alert.text or "").strip()
        alert.accept()
        return True, text
    except Exception:
        return False, ""


def _ensure_main_window(driver):
    """메인 창으로 복귀 (팝업이 닫힌 경우 대비)"""
    try:
        handles = driver.window_handles
        if handles:
            driver.switch_to.window(handles[0])
    except Exception:
        pass


def _close_extra_windows(driver):
    """팝업 등 추가 창이 있으면 닫고 메인 창만 남김"""
    try:
        handles = driver.window_handles
        while len(handles) > 1:
            driver.switch_to.window(handles[-1])
            driver.close()
            handles = driver.window_handles
        if handles:
            driver.switch_to.window(handles[0])
    except Exception:
        pass


def _is_error_page(driver) -> bool:
    """'페이지 주소를 확인해주세요' 등 오류 페이지인지 확인"""
    try:
        body = driver.find_element(By.TAG_NAME, "body").text or ""
        return "페이지 주소를 확인해주세요" in body or "페이지를 찾을 수 없" in body
    except Exception:
        return False


def _get_alert_text(driver) -> str:
    """알림창 문구 반환. 없으면 빈 문자열."""
    try:
        alert = driver.switch_to.alert
        return (alert.text or "").strip()
    except Exception:
        return ""


def _classify_alert_message(alert_text: str) -> str:
    """알림 문구에 따라 사용자에게 보여줄 한글 안내로 변환."""
    if not alert_text:
        return "알림으로 인해 스킵"
    t = alert_text
    if "5000" in t or "한도" in t or "초과" in t:
        return "이웃 한도 초과"
    if "이미" in t and ("신청" in t or "이웃" in t):
        return "이미 신청함 또는 이미 이웃"
    if "일일" in t or "하루" in t:
        return "일일 신청 한도 초과"
    if "차단" in t or "제한" in t:
        return "차단/제한으로 신청 불가"
    if "할 수 없" in t:
        return "이웃 신청 불가 (사유: 알림 참고)"
    return "알림으로 인해 스킵"


def _check_page_block_message(driver) -> str:
    """페이지 본문에 '이웃 신청 불가' 등 안내가 있으면 해당 사유 문자열 반환, 없으면 빈 문자열."""
    try:
        body = (driver.find_element(By.TAG_NAME, "body").text or "").strip()
        if "이웃 신청을 할 수 없" in body or "이웃추가를 할 수 없" in body:
            return "해당 블로그는 이웃 신청 불가"
        if "한도" in body and ("이웃" in body or "5000" in body):
            return "이웃 한도 초과"
    except Exception:
        pass
    return ""


def _safe_click(driver, element, max_attempts: int = 3) -> bool:
    """요소 클릭 시도. 일반 클릭 실패 시 스크롤·JS 클릭으로 재시도."""
    for attempt in range(max_attempts):
        try:
            element.click()
            return True
        except (StaleElementReferenceException, ElementClickInterceptedException, ElementNotInteractableException):
            try:
                driver.execute_script("arguments[0].scrollIntoView({block:'center'});", element)
                time.sleep(0.3)
                element.click()
                return True
            except Exception:
                pass
            try:
                driver.execute_script("arguments[0].click();", element)
                return True
            except Exception:
                pass
        except Exception:
            pass
        time.sleep(0.5)
    return False


def _wait_for_new_window(driver, current_handles: list, timeout_sec: float = 5) -> bool:
    """새 창이 열릴 때까지 대기. 열리면 True."""
    deadline = time.monotonic() + timeout_sec
    while time.monotonic() < deadline:
        try:
            if len(driver.window_handles) > len(current_handles):
                return True
        except Exception:
            pass
        time.sleep(0.3)
    return False


def _is_really_add_button(el) -> bool:
    """이웃추가 버튼인지 확인. 이미 서로이웃/이웃일 때의 '이웃', '서로이웃', '취소' 버튼은 제외."""
    try:
        text = (el.text or "").strip()
        # 자식 blind 텍스트 포함 (스크린리더용)
        try:
            for blind in el.find_elements(By.CSS_SELECTOR, ".blind, [class*='blind']"):
                text += " " + (blind.text or "")
        except Exception:
            pass
        text = text.strip()
        # 취소 관련이면 절대 클릭하지 않음 (서로이웃 취소 방지)
        if "취소" in text:
            return False
        # "이웃추가"만 허용. "이웃", "서로이웃"만 있는 버튼은 이미 이웃/서로이웃 상태 → 스킵
        if "추가" in text or "이웃추가" in text:
            return True
        return False
    except Exception:
        return False


def _find_add_btn(driver):
    """이웃추가 버튼 검색 (메인 + 모든 iframe). 이미 서로이웃/이웃인 경우 버튼은 반환하지 않음."""
    def search(ctx):
        for sel in ["a.btn_add_nb", "a._addBuddyPop", "a[class*='btn_add_nb']", "a[class*='addBuddyPop']"]:
            try:
                for el in ctx.find_elements(By.CSS_SELECTOR, sel):
                    if el.is_displayed() and el.is_enabled() and _is_really_add_button(el):
                        return el
            except Exception:
                pass
        try:
            for el in ctx.find_elements(By.XPATH, "//a[.//span[contains(@class,'blind') and contains(.,'이웃추가')]]"):
                if el.is_displayed() and _is_really_add_button(el):
                    return el
        except Exception:
            pass
        return None

    btn = search(driver)
    if btn:
        return btn, False  # 메인 문서에서 발견
    try:
        for frame in driver.find_elements(By.CSS_SELECTOR, "iframe"):
            driver.switch_to.frame(frame)
            btn = search(driver)
            if btn:
                return btn, True  # 프레임 내 발견, 현재 해당 프레임에 있음
            driver.switch_to.default_content()
    except Exception:
        driver.switch_to.default_content()
    return None, False


def send_neighbor_request(driver, blog_id: str, greeting: str, log_cb) -> bool:
    """블로그에 서로이웃 신청"""
    try:
        urls_to_try = [
            f"https://blog.naver.com/{blog_id}",  # 블로그 메인 (우선)
            f"https://blog.naver.com/ProfileView.naver?blogId={blog_id}",  # 프로필
        ]
        add_btn = None
        in_frame = False

        for url in urls_to_try:
            try:
                driver.get(url)
                time.sleep(2)
            except Exception:
                continue
            if _is_error_page(driver):
                continue
            block_msg = _check_page_block_message(driver)
            if block_msg:
                if log_cb:
                    log_cb(f"  [{blog_id}] {block_msg} - 스킵\n")
                return False
            driver.switch_to.default_content()
            add_btn, in_frame = _find_add_btn(driver)
            if add_btn:
                break

        if not add_btn:
            if log_cb:
                log_cb(f"  [{blog_id}] 이웃추가 버튼 없음 - 스킵 (이미 서로이웃/이웃이거나 페이지 없음)\n")
            return False

        # 이웃추가 버튼 클릭 (Stale/가림 예외 대응)
        if not _safe_click(driver, add_btn):
            if log_cb:
                log_cb(f"  [{blog_id}] 이웃추가 버튼 클릭 실패 - 스킵\n")
            driver.switch_to.default_content()
            return False
        time.sleep(1)
        driver.switch_to.default_content()

        handles_before = list(driver.window_handles)
        _wait_for_new_window(driver, handles_before, timeout_sec=5)
        handles = list(driver.window_handles)
        popup_open = len(handles) > len(handles_before)
        if popup_open:
            driver.switch_to.window(handles[-1])
        time.sleep(1)

        # 알림창 처리 (한도·이미 신청 등) — 문구 감지 후 안내
        dismissed, alert_text = _dismiss_alert_get_text(driver)
        if dismissed:
            reason = _classify_alert_message(alert_text)
            if log_cb:
                log_cb(f"  [{blog_id}] {reason} - 스킵\n")
            if popup_open:
                try:
                    driver.close()
                    driver.switch_to.window(handles_before[0])
                except Exception:
                    _ensure_main_window(driver)
            return False

        # 1. 서로이웃 라디오 체크 (이웃 X, 서로이웃 O)
        try:
            clicked = False
            for el in driver.find_elements(By.XPATH, "//label[contains(., '서로이웃')] | //*[contains(text(), '서로이웃') and not(contains(text(), '이웃과 서로이웃'))]"):
                if el.is_displayed() and "이웃과" not in (el.text or ""):
                    try:
                        _safe_click(driver, el)
                        clicked = True
                        break
                    except Exception:
                        pass
            if not clicked:
                radios = driver.find_elements(By.CSS_SELECTOR, "input[type='radio']")
                if len(radios) >= 2:
                    try:
                        _safe_click(driver, radios[1])
                    except Exception:
                        radios[1].click()
        except Exception:
            dismissed, at = _dismiss_alert_get_text(driver)
            if dismissed and log_cb:
                log_cb(f"  [{blog_id}] {_classify_alert_message(at)} - 스킵\n")
            if popup_open:
                try:
                    driver.close()
                    driver.switch_to.window(handles_before[0])
                except Exception:
                    pass
            _ensure_main_window(driver)
            return False
        time.sleep(0.5)

        # 2. 다음 버튼 클릭
        def click_next():
            has_alert, at = _dismiss_alert_get_text(driver)
            if has_alert:
                return False
            for btn in driver.find_elements(By.XPATH, "//button[contains(., '다음')] | //a[contains(., '다음')] | //span[contains(., '다음')]/ancestor::button | //*[@class and contains(@class,'btn') and contains(., '다음')]"):
                txt = (btn.text or "").strip()
                if btn.is_displayed() and "다음" in txt and "취소" not in txt:
                    try:
                        if _safe_click(driver, btn):
                            return True
                    except Exception:
                        pass
                    _dismiss_alert_get_text(driver)
                    break
            return False

        if click_next():
            time.sleep(2)
        has_alert, alert_text2 = _dismiss_alert_get_text(driver)
        if has_alert:
            if log_cb:
                log_cb(f"  [{blog_id}] {_classify_alert_message(alert_text2)} - 스킵\n")
            if popup_open:
                try:
                    driver.close()
                    driver.switch_to.window(handles_before[0])
                except Exception:
                    pass
            _ensure_main_window(driver)
            return False

        # 2-2. 인사말 입력 (다음 클릭 후 나타나는 입력란)
        greeting_entered = False
        for _ in range(2):
            try:
                # textarea (인사말 입력창)
                for textarea in driver.find_elements(By.CSS_SELECTOR, "textarea"):
                    if textarea.is_displayed() and textarea.is_enabled():
                        textarea.clear()
                        textarea.click()
                        time.sleep(0.2)
                        textarea.send_keys(greeting or "안녕하세요! 서로이웃 신청드립니다 :)")
                        greeting_entered = True
                        break
                if greeting_entered:
                    break
                # contenteditable 또는 input
                for el in driver.find_elements(By.XPATH, "//*[@contenteditable='true'] | //input[@type='text' and (contains(@placeholder,'인사') or contains(@placeholder,'메시지') or contains(@name,'msg') or contains(@name,'greeting'))]"):
                    if el.is_displayed() and el.is_enabled():
                        try:
                            el.clear()
                        except Exception:
                            pass
                        el.click()
                        time.sleep(0.2)
                        el.send_keys(greeting or "안녕하세요! 서로이웃 신청드립니다 :)")
                        greeting_entered = True
                        break
                if greeting_entered:
                    break
            except Exception:
                pass
            time.sleep(0.8)

        if not greeting_entered and (greeting or "").strip():
            try:
                # 한 번 더: placeholder에 '인사' 포함된 요소
                for el in driver.find_elements(By.CSS_SELECTOR, "textarea[placeholder*='인사'], input[placeholder*='인사'], textarea, input[type='text']"):
                    if el.is_displayed() and el.is_enabled():
                        el.clear()
                        el.click()
                        time.sleep(0.2)
                        el.send_keys(greeting.strip())
                        greeting_entered = True
                        break
            except Exception:
                pass

        time.sleep(0.5)

        # 3. 다음 또는 신청 버튼 클릭 (인사말 입력 후 최종 제출)
        submitted = click_next()
        if not submitted:
            try:
                for btn in driver.find_elements(By.XPATH, "//button[contains(., '신청')] | //a[contains(., '신청')] | //span[contains(., '신청')]/ancestor::button"):
                    txt = (btn.text or "").strip()
                    if btn.is_displayed() and "신청" in txt and "취소" not in txt:
                        if _safe_click(driver, btn):
                            submitted = True
                        break
            except Exception:
                pass
        if submitted:
            time.sleep(1.5)

        # 새 창이었으면 닫고 메인 창으로 복귀
        if popup_open:
            try:
                driver.close()
                driver.switch_to.window(handles_before[0])
            except Exception:
                _ensure_main_window(driver)

        if log_cb:
            log_cb(f"  [{blog_id}] 서로이웃 신청 완료\n")
        return True
    except Exception as e:
        err_raw = str(e).split("\n")[0] if "\n" in str(e) else str(e)
        err_lower = err_raw.lower()
        try:
            _dismiss_alert(driver)
            _close_extra_windows(driver)
            _ensure_main_window(driver)
        except Exception:
            pass
        # 예외 종류별 사용자 안내 메시지
        if isinstance(e, StaleElementReferenceException):
            err_msg = "요소가 사라져 스킵 (페이지 갱신 등)"
        elif isinstance(e, ElementClickInterceptedException):
            err_msg = "버튼이 가려져 클릭 불가 - 스킵"
        elif isinstance(e, ElementNotInteractableException):
            err_msg = "요소 클릭 불가 - 스킵"
        elif isinstance(e, TimeoutException):
            err_msg = "대기 시간 초과 - 스킵"
        elif "invalid session" in err_lower or "session" in err_lower:
            err_msg = "브라우저 종료됨"
        elif "no such window" in err_lower or "target window already closed" in err_lower:
            err_msg = "창이 닫혀 스킵"
        elif "alert" in err_lower:
            err_msg = "알림으로 스킵"
        elif "element not found" in err_lower or "no such element" in err_lower:
            err_msg = "요소를 찾지 못함 - 스킵"
        elif "timeout" in err_lower:
            err_msg = "대기 시간 초과 - 스킵"
        else:
            err_msg = err_raw[:80] + ("..." if len(err_raw) > 80 else "")
        if log_cb:
            log_cb(f"  [{blog_id}] {err_msg}\n")
        return False


def run_workflow(naver_id, naver_pw, api_key, greetings: list, search_terms: list, log_cb, stop_event, save_session: bool = True, manual_login: bool = False, wait_for_manual_cb=None):
    """메인 워크플로우"""
    if not webdriver:
        if log_cb:
            log_cb("selenium이 설치되지 않았습니다. pip install selenium webdriver-manager\n")
        return

    try:
        def log(msg):
            if log_cb and not stop_event.is_set():
                log_cb(msg)

        log("Chrome 브라우저를 시작합니다...\n")
        driver = create_driver()

        try:
            if stop_event.is_set():
                return
            log("네이버에 로그인 중입니다...\n")
            naver_login(driver, naver_id, naver_pw, use_saved_session=save_session, save_session=save_session, manual_login=manual_login, log_cb=log, wait_for_manual_cb=wait_for_manual_cb)

            greeting_idx = 0
            total_requested = 0
            all_blog_ids = []

            for term_data in search_terms:
                if stop_event.is_set():
                    break
                keyword = term_data.get("keyword", "").strip()
                try:
                    count = int(term_data.get("count", 30))
                except (ValueError, TypeError):
                    count = 30
                count = max(1, min(100, count))
                if not keyword:
                    continue

                log(f"검색어 '{keyword}'로 블로그 ID 수집 중... (최대 {count}개)\n")
                ids = collect_blog_ids_from_search(driver, keyword, count)
                log(f"  수집된 ID: {len(ids)}개\n")
                all_blog_ids.extend(ids)

            # 중복 제거
            seen = set()
            unique_ids = []
            for bid in all_blog_ids:
                if bid not in seen and bid != naver_id:
                    seen.add(bid)
                    unique_ids.append(bid)

            log(f"\n총 {len(unique_ids)}개 블로그에 서로이웃 신청을 시작합니다...\n")

            for i, blog_id in enumerate(unique_ids):
                if stop_event.is_set():
                    log("사용자가 중지했습니다.\n")
                    break
                greeting = greetings[greeting_idx % len(greetings)] if greetings else "안녕하세요! 서로이웃 신청드립니다 :)"
                if send_neighbor_request(driver, blog_id, greeting, log):
                    total_requested += 1
                greeting_idx += 1
                time.sleep(2)  # 과도한 요청 방지

            log(f"\n완료. 총 {total_requested}건 신청했습니다.\n")
        finally:
            try:
                driver.quit()
            except Exception:
                pass
    except Exception as e:
        if log_cb:
            log_cb(f"오류 발생: {e}\n")


class AddFriendsGUI:
    # === 프리미엄 테마 (Auto_blog와 동일) ===
    BG_MAIN = "#F1F5F9"
    BG_CARD = "#FFFFFF"
    BG_HEADER = "#1E1B4B"
    ACCENT = "#6366F1"
    ACCENT_DARK = "#4F46E5"
    GREEN_BTN = "#10B981"
    GREEN_DARK = "#059669"
    ORANGE_BTN = "#F59E0B"
    RED_BTN = "#EF4444"
    SLATE_BTN = "#475569"
    TEXT_MAIN = "#0F172A"
    TEXT_MUTED = "#64748B"
    BORDER = "#E2E8F0"
    INPUT_BG = "#F8FAFC"
    LOG_BG = "#0F172A"
    LOG_FG = "#E2E8F0"

    def __init__(self):
        self.root = tk.Tk()
        self.root.title("네이버 서로이웃 자동화봇 v1.0.0")
        self.root.geometry("620x780")
        self.root.minsize(520, 650)
        self.root.configure(bg=self.BG_MAIN)

        self.stop_event = threading.Event()
        self.work_thread = None

        self.greetings = ["안녕하세요! 서로이웃 신청드립니다 :)"]
        self.search_terms = [{"keyword": "", "count": 30}]

        self._build_ui()
        self._load_config()

    def _section(self, parent, title):
        return tk.LabelFrame(
            parent,
            text=f"  {title}  ",
            fg=self.ACCENT,
            bg=self.BG_CARD,
            font=("Malgun Gothic", 10, "bold"),
            padx=14,
            pady=12,
            relief=tk.FLAT,
            highlightthickness=2,
            highlightbackground=self.BORDER,
        )

    def _build_ui(self):
        # 헤더
        header = tk.Frame(self.root, bg=self.BG_HEADER, pady=16, padx=20)
        header.pack(fill=tk.X)
        tk.Frame(header, height=3, bg=self.ACCENT).pack(fill=tk.X, pady=(0, 12))
        tk.Label(
            header,
            text="네이버 서로이웃 자동화봇  v1.0.0",
            fg="#F8FAFC",
            bg=self.BG_HEADER,
            font=("Malgun Gothic", 18, "bold"),
        ).pack(anchor=tk.W)
        tk.Label(
            header,
            text="검색어로 블로그를 찾아 서로이웃 신청을 자동으로 수행합니다",
            fg="#A5B4FC",
            bg=self.BG_HEADER,
            font=("Malgun Gothic", 10),
        ).pack(anchor=tk.W, pady=(6, 0))

        main = tk.Frame(self.root, bg=self.BG_MAIN, padx=20, pady=16)
        main.pack(fill=tk.BOTH, expand=True)

        _entry_kw = dict(
            font=("Malgun Gothic", 10),
            relief=tk.FLAT,
            highlightthickness=2,
            highlightbackground=self.BORDER,
            highlightcolor=self.ACCENT,
            bg=self.INPUT_BG,
            insertbackground=self.TEXT_MAIN,
        )
        chk_kw = dict(
            bg=self.BG_CARD,
            font=("Malgun Gothic", 9),
            activebackground=self.BG_CARD,
            selectcolor="#C7D2FE",
            fg=self.TEXT_MAIN,
            activeforeground=self.TEXT_MAIN,
        )

        # API 키
        api_frame = self._section(main, "API 키 (Gemini)")
        api_frame.pack(fill=tk.X, pady=(0, 10))
        api_row = tk.Frame(api_frame, bg=self.BG_CARD)
        api_row.pack(fill=tk.X)
        tk.Label(api_row, text="Gemini API 키", bg=self.BG_CARD, fg=self.TEXT_MUTED, font=("Malgun Gothic", 9)).pack(anchor=tk.W)
        self.entry_api = tk.Entry(api_row, width=40, show="•", **_entry_kw)
        self.entry_api.pack(fill=tk.X, pady=(4, 8), ipady=8)
        tk.Button(
            api_row,
            text="API키 발급 링크",
            fg="white",
            bg=self.ACCENT,
            activebackground=self.ACCENT_DARK,
            font=("Malgun Gothic", 9, "bold"),
            relief=tk.FLAT,
            padx=12,
            pady=6,
            cursor="hand2",
            command=lambda: self._open_url(GEMINI_API_URL),
        ).pack(anchor=tk.W)

        # 네이버 로그인
        login_frame = self._section(main, "네이버 로그인")
        login_frame.pack(fill=tk.X, pady=(0, 10))
        tk.Label(login_frame, text="아이디", bg=self.BG_CARD, fg=self.TEXT_MUTED, font=("Malgun Gothic", 9)).pack(anchor=tk.W)
        self.entry_id = tk.Entry(login_frame, width=40, **_entry_kw)
        self.entry_id.pack(fill=tk.X, pady=(4, 8), ipady=8)
        tk.Label(login_frame, text="비밀번호", bg=self.BG_CARD, fg=self.TEXT_MUTED, font=("Malgun Gothic", 9)).pack(anchor=tk.W)
        self.entry_pw = tk.Entry(login_frame, width=40, show="•", **_entry_kw)
        self.entry_pw.pack(fill=tk.X, pady=(4, 8), ipady=8)

        # 설정 저장
        save_frame = self._section(main, "설정 저장")
        save_frame.pack(fill=tk.X, pady=(0, 10))
        self.var_save_login = tk.BooleanVar(value=True)
        self.var_save_api = tk.BooleanVar(value=False)
        self.var_manual_login = tk.BooleanVar(value=False)
        tk.Checkbutton(save_frame, text="다음 실행 시 자동으로 로그인 정보 불러오기", variable=self.var_save_login, **chk_kw).pack(anchor=tk.W)
        tk.Checkbutton(save_frame, text="API 키도 함께 저장", variable=self.var_save_api, **chk_kw).pack(anchor=tk.W, pady=(2, 0))
        tk.Checkbutton(save_frame, text="수동 로그인 (캡차/자동감시문자 직접 처리)", variable=self.var_manual_login, **chk_kw).pack(anchor=tk.W, pady=(2, 0))

        # 검색 설정
        search_frame = self._section(main, "검색 설정")
        search_frame.pack(fill=tk.BOTH, expand=True, pady=(0, 10))

        greet_frame = tk.Frame(search_frame, bg=self.BG_CARD)
        greet_frame.pack(fill=tk.X, pady=(0, 8))
        tk.Label(greet_frame, text="서로이웃 인사말", bg=self.BG_CARD, fg=self.TEXT_MUTED, font=("Malgun Gothic", 9)).pack(anchor=tk.W)
        self.greet_tabs = []
        self.greet_content = tk.Text(
            greet_frame,
            height=3,
            wrap=tk.WORD,
            font=("Malgun Gothic", 9),
            bg=self.INPUT_BG,
            relief=tk.FLAT,
            padx=10,
            pady=8,
            highlightthickness=2,
            highlightbackground=self.BORDER,
        )
        self.greet_content.pack(fill=tk.X, pady=(4, 6))
        self.greet_content.insert("1.0", "안녕하세요! 서로이웃 신청드립니다 :)")
        btn_row = tk.Frame(greet_frame, bg=self.BG_CARD)
        btn_row.pack(fill=tk.X)
        _btn = dict(fg="white", relief=tk.FLAT, cursor="hand2", font=("Malgun Gothic", 9))
        tk.Button(btn_row, text="인사말 추가", bg=self.ACCENT, activebackground=self.ACCENT_DARK, padx=10, pady=4, command=self._add_greeting, **_btn).pack(side=tk.LEFT, padx=(0, 6))
        tk.Button(btn_row, text="AI 인사말 추천", bg=self.SLATE_BTN, activebackground="#334155", padx=10, pady=4, command=self._ai_greeting, **_btn).pack(side=tk.LEFT, padx=(0, 6))
        tk.Button(btn_row, text="현재 인사말 삭제", bg=self.RED_BTN, activebackground="#DC2626", padx=10, pady=4, command=self._del_greeting, **_btn).pack(side=tk.LEFT)

        terms_frame = tk.Frame(search_frame, bg=self.BG_CARD)
        terms_frame.pack(fill=tk.X, pady=(8, 0))
        tk.Label(terms_frame, text="검색어 1", bg=self.BG_CARD, fg=self.TEXT_MAIN, font=("Malgun Gothic", 9, "bold")).pack(anchor=tk.W)
        term_row = tk.Frame(terms_frame, bg=self.BG_CARD)
        term_row.pack(fill=tk.X, pady=(4, 6))
        tk.Label(term_row, text="검색어:", bg=self.BG_CARD, fg=self.TEXT_MUTED, font=("Malgun Gothic", 9)).pack(side=tk.LEFT, padx=(0, 8))
        self.entry_keyword = tk.Entry(term_row, width=22, **_entry_kw)
        self.entry_keyword.pack(side=tk.LEFT, padx=(0, 12), ipady=6)
        tk.Label(term_row, text="수집할 ID 수:", bg=self.BG_CARD, fg=self.TEXT_MUTED, font=("Malgun Gothic", 9)).pack(side=tk.LEFT, padx=(0, 6))
        self.spin_count = tk.Spinbox(
            term_row, from_=1, to=100, width=6,
            font=("Malgun Gothic", 10),
            relief=tk.FLAT, highlightthickness=2, highlightbackground=self.BORDER,
            bg=self.INPUT_BG,
        )
        self.spin_count.delete(0, tk.END)
        self.spin_count.insert(0, "30")
        self.spin_count.pack(side=tk.LEFT, ipady=4)
        term_btn_row = tk.Frame(terms_frame, bg=self.BG_CARD)
        term_btn_row.pack(fill=tk.X, pady=(4, 0))
        tk.Button(term_btn_row, text="검색어 추가", bg=self.ACCENT, activebackground=self.ACCENT_DARK, padx=10, pady=4, command=self._add_search_term, **_btn).pack(side=tk.LEFT, padx=(0, 6))
        tk.Button(term_btn_row, text="검색어 삭제", bg=self.SLATE_BTN, activebackground="#334155", padx=10, pady=4, command=self._del_search_term, **_btn).pack(side=tk.LEFT)

        # 버튼
        btn_frame = tk.Frame(main, bg=self.BG_MAIN)
        btn_frame.pack(fill=tk.X, pady=(14, 10))
        foot_btn = dict(relief=tk.FLAT, cursor="hand2", font=("Malgun Gothic", 10, "bold"), padx=16, pady=8, bd=0, activeforeground="white")
        self.btn_start = tk.Button(btn_frame, text="▶ 자동화 시작", fg="white", bg=self.GREEN_BTN, activebackground=self.GREEN_DARK, command=self._on_start, **foot_btn)
        self.btn_start.pack(side=tk.LEFT, padx=(0, 8))
        self.btn_stop = tk.Button(btn_frame, text="중지", fg="white", bg=self.ORANGE_BTN, activebackground="#D97706", command=self._on_stop, state=tk.DISABLED, **foot_btn)
        self.btn_stop.pack(side=tk.LEFT, padx=(0, 8))
        tk.Button(btn_frame, text="미수락 신청 취소", fg="white", bg=self.SLATE_BTN, activebackground="#334155", command=self._on_cancel, **foot_btn).pack(side=tk.LEFT)

        # 로그
        log_frame = self._section(main, "실행 로그")
        log_frame.pack(fill=tk.BOTH, expand=True, pady=(0, 0))
        self.log_text = scrolledtext.ScrolledText(
            log_frame,
            height=10,
            wrap=tk.WORD,
            state=tk.DISABLED,
            font=("Consolas", 9),
            bg=self.LOG_BG,
            fg=self.LOG_FG,
            insertbackground="white",
            relief=tk.FLAT,
            padx=12,
            pady=10,
        )
        self.log_text.pack(fill=tk.BOTH, expand=True)

    def _open_url(self, url):
        import webbrowser
        webbrowser.open(url)

    def _add_greeting(self):
        text = self.greet_content.get("1.0", tk.END).strip()
        if text and text not in self.greetings:
            self.greetings.append(text)
        self.greet_content.delete("1.0", tk.END)
        self.greet_content.insert("1.0", "새 인사말을 입력하세요")
        self.log("인사말을 추가했습니다.\n")

    def _del_greeting(self):
        text = self.greet_content.get("1.0", tk.END).strip()
        if text in self.greetings:
            self.greetings.remove(text)
        if not self.greetings:
            self.greetings = ["안녕하세요! 서로이웃 신청드립니다 :)"]
        self.greet_content.delete("1.0", tk.END)
        self.greet_content.insert("1.0", self.greetings[0])
        self.log("현재 인사말을 삭제했습니다.\n")

    def _ai_greeting(self):
        self.log("AI 인사말 추천은 Gemini API 연동 시 사용 가능합니다. (추후 업데이트)\n")

    def _add_search_term(self):
        self.search_terms.append({"keyword": "", "count": 30})
        self.log("검색어 슬롯을 추가했습니다.\n")

    def _del_search_term(self):
        if len(self.search_terms) > 1:
            self.search_terms.pop()
            self.log("검색어 슬롯을 삭제했습니다.\n")

    def _load_config(self):
        cfg = load_config()
        if cfg.get("save_login", True):
            self.entry_id.insert(0, cfg.get("naver_id", ""))
            self.entry_pw.insert(0, cfg.get("naver_pw", ""))
        if cfg.get("save_api", False):
            self.entry_api.insert(0, cfg.get("api_key", ""))

    def _save_config(self):
        data = {
            "save_login": self.var_save_login.get(),
            "save_api": self.var_save_api.get(),
        }
        if self.var_save_login.get():
            data["naver_id"] = self.entry_id.get().strip()
            data["naver_pw"] = self.entry_pw.get().strip()
        if self.var_save_api.get():
            data["api_key"] = self.entry_api.get().strip()
        save_config(data)

    def log(self, msg: str):
        self.log_text.configure(state=tk.NORMAL)
        self.log_text.insert(tk.END, msg)
        self.log_text.see(tk.END)
        self.log_text.configure(state=tk.DISABLED)

    def _on_start(self):
        naver_id = self.entry_id.get().strip()
        naver_pw = self.entry_pw.get().strip()
        api_key = self.entry_api.get().strip()
        if not naver_id or not naver_pw:
            messagebox.showwarning("입력 확인", "네이버 아이디와 비밀번호를 입력해 주세요.")
            return

        greeting_text = self.greet_content.get("1.0", tk.END).strip()
        if greeting_text and greeting_text not in self.greetings:
            self.greetings = [greeting_text] + [g for g in self.greetings if g != greeting_text]

        try:
            count = max(1, min(100, int(self.spin_count.get())))
        except ValueError:
            count = 30
        self.search_terms[0] = {"keyword": self.entry_keyword.get().strip(), "count": count}

        if not any(t.get("keyword") for t in self.search_terms):
            messagebox.showwarning("입력 확인", "검색어를 입력해 주세요.")
            return

        self._save_config()
        self.stop_event.clear()
        self.btn_start.configure(state=tk.DISABLED)
        self.btn_stop.configure(state=tk.NORMAL)
        self.log_text.configure(state=tk.NORMAL)
        self.log_text.delete(1.0, tk.END)
        self.log_text.configure(state=tk.DISABLED)

        def log_cb(msg):
            self.root.after(0, lambda: self.log(msg))

        def wait_for_manual_cb():
            ev = threading.Event()
            def show_dialog():
                messagebox.showinfo("수동 로그인", "브라우저에서 로그인(캡차/자동감시문자 포함)을 완료한 후\n확인을 눌러 주세요.")
                ev.set()
            self.root.after(0, show_dialog)
            ev.wait()

        self.work_thread = threading.Thread(
            target=run_workflow,
            args=(
                naver_id, naver_pw, api_key, self.greetings, self.search_terms,
                log_cb, self.stop_event, self.var_save_login.get(),
                self.var_manual_login.get(), wait_for_manual_cb,
            ),
            daemon=True,
        )
        self.work_thread.start()
        self._check_thread()

    def _check_thread(self):
        if self.work_thread and self.work_thread.is_alive():
            self.root.after(200, self._check_thread)
        else:
            self.btn_start.configure(state=tk.NORMAL)
            self.btn_stop.configure(state=tk.DISABLED)
            self.log("자동화가 종료되었습니다.\n")

    def _on_stop(self):
        self.stop_event.set()
        self.log("중지 요청했습니다...\n")

    def _on_cancel(self):
        self.log("미수락 신청 취소는 네이버 블로그 > 이웃 관리에서 수동으로 진행해 주세요.\n")

    def run(self):
        self.root.mainloop()


if __name__ == "__main__":
    app = AddFriendsGUI()
    app.run()
