# 싱가포르 의약품 수출 적합성 분석 시스템

> **대상 독자:** 코딩을 몰라도 사용할 수 있도록 작성되었습니다.

---

## 이 프로그램은 무엇인가요?

싱가포르에 수출하려는 **8가지 의약품**이 현지 시장에서 팔릴 수 있는지를 자동으로 분석해 주는 도구입니다.

직접 사이트를 돌아다니며 가격을 찾거나, 규제 서류를 읽을 필요 없이 **버튼 한 번**으로 아래 세 가지를 자동으로 처리합니다.

1. **크롤링** — 싱가포르 정부·약국 사이트에서 가격과 등재 정보를 자동 수집
2. **AI 분석** — Claude AI가 수집된 데이터를 바탕으로 수출 적합성을 판단
3. **보고서 생성** — 분석 결과를 PDF 보고서로 자동 생성

---

## 분석 대상 품목 (8가지)

| 품목 | 성분 |
|------|------|
| Hydrine | 하이드록시우레아 500mg |
| Gadvoa | 가도부트롤 604mg |
| Sereterol/Activair | 플루티카손+살메테롤 (흡입제) |
| Omethyl | 오메가-3 지방산 2g |
| Rosumeg Combigel | 로수바스타틴+오메가-3 |
| Atmeg Combigel | 아토르바스타틴+오메가-3 |
| Ciloduo | 실로스타졸+로수바스타틴 |
| Gastiin CR | 모사프리드 |

---

## 시작 전 준비사항

### 1. Python 설치 확인

터미널(검은 창)을 열고 아래 명령어를 입력합니다.

```
python3 --version
```

