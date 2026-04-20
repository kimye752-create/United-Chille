-- =============================================================================
-- SG 전용 보조 테이블 (Supabase SQL Editor에서 한 번 실행)
-- team_schema.md의 products/sources 등 공통 테이블은 이미 생성된 상태
-- =============================================================================

-- 1. 품목별 분석 컨텍스트 (context_cache.json 대체)
create table if not exists sg_product_context (
  id                    uuid primary key default gen_random_uuid(),
  product_id            text not null unique,
  hsa_matches           jsonb default '[]'::jsonb,
  hsa_registered        boolean default false,
  competitor_count      int default 0,
  prescription_only     boolean default true,
  pdf_snippets          jsonb default '[]'::jsonb,
  brochure_snippets     jsonb default '[]'::jsonb,
  regulatory_summary    text default '',
  built_at              timestamptz not null default now(),
  updated_at            timestamptz not null default now()
);

-- 2. 싱가포르 암 발생률 (GLOBOCAN 2022)
create table if not exists sg_cancer_incidence (
  id                    bigserial primary key,
  cancer_code           text,
  icd_code              text,
  label                 text not null,
  sex                   int,
  number                int,
  ui_low                int,
  ui_high               int,
  asr_world             numeric(10,2),
  crude_rate            numeric(10,2),
  cumulative_risk       numeric(8,2),
  data_year             int default 2022,
  created_at            timestamptz not null default now()
);

-- 3. WHO 필수의약품 목록 (EML 2023)
create table if not exists sg_who_eml (
  id                    bigserial primary key,
  inn_name              text not null,
  atc_code              text,
  dosage_form           text,
  strength              text,
  section_code          text,
  section_name          text,
  eml_type              text,
  indication            text,
  notes                 text,
  eml_year              int default 2023,
  raw_payload           jsonb,
  created_at            timestamptz not null default now()
);

-- 4. 세계 인구 데이터 (World Bank)
create table if not exists sg_world_population (
  id                    bigserial primary key,
  country_name          text not null,
  country_code          text not null,
  year                  int not null,
  population            bigint,
  created_at            timestamptz not null default now(),
  unique(country_code, year)
);

-- 5. 보건 지출 데이터 (UN SYB67)
create table if not exists sg_health_expenditure (
  id                    bigserial primary key,
  country_or_area       text not null,
  year                  int not null,
  series                text not null,
  value                 numeric(20,6),
  footnotes             text,
  source                text,
  created_at            timestamptz not null default now()
);

-- 6. WHO GHED 보건 지출 상세
create table if not exists sg_ghed_expenditure (
  id                    bigserial primary key,
  country               text not null,
  country_code          text,
  year                  int not null,
  indicator_code        text not null,
  indicator_name        text,
  value                 numeric(20,6),
  created_at            timestamptz not null default now(),
  unique(country_code, year, indicator_code)
);

-- 7. PDF 문서 메타데이터 (Supabase Storage)
create table if not exists sg_documents (
  id                    uuid primary key default gen_random_uuid(),
  filename              text not null unique,
  storage_path          text not null,
  bucket                text not null default 'sg-documents',
  category              text check (category in
    ('regulation','brochure','paper','report','market','strategy')),
  product_id            text,
  label                 text,
  file_size_bytes       bigint,
  created_at            timestamptz not null default now()
);

-- 8. 시장조사 희망 대상 (AX 마스터 캡스톤)
create table if not exists sg_market_targets (
  id                    bigserial primary key,
  country               text,
  product_name          text,
  inn_name              text,
  notes                 text,
  priority              int,
  raw_payload           jsonb,
  created_at            timestamptz not null default now()
);

-- =============================================================================
-- 인덱스
-- =============================================================================
create index if not exists idx_sg_product_context_pid   on sg_product_context(product_id);
create index if not exists idx_sg_cancer_label          on sg_cancer_incidence(label);
create index if not exists idx_sg_who_eml_inn           on sg_who_eml(inn_name);
create index if not exists idx_sg_world_pop_code_year   on sg_world_population(country_code, year);
create index if not exists idx_sg_health_exp_country    on sg_health_expenditure(country_or_area, year);
create index if not exists idx_sg_ghed_code_year        on sg_ghed_expenditure(country_code, year);
create index if not exists idx_sg_documents_category    on sg_documents(category);
create index if not exists idx_sg_documents_product     on sg_documents(product_id);
