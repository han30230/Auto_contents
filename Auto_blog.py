import json
import os
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

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


def generate_hot_issue_post(client: OpenAI, topic: str | None = None) -> tuple[str, str]:
    """
    GPT로 '최신 핫이슈' 블로그 글을 생성합니다.

    topic 이 None이면 GPT가 스스로 한국 최신 이슈 중 하나를 선택해서 작성하도록 프롬프트합니다.
    반환값: (title, content)
    """
    if not topic:
        topic_instruction = (
            "한국에서 최근 1~3일 내에 화제가 될 만한 이슈를 하나 선택해서,"
            " 그 이슈를 주제로 글을 작성해 주세요. (이슈 이름은 글 안에서 자연스럽게 언급)"
        )
    else:
        topic_instruction = f"주제는 반드시 다음을 중심으로 작성해 주세요: '{topic}'."

    system_msg = (
        "당신은 네이버 블로그용 글을 작성하는 한국어 콘텐츠 마케터입니다. "
        "검색에 잘 노출되도록 제목을 자극적이지 않으면서도 클릭을 부르는 스타일로 작성하고, "
        "본문은 말투는 친근하지만 정보는 정확하게, 소제목과 목록을 적절히 활용해 주세요."
    )

    user_msg = (
        f"{topic_instruction}\n\n"
        "아래 형식으로만 답변해 주세요.\n\n"
        "===TITLE===\n"
        "여기에 블로그 글 제목\n"
        "===CONTENT===\n"
        "여기에 본문 전체 (네이버 블로그 에디터에 바로 붙여넣을 수 있는 형식, 마크다운 X, HTML X)"
    )

    completion = client.chat.completions.create(
        model="gpt-4.1-mini",
        messages=[
            {"role": "system", "content": system_msg},
            {"role": "user", "content": user_msg},
        ],
        temperature=0.8,
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
        # 혹시 형식을 안지킨 경우 전체를 본문으로 사용하고 제목은 첫 줄
        lines = [line for line in text.splitlines() if line.strip()]
        if lines:
            title = lines[0].strip()
            content = "\n".join(lines[1:]).strip()
        else:
            title = "자동 생성된 블로그 글"
            content = text.strip()

    return title, content


def create_driver() -> webdriver.Chrome:
    """
    Chrome WebDriver를 생성합니다.

    - 크롬이 설치되어 있어야 합니다.
    - webdriver_manager가 자동으로 드라이버를 내려받습니다.
    """
    chrome_options = webdriver.ChromeOptions()
    # 필요에 따라 아래 옵션 조절 (백그라운드 실행 원하면 headless 등)
    # chrome_options.add_argument("--headless=new")
    chrome_options.add_argument("--start-maximized")

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

    manual=True 이면 로그인 페이지만 열고, 사용자가 직접 로그인(캡차 등 처리) 후
    manual_login_event.set() 또는 콘솔 Enter로 다음 단계 진행.

    manual=False 이면 ID/PW를 자동 입력합니다.
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
    # 네이버가 send_keys를 감지해 차단할 수 있어, JS로 값 설정하는 방식 추가
    try:
        driver.execute_script("arguments[0].value = arguments[1];", id_input, cfg.naver_id)
        driver.execute_script("arguments[0].value = arguments[1];", pw_input, cfg.naver_pw)
        pw_input.send_keys(Keys.RETURN)
    except Exception:
        id_input.send_keys(cfg.naver_id)
        pw_input.send_keys(cfg.naver_pw)
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
    ActionChains를 사용해 한 글자씩 interval 간격으로 입력합니다.
    element.send_keys()는 사용하지 않습니다.
    click_first=False 이면 클릭 없이 바로 타이핑만 수행합니다.
    """
    if click_first and element:
        ActionChains(driver).click(element).perform()
    for char in text:
        ActionChains(driver).send_keys(char).perform()
        time.sleep(interval)


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

    # 3. 제목 입력 (.se-section-documentTitle 클릭 후 ActionChains로 타이핑)
    title_area = wait.until(EC.element_to_be_clickable((By.CSS_SELECTOR, ".se-section-documentTitle")))
    _type_with_action_chains(driver, title_area, title or "", interval=0.03)

    # 4. 본문 입력 (.se-section-text 클릭 후 GPT 생성 내용을 줄바꿈 포함 입력)
    body_area = wait.until(EC.element_to_be_clickable((By.CSS_SELECTOR, ".se-section-text")))
    body_lines = (content or "").split("\n") if content else [""]

    for i, line in enumerate(body_lines):
        _type_with_action_chains(
            driver, body_area, line, interval=0.03, click_first=(i == 0)
        )
        if i < len(body_lines) - 1:
            ActionChains(driver).send_keys(Keys.ENTER).perform()
            time.sleep(0.03)

    time.sleep(0.5)

    # 5. 저장 또는 발행 버튼 클릭
    driver.switch_to.default_content()
    do_publish = action.strip().lower() == "publish"

    if do_publish:
        publish_selectors = [
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
        if btn:
            btn.click()
            print("발행 버튼을 클릭했습니다.")
        else:
            raise RuntimeError("발행 버튼을 찾지 못했습니다. F12로 selector를 확인해 주세요.")
    else:
        try:
            save_btn = wait.until(EC.element_to_be_clickable((By.ID, "save_btn_bcz58")))
        except Exception:
            save_btn = wait.until(EC.element_to_be_clickable((By.CSS_SELECTOR, ".save_btn_bcz58")))
        save_btn.click()
        print("저장 버튼을 클릭했습니다.")

    time.sleep(5)


def run_blog_workflow(
    cfg: NaverConfig,
    topic: str | None,
    log_fn: Callable[[str], None] | None = None,
    manual_login_event: threading.Event | None = None,
) -> tuple[webdriver.Chrome | None, str | None]:
    """
    블로그 글 생성 ~ 저장/발행까지 실행합니다.
    log_fn: 로그 메시지 콜백 (GUI용).
    manual_login_event: 수동 로그인 시 이벤트가 set될 때까지 대기.
    반환: (driver, None) 성공 시 브라우저는 닫지 않고 반환. (None, error_msg) 실패 시.
    """
    def log(msg: str) -> None:
        if log_fn:
            log_fn(msg)
        else:
            print(msg)

    try:
        client = create_openai_client(cfg.openai_api_key)
        topic_trimmed = topic.strip() if topic else None

        log("GPT로 블로그 글을 생성 중입니다...")
        title, content = generate_hot_issue_post(client, topic=topic_trimmed)
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

            log("작업이 완료되었습니다. 브라우저를 닫으려면 '브라우저 닫기' 버튼을 누르세요.")
            return (driver, None)
        except Exception as e:
            try:
                driver.quit()
            except Exception:
                pass
            raise
    except Exception as e:
        return (None, str(e))


def main():
    """
    네이버 블로그 핫이슈 글 자동 생성 + 발행 (콘솔 진입점).
    """
    cfg = load_config()

    def on_log(msg: str) -> None:
        print(msg)

    driver, err = run_blog_workflow(cfg, topic=None, log_fn=on_log, manual_login_event=None)
    if err:
        print("오류:", err)
        return
    if driver:
        input("브라우저를 닫으려면 Enter 키를 누르세요...")
        try:
            driver.quit()
        except Exception:
            pass


# ---------------------------- GUI ----------------------------

def _gui_config_path() -> Path:
    return Path(__file__).resolve().parent / "gui_config.json"


def load_gui_config() -> dict:
    path = _gui_config_path()
    if path.exists():
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def save_gui_config(data: dict) -> None:
    with open(_gui_config_path(), "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def main_gui():
    import tkinter as tk
    from tkinter import ttk, scrolledtext, messagebox

    root = tk.Tk()
    root.title("네이버 블로그 자동 글쓰기")
    root.geometry("620x580")
    root.minsize(500, 450)

    # 변수
    var_naver_id = tk.StringVar()
    var_naver_pw = tk.StringVar()
    var_api_key = tk.StringVar()
    var_manual = tk.BooleanVar(value=False)
    var_action = tk.StringVar(value="save")
    var_topic = tk.StringVar()

    cfg = load_gui_config()
    var_naver_id.set(cfg.get("naver_id", ""))
    var_naver_pw.set(cfg.get("naver_pw", ""))
    var_api_key.set(cfg.get("api_key", ""))
    var_manual.set(cfg.get("manual_login", False))
    var_action.set("publish" if cfg.get("blog_action") == "publish" else "save")
    var_topic.set(cfg.get("topic", ""))

    current_driver: webdriver.Chrome | None = None
    manual_login_event: threading.Event | None = None
    run_thread: threading.Thread | None = None

    # 로그 영역 (스크롤 텍스트)
    log_frame = ttk.LabelFrame(root, text="로그", padding=6)
    log_frame.pack(fill=tk.BOTH, expand=True, padx=8, pady=4)
    log_text = scrolledtext.ScrolledText(log_frame, height=12, state=tk.DISABLED, wrap=tk.WORD, font=("Consolas", 9))
    log_text.pack(fill=tk.BOTH, expand=True)

    def log_msg(msg: str) -> None:
        def _():
            log_text.configure(state=tk.NORMAL)
            log_text.insert(tk.END, msg + "\n")
            log_text.see(tk.END)
            log_text.configure(state=tk.DISABLED)
        root.after(0, _)

    # 설정 프레임
    set_frame = ttk.LabelFrame(root, text="설정", padding=8)
    set_frame.pack(fill=tk.X, padx=8, pady=4)

    ttk.Label(set_frame, text="네이버 ID").grid(row=0, column=0, sticky=tk.W, pady=2)
    ttk.Entry(set_frame, textvariable=var_naver_id, width=28).grid(row=0, column=1, sticky=tk.EW, padx=4, pady=2)
    ttk.Label(set_frame, text="네이버 비밀번호").grid(row=1, column=0, sticky=tk.W, pady=2)
    ttk.Entry(set_frame, textvariable=var_naver_pw, show="*", width=28).grid(row=1, column=1, sticky=tk.EW, padx=4, pady=2)
    ttk.Label(set_frame, text="OpenAI API Key").grid(row=2, column=0, sticky=tk.W, pady=2)
    ttk.Entry(set_frame, textvariable=var_api_key, show="*", width=28).grid(row=2, column=1, sticky=tk.EW, padx=4, pady=2)
    ttk.Label(set_frame, text="주제 (비우면 자동)").grid(row=3, column=0, sticky=tk.W, pady=2)
    ttk.Entry(set_frame, textvariable=var_topic, width=28).grid(row=3, column=1, sticky=tk.EW, padx=4, pady=2)

    ttk.Checkbutton(set_frame, text="수동 로그인 (캡차/2단계 시)", variable=var_manual).grid(row=4, column=0, columnspan=2, sticky=tk.W, pady=4)
    act_frame = ttk.Frame(set_frame)
    act_frame.grid(row=5, column=0, columnspan=2, sticky=tk.W, pady=2)
    ttk.Radiobutton(act_frame, text="저장", variable=var_action, value="save").pack(side=tk.LEFT, padx=(0, 12))
    ttk.Radiobutton(act_frame, text="발행", variable=var_action, value="publish").pack(side=tk.LEFT)

    set_frame.columnconfigure(1, weight=1)

    # 버튼: 로그인 완료 (수동 로그인 시), 브라우저 닫기, 실행, 설정 저장
    btn_frame = ttk.Frame(root)
    btn_frame.pack(fill=tk.X, padx=8, pady=6)

    btn_manual_ok = ttk.Button(btn_frame, text="로그인 완료", state=tk.DISABLED)
    btn_manual_ok.pack(side=tk.LEFT, padx=(0, 8))

    def do_manual_ok() -> None:
        if manual_login_event:
            manual_login_event.set()
        btn_manual_ok.configure(state=tk.DISABLED)

    btn_manual_ok.configure(command=do_manual_ok)

    btn_close_browser = ttk.Button(btn_frame, text="브라우저 닫기", state=tk.DISABLED)

    def do_close_browser() -> None:
        nonlocal current_driver
        if current_driver:
            try:
                current_driver.quit()
            except Exception:
                pass
            current_driver = None
        btn_close_browser.configure(state=tk.DISABLED)
        log_msg("브라우저를 닫았습니다.")

    btn_close_browser.configure(command=do_close_browser)
    btn_close_browser.pack(side=tk.LEFT, padx=(0, 8))

    def save_config_click() -> None:
        save_gui_config({
            "naver_id": var_naver_id.get().strip(),
            "naver_pw": var_naver_pw.get(),
            "api_key": var_api_key.get(),
            "manual_login": var_manual.get(),
            "blog_action": var_action.get(),
            "topic": var_topic.get().strip(),
        })
        messagebox.showinfo("저장", "설정을 저장했습니다.")

    ttk.Button(btn_frame, text="설정 저장", command=save_config_click).pack(side=tk.LEFT, padx=(0, 8))

    def run_click() -> None:
        nonlocal current_driver, manual_login_event, run_thread

        naver_id = var_naver_id.get().strip()
        naver_pw = var_naver_pw.get()
        api_key = var_api_key.get().strip()
        if not naver_id or not naver_pw or not api_key:
            messagebox.showwarning("입력 오류", "네이버 ID, 비밀번호, OpenAI API Key를 모두 입력하세요.")
            return

        cfg = NaverConfig(
            naver_id=naver_id,
            naver_pw=naver_pw,
            openai_api_key=api_key,
            manual_login=var_manual.get(),
            blog_action=var_action.get(),
        )
        topic = var_topic.get().strip() or None
        manual_login_event = threading.Event()
        if cfg.manual_login:
            btn_manual_ok.configure(state=tk.NORMAL)

        def run() -> None:
            nonlocal current_driver
            driver, err = run_blog_workflow(
                cfg, topic, log_fn=log_msg, manual_login_event=manual_login_event
            )
            def after_run() -> None:
                nonlocal current_driver
                if err:
                    messagebox.showerror("오류", err)
                    return
                current_driver = driver
                if driver:
                    btn_close_browser.configure(state=tk.NORMAL)
            root.after(0, after_run)

        run_thread = threading.Thread(target=run, daemon=True)
        run_thread.start()
        log_msg("실행을 시작합니다...")

    ttk.Button(btn_frame, text="실행", command=run_click).pack(side=tk.LEFT)

    root.mainloop()


if __name__ == "__main__":
    main_gui()

