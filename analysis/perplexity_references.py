"""Perplexity API로 품목별 관련 논문·레퍼런스 검색.

PERPLEXITY_API_KEY 설정 시 자동 실행.
미설정 시 빈 리스트 반환 (UI에서 "API 키 미설정" 표시).

쿼리 방향:
  - HSA 등재 품목: 임상 근거 + 싱가포르 시장 데이터
  - 미등재 품목(개량신약): 규제 진입 경로 + 복합제 승인 사례 중심

출력 (품목별):
  [
    {"title": "...", "url": "https://...", "reason": "한 줄 근거", "source": "PubMed 등"},
    ...
  ]
"""

from __future__ import annotations

import os
from typing import Any

# 등재 품목: 임상 근거 + 싱가포르 현지 시장 쿼리
# 미등재 품목(개량신약): 규제 진입 경로 + 복합제 승인 사례 쿼리
_QUERIES: dict[str, str] = {
    "SG_hydrine_hydroxyurea_500": (
        "hydroxyurea hospital procurement Singapore MOH tender clinical evidence "
        "sickle cell chronic myeloid leukemia treatment guidelines Asia"
    ),
    "SG_gadvoa_gadobutrol_604": (
        "gadobutrol MRI contrast agent HSA Singapore registration macrocyclic GBCA "
        "safety efficacy radiology hospital formulary Southeast Asia"
    ),
    "SG_sereterol_activair": (
        "fluticasone salmeterol fixed dose combination HSA Singapore registration "
        "asthma COPD inhaler GINA GOLD guideline Southeast Asia market"
    ),
    "SG_omethyl_omega3_2g": (
        "omega-3 ethyl esters 2g single capsule HSA Singapore new drug application "
        "hypertriglyceridemia NDA registration pathway REDUCE-IT cardiovascular"
    ),
    "SG_rosumeg_combigel": (
        "rosuvastatin omega-3 fixed dose combination HSA Singapore approval pathway "
        "NDA registration dyslipidemia combination product regulatory Southeast Asia"
    ),
    "SG_atmeg_combigel": (
        "atorvastatin omega-3 fixed dose combination approval pathway Singapore "
        "HSA NDA registration IMD incrementally modified drug dyslipidemia Asia"
    ),
    "SG_ciloduo_cilosta_rosuva": (
        "cilostazol new drug registration Asia Singapore HSA NDA approval pathway "
        "peripheral artery disease rosuvastatin combination regulatory evidence"
    ),
    "SG_gastiin_cr_mosapride": (
        "mosapride regulatory approval Southeast Asia Singapore HSA new market entry "
        "prokinetic gastric motility NDA registration sustained release clinical"
    ),
}

# 각 품목의 쿼리 초점 유형 (프롬프트 커스터마이징용)
_QUERY_FOCUS: dict[str, str] = {
    "SG_hydrine_hydroxyurea_500": "clinical_evidence",
    "SG_gadvoa_gadobutrol_604": "clinical_evidence",
    "SG_sereterol_activair": "clinical_evidence",
    "SG_omethyl_omega3_2g": "regulatory_pathway",
    "SG_rosumeg_combigel": "regulatory_pathway",
    "SG_atmeg_combigel": "regulatory_pathway",
    "SG_ciloduo_cilosta_rosuva": "regulatory_pathway",
    "SG_gastiin_cr_mosapride": "regulatory_pathway",
}


