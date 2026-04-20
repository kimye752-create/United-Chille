-- =============================================================================
-- UY 전용 보조 테이블 (Supabase SQL Editor에서 한 번 실행)
-- team_schema.md의 products/sources 등 공통 테이블은 이미 생성된 상태
-- =============================================================================

-- 1. 우루과이 약가 수집 결과 (팀 공통 6컬럼 + UY 확장)
create table if not exists uy_pricing (
  -- 팀 공통 6컬럼 (변경 금지)
  id                  uuid primary key default gen_random_uuid(),
  product_id          uuid,
  market_segment      text check (market_segment in ('public', 'private')),
  fob_estimated_usd   decimal(12,4),
  confidence          decimal(3,2) check (confidence between 0.0 and 1.0),
  crawled_at          timestamptz not null default now(),

  -- UY 확장 컬럼
  inn_name            text not null,
  brand_name          text,
  source_site         text check (source_site in
    ('sice', 'farmashop', 'farmauy', 'sanroque', 'rex', 'orpm', 'msp')),
  raw_price_uyu       decimal(12,2),
  package_size        int,
  price_per_unit_uyu  decimal(10,4),
  -- 환경변수 UY_VAT_PHARMA_PCT 우선, 기본 10%
  vat_rate            decimal(4,3) default 0.100,
  pharmacy_margin     decimal(4,3),
  farmacard_price_uyu decimal(12,2),
  source_url          text,
  raw_text            text,
  strength_mg         decimal(10,3),
  dosage_form         text,
  manufacturer        text
);

-- 2. 품목별 분석 컨텍스트
create table if not exists uy_product_context (
  id                    uuid primary key default gen_random_uuid(),
  product_id            text not null unique,
  competitor_count      int default 0,
  prescription_only     boolean default true,
  pdf_snippets          jsonb default '[]'::jsonb,
  brochure_snippets     jsonb default '[]'::jsonb,
  regulatory_summary    text default '',
  msp_registered        boolean default false,
  bpum_registered       boolean default false,
  built_at              timestamptz not null default now(),
  updated_at            timestamptz not null default now()
);

-- 3. 인도네시아 AHP 파트너 매칭 결과
create table if not exists indonesia_ahp_partners (
  id              uuid primary key default gen_random_uuid(),
  company_name    text not null unique,
  rank            int,
  psi_score       decimal(5,3),
  cardio_score    decimal(5,3),
  market_score    decimal(5,3),
  intl_score      decimal(5,3),
  has_ir_product  boolean default false,
  pitch_strategy  text check (pitch_strategy in ('direct', 'line_extension')),
  email           text,
  phone           text,
  headquarters    text,
  key_products    jsonb default '[]'::jsonb,
  notes           text,
  scored_at       timestamptz not null default now()
);

-- 4. 시장조사 대상 (UY 및 인도네시아)
create table if not exists uy_market_targets (
  id            bigserial primary key,
  country       text,
  product_name  text,
  inn_name      text,
  notes         text,
  priority      int,
  raw_payload   jsonb,
  created_at    timestamptz not null default now()
);

-- =============================================================================
-- 인덱스
-- =============================================================================
create index if not exists idx_uy_pricing_inn        on uy_pricing(inn_name);
create index if not exists idx_uy_pricing_segment    on uy_pricing(market_segment);
create index if not exists idx_uy_pricing_source     on uy_pricing(source_site);
create index if not exists idx_uy_pricing_crawled    on uy_pricing(crawled_at desc);
create index if not exists idx_uy_ctx_pid            on uy_product_context(product_id);
create index if not exists idx_ahp_rank              on indonesia_ahp_partners(rank);
create index if not exists idx_uy_targets_country    on uy_market_targets(country);
