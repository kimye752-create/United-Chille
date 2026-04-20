"""대시보드에 표시할 칠레(CL) 소스 라벨 (한국어)."""

from __future__ import annotations

from typing import Any, TypedDict


class SiteDef(TypedDict):
    id: str
    name: str
    hint: str
    domain: str


DASHBOARD_SITES: tuple[SiteDef, ...] = (
    {
        "id": "cruzverde",
        "name": "Cruz Verde · 소매 약국 (1위 체인)",
        "hint": "SPA 동적 렌더링 — Playwright DOM 대기 후 가격 태그 파싱",
        "domain": "cruzverde.cl",
    },
    {
        "id": "salcobrand",
        "name": "Salcobrand · 소매 약국 (2위 체인)",
        "hint": "프론트엔드 API 역추적 → JSON 엔드포인트 직접 Fetch (멤버십가 포함)",
        "domain": "salcobrand.cl",
    },
    {
        "id": "ahumada",
        "name": "Farmacias Ahumada · 소매 약국 (3위 체인)",
        "hint": "CAPTCHA/IP 차단 대비 — ScraperAPI 프록시 + Exponential Backoff",
        "domain": "farmaciasahumada.cl",
    },
    {
        "id": "mercadopublico",
        "name": "Mercado Público · 공공조달 포털",
        "hint": "OCDS 표준 Open Data API 연동 — 정부기관 850+ 입찰·낙찰 데이터",
        "domain": "mercadopublico.cl",
    },
    {
        "id": "cenabast",
        "name": "CENABAST · 국가중앙공급센터",
        "hint": "Ley 21.198 소매 상한가(Precio Máximo) 주기적 파싱 → DB 적재",
        "domain": "cenabast.cl",
    },
)


def initial_site_states() -> dict[str, dict[str, Any]]:
    return {
        s["id"]: {
            "status": "pending",
            "message": "아직 시작 전이에요",
            "ts": 0.0,
        }
        for s in DASHBOARD_SITES
    }