async def fetch_references(
    product_id: str,
    max_refs: int = 4,
) -> list[dict[str, str]]:
    """Perplexity sonar-pro로 관련 논문·규제 사례 검색.

    Returns:
        [{"title", "url", "reason", "source"}, ...]
        API 키 없으면 빈 리스트.
    """
    api_key = os.environ.get("PERPLEXITY_API_KEY")
    if not api_key:
        return []

    query = _QUERIES.get(product_id)
    if not query:
        return []

    try:
        import httpx
    except ImportError:
        return []

    focus = _QUERY_FOCUS.get(product_id, "clinical_evidence")

    if focus == "regulatory_pathway":
        system_msg = (
            "You are a pharmaceutical regulatory expert specializing in Singapore HSA "
            "and Southeast Asian drug registration. Focus on regulatory approval pathways, "
            "NDA requirements, combination product registration, and market entry precedents."
        )
        reason_instruction = (
            "반드시 한국어로: 이 자료가 싱가포르 HSA 등록 진입 경로 판단에 관련 있는 이유를 한 문장으로 요약"
        )
    else:
        system_msg = (
            "You are a pharmaceutical research assistant specializing in Singapore "
            "and Southeast Asian markets. Focus on clinical evidence, market data, "
            "and Singapore MOH/HSA guidelines."
        )
        reason_instruction = (
            "반드시 한국어로: 이 논문/자료가 싱가포르 수출 적합성 판단에 관련 있는 이유를 한 문장으로 요약"
        )

    prompt = f"""Find {max_refs} relevant academic papers, regulatory documents, or clinical studies for:
"{query}"

IMPORTANT: The "reason" field MUST be written in Korean (한국어). Do not use English for the reason field.

Return ONLY valid JSON array, no other text:
[
  {{
    "title": "<paper or document title in original language>",
    "url": "<direct URL to paper, PubMed, or regulatory document>",
    "reason": "<{reason_instruction} — 반드시 한국어 한 문장>",
    "source": "<PubMed / Lancet / NEJM / HSA / MOH / WHO 등>"
  }}
]"""

    try:
        async with httpx.AsyncClient(timeout=25.0) as client:
            resp = await client.post(
                "https://api.perplexity.ai/chat/completions",
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": "sonar-pro",
                    "messages": [
                        {"role": "system", "content": system_msg},
                        {"role": "user", "content": prompt},
                    ],
                    "max_tokens": 800,
                    "return_citations": True,
                },
            )
            resp.raise_for_status()
            content = resp.json()["choices"][0]["message"]["content"].strip()

            if "```" in content:
                for part in content.split("```"):
                    part = part.strip()
                    if part.startswith("json"):
                        part = part[4:].strip()
                    if part.startswith("["):
                        content = part
                        break

            import json
            refs = json.loads(content)
            return [r for r in refs if r.get("url")][:max_refs]

    except Exception:
        return []


async def fetch_references_for_custom(
    trade_name: str,
    inn: str,
    max_refs: int = 4,
) -> list[dict[str, str]]:
    """신약(커스텀 입력) 논문·규제 사례 검색."""
    api_key = os.environ.get("PERPLEXITY_API_KEY")
    if not api_key:
        return []
    try:
        import httpx
    except ImportError:
        return []

    query = (
        f"Singapore HSA regulatory approval status, clinical evidence, and market data "
        f"for {trade_name} ({inn}). Include registration precedents, formulary listing, "
        f"and any Singapore MOH or HSA decisions."
    )
    prompt = f"""Find {max_refs} relevant academic papers, regulatory documents, or clinical studies for:
"{query}"

IMPORTANT: The "reason" field MUST be written in Korean (한국어). Do not use English for the reason field.

Return ONLY valid JSON array, no other text:
[
  {{
    "title": "<paper or document title in original language>",
    "url": "<direct URL to paper, PubMed, or regulatory document>",
    "reason": "<반드시 한국어로: 이 자료가 싱가포르 HSA 등록 판단에 관련 있는 이유를 한 문장으로 요약>",
    "source": "<PubMed / Lancet / NEJM / HSA / MOH / WHO 등>"
  }}
]"""

    try:
        async with httpx.AsyncClient(timeout=25.0) as client:
            resp = await client.post(
                "https://api.perplexity.ai/chat/completions",
                headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                json={
                    "model": "sonar-pro",
                    "messages": [
                        {"role": "system", "content": "You are a pharmaceutical regulatory expert specializing in Singapore HSA."},
                        {"role": "user", "content": prompt},
                    ],
                    "max_tokens": 800,
                    "return_citations": True,
                },
            )
            resp.raise_for_status()
            content = resp.json()["choices"][0]["message"]["content"].strip()
            if "```" in content:
                for part in content.split("```"):
                    part = part.strip()
                    if part.startswith("json"):
                        part = part[4:].strip()
                    if part.startswith("["):
                        content = part
                        break
            import json as _json
            refs = _json.loads(content)
            return [r for r in refs if r.get("url")][:max_refs]
    except Exception:
        return []


async def fetch_all_references(
    product_ids: list[str] | None = None,
) -> dict[str, list[dict[str, str]]]:
    """8품목 전체 논문·규제 사례 검색. product_ids 미지정 시 전체."""
    import asyncio

    targets = product_ids or list(_QUERIES.keys())
    tasks = {pid: fetch_references(pid) for pid in targets}
    results = await asyncio.gather(*tasks.values())
    return dict(zip(tasks.keys(), results))
