# 바이어 발굴 파이프라인 — 전달 파일 가이드

## 전달 파일 목록

| 파일 | 역할 |
|------|------|
| `utils/cphi_crawler.py` | **1차 수집** — CPHI Japan 전시회 크롤링. 성분 키워드 검색 → 상세 페이지 파싱 |
| `utils/perplexity_searcher.py` | **관련성 검증** — Perplexity Sonar API로 기업의 타깃 국가 시장 진출 여부 실시간 확인 |
| `utils/buyer_enricher.py` | **심층조사** — CPHI 페이지 텍스트 + Perplexity 결과를 Claude Haiku로 구조화 파싱 |
| `analysis/buyer_scorer.py` | **랭킹** — 기업 규모·GMP·채널·타깃국가 진출 등 항목별 점수 → Top 10 선정 |
| `analysis/buyer_report_generator.py` | **PDF 생성** — Top 10 바이어 보고서 (ReportLab) |
| `frontend/server.py` | **API 서버** — FastAPI. `/api/buyers/run`, `/api/buyers/status`, `/api/buyers/result`, `/api/buyers/report/download` |
| `frontend/static/frontend3.html` | **UI** — 파이프라인 실행·실시간 로그·카드·모달·PDF 다운로드 |
| `requirements.txt` | Python 패키지 의존성 |

---

## 파이프라인 흐름

```
[frontend3.html]
  POST /api/buyers/run
        │
        ▼
[server.py] — _run_buyer_pipeline()
        │
        ├─ Step 1: cphi_crawler.crawl()
        │    ├─ CPHI Japan 세션 취득 + 전체 기업 목록(573개) 수집
        │    ├─ 성분 키워드 검색 → ingredient_bucket
        │    ├─ 랜덤 샘플링 → supplement_bucket (보충)
        │    └─ 상세 페이지 파싱 → 최대 20개 후보 반환
        │
        ├─ Step 2: buyer_enricher.enrich_all()
        │    ├─ 기업당 Perplexity verify_company() 호출
        │    │    └─ "[기업명]이 Singapore에서 영업하나?" 실시간 웹 검색
        │    └─ CPHI 텍스트 + Perplexity 결과 → Claude Haiku 구조화
        │         (GMP, 채널, 매출, 싱가포르 진출 여부 등 추출)
        │
        ├─ Step 3: buyer_scorer.rank_companies()
        │    └─ 타깃국가 진출 → 성분 매칭 → 기업 완성도 순 Top 10 선정
        │
        └─ Step 4: buyer_report_generator.build_buyer_pdf()
             └─ PDF 저장 → reports/sg_buyers_*.pdf
```

---

## 필요 환경변수 (`.env`)

```env
ANTHROPIC_API_KEY=sk-ant-...   # Claude Haiku (심층조사 필수)
PERPLEXITY_API_KEY=pplx-...    # Perplexity Sonar (싱가포르 관련성 검증, 없으면 스킵)
```

> `PERPLEXITY_API_KEY`가 없으면 Perplexity 검증 단계를 자동 스킵하고 CPHI 텍스트만으로 분석합니다.

---

## 설치 및 실행

```bash
# 1. 의존성 설치
pip install -r requirements.txt

# 2. 환경변수 설정
cp .env.example .env   # 키 입력

# 3. 서버 실행
python -m frontend.server

# 4. 브라우저에서 접속
open http://127.0.0.1:8765/frontend3
```

---

## 품목별 검색 키워드 설정

`utils/cphi_crawler.py`의 `PRODUCT_SEARCH_MAP` dict에 품목별 성분·치료군 키워드를 정의합니다.

```python
PRODUCT_SEARCH_MAP = {
    "SG_omethyl_omega3_2g": {
        "ingredients": ["Omega-3", "EPA", "DHA", "fish oil"],
        "therapeutic": ["cardiovascular", "lipid", "triglyceride"],
    },
    ...
}
```

새 품목 추가 시 이 dict에 항목을 추가하고, `frontend3.html`의 `<select id="product-select">`에 옵션을 추가하면 됩니다.

---

## 핵심 파일별 주요 함수

### `cphi_crawler.py`
- `crawl(product_key, candidate_pool, emit)` — 메인 진입점. `candidate_pool`개 후보 반환

### `perplexity_searcher.py`
- `verify_company(company_name, products_hint, target_country, target_region)` — 기업 단건 검증
- `batch_verify_companies(companies, target_country, target_region)` — 배치 검증

### `buyer_enricher.py`
- `enrich_all(companies, product_label, target_country, target_region, emit)` — 메인 진입점
- `enrich_company(company, ...)` — 단건 심층조사 (Perplexity + Claude Haiku)

### `buyer_scorer.py`
- `rank_companies(all_candidates, active_criteria, top_n)` — Top N 선정
- `compute_scores(company)` — 항목별 점수 dict 반환

### `buyer_report_generator.py`
- `build_buyer_pdf(buyers, product_label, output_path)` — PDF 파일 생성
