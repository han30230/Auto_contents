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
from tkinter import ttk, messagebox, filedialog, scrolledtext

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
    from webdriver_manager.chrome import ChromeDriverManager
except ImportError:
    webdriver = None
    ActionChains = None

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
    chrome_options = webdriver.ChromeOptions()
    chrome_options.add_argument("--start-maximized")
    service = Service(ChromeDriverManager().install())
    return webdriver.Chrome(service=service, options=chrome_options)


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
    """저장된 세션(쿠키) 로드 후 로그인 여부 확인"""
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
        try:
            driver.find_element(By.CSS_SELECTOR, "a[href*='nidlogout'], .my_nickname, #account")
            return True
        except Exception:
            return False
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
    """네이버 로그인 (수동/자동 + 세션 저장/로드)"""
    # 수동 로그인일 땐 저장된 세션 사용 안 함 (항상 로그인 페이지에서 대기)
    if not manual_login and use_saved_session and _try_load_session(driver):
        if log_cb:
            log_cb("저장된 세션으로 로그인했습니다.\n")
        return

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


def _ensure_main_window(driver):
    """메인 창으로 복귀 (팝업이 닫힌 경우 대비)"""
    try:
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


def _find_add_btn(driver):
    """이웃추가 버튼 검색 (메인 + 모든 iframe). 찾으면 (버튼, 프레임여부) 반환. 프레임 내 발견 시 해당 프레임에 머무름."""
    def search(ctx):
        for sel in ["a.btn_add_nb", "a._addBuddyPop", "a[class*='btn_add_nb']", "a[class*='addBuddyPop']"]:
            try:
                for el in ctx.find_elements(By.CSS_SELECTOR, sel):
                    if el.is_displayed() and el.is_enabled():
                        return el
            except Exception:
                pass
        try:
            for el in ctx.find_elements(By.XPATH, "//a[.//span[contains(@class,'blind') and contains(.,'이웃추가')]]"):
                if el.is_displayed():
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
            driver.get(url)
            time.sleep(2)
            if _is_error_page(driver):
                continue
            add_btn, in_frame = _find_add_btn(driver)
            if add_btn:
                break
            driver.switch_to.default_content()

        if not add_btn:
            if log_cb:
                log_cb(f"  [{blog_id}] 이웃추가 버튼을 찾지 못함 (페이지 없음/이미 이웃/구조 변경)\n")
            return False

        # in_frame이 True면 이미 해당 iframe에 있음
        add_btn.click()
        time.sleep(2)
        driver.switch_to.default_content()

        # 새 창/탭(BuddyAdd)으로 전환 (열렸을 경우)
        handles = driver.window_handles
        if len(handles) > 1:
            driver.switch_to.window(handles[-1])
        time.sleep(1)

        # 알림창 처리 (이웃 5000명 초과 등)
        if _dismiss_alert(driver):
            if log_cb:
                log_cb(f"  [{blog_id}] 알림으로 인해 스킵\n")
            if len(handles) > 1:
                try:
                    driver.close()
                    driver.switch_to.window(handles[0])
                except Exception:
                    _ensure_main_window(driver)
            return False

        # 1. 서로이웃 라디오 체크 (이웃 X, 서로이웃 O)
        try:
            clicked = False
            for el in driver.find_elements(By.XPATH, "//label[contains(., '서로이웃')] | //*[contains(text(), '서로이웃') and not(contains(text(), '이웃과 서로이웃'))]"):
                if el.is_displayed() and "이웃과" not in (el.text or ""):
                    el.click()
                    clicked = True
                    break
            if not clicked:
                radios = driver.find_elements(By.CSS_SELECTOR, "input[type='radio']")
                if len(radios) >= 2:
                    radios[1].click()
        except Exception:
            if _dismiss_alert(driver):
                pass
        time.sleep(0.5)

        # 2. 다음 버튼 클릭
        def click_next():
            if _dismiss_alert(driver):
                return False
            for btn in driver.find_elements(By.XPATH, "//button[contains(., '다음')] | //a[contains(., '다음')] | //span[contains(., '다음')]/ancestor::button | //*[@class and contains(@class,'btn') and contains(., '다음')]"):
                txt = (btn.text or "").strip()
                if btn.is_displayed() and "다음" in txt and "취소" not in txt:
                    try:
                        btn.click()
                        return True
                    except Exception:
                        if _dismiss_alert(driver):
                            pass
                        break
            return False

        if click_next():
            time.sleep(2)
        if _dismiss_alert(driver):
            if log_cb:
                log_cb(f"  [{blog_id}] 알림으로 인해 스킵\n")
            if len(handles) > 1:
                try:
                    driver.close()
                except Exception:
                    pass
            _ensure_main_window(driver)
            return False

        # 3. 다음 버튼 클릭 (두 번째)
        if click_next():
            time.sleep(1)

        # 새 창이었으면 닫고 메인 창으로 복귀
        if len(handles) > 1:
            try:
                driver.close()
                driver.switch_to.window(handles[0])
            except Exception:
                _ensure_main_window(driver)

        if log_cb:
            log_cb(f"  [{blog_id}] 서로이웃 신청 완료\n")
        return True
    except Exception as e:
        err_msg = str(e).split("\n")[0] if "\n" in str(e) else str(e)
        try:
            _dismiss_alert(driver)
            _ensure_main_window(driver)
        except Exception:
            pass
        if "invalid session" in err_msg.lower():
            err_msg = "브라우저 종료됨"
        elif "no such window" in err_msg.lower() or "target window already closed" in err_msg.lower():
            err_msg = "창이 닫혀 스킵"
        elif "alert" in err_msg.lower():
            _dismiss_alert(driver)
            err_msg = "알림으로 스킵"
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
                count = int(term_data.get("count", 30))
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
                if send_neighbor_request(driver, blog_id, greeting, log_cb):
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
    def __init__(self):
        self.root = tk.Tk()
        self.root.title("네이버 서로이웃 자동화봇 v1.0.0")
        self.root.geometry("600x720")
        self.root.minsize(500, 600)

        self.stop_event = threading.Event()
        self.work_thread = None

        self.greetings = ["안녕하세요! 서로이웃 신청드립니다 :)"]
        self.search_terms = [{"keyword": "", "count": 30}]

        self._build_ui()
        self._load_config()

    def _build_ui(self):
        main = ttk.Frame(self.root, padding=15)
        main.pack(fill=tk.BOTH, expand=True)

        ttk.Label(main, text="네이버 서로이웃 자동화봇 v1.0.0", font=("", 14, "bold")).pack(pady=(0, 5))
        ttk.Label(
            main,
            text="안녕하세요. 네이버 서로이웃 자동화 프로그램입니다.\n로그인 후 시작 버튼을 클릭하면 자동으로 검색 결과의 블로그에 서로이웃을 신청합니다.",
            foreground="gray",
        ).pack(pady=(0, 15))

        # API 키 (Gemini - AI 인사말용, 선택)
        api_frame = ttk.LabelFrame(main, text="API 키 입력 (Gemini)", padding=10)
        api_frame.pack(fill=tk.X, pady=5)
        api_row = ttk.Frame(api_frame)
        api_row.pack(fill=tk.X)
        ttk.Label(api_row, text="Gemini API 키:").pack(side=tk.LEFT)
        self.entry_api = ttk.Entry(api_row, width=35, show="•")
        self.entry_api.pack(side=tk.LEFT, padx=5, fill=tk.X, expand=True)
        ttk.Button(api_row, text="API키 발급 링크", command=lambda: self._open_url(GEMINI_API_URL)).pack(side=tk.LEFT, padx=2)

        # 네이버 로그인
        login_frame = ttk.LabelFrame(main, text="네이버 로그인", padding=10)
        login_frame.pack(fill=tk.X, pady=5)
        ttk.Label(login_frame, text="아이디:").grid(row=0, column=0, sticky=tk.W, pady=2)
        self.entry_id = ttk.Entry(login_frame, width=35)
        self.entry_id.grid(row=0, column=1, padx=5, pady=2, sticky=tk.EW)
        ttk.Label(login_frame, text="비밀번호:").grid(row=1, column=0, sticky=tk.W, pady=2)
        self.entry_pw = ttk.Entry(login_frame, width=35, show="•")
        self.entry_pw.grid(row=1, column=1, padx=5, pady=2, sticky=tk.EW)
        login_frame.columnconfigure(1, weight=1)

        # 설정 저장
        save_frame = ttk.LabelFrame(main, text="설정 저장", padding=10)
        save_frame.pack(fill=tk.X, pady=5)
        self.var_save_login = tk.BooleanVar(value=True)
        self.var_save_api = tk.BooleanVar(value=False)
        self.var_manual_login = tk.BooleanVar(value=False)
        ttk.Checkbutton(save_frame, text="다음 실행 시 자동으로 로그인 정보 불러오기", variable=self.var_save_login).pack(anchor=tk.W)
        ttk.Checkbutton(save_frame, text="API 키도 함께 저장", variable=self.var_save_api).pack(anchor=tk.W)
        ttk.Checkbutton(save_frame, text="수동 로그인 (캡차/자동감시문자 직접 처리)", variable=self.var_manual_login).pack(anchor=tk.W)

        # 검색 설정
        search_frame = ttk.LabelFrame(main, text="검색 설정", padding=10)
        search_frame.pack(fill=tk.BOTH, expand=True, pady=5)

        # 인사말
        greet_frame = ttk.Frame(search_frame)
        greet_frame.pack(fill=tk.X, pady=5)
        ttk.Label(greet_frame, text="서로이웃 인사말:").pack(anchor=tk.W)
        self.greet_tabs = []
        self.greet_content = tk.Text(greet_frame, height=3, width=50)
        self.greet_content.pack(fill=tk.X, pady=2)
        self.greet_content.insert("1.0", "안녕하세요! 서로이웃 신청드립니다 :)")
        btn_row = ttk.Frame(greet_frame)
        btn_row.pack(fill=tk.X)
        ttk.Button(btn_row, text="인사말 추가", command=self._add_greeting).pack(side=tk.LEFT, padx=(0, 5))
        ttk.Button(btn_row, text="AI 인사말 추천", command=self._ai_greeting).pack(side=tk.LEFT, padx=5)
        ttk.Button(btn_row, text="현재 인사말 삭제", command=self._del_greeting).pack(side=tk.LEFT, padx=5)

        # 검색어
        terms_frame = ttk.Frame(search_frame)
        terms_frame.pack(fill=tk.X, pady=5)
        ttk.Label(terms_frame, text="검색어 1").pack(anchor=tk.W)
        term_row = ttk.Frame(terms_frame)
        term_row.pack(fill=tk.X)
        ttk.Label(term_row, text="검색어:").pack(side=tk.LEFT)
        self.entry_keyword = ttk.Entry(term_row, width=25)
        self.entry_keyword.pack(side=tk.LEFT, padx=5)
        ttk.Label(term_row, text="수집할 ID 수:").pack(side=tk.LEFT, padx=(10, 0))
        self.spin_count = ttk.Spinbox(term_row, from_=1, to=100, width=5)
        self.spin_count.delete(0, tk.END)
        self.spin_count.insert(0, "30")
        self.spin_count.pack(side=tk.LEFT, padx=5)
        term_btn_row = ttk.Frame(terms_frame)
        term_btn_row.pack(fill=tk.X, pady=2)
        ttk.Button(term_btn_row, text="검색어 추가", command=self._add_search_term).pack(side=tk.LEFT, padx=(0, 5))
        ttk.Button(term_btn_row, text="검색어 삭제", command=self._del_search_term).pack(side=tk.LEFT)

        # 버튼
        btn_frame = ttk.Frame(main)
        btn_frame.pack(fill=tk.X, pady=(10, 5))
        self.btn_start = ttk.Button(btn_frame, text="자동화 시작", command=self._on_start)
        self.btn_start.pack(side=tk.LEFT, padx=(0, 10))
        self.btn_stop = ttk.Button(btn_frame, text="중지", command=self._on_stop, state=tk.DISABLED)
        self.btn_stop.pack(side=tk.LEFT, padx=(0, 10))
        self.btn_cancel = ttk.Button(btn_frame, text="미수락 신청 취소", command=self._on_cancel)
        self.btn_cancel.pack(side=tk.LEFT)

        # 로그
        log_frame = ttk.LabelFrame(main, text="실행 로그", padding=5)
        log_frame.pack(fill=tk.BOTH, expand=True, pady=(0, 10))
        self.log_text = scrolledtext.ScrolledText(log_frame, height=8, wrap=tk.WORD, state=tk.DISABLED)
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
            count = int(self.spin_count.get())
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
            self.log("프로그램 종료\n")

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
