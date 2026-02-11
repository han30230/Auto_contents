# 네이버 블로그 자동 글쓰기 (GPT + Selenium)

이 프로젝트는 OpenAI GPT API를 사용해 **최신 핫이슈 블로그 글을 자동 생성**하고,  
Selenium을 이용해 **네이버에 로그인 → 블로그 글쓰기 → 제목/본문 입력 → 발행 버튼 클릭**까지 자동으로 수행합니다.

> ⚠️ 네이버 약관, 로봇/자동화 관련 정책을 반드시 확인하고 **본인 계정에 한해서** 사용하세요.  
> 캡챠/2단계 인증이 뜨면 100% 자동화는 불가능하며, 중간에 사람이 직접 인증을 해줘야 할 수 있습니다.

---

## 1. 준비사항

- Windows 10 이상 (현재 PC 환경)
- Python 3.10+ 설치
- 크롬 브라우저 설치
- OpenAI API 키 발급 (`https://platform.openai.com`)

---

## 2. 라이브러리 설치

프로젝트 폴더에서 아래 명령 실행:

```bash
pip install -r requirements.txt
```

설치되는 라이브러리:

- `selenium` – 브라우저 자동 제어
- `webdriver-manager` – 크롬 드라이버 자동 설치
- `python-dotenv` – `.env` 파일에서 환경변수 로드
- `openai` – GPT API 호출

---

## 3. 환경변수(.env) 설정

프로젝트 루트(`Auto_blog` 폴더)에 `.env` 파일을 만들고 아래 내용 채워 넣기:

```env
NAVER_ID=네이버아이디
NAVER_PW=네이버비밀번호
OPENAI_API_KEY=발급받은_OpenAI_API_키
```

- **수동 로그인**(캡차·2단계 인증 시): 같은 파일에 아래 추가 후 실행하면, 로그인 페이지만 열고 사용자가 직접 로그인한 뒤 Enter를 누르면 글쓰기·발행으로 진행합니다.
  ```env
  MANUAL_LOGIN=1
  ```

- `NAVER_ID` / `NAVER_PW` 는 **개인 계정 정보**이므로, 절대 깃허브 등에 올리지 마세요.
- `OPENAI_API_KEY` 역시 외부에 공유 금지.

---

## 4. 실행 방법

프로젝트 폴더에서:

```bash
python Auto_blog.py
```

흐름:

1. GPT가 한국 최신 핫이슈(또는 직접 지정한 주제)를 기반으로 **제목 + 본문** 생성
2. 크롬 브라우저 자동 실행
3. 네이버 로그인 (자동 ID/PW 입력 또는 `MANUAL_LOGIN=1` 시 수동 로그인 후 Enter)
4. 블로그 글쓰기 페이지 열기 (직접 URL 시도 후, 필요 시 '글쓰기' 버튼 클릭)
5. 제목/본문 자동 입력
6. **발행 버튼 클릭**까지 자동 수행
7. 완료 후 브라우저 확인, 콘솔에서 Enter 시 브라우저 종료

> 참고: 네이버가 캡챠 또는 추가 인증을 요구하면, 그 부분은 사용자가 직접 해결해야 합니다.  
> 인증 완료 후 브라우저가 열린 상태에서 스크립트를 다시 실행하거나, 코드에 일시 중지(`input()`)를 넣어 수동으로 이어갈 수 있습니다.

---

## 5. 주제(이슈) 직접 지정하고 싶을 때

`Auto_blog.py` 의 `main()` 함수에서:

```python
# topic = "원하는 이슈 직접 지정"
topic = None
```

부분을 예를 들어 다음과 같이 바꾸면 됩니다:

```python
topic = "테슬라 주가 급등 이슈"
```

그러면 GPT가 해당 이슈를 중심으로 블로그 글을 작성합니다.

---

## 6. EXE(실행 파일)로 만들기 (PyInstaller)

PyInstaller 설치:

```bash
pip install pyinstaller
```

실행 파일 생성:

```bash
pyinstaller --onefile --name AutoBlog Auto_blog.py
```

빌드가 끝나면:

- `dist/AutoBlog.exe` 파일이 생성됩니다.
- 이 파일을 더블 클릭하면 콘솔 창이 열리고, 파이썬 없이도 동일한 자동화가 실행됩니다.

> 첫 실행 시 크롬 드라이버 다운로드 등으로 시간이 조금 걸릴 수 있습니다.

---

## 7. 자주 수정하게 될 부분

- **모델 이름 변경**  
  `Auto_blog.py` 안의 `generate_hot_issue_post()` 에서:

  ```python
  completion = client.chat.completions.create(
      model="gpt-4.1-mini",
      ...
  )
  ```

  부분을 사용 중인 모델 이름으로 교체하면 됩니다.

- **블로그 주소가 NAVER_ID 와 다른 경우**  
  `main()` 안에서:

  ```python
  blog_id = cfg.naver_id
  ```

  부분을 실제 블로그 주소의 아이디로 변경:

  ```python
  blog_id = "실제_블로그_아이디"
  ```

- **네이버 에디터 구조 변경 시**  
  제목 input, 본문 contenteditable, 발행 버튼의 **CSS Selector** 가 바뀌면  
  `fill_post_and_publish()` 함수 안의 selector 를 실제 DOM 구조에 맞게 수정해야 합니다.  
  (크롬 개발자도구(F12)로 요소를 찍어서 id/class 등을 확인 후 수정)

---

## 8. 주의 사항

- 네이버의 약관, 봇/자동화 정책을 위반하는 용도로 사용하면 안 됩니다.
- 너무 잦은 자동 포스팅은 계정 제한 위험이 있으니, **빈도 조절**을 권장합니다.
- 본 스크립트는 교육/개인용 예제로 제공되며, 실제 운영 환경에 적용 시에는  
  충분한 테스트와 정책 검토가 필요합니다.

