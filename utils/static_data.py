"""정적 데이터 파이프라인 — Supabase 버전.

sg_product_context 테이블에서 품목별 컨텍스트를 읽어옴.
로컬 CSV/PDF 의존성 없음.

공개 API (기존과 동일):
  get_product_context(product_id) → StaticContext | None
  context_to_prompt_text(ctx)     → str
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any


@dataclass
class StaticContext:
    product_id: str
    hsa_matches: list[dict[str, str]] = field(default_factory=list)
    hsa_registered: bool = False
    competitor_count: int = 0
    prescription_only: bool = True
    pdf_snippets: list[dict[str, str]] = field(default_factory=list)
    brochure_snippets: list[dict[str, str]] = field(default_factory=list)
    regulatory_summary: str = ""
    built_at: str = ""


_CONTEXT_CACHE: dict[str, StaticContext] | None = None


def _load_all_contexts() -> dict[str, StaticContext]:
    from utils.db import get_client
    sb = get_client()
    try:
        rows = sb.table("sg_product_context").select("*").execute().data or []
    except Exception:
        rows = []

    result: dict[str, StaticContext] = {}
    for row in rows:
        pid = row.get("product_id", "")
        result[pid] = StaticContext(
            product_id=pid,
            hsa_matches=row.get("hsa_matches") or [],
            hsa_registered=row.get("hsa_registered", False),
            competitor_count=row.get("competitor_count", 0),
            prescription_only=row.get("prescription_only", True),
            pdf_snippets=row.get("pdf_snippets") or [],
            brochure_snippets=row.get("brochure_snippets") or [],
            regulatory_summary=row.get("regulatory_summary", ""),
            built_at=str(row.get("built_at", "")),
        )

    # Supabase에 데이터 없으면 HSA 등재 여부만 실시간 조회로 보완
    if not result:
        result = _build_from_hsa()

    return result


def _build_from_hsa() -> dict[str, StaticContext]:
    """sg_product_context 테이블이 비었을 때 HSA products 테이블로 즉석 생성."""
    from utils.db import get_client
    sb = get_client()

    _KEYWORDS: dict[str, list[str]] = {
        "SG_hydrine_hydroxyurea_500": ["hydroxyurea", "hydrine"],
        "SG_gadvoa_gadobutrol_604": ["gadobutrol", "gadvoa", "gadova"],
        "SG_sereterol_activair": ["fluticasone", "salmeterol"],
        "SG_omethyl_omega3_2g": ["omega-3"],
        "SG_rosumeg_combigel": ["rosuvastatin"],
        "SG_atmeg_combigel": ["atorvastatin"],
        "SG_ciloduo_cilosta_rosuva": ["cilostazol"],
        "SG_gastiin_cr_mosapride": ["mosapride"],
    }

    try:
        hsa_rows = (
            sb.table("products")
            .select("product_id,trade_name,registration_number,country_specific,active_ingredient")
            .eq("country", "SG")
            .eq("source_name", "SG:hsa_registry")
            .execute()
            .data or []
        )
    except Exception:
        hsa_rows = []

    result: dict[str, StaticContext] = {}
    for pid, kws in _KEYWORDS.items():
        matches = [
            r for r in hsa_rows
            if any(kw.lower() in (r.get("active_ingredient") or "").lower() for kw in kws)
        ][:10]

        hsa_dicts = [
            {
                "licence_no": m.get("registration_number", ""),
                "product_name": m.get("trade_name", ""),
                "forensic_classification": (m.get("country_specific") or {}).get("forensic_classification", ""),
                "atc_code": (m.get("country_specific") or {}).get("atc_code", ""),
                "active_ingredients": m.get("active_ingredient", ""),
            }
            for m in matches
        ]
        rx_only = any("Prescription" in m.get("forensic_classification", "") for m in hsa_dicts)
        reg_summary = (
            f"HSA 등재 경쟁품 {len(hsa_dicts)}건 확인."
            if hsa_dicts
            else "HSA 등재 기록 없음 — 신규 등록 필요"
        )
        result[pid] = StaticContext(
            product_id=pid,
            hsa_matches=hsa_dicts,
            hsa_registered=len(hsa_dicts) > 0,
            competitor_count=len(hsa_dicts),
            prescription_only=rx_only,
            regulatory_summary=reg_summary,
        )
    return result


def get_product_context(product_id: str, force_rebuild: bool = False) -> StaticContext | None:
    global _CONTEXT_CACHE
    if _CONTEXT_CACHE is None or force_rebuild:
        _CONTEXT_CACHE = _load_all_contexts()
    return _CONTEXT_CACHE.get(product_id)


def context_to_prompt_text(ctx: StaticContext) -> str:
    lines = [
        f"=== 시장 조사 데이터: {ctx.product_id} ===",
        f"HSA 등재 여부: {'등재 경쟁품 있음' if ctx.hsa_registered else '등재 기록 없음 — 신규 등록 필요'}",
        f"경쟁품 수: {ctx.competitor_count}건",
        f"처방 분류: {'Rx (처방전 필요)' if ctx.prescription_only else 'OTC 가능'}",
        f"규제 요약: {ctx.regulatory_summary}",
    ]

    if ctx.hsa_matches:
        lines.append("\n[HSA 등재 경쟁품 상위 3건]")
        for m in ctx.hsa_matches[:3]:
            lines.append(
                f"  - {m.get('product_name', '')} "
                f"({m.get('licence_no', '')}, {m.get('forensic_classification', '')})"
            )

    if ctx.brochure_snippets:
        lines.append("\n[제품 브로슈어 임상 근거]")
        for s in ctx.brochure_snippets[:3]:
            snippet_short = re.sub(r"\s+", " ", s.get("text", ""))[:250]
            lines.append(f"  [{s.get('source', '')} p.{s.get('page', '')} / 키워드: {s.get('keyword', '')}]")
            lines.append(f"  {snippet_short}...")

    if ctx.pdf_snippets:
        lines.append("\n[관련 문서 발췌]")
        for s in ctx.pdf_snippets[:3]:
            snippet_short = re.sub(r"\s+", " ", s.get("text", ""))[:200]
            lines.append(f"  [{s.get('source', '')} p.{s.get('page', '')} / 키워드: {s.get('keyword', '')}]")
            lines.append(f"  {snippet_short}...")

    return "\n".join(lines)
