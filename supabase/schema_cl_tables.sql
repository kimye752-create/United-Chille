-- =============================================================================
-- CL (칠레) 전용 보조 테이블 (Supabase SQL Editor에서 한 번 실행)
-- 주의: 동일 워크스페이스에 타 팀(sg_, uy_) 테이블이 공존하므로 cl_ 접두사 엄수
-- team_schema.md의 products/sources 등 공통 테이블은 이미 생성된 상태
-- =============================================================================

-- 1. 칠레 약가 수집 결과 (팀 공통 6컬럼 + CL 확장)
create table if not exists cl_pricing (
  -- 팀 공통 6컬럼 (변경 금지)
  id                  uuid primary key default gen_random_uuid(),
  product_id          uuid,
  market_segment      text check (market_segment in ('public', 'private')),
  fob_estimated_usd   decimal(12,4),
  confidence          decimal(3,2) check (confidence between 0.0 and 1.0),
  crawled_at          timestamptz not null default now(),

  -- CL 확장 컬럼
  inn_name            text not null,
  brand_name          text,
  source_site         text check (source_site in
    ('cruzverde', 'salcobrand', 'ahumada', 'mercadopublico', 'cenabast')),
  raw_price_clp       decimal(14,2),
  package_size        int,
  price_per_unit_clp  decimal(12,4),
  -- 칠레 의약품 VAT: 기본 19% (환경변수 CL_VAT_PHARMA_PCT 우선)
  vat_rate            decimal(4,3) default 0.190,
  pharmacy_margin     decimal(4,3),
  source_url          text,
  raw_text            text,
  strength_mg         decimal(10,3),
  dosage_form         text,
  manufacturer        text,
  -- CENABAST 소매 상한가 (Ley 21.198)
  cenabast_max_price_clp decimal(14,2),
  is_cenabast_regulated  boolean default false
);

-- 2. 품목별 분석 컨텍스트
create table if not exists cl_product_context (
  id                    uuid primary key default gen_random_uuid(),
  product_id            text not null unique,
  competitor_count      int default 0,
  prescription_only     boolean default true,
  pdf_snippets          jsonb default '[]'::jsonb,
  brochure_snippets     jsonb default '[]'::jsonb,
  regulatory_summary    text default '',
  isp_registered        boolean default false,   -- ISP(칠레 식약청) 등록 여부
  cenabast_listed       boolean default false,    -- CENABAST 공급 목록 여부
  built_at              timestamptz not null default now(),
  updated_at            timestamptz not null default now()
);

-- 3. 칠레 CENABAST 공급가 / 소매 상한가 마스터
create table if not exists cl_cenabast_prices (
  id                    bigserial primary key,
  inn_name              text not null,
  brand_name            text,
  strength              text,
  dosage_form           text,
  package_size          int,
  cenabast_supply_price_clp  decimal(14,2),   -- CENABAST 공급가 (약국 매입가)
  max_retail_price_clp       decimal(14,2),   -- 소비자 판매 상한가 (Precio Máximo)
  regulation_law        text default 'Ley 21.198',
  valid_from            date,
  valid_until           date,
  source_url            text,
  scraped_at            timestamptz not null default now()
);

-- 4. Mercado Público 공공조달 입찰/낙찰 결과
create table if not exists cl_procurement (
  id                    uuid primary key default gen_random_uuid(),
  tender_id             text unique,            -- Mercado Público 입찰 번호
  buyer_org             text,                   -- 발주 기관명
  inn_name              text not null,
  brand_name            text,
  quantity              int,
  unit                  text,
  awarded_price_clp     decimal(14,2),          -- 낙찰 단가 (CLP)
  awarded_price_usd     decimal(12,4),          -- USD 환산
  award_date            date,
  tender_status         text check (tender_status in
    ('open', 'awarded', 'cancelled', 'evaluation')),
  ocds_data             jsonb,                  -- OCDS 표준 원본 페이로드
  source_url            text,
  crawled_at            timestamptz not null default now()
);

-- 5. 시장조사 대상 (CL 전용)
create table if not exists cl_market_targets (
  id            bigserial primary key,
  country       text default 'Chile',
  product_name  text,
  inn_name      text,
  notes         text,
  priority      int,
  raw_payload   jsonb,
  created_at    timestamptz not null default now()
);

-- 6. 품목별 분석 컨텍스트 (sg_product_context 동일 구조)
create table if not exists cl_documents (
  id                    uuid primary key default gen_random_uuid(),
  filename              text not null unique,
  storage_path          text not null,
  bucket                text not null default 'cl-documents',
  category              text check (category in
    ('regulation','brochure','paper','report','market','strategy')),
  product_id            text,
  label                 text,
  file_size_bytes       bigint,
  created_at            timestamptz not null default now()
);

-- =============================================================================
-- 인덱스
-- =============================================================================
create index if not exists idx_cl_pricing_inn        on cl_pricing(inn_name);
create index if not exists idx_cl_pricing_segment    on cl_pricing(market_segment);
create index if not exists idx_cl_pricing_source     on cl_pricing(source_site);
create index if not exists idx_cl_pricing_crawled    on cl_pricing(crawled_at desc);
create index if not exists idx_cl_ctx_pid            on cl_product_context(product_id);
create index if not exists idx_cl_cenabast_inn       on cl_cenabast_prices(inn_name);
create index if not exists idx_cl_procurement_inn    on cl_procurement(inn_name);
create index if not exists idx_cl_procurement_date   on cl_procurement(award_date desc);
create index if not exists idx_cl_targets_country    on cl_market_targets(country);
create index if not exists idx_cl_documents_category on cl_documents(category);
create index if not exists idx_cl_documents_product  on cl_documents(product_id);
