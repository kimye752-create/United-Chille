-- =============================================================================
-- CL (칠레) AI 분석 결과 적재 테이블 (Supabase SQL Editor에서 한 번 실행)
-- Anthropic API로 수집한 P1/P2/P3 분석 결과를 도메인별 칼럼으로 저장
-- cl_ 접두사 엄수 (타 팀 sg_, uy_ 테이블과 공존)
-- =============================================================================

-- ─────────────────────────────────────────────────────────────────────────────
-- 1. P1 분석 결과: 시장 적합성 / 거시환경 / 규제 / 가격포지셔닝 / 리스크
-- ─────────────────────────────────────────────────────────────────────────────
create table if not exists cl_analysis_p1 (
  id                  uuid primary key default gen_random_uuid(),

  -- 품목 식별
  product_id          text not null,          -- 예: CL_cilostazol_cr_200
  trade_name          text,
  inn                 text,
  hs_code             text,

  -- [도메인: 수출 적합성 판정]
  verdict             text,                   -- '적합' | '조건부' | '부적합' | '미상'
  verdict_confidence  decimal(3,2),           -- 0.00~1.00
  rationale           text,                   -- 판정 근거 (3~5문장)
  entry_pathway       text,                   -- 진입 경로 권고

  -- [도메인: 거시환경분석]
  market_context      text,                   -- 시장 맥락 요약

  -- [도메인: 규제분석]
  isp_reg             text,                   -- ISP 등록 현황 / 필요 절차

  -- [도메인: 가격분석]
  price_positioning   text,                   -- 가격 포지셔닝 권고

  -- [도메인: 리스크분석]
  risks_conditions    text,                   -- 리스크·전제조건

  -- [도메인: 핵심 근거 (구조화)]
  key_factors         jsonb default '[]',     -- 주요 판단 근거 배열
  sources             jsonb default '[]',     -- 출처 URL/논문 목록

  -- 메타
  analyzed_at         timestamptz not null default now(),
  model               text default 'claude-haiku-4-5-20251001'
);

-- 품목당 1행 유지 (재분석 시 덮어쓰기)
create unique index if not exists uq_cl_p1_product_id
  on cl_analysis_p1 (product_id);

create index if not exists idx_cl_p1_inn          on cl_analysis_p1 (inn);
create index if not exists idx_cl_p1_verdict       on cl_analysis_p1 (verdict);
create index if not exists idx_cl_p1_analyzed      on cl_analysis_p1 (analyzed_at desc);


-- ─────────────────────────────────────────────────────────────────────────────
-- 2. P2 분석 결과: 가격 전략 / 시나리오 / FOB 역산
-- ─────────────────────────────────────────────────────────────────────────────
create table if not exists cl_analysis_p2 (
  id                  uuid primary key default gen_random_uuid(),

  -- 품목 식별
  product_name        text not null,          -- 추출된 제품명 (PDF 기반)
  inn                 text,

  -- [도메인: 가격분석 — 시장 구분]
  market_segment      text not null           -- 'public' | 'private'
    check (market_segment in ('public', 'private')),

  -- [도메인: 가격분석 — 최종 권고가]
  final_price_clp     decimal(14,2),
  final_price_usd     decimal(12,4),
  ref_price_clp       decimal(14,2),          -- 참조 소매가 (CLP)
  ref_price_usd       decimal(12,4),
  rationale           text,                   -- 가격 산정 근거 (3문장)

  -- [도메인: 가격분석 — 시나리오 3종]
  scenarios           jsonb default '[]',     -- [{name, price_clp, price_usd, reason, formula, base_usd}]

  -- [도메인: 가격분석 — FOB 역산 요소]
  formula_elements    jsonb default '[]',     -- [{key, label, value, unit, type}]

  -- [도메인: 거시환경분석]
  market_summary      text,                   -- §1 칠레 시장 요약

  -- 환율 (분석 시점)
  exchange_usd_clp    decimal(10,4),
  exchange_usd_krw    decimal(10,4),

  -- 메타
  analyzed_at         timestamptz not null default now(),
  model               text default 'claude-haiku-4-5-20251001'
);

-- (제품명 + 시장) 조합당 1행 유지
create unique index if not exists uq_cl_p2_product_seg
  on cl_analysis_p2 (product_name, market_segment);

create index if not exists idx_cl_p2_seg           on cl_analysis_p2 (market_segment);
create index if not exists idx_cl_p2_analyzed      on cl_analysis_p2 (analyzed_at desc);


-- ─────────────────────────────────────────────────────────────────────────────
-- 3. P3 분석 결과: 바이어 발굴 / 심층 조사
-- ─────────────────────────────────────────────────────────────────────────────
create table if not exists cl_analysis_p3 (
  id                  uuid primary key default gen_random_uuid(),

  -- 기업 식별
  company_name        text not null,
  company_country     text,
  product_label       text not null,          -- 분석 대상 품목명
  product_key         text,                   -- 예: CL_cilostazol_cr_200

  -- [도메인: 바이어분석 — 기업 개요]
  company_overview_kr text,                   -- CPHI 기반 기업 개요 (한국어)
  revenue             text,                   -- 연매출 규모 (예: ~$50M)
  employees           text,                   -- 임직원 수
  founded             text,                   -- 설립연도
  territories         jsonb default '[]',     -- 영업 국가/지역 배열
  certifications      jsonb default '[]',     -- 보유 인증 배열 (USFDA, EU GMP 등)

  -- [도메인: 바이어분석 — 자격 검증]
  has_gmp             boolean,
  import_history      boolean,
  has_pharmacy_chain  boolean,
  public_channel      boolean,
  private_channel     boolean,
  mah_capable         boolean,
  procurement_history boolean,
  korea_experience    text,
  has_target_country_presence boolean,

  -- [도메인: 바이어분석 — 추천 근거]
  recommendation_reason text,                 -- 파트너 후보 추천 이유 (한국어)
  score               decimal(5,2),           -- 스코어링 점수
  rank_position       int,                    -- 순위 (1 = Top)

  -- 출처
  source_urls         jsonb default '[]',

  -- 메타
  enriched_at         timestamptz not null default now(),
  model               text default 'claude-haiku-4-5-20251001'
);

-- (기업명 + 품목) 조합당 1행 유지
create unique index if not exists uq_cl_p3_company_product
  on cl_analysis_p3 (company_name, product_label);

create index if not exists idx_cl_p3_product_key   on cl_analysis_p3 (product_key);
create index if not exists idx_cl_p3_score         on cl_analysis_p3 (score desc);
create index if not exists idx_cl_p3_enriched      on cl_analysis_p3 (enriched_at desc);
create index if not exists idx_cl_p3_has_gmp       on cl_analysis_p3 (has_gmp);
create index if not exists idx_cl_p3_mah_capable   on cl_analysis_p3 (mah_capable);