`Python 3.10` 이상이 나오면 됩니다. 없으면 [python.org](https://www.python.org/downloads/)에서 다운로드하세요.

---

### 2. 프로그램 설치 (최초 1회만)

터미널에서 이 프로그램 폴더로 이동한 뒤 아래를 순서대로 실행합니다.

```bash
# 가상환경 만들기
python3 -m venv .venv

# 필요한 패키지 설치
.venv/bin/pip install -r requirements.txt

# AI 분석 기능 사용 시 추가 설치
.venv/bin/pip install anthropic

# 브라우저 자동화(실크롤) 사용 시 추가 설치
.venv/bin/pip install playwright playwright-stealth
.venv/bin/playwright install chromium
```

> **중요:** `playwright-stealth`는 브라우저 자동화 감지(봇 탐지)를 우회하는 데 필요합니다.
> 설치하지 않아도 동작하지만, GeBIZ·Guardian·Watsons 크롤 시 차단될 가능성이 높아집니다.

---

### 3. API 키 설정 (필수)

루트 폴더(이 README가 있는 폴더)에 `.env` 파일을 만들고 아래처럼 입력합니다.

```
# AI 분석
ANTHROPIC_API_KEY=여기에_Claude_API_키_입력
PERPLEXITY_API_KEY=여기에_Perplexity_API_키_입력

# 실크롤 활성화 (1로 설정 시 실제 브라우저/HTTP 요청 실행)
PLAYWRIGHT_LIVE=1
GEBIZ_LIVE=1

# 크롤 우회 에스컬레이션 (선택, §8 순서)
# 1단계: Jina AI Reader — 무료 10M 토큰/월 (키 없이도 동작)
JINA_API_KEY=
# 2단계: Firecrawl — $16/월 (마크다운 변환, Cloudflare 우회)
FIRECRAWL_API_KEY=
# 3단계: Browserless — $25/월 (강한 봇 차단 대비)
BROWSERLESS_API_KEY=
```

> **중요:** API 키가 없으면 PDF 보고서에 "API 키 미설정" 메시지만 출력됩니다.
> 실제 수출 적합성 분석과 논문 추천은 두 API 키가 모두 있어야 실행됩니다.
>
> - `ANTHROPIC_API_KEY` — Claude AI 수출 적합성 분석 (품목별 심층 근거 생성)
> - `PERPLEXITY_API_KEY` — 품목별 관련 논문·임상 연구 자동 검색
> - `PLAYWRIGHT_LIVE=1` — Guardian·Watsons·Unity 소매가 실크롤 활성화
> - `GEBIZ_LIVE=1` — GeBIZ 정부 조달 낙찰가 실크롤 활성화
> - `JINA_API_KEY` — Playwright CAPTCHA·차단 시 Jina Reader 폴백 (키 없이도 무료 사용 가능)

---

## 실행 방법

### Render 배포 (권장: Blueprint)

1. 이 저장소를 GitHub에 push 합니다.
2. Render 대시보드에서 **New + → Blueprint** 를 선택하고 저장소를 연결합니다.
3. 루트의 `render.yaml`을 자동 인식하면 서비스가 생성됩니다.
4. Render 환경변수에 아래 키를 입력합니다 (`sync: false` 항목):
   - `SUPABASE_URL`
   - `SUPABASE_KEY`
   - `ANTHROPIC_API_KEY` (또는 `CLAUDE_API_KEY`)
   - `PERPLEXITY_API_KEY`
5. 배포 완료 후 헬스체크 경로 `GET /api/health` 가 `{"ok": true, ...}`를 반환하면 정상입니다.
6. 배포 전 로컬 점검:

```bash
python scripts/render_preflight.py
```

> 메모:
> - 기본 설정은 `PLAYWRIGHT_LIVE=0`, `GEBIZ_LIVE=0` 입니다 (Render 환경 안정성 우선).
> - PBS 참고가격 추산은 `PBS_FETCH=1` 기본 활성화입니다.

### Render 기준 리팩토링 정리

- Render 단일 배포 기준으로 **Netlify 관련 파일과 중복 web 서버 코드**를 정리했습니다.
- 현재 배포 엔트리포인트는 `frontend.server:app` 하나로 통일되었습니다.

### 가장 쉬운 방법 — 스크립트 한 줄

터미널에서 아래 명령어 하나만 실행합니다.

```bash
sh run_dashboard.sh
```

자동으로 서버가 켜지고 브라우저에 대시보드가 열립니다.
브라우저가 안 열리면 주소창에 직접 입력하세요:

```
http://127.0.0.1:8765/
```

> **주의:** `localhost` 대신 반드시 `127.0.0.1`을 사용하세요.
> Safari나 Chrome에서 `localhost`로 접속하면 연결이 안 될 수 있습니다.

### 직접 서버 실행 (고급)

스크립트 대신 수동으로 실행할 때는 반드시 `.venv` 파이썬을 사용하세요.
시스템 Python으로 실행하면 `.env` 패키지 등이 로드되지 않아 실크롤이 동작하지 않습니다.

```bash
.venv/bin/python -m uvicorn frontend.server:app --host 0.0.0.0 --port 8765
```

---

## 대시보드 사용법

브라우저에서 대시보드가 열리면 아래 순서로 사용합니다.

### 1단계: 크롤링 시작

왼쪽 상단 **"크롤링 시작"** 버튼을 클릭합니다.

- 오른쪽 로그 창에 실시간으로 진행 상황이 표시됩니다.
- 가운데 사이트 목록에서 각 사이트의 상태(대기중 / 완료 / 오류)를 확인할 수 있습니다.
- 완료까지 보통 **2~5분** 소요됩니다.
- 로그 아래 **"이상치 탐지 & 신뢰도"** 카드에서 품목별 신뢰도와 이상치 여부를 확인할 수 있습니다.

### 2단계: AI 분석 실행

**"AI 분석 실행"** 버튼을 클릭합니다.

- `ANTHROPIC_API_KEY`가 설정되어 있으면 Claude가 품목별 수출 적합성을 실시간 분석합니다.
  - 판정이 "조건부"인 경우 Perplexity가 추가 규제 정보를 검색하여 Claude가 재분석합니다.
- API 키가 없으면 PDF에 "API 키 미설정" 안내가 표시됩니다 (정적 가데이터 없음).
- 완료 후 품목별 수출 적합성 판정(적합/부적합/조건부)과 근거 문단이 표시됩니다.
- `PERPLEXITY_API_KEY`가 있으면 **관련 논문·임상 연구**가 보고서에 자동 반영됩니다.

### 3단계: 보고서 생성

**"보고서 생성"** 버튼을 클릭합니다.

- `reports/` 폴더에 PDF 파일이 저장됩니다.
- "보고서 다운로드" 버튼으로 바로 다운받을 수 있습니다.

---

## 폴더 구조 설명

```
1st_logic/
├── run_dashboard.sh        ← 실행 스크립트 (이걸 실행하세요)
├── .env                    ← API 키 설정 파일 (직접 만들어야 함)
├── requirements.txt        ← 필요한 패키지 목록
│
├── frontend/               ← 대시보드 웹 화면
│   ├── server.py           ← 웹 서버 (자동 실행됨)
│   └── static/index.html   ← 브라우저에 보이는 화면
│
├── crawlers/               ← 크롤링 모듈 (자동 실행됨)
│   └── pipeline.py         ← 전체 크롤링 흐름 관리
│
├── analysis/               ← AI 분석 모듈
│   ├── sg_export_analyzer.py   ← Claude 분석 엔진
│   └── perplexity_references.py ← 관련 논문 검색
│
├── utils/                  ← 공통 유틸리티
│   └── db.py               ← 데이터베이스 관리
│
├── datas/                  ← 참고 데이터
│   ├── ListingofRegisteredTherapeuticProducts.csv  ← HSA 등재 목록
│   ├── singapore_regulation.pdf                   ← 싱가포르 규제 가이드
│   ├── 252026싱가포르진출전략.pdf                  ← KOTRA 진출 전략
│   └── local_products.db                          ← 수집된 데이터 저장소
│
└── reports/                ← 생성된 PDF 보고서 저장 위치
```

---

## 크롤링 대상 사이트

| 사이트 | 수집 정보 | 신뢰도 |
|--------|----------|--------|
| HSA (보건과학청) | 싱가포르 등록 의약품 목록 (정적 CSV) | Tier 1 |
| GeBIZ (정부 조달) | 정부 조달 낙찰가 (Award Notice) | Tier 1 |
| MOH (보건부) | 약가 안내, 정책 공고 (PDF) | Tier 2 |
| NDF (국가 필수약 목록) | 필수약 등재 여부 | Tier 2 |
| Guardian (약국몰) | 소매 시판가 (Playwright 자동화) | Tier 3 |
| Watsons (약국몰) | 소매 시판가 | Tier 3 |
| Unity (약국몰) | Sereterol 삼각검증용 소매가 | Tier 3 |
| SAR (해외 참고) | 미등재 품목 인근국 약가 (3레이어) | Tier 4 |

---

## 자주 묻는 질문

**Q. 버튼을 눌러도 아무것도 안 돼요.**
A. 터미널에서 `sh run_dashboard.sh`가 실행 중인지 확인하세요. 터미널을 닫으면 서버도 꺼집니다.

**Q. "가상환경 없음" 오류가 나와요.**
A. 아직 설치를 안 한 것입니다. "시작 전 준비사항 > 2번" 단계를 먼저 진행하세요.

**Q. AI 분석 버튼을 눌렀는데 PDF에 "API 키 미설정"이 나와요.**
A. `.env` 파일에 `ANTHROPIC_API_KEY`가 올바르게 입력되었는지 확인하세요. API 키 없이는 분석이 실행되지 않습니다 (정적 가데이터 제공 없음).

**Q. 크롤링은 얼마나 자주 해야 하나요?**
A. 버튼 1회 클릭 = 1사이클 (전 사이트 1회 수집). 최신 데이터가 필요할 때마다 누르면 됩니다.

**Q. 수집된 데이터는 어디에 저장되나요?**
A. `datas/local_products.db` 파일에 저장됩니다. 크롤링할 때마다 최신 데이터로 갱신됩니다.

**Q. 서버를 끄려면 어떻게 하나요?**
A. 터미널에서 `Ctrl + C`를 누르면 서버가 종료됩니다.

**Q. 크롤링은 되는데 가격이 안 나오고 "시뮬 모드"라고 나와요.**
A. `.env`에 `PLAYWRIGHT_LIVE=1`과 `GEBIZ_LIVE=1`이 설정되어 있는지 확인하세요.
반드시 `.venv` 파이썬으로 서버를 실행해야 `.env`가 로드됩니다 (`sh run_dashboard.sh` 사용 권장).

**Q. Guardian·Watsons 크롤 시 "CAPTCHA 감지" 또는 "ERR_HTTP2" 오류가 나와요.**
A. 외부 약국 사이트의 봇 차단 정책으로 인한 것입니다. 자동으로 Jina Reader 폴백이 시도됩니다.
더 안정적인 수집이 필요하면 `.env`에 `JINA_API_KEY`(무료)를 추가하세요.

**Q. "신뢰도 0.3" 또는 이상치가 너무 많이 표시돼요.**
A. SAR(해외 참고가) 데이터는 API 키 미설정 시 L3 신뢰도(0.3)로 표시됩니다.
`ANTHROPIC_API_KEY`와 `PERPLEXITY_API_KEY`를 설정하면 신뢰도가 개선됩니다.

---

## 문제 발생 시

터미널에 출력된 오류 메시지를 복사해서 담당자에게 전달해 주세요.
대부분의 오류는 설치 누락 또는 API 키 미설정으로 발생합니다.

### 실크롤 체크리스트

크롤링이 "시뮬 모드"로만 동작한다면 아래를 순서대로 확인하세요.

1. `.env` 파일에 `PLAYWRIGHT_LIVE=1`, `GEBIZ_LIVE=1` 설정 확인
2. `sh run_dashboard.sh` 또는 `.venv/bin/python -m uvicorn ...` 으로 서버 실행 확인
   (시스템 Python으로 실행하면 `.env`가 로드되지 않음)
3. `playwright-stealth` 설치 확인: `.venv/bin/pip install playwright-stealth`
4. Chromium 브라우저 설치 확인: `.venv/bin/playwright install chromium`
