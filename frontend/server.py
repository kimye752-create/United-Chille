"""분석 대시보드 서버: SSE 실시간 로그 + 분석/보고서 API."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import threading
import time
import webbrowser
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
try:
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env", override=True)
except ImportError:
    pass

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from frontend.dashboard_sites import DASHBOARD_SITES

STATIC = Path(__file__).resolve().parent / "static"

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765

_state: dict[str, Any] = {
    "events": [],
    "lock": None,
}


@asynccontextmanager
async def _lifespan(app: FastAPI):
    _state["lock"] = asyncio.Lock()
    yield


app = FastAPI(title="CL Analysis Dashboard", version="4.0.0", lifespan=_lifespan)

import os as _os
_cors_origins = _os.environ.get("CORS_ORIGINS", "*").split(",")
app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


async def _emit(event: dict[str, Any]) -> None:
    payload = {**event, "ts": time.time()}
    lock = _state["lock"]
    if lock is None:
        return
    async with lock:
        _state["events"].append(payload)
        if len(_state["events"]) > 500:
            _state["events"] = _state["events"][-400:]


# ── API 키 런타임 설정 ────────────────────────────────────────────────────────

class ApiKeysBody(BaseModel):
    perplexity_api_key: str = ""
    anthropic_api_key:  str = ""


@app.post("/api/settings/keys")
async def set_api_keys(body: ApiKeysBody) -> JSONResponse:
    """프론트엔드에서 API 키를 런타임에 설정 (프로세스 환경변수 갱신)."""
    import os
    updated: list[str] = []
    if body.perplexity_api_key.strip():
        os.environ["PERPLEXITY_API_KEY"] = body.perplexity_api_key.strip()
        updated.append("PERPLEXITY_API_KEY")
    if body.anthropic_api_key.strip():
        os.environ["ANTHROPIC_API_KEY"] = body.anthropic_api_key.strip()
        updated.append("ANTHROPIC_API_KEY")
    return JSONResponse({"ok": True, "updated": updated})


@app.get("/api/settings/keys/status")
async def get_keys_status() -> JSONResponse:
    """현재 API 키 설정 여부 확인 (값은 노출하지 않음)."""
    import os
    return JSONResponse({
        "perplexity": bool(os.environ.get("PERPLEXITY_API_KEY", "").strip()),
        "anthropic":  bool(os.environ.get("ANTHROPIC_API_KEY", "").strip()),
    })


# ── 분석 ──────────────────────────────────────────────────────────────────────

_analysis_cache: dict[str, Any] = {"result": None, "running": False}


class AnalyzeBody(BaseModel):
    use_perplexity: bool = True
    force_refresh: bool = False


@app.post("/api/analyze")
async def trigger_analyze(body: AnalyzeBody | None = None) -> JSONResponse:
    """8품목 수출 적합성 분석 실행 (Claude API + Perplexity 보조)."""
    req = body if body is not None else AnalyzeBody()
    if _analysis_cache["running"]:
        raise HTTPException(status_code=409, detail="분석이 이미 실행 중입니다.")
    if _analysis_cache["result"] and not req.force_refresh:
        return JSONResponse({"ok": True, "message": "캐시된 분석 결과 사용. force_refresh=true로 재실행."})

    async def _run() -> None:
        _analysis_cache["running"] = True
        try:
            from analysis.ch_export_analyzer import analyze_all
            from analysis.perplexity_references import fetch_all_references

            results = await analyze_all(use_perplexity=req.use_perplexity)
            pids = [r["product_id"] for r in results]
            refs = await fetch_all_references(pids)
            for r in results:
                r["references"] = refs.get(r["product_id"], [])
            _analysis_cache["result"] = results
        finally:
            _analysis_cache["running"] = False

    asyncio.create_task(_run())
    return JSONResponse({"ok": True, "message": "분석을 백그라운드에서 시작했습니다."})


@app.get("/api/analyze/result")
async def analyze_result() -> JSONResponse:
    if _analysis_cache["running"]:
        return JSONResponse({"status": "running"}, status_code=202)
    if not _analysis_cache["result"]:
        raise HTTPException(status_code=404, detail="분석 결과 없음. POST /api/analyze 먼저 실행")
    return JSONResponse({
        "status": "done",
        "count": len(_analysis_cache["result"]),
        "results": _analysis_cache["result"],
    })


@app.get("/api/analyze/status")
async def analyze_status() -> dict[str, Any]:
    return {
        "running": _analysis_cache["running"],
        "has_result": _analysis_cache["result"] is not None,
        "product_count": len(_analysis_cache["result"]) if _analysis_cache["result"] else 0,
    }


# ── 시장 신호 · 뉴스 (Perplexity) ─────────────────────────────────────────────

_news_cache: dict[str, Any] = {"data": None, "ts": 0.0}
_NEWS_TTL = 1800  # 30분 캐시


def _parse_perplexity_news_items(raw_text: str) -> list[dict[str, str]]:
    """Perplexity 텍스트 응답에서 뉴스 배열(JSON) 파싱."""
    import re

    text = (raw_text or "").strip()
    if not text:
        return []

    candidates: list[str] = [text]
    m = re.search(r"\[\s*\{.*\}\s*\]", text, flags=re.S)
    if m:
        candidates.append(m.group(0))

    for cand in candidates:
        try:
            parsed = json.loads(cand)
        except Exception:
            continue
        if not isinstance(parsed, list):
            continue
        items: list[dict[str, str]] = []
        for row in parsed[:8]:
            if not isinstance(row, dict):
                continue
            title = str(row.get("title", "") or "").strip()
            if not title:
                continue
            items.append(
                {
                    "title":   title,
                    "source":  str(row.get("source",  "") or "").strip(),
                    "date":    str(row.get("date",    "") or "").strip(),
                    "link":    str(row.get("link",    "") or "").strip(),
                    "summary": str(row.get("summary", "") or "").strip(),
                }
            )
        if items:
            return items
    return []


def _is_non_korean(text: str) -> bool:
    """제목에 한글이 거의 없으면 영문/스페인어로 판단."""
    if not text:
        return False
    korean_chars = sum(1 for c in text if "\uAC00" <= c <= "\uD7A3")
    total_alpha  = sum(1 for c in text if c.isalpha())
    if total_alpha == 0:
        return False
    return korean_chars / total_alpha < 0.3


async def _translate_titles_to_korean(items: list[dict[str, str]]) -> list[dict[str, str]]:
    """영문/스페인어 제목이 포함된 경우 Claude Haiku로 한국어 번역."""
    import os
    import anthropic

    non_korean = [i for i, it in enumerate(items) if _is_non_korean(it["title"])]
    if not non_korean:
        return items

    api_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if not api_key:
        return items  # API 키 없으면 원문 그대로

    titles_to_translate = [items[i]["title"] for i in non_korean]
    numbered = "\n".join(f"{n+1}. {t}" for n, t in enumerate(titles_to_translate))

    prompt = (
        "다음 제약/의약품 뉴스 제목들을 자연스러운 한국어로 번역하세요.\n"
        "번호와 함께 번역 결과만 출력하세요. 설명 없이 번호. 번역문 형식으로만.\n\n"
        f"{numbered}"
    )

    try:
        client = anthropic.Anthropic(api_key=api_key)
        msg = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=400,
            messages=[{"role": "user", "content": prompt}],
        )
        response_text = msg.content[0].text.strip()

        # "1. 번역문" 형식 파싱
        import re
        translated_lines = {}
        for line in response_text.splitlines():
            line = line.strip()
            m = re.match(r"^(\d+)[.)]\s*(.+)$", line)
            if m:
                translated_lines[int(m.group(1))] = m.group(2).strip()

        # 번역된 제목 교체
        for seq, orig_idx in enumerate(non_korean):
            translated = translated_lines.get(seq + 1, "")
            if translated:
                items[orig_idx]["title"] = translated

    except Exception:
        pass  # 번역 실패 시 원문 유지

    return items


@app.get("/api/news")
async def api_news() -> JSONResponse:
    """Perplexity 기반 칠레 제약 시장 뉴스 (30분 캐시)."""
    import time as _time
    import os
    import httpx

    if _news_cache["data"] and _time.time() - _news_cache["ts"] < _NEWS_TTL:
        return JSONResponse(_news_cache["data"])

    px_key = os.environ.get("PERPLEXITY_API_KEY", "").strip()
    if not px_key:
        return JSONResponse({"ok": False, "error": "PERPLEXITY_API_KEY 미설정", "items": []})

    try:
        payload = {
            "model": "sonar-pro",
            "search_recency_filter": "month",   # 최근 1개월 기사 우선 (2026년 기사)
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "당신은 칠레 제약 시장 전문 애널리스트입니다. "
                        "반드시 JSON 배열만 반환하세요. 설명 텍스트 없음. "
                        "모든 title 값은 반드시 한국어(Korean)로 작성하세요. "
                        "영어·스페인어 제목은 반드시 자연스러운 한국어로 번역하세요. "
                        "각 항목에 summary(한국어 2문장 요약)도 포함하세요."
                    ),
                },
                {
                    "role": "user",
                    "content": (
                        "2026년 칠레 제약·의약품 시장의 최신 뉴스 8건을 찾아주세요 "
                        "(ISP, CENABAST, FONASA, Ley Fármacos, Mercado Público, "
                        "Cruz Verde, Salcobrand, MINSAL, 약가 규제, 바이오시밀러 등 관련). "
                        "2026년 기사를 최우선으로 하고, 없으면 2025년 기사를 포함하세요. "
                        "반환 형식(JSON 배열만): "
                        "[{\"title\": \"한국어 제목\", \"summary\": \"한국어 2문장 요약\", "
                        "\"source\": \"출처명\", \"date\": \"YYYY-MM-DD\", \"link\": \"URL\"}] "
                        "title과 summary는 반드시 한국어로 작성하세요."
                    ),
                },
            ],
            "max_tokens": 1600,
            "temperature": 0.1,
        }
        headers = {
            "Authorization": f"Bearer {px_key}",
            "Content-Type": "application/json",
        }
        async with httpx.AsyncClient(timeout=20.0) as client:
            resp = await client.post(
                "https://api.perplexity.ai/chat/completions",
                headers=headers,
                json=payload,
            )
            resp.raise_for_status()
            raw = resp.json()

        content = str(
            raw.get("choices", [{}])[0]
            .get("message", {})
            .get("content", "")
        )
        items = _parse_perplexity_news_items(content)
        if not items:
            return JSONResponse({"ok": False, "error": "Perplexity 응답 파싱 실패", "items": []})

        # 영문/스페인어 제목 → Claude Haiku 한국어 번역
        items = await _translate_titles_to_korean(items)

        data = {"ok": True, "items": items}
        _news_cache["data"] = data
        _news_cache["ts"]   = _time.time()
        return JSONResponse(data)
    except Exception as exc:
        return JSONResponse({"ok": False, "error": str(exc)[:120], "items": []})


# ── 거시지표 ──────────────────────────────────────────────────────────────────

@app.get("/api/macro")
async def api_macro() -> JSONResponse:
    from utils.ch_macro import get_ch_macro
    return JSONResponse(get_ch_macro())


# ── 환율 (yfinance CLP/KRW) ───────────────────────────────────────────────────

_exchange_cache: dict[str, Any] = {"data": None, "ts": 0.0}
_EXCHANGE_TTL_SEC = 0.0


@app.get("/api/exchange")
async def api_exchange() -> JSONResponse:
    """CLP/KRW 실시간 환율 (yfinance). 짧은 캐시로 준실시간 제공."""
    import time as _time

    if _exchange_cache["data"] and _time.time() - _exchange_cache["ts"] < _EXCHANGE_TTL_SEC:
        return JSONResponse(_exchange_cache["data"])

    def _fetch() -> dict[str, Any]:
        import yfinance as yf  # type: ignore[import]
        usd_krw = float(yf.Ticker("USDKRW=X").fast_info.last_price)
        usd_clp = float(yf.Ticker("USDCLP=X").fast_info.last_price)
        clp_usd = 1.0 / usd_clp if usd_clp else 0
        clp_krw = clp_usd * usd_krw
        return {
            "clp_krw": round(clp_krw, 6),
            "clp_usd": round(clp_usd, 8),
            "usd_clp": round(usd_clp, 2),
            "usd_krw": round(usd_krw, 2),
            "source": "Yahoo Finance",
            "fetched_at": _time.time(),
            "ok": True,
        }

    try:
        loop = asyncio.get_event_loop()
        data = await loop.run_in_executor(None, _fetch)
        _exchange_cache["data"] = data
        _exchange_cache["ts"]   = _time.time()
        return JSONResponse(data)
    except Exception as exc:
        fallback: dict[str, Any] = {
            "clp_krw": 1.495,
            "clp_usd": 0.001073,
            "usd_clp": 932.0,
            "usd_krw": 1393.0,
            "source": "폴백 (Yahoo Finance 연결 실패)",
            "fetched_at": _time.time(),
            "ok": False,
            "error": str(exc),
        }
        return JSONResponse(fallback)


# ── 단일 품목 파이프라인 (분석 + 논문 + PDF) ──────────────────────────────────

_pipeline_tasks: dict[str, dict[str, Any]] = {}


async def _run_pipeline_for_product(product_key: str) -> None:
    task = _pipeline_tasks[product_key]
    try:
        # 0. DB 조회 (Supabase)
        task.update({"step": "db_load", "step_label": "Supabase 데이터 로드 중…"})
        await _emit({"phase": "pipeline", "message": f"{product_key} — DB 조회 중", "level": "info"})

        from utils.db import fetch_kup_products
        if product_key.startswith("CL_"):
            _country = "CL"
        elif product_key.startswith("UY_"):
            _country = "UY"
        else:
            _country = "SG"
        kup_rows = await asyncio.to_thread(fetch_kup_products, _country)
        db_row = next((r for r in kup_rows if r.get("product_id") == product_key), None)

        if db_row is None:
            await _emit({"phase": "pipeline", "message": f"DB에서 품목 미발견: {product_key}", "level": "warn"})

        # 1. Claude 분석
        task.update({"step": "analyze", "step_label": "Claude 분석 중…"})
        await _emit({"phase": "pipeline", "message": f"{product_key} — 분석 시작", "level": "info"})

        # CL_ 품목: cl_pricing DB에서 현지 크롤 약가 조회 → AI 프롬프트에 주입 (할루시네이션 방지)
        pricing_ctx = ""
        if product_key.startswith("CL_"):
            from analysis.ch_export_analyzer import analyze_product
            from utils.db import fetch_cl_pricing_context
            _inn_hint = (db_row or {}).get("inn", "") or product_key.replace("CL_", "").replace("_", " ")
            pricing_ctx = await asyncio.to_thread(fetch_cl_pricing_context, _inn_hint, 8)
            if pricing_ctx:
                await _emit({"phase": "pipeline", "message": f"cl_pricing DB {len(pricing_ctx)}자 로드 완료", "level": "info"})
        else:
            from analysis.sg_export_analyzer import analyze_product  # type: ignore[assignment]
        result = await analyze_product(product_key, db_row, pricing_context=pricing_ctx)
        task["result"] = result
        verdict = result.get("verdict") or "미분석"
        await _emit({"phase": "pipeline", "message": f"분석 완료 — {verdict}", "level": "success"})

        # ── DB 적재: P1 분석 결과 → cl_analysis_p1 ──────────────────────────
        if product_key.startswith("CL_"):
            try:
                from utils.db import upsert_cl_analysis_p1
                _inn_hint_p1 = (db_row or {}).get("inn", "") or product_key.replace("CL_", "").replace("_", " ")
                _p1_row: dict[str, Any] = {
                    "product_id":         product_key,
                    "trade_name":         result.get("trade_name") or product_key,
                    "inn":                result.get("inn") or _inn_hint_p1,
                    "hs_code":            result.get("hs_code", ""),
                    # 도메인: 수출 적합성 판정
                    "verdict":            result.get("verdict", ""),
                    "verdict_confidence": result.get("confidence"),
                    "rationale":          result.get("rationale", ""),
                    "entry_pathway":      result.get("entry_pathway", ""),
                    # 도메인: 거시환경
                    "market_context":     result.get("market_context", ""),
                    # 도메인: 규제
                    "isp_reg":            result.get("isp_reg", ""),
                    # 도메인: 가격
                    "price_positioning":  result.get("price_positioning", ""),
                    # 도메인: 리스크
                    "risks_conditions":   result.get("risks_conditions", ""),
                    # 도메인: 구조화 근거
                    "key_factors":        result.get("key_factors", []),
                    "sources":            result.get("sources", []),
                }
                await asyncio.to_thread(upsert_cl_analysis_p1, _p1_row)
                await _emit({"phase": "pipeline", "message": "P1 결과 DB 저장 완료 (cl_analysis_p1)", "level": "info"})
            except Exception as _e_p1db:
                await _emit({"phase": "pipeline", "message": f"P1 DB 저장 스킵: {_e_p1db}", "level": "warn"})

        # 2. Perplexity 논문
        task.update({"step": "refs", "step_label": "논문 검색 중…"})
        from analysis.perplexity_references import fetch_references
        refs = await fetch_references(product_key)
        task["refs"] = refs
        if refs:
            await _emit({"phase": "pipeline", "message": f"논문 {len(refs)}건 검색 완료", "level": "success"})

        # 3. PDF 보고서 (in-process 생성 — subprocess 의존성 제거)
        task.update({"step": "report", "step_label": "PDF 생성 중…"})
        await _emit({"phase": "pipeline", "message": "PDF 보고서 생성 중…", "level": "info"})

        from datetime import datetime, timezone as _tz
        from report_generator import build_report, render_pdf

        _ts = datetime.now(_tz.utc).strftime("%Y%m%d_%H%M%S")
        _reports_dir = ROOT / "reports"
        _reports_dir.mkdir(parents=True, exist_ok=True)

        # kup_rows는 Step 0에서 이미 비동기로 가져왔으므로 재사용 (DB 이중 조회 방지)
        if product_key.startswith("CL_"):
            # ── 칠레 P1: SG_01 양식 기반 전용 보고서 생성기 사용 ────────────────
            _prefix = "cl_report"
            _pdf_name = f"{_prefix}_{product_key}_{_ts}.pdf"
            _pdf_path = _reports_dir / _pdf_name

            # cl_pricing DB에서 실측 가격 행 조회 (PDF §3용)
            _pr_rows: list[dict] = []
            try:
                from utils.db import get_client as _get_sb_p1
                _sb_p1 = _get_sb_p1()
                _inn_p1 = result.get("inn", "") or pricing_ctx[:50]
                _pr_r = await asyncio.to_thread(
                    lambda: _sb_p1.table("cl_pricing")
                        .select("source_site,brand_name,inn_name,raw_price_clp,cenabast_max_price_clp")
                        .ilike("inn_name", f"%{_inn_p1[:20]}%")
                        .limit(12).execute()
                )
                _pr_rows = _pr_r.data or []
            except Exception:
                pass

            _meta_list = []
            try:
                from analysis.ch_export_analyzer import _get_product_meta
                _meta_list = _get_product_meta()
            except Exception:
                pass
            _meta = next((m for m in _meta_list if m.get("product_id") == product_key), {})

            _p1_data = {
                **result,
                "product_id":   product_key,
                "trade_name":   result.get("trade_name") or _meta.get("trade_name", product_key),
                "inn":          result.get("inn") or _meta.get("inn", ""),
                "isp_reg":      _meta.get("isp_reg", "ISP 등록 필요"),
                "pricing_rows": _pr_rows,
                "hs_code":      _meta.get("hs_code", "3004.90"),
                "market_context": result.get("market_context", ""),
            }
            from analysis.cl_p1_report import render_cl_p1_pdf
            await asyncio.to_thread(render_cl_p1_pdf, _p1_data, _pdf_path, refs=refs)
        else:
            # SG / UY 기존 보고서
            _refs_map = {product_key: refs}
            _report = await asyncio.to_thread(
                lambda: build_report(
                    kup_rows,
                    datetime.now(_tz.utc).isoformat(),
                    [result],
                    references=_refs_map,
                )
            )
            _prefix = "uy_report" if product_key.startswith("UY_") else "cl_report"
            _pdf_name = f"{_prefix}_{product_key}_{_ts}.pdf"
            _pdf_path = _reports_dir / _pdf_name
            await asyncio.to_thread(render_pdf, _report, _pdf_path)

        task["pdf"] = _pdf_name
        task.update({"status": "done", "step": "done", "step_label": "완료"})
        await _emit({"phase": "pipeline", "message": "파이프라인 완료", "level": "success"})

    except Exception as exc:
        task.update({"status": "error", "step": "error", "step_label": str(exc)})
        await _emit({"phase": "pipeline", "message": f"오류: {exc}", "level": "error"})


# ── 신약(커스텀) 파이프라인 ────────────────────────────────────────────────────
# 주의: 리터럴 경로("/api/pipeline/custom/...")는 반드시 {product_key} 라우트보다 먼저 선언

_custom_task: dict[str, Any] = {}


class CustomDrugBody(BaseModel):
    trade_name: str
    inn: str
    dosage_form: str = ""


async def _run_custom_pipeline(trade_name: str, inn: str, dosage_form: str) -> None:
    global _custom_task
    try:
        # Step 1: Claude 분석
        _custom_task.update({"step": "analyze", "step_label": "Claude 분석 중…"})
        from analysis.ch_export_analyzer import analyze_custom_product
        result = await analyze_custom_product(trade_name, inn, dosage_form)
        _custom_task["result"] = result

        # ── DB 적재: 커스텀 P1 분석 결과 → cl_analysis_p1 ─────────────────
        try:
            from utils.db import upsert_cl_analysis_p1
            _p1_custom_row: dict[str, Any] = {
                "product_id":         f"CL_custom_{inn[:20].replace(' ','_')}",
                "trade_name":         trade_name,
                "inn":                inn,
                "verdict":            result.get("verdict", ""),
                "verdict_confidence": result.get("confidence"),
                "rationale":          result.get("rationale", ""),
                "entry_pathway":      result.get("entry_pathway", ""),
                "market_context":     result.get("market_context", ""),
                "isp_reg":            result.get("isp_reg", "ISP 등록 필요"),
                "price_positioning":  result.get("price_positioning", ""),
                "risks_conditions":   result.get("risks_conditions", ""),
                "key_factors":        result.get("key_factors", []),
                "sources":            result.get("sources", []),
            }
            await asyncio.to_thread(upsert_cl_analysis_p1, _p1_custom_row)
        except Exception:
            pass  # DB 저장 실패는 파이프라인 중단 없이 계속

        # Step 2: Perplexity 논문
        _custom_task.update({"step": "refs", "step_label": "논문 검색 중…"})
        from analysis.perplexity_references import fetch_references_for_custom
        refs = await fetch_references_for_custom(trade_name, inn)
        _custom_task["refs"] = refs

        # Step 2-b: cl_pricing 조회 (할루시네이션 방지)
        _custom_task.update({"step": "refs", "step_label": "DB 가격 데이터 조회 중…"})
        _ctx_custom = ""
        _pr_rows_custom: list[dict] = []
        try:
            from utils.db import fetch_cl_pricing_context, get_client as _get_sb_cu
            _ctx_custom = await asyncio.to_thread(fetch_cl_pricing_context, inn, 8)
            _sb_cu = _get_sb_cu()
            _pr_cu = await asyncio.to_thread(
                lambda: _sb_cu.table("cl_pricing")
                    .select("source_site,brand_name,inn_name,raw_price_clp,cenabast_max_price_clp")
                    .ilike("inn_name", f"%{inn[:20]}%")
                    .limit(10).execute()
            )
            _pr_rows_custom = _pr_cu.data or []
        except Exception:
            pass

        # Step 3: PDF 보고서 (SG_01 양식 기반 cl_p1_report)
        _custom_task.update({"step": "report", "step_label": "PDF 생성 중…"})
        from datetime import datetime, timezone as _tz2

        _ts2 = datetime.now(_tz2.utc).strftime("%Y%m%d_%H%M%S")
        _reports_dir2 = ROOT / "reports"
        _reports_dir2.mkdir(parents=True, exist_ok=True)

        _pdf_name2 = f"cl_report_custom_{_ts2}.pdf"
        _pdf_path2 = _reports_dir2 / _pdf_name2

        _p1_custom = {
            **result,
            "product_id":   "CL_custom",
            "trade_name":   trade_name,
            "inn":          inn,
            "isp_reg":      "ISP 등록 필요 — 신규 RM 절차 (시판 전 허가 미확인)",
            "pricing_rows": _pr_rows_custom,
            "hs_code":      "3004.90",
        }
        from analysis.cl_p1_report import render_cl_p1_pdf
        await asyncio.to_thread(render_cl_p1_pdf, _p1_custom, _pdf_path2, refs=refs)

        _custom_task["pdf"] = _pdf_name2
        _custom_task.update({"status": "done", "step": "done", "step_label": "완료"})

    except Exception as exc:
        _custom_task.update({"status": "error", "step": "error", "step_label": str(exc)})


@app.post("/api/pipeline/custom")
async def trigger_custom_pipeline(body: CustomDrugBody) -> JSONResponse:
    global _custom_task
    if _custom_task.get("status") == "running":
        raise HTTPException(status_code=409, detail="신약 분석이 이미 실행 중입니다.")
    _custom_task = {
        "status": "running", "step": "analyze", "step_label": "시작 중…",
        "result": None, "refs": [], "pdf": None,
    }
    asyncio.create_task(_run_custom_pipeline(body.trade_name, body.inn, body.dosage_form))
    return JSONResponse({"ok": True})


@app.get("/api/pipeline/custom/status")
async def custom_pipeline_status() -> JSONResponse:
    if not _custom_task:
        return JSONResponse({"status": "idle"})
    return JSONResponse({
        "status":     _custom_task.get("status", "idle"),
        "step":       _custom_task.get("step", ""),
        "step_label": _custom_task.get("step_label", ""),
        "has_result": _custom_task.get("result") is not None,
        "has_pdf":    bool(_custom_task.get("pdf")),
    })


@app.get("/api/pipeline/custom/result")
async def custom_pipeline_result() -> JSONResponse:
    if not _custom_task:
        raise HTTPException(404, "신약 분석 미실행")
    return JSONResponse({
        "status": _custom_task.get("status"),
        "result": _custom_task.get("result"),
        "refs":   _custom_task.get("refs", []),
        "pdf":    _custom_task.get("pdf"),
    })


# ── 기존 품목 파이프라인 ──────────────────────────────────────────────────────

@app.post("/api/pipeline/{product_key}")
async def trigger_pipeline(product_key: str) -> JSONResponse:
    if _pipeline_tasks.get(product_key, {}).get("status") == "running":
        raise HTTPException(status_code=409, detail="이미 실행 중입니다.")
    _pipeline_tasks[product_key] = {
        "status": "running", "step": "init", "step_label": "시작 중…",
        "result": None, "refs": [], "pdf": None,
    }
    asyncio.create_task(_run_pipeline_for_product(product_key))
    return JSONResponse({"ok": True, "message": "파이프라인 시작됨"})


@app.get("/api/pipeline/{product_key}/status")
async def pipeline_status(product_key: str) -> JSONResponse:
    task = _pipeline_tasks.get(product_key)
    if not task:
        return JSONResponse({"status": "idle"})
    return JSONResponse({
        "status":     task["status"],
        "step":       task["step"],
        "step_label": task["step_label"],
        "has_result": task["result"] is not None,
        "has_pdf":    bool(task["pdf"]),
        "ref_count":  len(task.get("refs", [])),
    })


@app.get("/api/pipeline/{product_key}/result")
async def pipeline_result(product_key: str) -> JSONResponse:
    task = _pipeline_tasks.get(product_key)
    if not task:
        raise HTTPException(404, "파이프라인 미실행")
    return JSONResponse({
        "status": task["status"],
        "step":   task["step"],
        "result": task.get("result"),
        "refs":   task.get("refs", []),
        "pdf":    task.get("pdf"),
    })


# ── 보고서 ────────────────────────────────────────────────────────────────────

_report_cache: dict[str, Any] = {"path": None, "running": False}

def _latest_report_pdf() -> Path | None:
    reports_dir = ROOT / "reports"
    if not reports_dir.exists():
        return None
    pdfs = [p for p in reports_dir.glob("cl_report_*.pdf") if p.is_file()]
    if not pdfs:
        # 구버전 sg_ 파일 폴백
        pdfs = [p for p in reports_dir.glob("sg_report_*.pdf") if p.is_file()]
    if not pdfs:
        return None
    return max(pdfs, key=lambda p: p.stat().st_mtime)


class ReportBody(BaseModel):
    run_analysis: bool = False
    use_perplexity: bool = False


@app.post("/api/report")
async def trigger_report(body: ReportBody | None = None) -> JSONResponse:
    req = body if body is not None else ReportBody()
    if _report_cache["running"]:
        raise HTTPException(status_code=409, detail="보고서 생성이 이미 실행 중입니다.")

    async def _run_report() -> None:
        _report_cache["running"] = True
        try:
            import subprocess
            cmd = [
                sys.executable, str(ROOT / "report_generator.py"),
                "--out", str(ROOT / "reports"),
            ]
            if req.run_analysis:
                cmd.append("--run-analysis")
            await asyncio.get_event_loop().run_in_executor(
                None, lambda: subprocess.run(cmd, capture_output=True, text=True)
            )
            reports_dir = ROOT / "reports"
            pdfs = sorted(reports_dir.glob("cl_report_*.pdf"), reverse=True)
            _report_cache["path"] = str(pdfs[0]) if pdfs else None
        finally:
            _report_cache["running"] = False

    asyncio.create_task(_run_report())
    return JSONResponse({"ok": True, "message": "보고서 생성을 백그라운드에서 시작했습니다."})


@app.get("/api/report/status")
async def report_status() -> dict[str, Any]:
    reports_dir = ROOT / "reports"
    pdfs = [p for p in reports_dir.glob("cl_report_*.pdf")] if reports_dir.exists() else []
    latest = _latest_report_pdf()
    return {
        "running": _report_cache["running"],
        "latest_pdf": str(latest) if latest else _report_cache["path"],
        "pdf_count": len(pdfs),
    }


@app.get("/api/report/download")
async def download_report(name: str | None = None, inline: bool = False) -> Any:
    """PDF 반환. inline=true면 브라우저/iframe 미리보기용(Content-Disposition: inline)."""
    reports_dir = ROOT / "reports"
    disp = "inline" if inline else "attachment"
    if name:
        target = reports_dir / Path(name).name
        if target.is_file():
            return FileResponse(
                str(target),
                media_type="application/pdf",
                filename=target.name,
                content_disposition_type=disp,
            )

    latest = _latest_report_pdf()
    if not latest:
        raise HTTPException(status_code=404, detail="생성된 보고서 없음. POST /api/report 먼저 실행")
    return FileResponse(
        str(latest),
        media_type="application/pdf",
        filename=latest.name,
        content_disposition_type=disp,
    )


# ── 합본 보고서 (표지 + P1 + P2 + P3) ──────────────────────────────────────────

class CombinedReportBody(BaseModel):
    product_name:    str  = ""
    inn_label:       str  = ""
    country:         str  = "칠레"
    p1_report:       dict | None = None   # render_pdf()용 보고서 dict
    p2_data:         dict | None = None   # render_p2_pdf()용 데이터 dict
    p3_companies:    list | None = None   # build_buyer_pdf()용 바이어 리스트
    p3_product_label: str = ""
    use_latest_pdfs: bool = True          # True: 최신 개별 PDF 병합 / False: 데이터로 재생성


@app.post("/api/cl/report/combined")
async def generate_combined_report(body: CombinedReportBody) -> JSONResponse:
    """표지 + P1 + P2 + P3 합본 PDF 생성.

    use_latest_pdfs=True(기본값): reports/ 에서 최신 cl_report_*.pdf, cl_p2_*.pdf, cl_p3_*.pdf 를 병합.
    use_latest_pdfs=False: body 에 담긴 데이터로 각 섹션을 재생성 후 병합.
    """
    import re
    from datetime import datetime, timezone as _tz_c

    _ts = datetime.now(_tz_c.utc).strftime("%Y%m%d_%H%M%S")
    reports_dir = ROOT / "reports"
    reports_dir.mkdir(parents=True, exist_ok=True)

    safe = re.sub(r"[^\w가-힣]", "_", body.product_name)[:25] or "report"
    out_name = f"cl_combined_{safe}_{_ts}.pdf"
    out_path = reports_dir / out_name

    if body.use_latest_pdfs:
        # 최신 개별 PDF 병합
        try:
            from pypdf import PdfWriter, PdfReader  # type: ignore[import]
        except ImportError:
            from PyPDF2 import PdfWriter, PdfReader  # type: ignore[import]

        from report_generator import render_cover_pdf
        import tempfile

        p1_pdfs = sorted(reports_dir.glob("cl_report_*.pdf"), key=lambda p: p.stat().st_mtime, reverse=True)
        p2_pdfs = sorted(reports_dir.glob("cl_p2_*.pdf"),     key=lambda p: p.stat().st_mtime, reverse=True)
        p3_pdfs = sorted(reports_dir.glob("cl_p3_*.pdf"),     key=lambda p: p.stat().st_mtime, reverse=True)

        writer = PdfWriter()

        with tempfile.TemporaryDirectory() as _tmp:
            cover_path = Path(_tmp) / "cover.pdf"
            await asyncio.to_thread(
                render_cover_pdf, cover_path,
                country=body.country,
                product_name=body.product_name,
                inn_label=body.inn_label,
            )
            reader = PdfReader(str(cover_path))
            for page in reader.pages:
                writer.add_page(page)

        for pdf_path in [
            p1_pdfs[0] if p1_pdfs else None,
            p2_pdfs[0] if p2_pdfs else None,
            p3_pdfs[0] if p3_pdfs else None,
        ]:
            if pdf_path and pdf_path.is_file():
                reader = PdfReader(str(pdf_path))
                for page in reader.pages:
                    writer.add_page(page)

        with open(str(out_path), "wb") as fout:
            writer.write(fout)
    else:
        # 데이터로 재생성
        from report_generator import render_combined_pdf
        await asyncio.to_thread(
            render_combined_pdf,
            p1_report=body.p1_report,
            p2_data=body.p2_data,
            p3_companies=body.p3_companies,
            p3_product_label=body.p3_product_label,
            country=body.country,
            product_name=body.product_name,
            inn_label=body.inn_label,
            out_path=out_path,
        )

    return JSONResponse({"ok": True, "pdf": out_name})


# ── 2공정 가격 전략 PDF ───────────────────────────────────────────────────────

class P2ReportBody(BaseModel):
    product_name:       str   = ""
    verdict:            str   = ""
    seg_label:          str   = ""
    base_price:         float | None = None
    formula_str:        str   = ""
    mode_label:         str   = ""
    scenarios:          list  = []
    ai_rationale:       list  = []
    market_summary:     str   = ""   # §1 거시 시장 요약 (3~4줄)
    competitor_prices:  list  = []   # §3 거래처 참고가격 [{name,product,ingredient,price}]


@app.post("/api/p2/report")
async def generate_p2_report(body: P2ReportBody) -> JSONResponse:
    """2공정 수출 가격 전략 PDF 생성."""
    import re
    from datetime import datetime, timezone as _tz_p2

    _ts = datetime.now(_tz_p2.utc).strftime("%Y%m%d_%H%M%S")
    _reports_dir = ROOT / "reports"
    _reports_dir.mkdir(parents=True, exist_ok=True)

    safe_name = re.sub(r"[^\w가-힣]", "_", body.product_name)[:30] or "product"
    pdf_name  = f"cl_p2_{safe_name}_{_ts}.pdf"
    pdf_path  = _reports_dir / pdf_name

    p2_data = {
        "product_name":  body.product_name,
        "verdict":       body.verdict,
        "seg_label":     body.seg_label,
        "base_price":    body.base_price,
        "formula_str":   body.formula_str,
        "mode_label":    body.mode_label,
        "scenarios":     body.scenarios,
        "ai_rationale":  body.ai_rationale,
    }

    from report_generator import render_p2_pdf
    await asyncio.to_thread(render_p2_pdf, p2_data, pdf_path)

    return JSONResponse({"ok": True, "pdf": pdf_name})


# ── 2공정 AI 파이프라인 (PDF → Haiku 가격 추출 → 계산 → Haiku 분석 × 2시장 → PDF) ────

# 칠레 시장별 FOB 역산 기본 요소 (AI 추천 기본값)
_CL_FOB_ELEMENTS_PUBLIC = [
    {"key": "agent_fee",       "label": "에이전트 수수료",   "value": 5,   "unit": "%",   "type": "pct_deduct"},
    {"key": "freight",         "label": "운임 배수",         "value": 1.0, "unit": "×배수","type": "mult"},
    {"key": "procurement_fee", "label": "조달청 입찰 수수료","value": 3,   "unit": "%",   "type": "pct_deduct"},
    {"key": "gpo_discount",    "label": "GPO 물량 할인율",   "value": 2,   "unit": "%",   "type": "pct_deduct"},
]
_CL_FOB_ELEMENTS_PRIVATE = [
    {"key": "agent_fee",         "label": "에이전트 수수료",      "value": 5,   "unit": "%",   "type": "pct_deduct"},
    {"key": "freight",           "label": "운임 배수",            "value": 1.0, "unit": "×배수","type": "mult"},
    {"key": "pharma_margin",     "label": "병원·약국 유통 마진",  "value": 15,  "unit": "%",   "type": "pct_deduct"},
    {"key": "distributor_markup","label": "유통사 마크업",        "value": 8,   "unit": "%",   "type": "pct_add"},
]

def _elements_product(elements: list[dict]) -> float:
    """FOB 역산 요소 목록에서 종합 배율을 계산합니다."""
    result = 1.0
    for e in elements:
        v = float(e.get("value", 0))
        t = e.get("type", "pct_deduct")
        if t == "pct_deduct":
            result *= (1 - v / 100)
        elif t == "pct_add":
            result *= (1 + v / 100)
        elif t == "mult":
            result *= v
    return result

_p2_ai_task: dict[str, Any] = {}

# 크롤링 상태
_crawl_task: dict[str, Any] = {}

# ── P2 시스템 프롬프트 ────────────────────────────────────────────────────────
_CLAUDE_P2_SYSTEM_PROMPT = (
    "당신은 한국유나이티드제약(주)의 칠레 수출 전략 시니어 애널리스트입니다. "
    "주어진 품목의 (1) 칠레 크롤링 데이터, (2) 규제·참고가 시드, (3) FOB 계산 결과를 종합해 "
    "한국어 보고서체 블록 9개를 작성합니다.\n\n"

    "【데이터 원칙 — 최우선】\n"
    "- 입력 데이터(cl_products 등)에 없는 수치나 업체명은 절대 창작하지 않습니다.\n"
    "- 칠레 페소(CLP)와 달러(USD) 환율은 제공된 dispatch 결과를 엄격히 따릅니다.\n"
    "- 값이 없으면 '미확보' 또는 '칠레 현지 추가 검증 필요'로 명시합니다.\n\n"

    "【칠레 특화 매핑 규칙】\n"
    "- block_market_macro: 칠레의 안정적인 경제 지표와 수입 의존도, FONASA(국가건강보험) 시장 특성 요약.\n"
    "- block_extract: CENABAST(공공조달청) 낙찰가 또는 약국 체인 소매가 근거 포함.\n"
    "- block_fob_intro: 칠레 이원화 시장(Public-CENABAST / Private-Retail) 중 타겟 명시.\n"
    "- block_strategy: segment='public'이면 CENABAST 입찰 전략, 'private'이면 대형 약국 체인 파트너십.\n"
    "- block_risks: ISP의 생물학적 동등성(BE) 요건 강화, CLP 변동성, 인접국 가격 공세 언급.\n"
    "- block_scenario_penetration: scenarios.aggressive 결과 기반 저가 진입 전략 설명.\n"
    "- block_scenario_reference: scenarios.average 결과 기반 기준가 전략 설명.\n"
    "- block_scenario_premium: scenarios.conservative 결과 기반 고가 진입 전략 설명.\n"
    "- block_disclaimer: 본 보고서의 AI 추정 기반 면책 사항.\n\n"

    "【시나리오 키 매핑 — 반드시 준수】\n"
    "수입상(Sponsor) 마진 프리셋이 반영된 결과:\n"
    "  scenario_penetration ← scenarios.aggressive.fob_usd  (저가 진입, 수입상마진 30%)\n"
    "  scenario_reference   ← scenarios.average.fob_usd     (기준가 진입, 수입상마진 20%)\n"
    "  scenario_premium     ← scenarios.conservative.fob_usd (고가 진입, 수입상마진 10%)\n"
    "각 시나리오 설명에 fob_usd를 주값으로 인용하고, CLP를 괄호로 병기합니다.\n"
    "각 시나리오 근거에 '수입상 마진'과 'FTA 관세 혜택'이 FOB 산정에 미친 영향을 반드시 포함합니다.\n\n"

    "【출력 JSON 스키마 — 반드시 아래 형식만 출력】\n"
    "{\n"
    "  \"block_market_macro\": \"\",\n"
    "  \"block_extract\": \"\",\n"
    "  \"block_fob_intro\": \"\",\n"
    "  \"block_strategy\": \"\",\n"
    "  \"block_risks\": \"\",\n"
    "  \"block_scenario_penetration\": \"\",\n"
    "  \"block_scenario_reference\": \"\",\n"
    "  \"block_scenario_premium\": \"\",\n"
    "  \"block_disclaimer\": \"\",\n"
    "  \"final_price_clp\": 0,\n"
    "  \"final_price_usd\": 0,\n"
    "  \"rationale\": \"\",\n"
    "  \"ref_price_clp\": 0,\n"
    "  \"scenarios\": {\n"
    "    \"aggressive\":    {\"fob_usd\": 0, \"fob_clp\": 0, \"formula\": \"\", \"reason\": \"\"},\n"
    "    \"average\":       {\"fob_usd\": 0, \"fob_clp\": 0, \"formula\": \"\", \"reason\": \"\"},\n"
    "    \"conservative\":  {\"fob_usd\": 0, \"fob_clp\": 0, \"formula\": \"\", \"reason\": \"\"}\n"
    "  }\n"
    "}\n\n"

    "【어투 및 품질 규칙】\n"
    "- 한국어 존댓말('-합니다', '-습니다') 사용.\n"
    "- 마크다운 기호(#, **, -, 백틱) 및 이모지 사용 절대 금지.\n"
    "- 9개 block 필드 모두 공란 없이 채웁니다.\n"
    "- JSON만 출력하며 코드블록(```) 래퍼 없이 순수 JSON 객체로 반환합니다.\n"
)


def _build_p2_user_message(
    extracted: dict,
    exchange_rates: dict,
    market: str,
    pricing_ctx: str = "",
) -> str:
    """P2 분석용 user 메시지를 구조화된 JSON 데이터로 빌드.

    시스템 프롬프트가 역할·출력 형식을 처리하므로, user 메시지는 순수 데이터만 전달한다.
    """
    import json as _json
    import os as _os_p2
    usd_clp = exchange_rates.get("usd_clp", 932.0)
    usd_krw = exchange_rates.get("usd_krw", 1393.0)
    vat_pct = float(_os_p2.environ.get("CL_VAT_PHARMA_PCT", "19.0"))
    import_duty_pct = float(_os_p2.environ.get("CL_IMPORT_DUTY_PCT", "6.0"))

    ref_price_clp = extracted.get("ref_price_clp") or 0
    ref_price_usd = extracted.get("ref_price_usd") or 0
    if not ref_price_clp and ref_price_usd:
        ref_price_clp = round(float(ref_price_usd) * usd_clp, 0)

    if market == "public":
        market_label = "공공 시장 (Mercado Público · CENABAST 공급 채널)"
        fob_ratios   = {"aggressive": 0.28, "average": 0.35, "conservative": 0.42}
        channel_desc = (
            "CLP 낙찰가 → IVA 없음(공공기관 면제) → "
            f"수입관세 {import_duty_pct}%(한-칠레 FTA 적용 시 0%) → "
            "파트너·수입상 마진 → FOB USD"
        )
    else:
        market_label = "민간 시장 (Cruz Verde / Salcobrand / Farmacias Ahumada 채널)"
        fob_ratios   = {"aggressive": 0.25, "average": 0.31, "conservative": 0.38}
        channel_desc = (
            f"CLP 소매가 ÷ (1+IVA {vat_pct}%) → 약국마진 25~35% 공제 → "
            "파트너·수입상 마진 공제 → "
            f"수입관세 {import_duty_pct}% 공제 → FOB USD"
        )

    # FOB 역산 dispatch 계산 (프리셋)
    dispatch_scenarios: dict = {}
    for key, ratio in fob_ratios.items():
        fob_usd = round(float(ref_price_clp) / usd_clp * ratio, 2) if usd_clp and ref_price_clp else 0
        fob_clp = round(float(ref_price_clp) * ratio, 0)
        dispatch_scenarios[key] = {
            "fob_usd":   fob_usd,
            "fob_clp":   fob_clp,
            "fob_ratio": ratio,
            "sponsor_margin_pct": {"aggressive": 30, "average": 20, "conservative": 10}[key],
        }

    payload: dict = {
        "product": {
            "name":            extracted.get("product_name", "미상"),
            "inn":             extracted.get("inn", ""),
            "hs_code":         extracted.get("hs_code", "3004.90"),
            "verdict":         extracted.get("verdict", "미상"),
            "market_context":  extracted.get("market_context", ""),
        },
        "market": {
            "segment":         market,
            "label":           market_label,
            "channel_formula": channel_desc,
        },
        "reference_price": {
            "clp":             ref_price_clp,
            "usd":             ref_price_usd,
            "text":            extracted.get("ref_price_text", "미확인"),
        },
        "exchange_rates": {
            "usd_clp":         usd_clp,
            "usd_krw":         usd_krw,
            "source":          exchange_rates.get("source", ""),
        },
        "fob_dispatch": {
            "description":     "수입상 마진 프리셋 기반 FOB 역산 결과 (참고값 — LLM이 재산정)",
            "vat_pct":         vat_pct,
            "import_duty_pct": import_duty_pct,
            "fta_rate":        "0% (한-칠레 FTA HS 3004·3006 적용 시)",
            "scenarios":       dispatch_scenarios,
        },
        "competitor_prices":   extracted.get("competitor_prices", []),
        "pricing_db_data":     pricing_ctx or "cl_pricing DB 데이터 없음 (크롤러 미실행 또는 해당 품목 미등재)",
    }
    return _json.dumps(payload, ensure_ascii=False, indent=2)


def _normalize_p2_result(llm: dict, market: str, ref_price_clp: float, usd_clp: float) -> dict:
    """새 9-블록 LLM 스키마 → 기존 파이프라인(server.py/report_generator.py) 기대 형식으로 변환."""

    # ── scenarios: dict(aggressive/average/conservative) → list([저가진입,기준가,프리미엄]) ──
    sc_raw = llm.get("scenarios", {})
    if isinstance(sc_raw, dict):
        def _sc_entry(key: str, name_ko: str) -> dict:
            sc = sc_raw.get(key, {})
            fob_usd = float(sc.get("fob_usd", 0) or 0)
            fob_clp = float(sc.get("fob_clp", 0) or 0)
            if not fob_clp and fob_usd and usd_clp:
                fob_clp = round(fob_usd * usd_clp)
            return {
                "name":      name_ko,
                "price_usd": fob_usd,
                "price_clp": fob_clp,
                "reason":    sc.get("reason", ""),
                "formula":   sc.get("formula", ""),
                "market":    market,
            }
        scenarios_list = [
            _sc_entry("aggressive",   "저가진입"),
            _sc_entry("average",      "기준가"),
            _sc_entry("conservative", "프리미엄"),
        ]
    elif isinstance(sc_raw, list):
        # 구버전 flat 배열 폴백
        scenarios_list = sc_raw
    else:
        scenarios_list = []

    # ── 기준가: scenarios 중간값(average) 또는 llm 직접값 ─────────────────────
    avg_sc    = next((s for s in scenarios_list if s.get("name") == "기준가"), {})
    final_usd = float(llm.get("final_price_usd", 0) or avg_sc.get("price_usd", 0) or 0)
    final_clp = float(llm.get("final_price_clp", 0) or avg_sc.get("price_clp", 0) or 0)
    ref_clp   = float(llm.get("ref_price_clp",   0) or ref_price_clp or 0)

    # ── 보고서 텍스트 블록들 ──────────────────────────────────────────────────
    block_macro   = str(llm.get("block_market_macro", "") or "")
    block_extract = str(llm.get("block_extract",      "") or "")
    block_fob     = str(llm.get("block_fob_intro",    "") or "")
    block_strat   = str(llm.get("block_strategy",     "") or "")
    block_risks   = str(llm.get("block_risks",        "") or "")

    # rationale: block들에서 조합 또는 직접값
    rationale = str(llm.get("rationale", "") or "")
    if not rationale:
        parts = [p for p in [block_extract, block_fob, block_strat] if p]
        rationale = " ".join(parts[:2])[:500] or "AI 분석 결과"

    return {
        # 기존 파이프라인 필드
        "final_price_clp":  final_clp,
        "final_price_usd":  final_usd,
        "rationale":        rationale,
        "ref_price_clp":    ref_clp,
        "scenarios":        scenarios_list,
        # 보고서 텍스트 블록 (report_generator §1·§2 보강용)
        "block_market_macro":   block_macro,
        "block_extract":        block_extract,
        "block_fob_intro":      block_fob,
        "block_strategy":       block_strat,
        "block_risks":          block_risks,
        "block_scenario_penetration": str(llm.get("block_scenario_penetration", "") or ""),
        "block_scenario_reference":   str(llm.get("block_scenario_reference",   "") or ""),
        "block_scenario_premium":     str(llm.get("block_scenario_premium",     "") or ""),
        "block_disclaimer":           str(llm.get("block_disclaimer",           "") or ""),
        # 원본 LLM 출력 보존 (디버그)
        "_llm_raw": llm,
    }


async def _analyze_market_haiku(
    client: Any,
    extracted: dict,
    exchange_rates: dict,
    market: str,
    pricing_ctx: str = "",
) -> dict:
    """Claude Haiku — 시스템 프롬프트 + JSON 데이터 방식으로 P2 가격 전략 분석."""
    import json as _json, re as _re, asyncio as _asyncio

    usd_clp       = float(exchange_rates.get("usd_clp", 932.0))
    ref_price_clp = float(extracted.get("ref_price_clp") or 0)
    ref_price_usd = float(extracted.get("ref_price_usd") or 0)
    if not ref_price_clp and ref_price_usd:
        ref_price_clp = round(ref_price_usd * usd_clp, 0)

    user_msg = _build_p2_user_message(extracted, exchange_rates, market, pricing_ctx)

    try:
        resp = await _asyncio.to_thread(
            lambda: client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=2000,
                system=_CLAUDE_P2_SYSTEM_PROMPT,
                messages=[{"role": "user", "content": user_msg}],
            )
        )
        raw = resp.content[0].text.strip()
        # 코드블록 래퍼 제거
        raw = _re.sub(r"```(?:json)?\s*", "", raw).strip("`").strip()
        m = _re.search(r"\{.*\}", raw, _re.S)
        if m:
            llm = _json.loads(m.group(0))
            return _normalize_p2_result(llm, market, ref_price_clp, usd_clp)
    except Exception:
        pass

    # ── 폴백 ────────────────────────────────────────────────────────────────
    base_clp = ref_price_clp or 5000.0
    ratio    = 0.35 if market == "public" else 0.31
    est_usd  = round(base_clp / usd_clp * ratio, 2) if usd_clp else 0.0
    est_clp  = round(base_clp * ratio, 0)
    return {
        "final_price_clp":  est_clp,
        "final_price_usd":  est_usd,
        "rationale":        "AI 응답 파싱 실패 — 기본 역산값입니다.",
        "ref_price_clp":    base_clp,
        "block_market_macro": (
            "칠레는 인구 약 1,926만 명의 남미 최대 의약품 수입국 중 하나로, "
            "의약품 시장 규모는 약 USD 24.5억(2024)이며 수입 의존도 80%+입니다. "
            "FONASA(공공) / ISAPRE(민간) 이중 구조로 운영됩니다."
        ),
        "block_strategy": (
            "CENABAST 공공조달 입찰 또는 Cruz Verde·Salcobrand 3대 체인 파트너십 검토가 필요합니다."
            if market == "public"
            else "Cruz Verde·Salcobrand·Farmacias Ahumada 3대 약국 체인 유통 파트너십이 권장됩니다."
        ),
        "block_risks": (
            "ISP 생물학적 동등성(BE) 요건 강화, CLP 환율 변동성, "
            "인접국(아르헨티나 등) 로컬 제약사 가격 공세 리스크가 있습니다."
        ),
        "block_extract": "", "block_fob_intro": "",
        "block_scenario_penetration": "", "block_scenario_reference": "",
        "block_scenario_premium": "", "block_disclaimer": "",
        "scenarios": [
            {"name": "저가진입", "price_clp": round(est_clp * 0.88), "price_usd": round(est_usd * 0.88, 2),
             "reason": "저마진 진입가 — FTA 관세 혜택으로 수입상 마진 30% 가정.",
             "formula": f"CLP {base_clp:,.0f} × {ratio*0.88:.2f} = USD {est_usd*0.88:.2f}",
             "market": market},
            {"name": "기준가",   "price_clp": est_clp,               "price_usd": est_usd,
             "reason": "기준가 — 수입상 마진 20%, FTA 0% 관세 적용.",
             "formula": f"CLP {base_clp:,.0f} × {ratio:.2f} = USD {est_usd:.2f}",
             "market": market},
            {"name": "프리미엄", "price_clp": round(est_clp * 1.12), "price_usd": round(est_usd * 1.12, 2),
             "reason": "프리미엄 포지셔닝 — 수입상 마진 10%, 개량신약 차별성 반영.",
             "formula": f"CLP {base_clp:,.0f} × {ratio*1.12:.2f} = USD {est_usd*1.12:.2f}",
             "market": market},
        ],
    }


async def _run_p2_ai_pipeline(report_path: str, market: str) -> None:
    global _p2_ai_task
    try:
        import json
        import os
        import re

        import anthropic

        api_key = (
            os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("CLAUDE_API_KEY", "")
        ).strip()
        if not api_key:
            raise ValueError("ANTHROPIC_API_KEY 미설정 — 환경변수를 확인하세요.")

        # ── Step 1: PDF 텍스트 추출 ────────────────────────────────────────────
        _p2_ai_task.update({"step": "extract", "step_label": "PDF 텍스트 추출 중…"})
        await _emit({"phase": "p2_pipeline", "message": "PDF 텍스트 추출 시작", "level": "info"})

        pdf_text = ""
        try:
            from pypdf import PdfReader  # type: ignore[import]
            reader = PdfReader(report_path)
            for page in reader.pages:
                pdf_text += (page.extract_text() or "") + "\n"
        except Exception as exc_pdf:
            await _emit({"phase": "p2_pipeline", "message": f"PDF 추출 경고: {exc_pdf}", "level": "warn"})

        if not pdf_text.strip():
            raise ValueError("PDF에서 텍스트를 추출할 수 없습니다. 스캔 이미지 PDF이거나 암호화된 파일일 수 있습니다.")

        await _emit({"phase": "p2_pipeline", "message": f"텍스트 {len(pdf_text)}자 추출 완료", "level": "success"})

        # ── Step 2: Claude Haiku — 가격 정보 추출 ──────────────────────────────
        _p2_ai_task.update({"step": "ai_extract", "step_label": "AI 가격 정보 추출 중…"})
        await _emit({"phase": "p2_pipeline", "message": "Claude Haiku — 가격 정보 추출", "level": "info"})

        client = anthropic.Anthropic(api_key=api_key)

        extract_prompt = f"""다음 의약품 수출 분석 보고서(칠레 시장)에서 가격 관련 정보를 추출하세요.

보고서 내용:
{pdf_text[:7000]}

아래 JSON 형식으로만 응답하세요 (다른 텍스트 없이):
{{
  "product_name": "제품명 (없으면 '미상')",
  "ref_price_clp": 숫자 또는 null,
  "ref_price_usd": 숫자 또는 null,
  "ref_price_text": "원문 가격 텍스트 (없으면 빈 문자열)",
  "competitor_prices": [{{"name": "경쟁사명", "price_clp": 숫자}}],
  "market_context": "시장 맥락 요약 (1-2문장)",
  "hs_code": "HS 코드 (없으면 빈 문자열)",
  "verdict": "수출 적합성 판정 (적합/조건부/부적합/미상)"
}}

가격 추출 규칙 (반드시 준수):
- Cruz Verde / Salcobrand / Farmacias Ahumada 소매가(CLP)를 ref_price_clp에 넣으세요.
- CENABAST 소매 상한가(Ley 21.198) 또는 낙찰가가 있으면 그 CLP 값을 우선시하세요.
- Mercado Público 낙찰가(CLP)가 있으면 ref_price_clp에 넣으세요.
- CLP 금액이 없고 USD($) 금액만 있다면 ref_price_usd에 넣으세요.
- '참고 USD X.XX', 'USD X.XX 수준', 'FOB USD X.XX' 등의 표현에서 숫자를 추출하세요."""

        extract_resp = await asyncio.to_thread(
            lambda: client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=1024,
                messages=[{"role": "user", "content": extract_prompt}],
            )
        )

        extracted: dict[str, Any] = {}
        try:
            raw_extract = extract_resp.content[0].text
            m_json = re.search(r"\{[^{}]*(?:\{[^{}]*\}[^{}]*)*\}", raw_extract, re.S)
            if m_json:
                extracted = json.loads(m_json.group(0))
        except Exception:
            extracted = {
                "product_name": "미상",
                "ref_price_clp": None,
                "ref_price_usd": None,
                "ref_price_text": "",
                "market_context": "",
                "verdict": "미상",
            }

        _p2_ai_task["extracted"] = extracted
        ref_clp_display = f"CLP {float(extracted.get('ref_price_clp') or 0):,.0f}" if extracted.get("ref_price_clp") else (extracted.get("ref_price_text") or "미확인")
        await _emit({
            "phase": "p2_pipeline",
            "message": f"가격 추출 완료 — 참조가: {ref_clp_display}",
            "level": "success",
        })

        # ── Step 3: 실시간 환율 (yfinance) ────────────────────────────────────
        _p2_ai_task.update({"step": "exchange", "step_label": "실시간 환율 조회 중…"})
        await _emit({"phase": "p2_pipeline", "message": "yfinance 환율 조회", "level": "info"})

        exchange_rates: dict[str, Any] = {
            "usd_clp": 932.0, "clp_usd": 0.001073,
            "usd_krw": 1393.0, "clp_krw": 1.495,
            "source": "폴백값 (Yahoo Finance 연결 실패)",
        }
        try:
            import yfinance as yf  # type: ignore[import]

            def _fetch_rates() -> dict[str, Any]:
                usd_clp = round(float(yf.Ticker("USDCLP=X").fast_info.last_price), 2)
                usd_krw = round(float(yf.Ticker("USDKRW=X").fast_info.last_price), 2)
                clp_usd = round(1.0 / usd_clp, 8) if usd_clp else 0.001073
                return {
                    "usd_clp": usd_clp,
                    "clp_usd": clp_usd,
                    "usd_krw": usd_krw,
                    "clp_krw": round(clp_usd * usd_krw, 6),
                    "source": "Yahoo Finance (실시간)",
                }

            exchange_rates = await asyncio.to_thread(_fetch_rates)
        except Exception as exc_fx:
            await _emit({"phase": "p2_pipeline", "message": f"환율 폴백: {exc_fx}", "level": "warn"})

        _p2_ai_task["exchange_rates"] = exchange_rates
        await _emit({
            "phase": "p2_pipeline",
            "message": f"환율 — 1 USD = {exchange_rates['usd_clp']} CLP / {exchange_rates['usd_krw']} KRW",
            "level": "success",
        })

        # ── Step 3-b: cl_pricing DB — 현지 크롤 약가 조회 ────────────────────────
        _p2_ai_task.update({"step": "ai_analysis", "step_label": "DB 가격 데이터 조회 + AI 분석…"})
        _pricing_ctx_p2 = ""
        _competitor_prices_p2: list[dict] = []
        try:
            from utils.db import fetch_cl_pricing_context, get_client as _get_sb_p2
            _inn_hint_p2 = extracted.get("product_name", "") or ""
            _pricing_ctx_p2 = await asyncio.to_thread(fetch_cl_pricing_context, _inn_hint_p2, 12)
            # 경쟁사 가격 테이블 (PDF §3 용)
            _sb_p2 = _get_sb_p2()
            _cp_r = await asyncio.to_thread(
                lambda: _sb_p2.table("cl_pricing")
                    .select("source_site,brand_name,inn_name,raw_price_clp,cenabast_max_price_clp")
                    .ilike("inn_name", f"%{_inn_hint_p2[:20]}%")
                    .limit(8).execute()
            )
            for _cp_row in (_cp_r.data or []):
                _retail = _cp_row.get("raw_price_clp") or _cp_row.get("max_retail_price_clp")
                _supply = _cp_row.get("cenabast_max_price_clp")
                _price_parts = []
                if _retail:  _price_parts.append(f"CLP {int(_retail):,} (소매)")
                if _supply:  _price_parts.append(f"CLP {int(_supply):,} (CENABAST)")
                _competitor_prices_p2.append({
                    "name": _cp_row.get("source_site", "—"),
                    "product": _cp_row.get("brand_name", "—") or "—",
                    "ingredient": _cp_row.get("inn_name", "—"),
                    "price": " / ".join(_price_parts) if _price_parts else "—",
                })
            if _pricing_ctx_p2:
                await _emit({"phase": "p2_pipeline", "message": f"cl_pricing {len(_competitor_prices_p2)}건 로드", "level": "info"})
        except Exception as _exc_cp:
            await _emit({"phase": "p2_pipeline", "message": f"cl_pricing 조회 스킵: {_exc_cp}", "level": "warn"})

        # ── Step 4: Claude Haiku — 공공·민간 동시 분석 (parallel) ───────────────
        await _emit({"phase": "p2_pipeline", "message": "Claude Haiku — 공공·민간 가격 전략 동시 분석", "level": "info"})

        pub_analysis, priv_analysis = await asyncio.gather(
            _analyze_market_haiku(client, extracted, exchange_rates, "public",  _pricing_ctx_p2),
            _analyze_market_haiku(client, extracted, exchange_rates, "private", _pricing_ctx_p2),
        )

        # 각 시장의 ref_price 결정 (공공=낙찰가 추정, 민간=소매가)
        usd_clp = exchange_rates["usd_clp"]
        base_ref_clp = extracted.get("ref_price_clp") or 0
        base_ref_usd = extracted.get("ref_price_usd") or (base_ref_clp / usd_clp if usd_clp and base_ref_clp else 0)

        # 공공: 소매가의 약 85% 수준이 Mercado Público 낙찰가 추정치
        pub_ref_clp = pub_analysis.get("ref_price_clp") or round(base_ref_clp * 0.85) or base_ref_clp
        pub_ref_usd = round(pub_ref_clp / usd_clp, 2) if usd_clp and pub_ref_clp else base_ref_usd
        priv_ref_clp = priv_analysis.get("ref_price_clp") or base_ref_clp
        priv_ref_usd = round(priv_ref_clp / usd_clp, 2) if usd_clp and priv_ref_clp else base_ref_usd

        # formula_elements 복사 (딥카피로 독립 보장)
        import copy
        pub_elements  = copy.deepcopy(_CL_FOB_ELEMENTS_PUBLIC)
        priv_elements = copy.deepcopy(_CL_FOB_ELEMENTS_PRIVATE)

        # 각 시나리오에 base_usd 계산 (기준가 = ref_price / elements_product)
        def _enrich_scenarios(scenarios: list, ref_usd: float, elements: list) -> list:
            ep = _elements_product(elements)
            out = []
            for sc in scenarios:
                fob_usd = float(sc.get("price_usd", 0))
                base = round(fob_usd / ep, 2) if ep and fob_usd else ref_usd
                out.append({**sc, "base_usd": base})
            return out

        pub_scenarios  = _enrich_scenarios(pub_analysis.get("scenarios", []),  pub_ref_usd,  pub_elements)
        priv_scenarios = _enrich_scenarios(priv_analysis.get("scenarios", []), priv_ref_usd, priv_elements)

        _p2_ai_task["public"] = {
            "analysis":        pub_analysis,
            "ref_price_clp":   pub_ref_clp,
            "ref_price_usd":   pub_ref_usd,
            "scenarios":       pub_scenarios,
            "formula_elements": pub_elements,
        }
        _p2_ai_task["private"] = {
            "analysis":        priv_analysis,
            "ref_price_clp":   priv_ref_clp,
            "ref_price_usd":   priv_ref_usd,
            "scenarios":       priv_scenarios,
            "formula_elements": priv_elements,
        }
        # 하위 호환 (기존 analysis 필드 유지)
        _p2_ai_task["analysis"] = pub_analysis

        await _emit({
            "phase": "p2_pipeline",
            "message": (
                f"분석 완료 — 공공 FOB USD {pub_analysis.get('final_price_usd', 0):.2f} "
                f"/ 민간 FOB USD {priv_analysis.get('final_price_usd', 0):.2f}"
            ),
            "level": "success",
        })

        # ── DB 적재: P2 분석 결과 → cl_analysis_p2 (공공 + 민간 각 1행) ───────
        try:
            from utils.db import upsert_cl_analysis_p2
            _p2_product_name = extracted.get("product_name", "미상")
            _p2_inn = extracted.get("inn", "")
            _p2_market_summary = (
                "칠레는 인구 약 1,926만 명의 남미 최대 의약품 수입국 중 하나로, "
                "의약품 시장 규모는 약 USD 24.5억(2024)이며 수입 의존도 80%+입니다."
            )
            for _seg, _ana, _ref_clp, _ref_usd, _scen, _elems in [
                ("public",  pub_analysis,  pub_ref_clp,  pub_ref_usd,  pub_scenarios,  pub_elements),
                ("private", priv_analysis, priv_ref_clp, priv_ref_usd, priv_scenarios, priv_elements),
            ]:
                _p2_row: dict[str, Any] = {
                    "product_name":     _p2_product_name,
                    "inn":              _p2_inn,
                    "market_segment":   _seg,
                    # 도메인: 가격분석
                    "final_price_clp":  _ana.get("final_price_clp"),
                    "final_price_usd":  _ana.get("final_price_usd"),
                    "ref_price_clp":    _ref_clp,
                    "ref_price_usd":    _ref_usd,
                    "rationale":        _ana.get("rationale", ""),
                    "scenarios":        _scen,
                    "formula_elements": _elems,
                    # 도메인: 거시환경
                    "market_summary":   _p2_market_summary,
                    # 환율 (분석 시점)
                    "exchange_usd_clp": exchange_rates.get("usd_clp"),
                    "exchange_usd_krw": exchange_rates.get("usd_krw"),
                }
                await asyncio.to_thread(upsert_cl_analysis_p2, _p2_row)
            await _emit({"phase": "p2_pipeline", "message": "P2 결과 DB 저장 완료 (cl_analysis_p2 공공·민간)", "level": "info"})
        except Exception as _e_p2db:
            await _emit({"phase": "p2_pipeline", "message": f"P2 DB 저장 스킵: {_e_p2db}", "level": "warn"})

        # ── Step 5: PDF 보고서 생성 (공공 + 민간 합본) ──────────────────────────
        _p2_ai_task.update({"step": "report", "step_label": "PDF 생성 중…"})
        await _emit({"phase": "p2_pipeline", "message": "2공정 PDF 보고서 생성 (공공·민간 합본)", "level": "info"})

        from datetime import datetime, timezone as _tz_p2ai
        import re as _re2

        _ts_p2 = datetime.now(_tz_p2ai.utc).strftime("%Y%m%d_%H%M%S")
        _reports_dir_p2 = ROOT / "reports"
        _reports_dir_p2.mkdir(parents=True, exist_ok=True)

        _safe = _re2.sub(r"[^\w가-힣]", "_", extracted.get("product_name", "product"))[:30] or "product"
        _pdf_name_p2 = f"cl_p2_{_safe}_{_ts_p2}.pdf"
        _pdf_path_p2 = _reports_dir_p2 / _pdf_name_p2

        def _norm_scenarios(scenarios: list) -> list:
            out = []
            for sc in scenarios:
                out.append({
                    "label":   sc.get("name", sc.get("label", "")),
                    "price":   float(sc.get("price_usd", 0)),
                    "price_clp": float(sc.get("price_clp", 0)),
                    "reason":  sc.get("reason", ""),
                    "formula": sc.get("formula", ""),
                    "market":  sc.get("market", ""),
                })
            return out

        # 공공 시나리오에 market 태그 추가 후 합산
        pub_norm  = [dict(s, market="public")  for s in _norm_scenarios(pub_scenarios)]
        priv_norm = [dict(s, market="private") for s in _norm_scenarios(priv_scenarios)]
        norm_scenarios = pub_norm + priv_norm

        verdict_src = extracted.get("verdict", "미상")

        # §1 거시 시장 요약 — LLM block_market_macro 우선, 없으면 기본값
        _macro_text = (
            pub_analysis.get("block_market_macro")
            or priv_analysis.get("block_market_macro")
            or (
                "칠레는 인구 약 1,926만 명의 남미 최대 의약품 수입국 중 하나로, "
                "의약품 시장 규모는 약 USD 24.5억(2024)이며 수입 의존도 80%+입니다. "
                "공공(FONASA·CENABAST)과 민간(ISAPRE·Cruz Verde·Salcobrand·Farmacias Ahumada) "
                "이중 구조로 운영되며 ISP(Instituto de Salud Pública)가 등록·허가를 담당합니다."
            )
        )

        # §2 기준 단가 보강 — LLM block_extract
        _extract_text = (
            pub_analysis.get("block_extract")
            or priv_analysis.get("block_extract")
            or ""
        )

        p2_data = {
            "product_name":    extracted.get("product_name", "미상"),
            "inn_label":       extracted.get("product_name", ""),
            "verdict":         verdict_src,
            "seg_label":       "공공 시장 + 민간 시장 통합",
            "base_price":      pub_analysis.get("final_price_usd", 0),
            "formula_str": (
                f"공공 FOB USD {pub_analysis.get('final_price_usd', 0):.2f} / "
                f"민간 FOB USD {priv_analysis.get('final_price_usd', 0):.2f}"
            ),
            "mode_label":      "AI 분석 (Claude Haiku · 칠레 IVA 역산 · 공공+민간)",
            "scenarios":       norm_scenarios,
            "ai_rationale": [
                pub_analysis.get("rationale", ""),
                priv_analysis.get("rationale", ""),
            ],
            # §3 크롤 경쟁가 (Cruz Verde / Salcobrand / CENABAST 실측)
            "competitor_prices": _competitor_prices_p2,
            # §1 칠레 거시 시장 요약 (LLM block_market_macro 우선)
            "market_summary":   _macro_text,
            # §2 기준 단가 보강 (LLM block_extract)
            "extract_summary":  _extract_text,
            # LLM 생성 전략·리스크 텍스트 블록 (보고서 §4 추가 서술용)
            "block_strategy_public":  pub_analysis.get("block_strategy",  ""),
            "block_strategy_private": priv_analysis.get("block_strategy", ""),
            "block_risks":            (
                pub_analysis.get("block_risks")
                or priv_analysis.get("block_risks") or ""
            ),
            "block_scenario_penetration": (
                pub_analysis.get("block_scenario_penetration")
                or priv_analysis.get("block_scenario_penetration") or ""
            ),
            "block_scenario_reference": (
                pub_analysis.get("block_scenario_reference")
                or priv_analysis.get("block_scenario_reference") or ""
            ),
            "block_scenario_premium": (
                pub_analysis.get("block_scenario_premium")
                or priv_analysis.get("block_scenario_premium") or ""
            ),
            "public_data_src":  "Mercado Público 낙찰가, CENABAST Ley 21.198 공급가, DB 크롤 데이터",
            "private_data_src": "Cruz Verde · Salcobrand · Farmacias Ahumada 소매가, DB 크롤 데이터",
        }

        from report_generator import render_p2_pdf
        await asyncio.to_thread(render_p2_pdf, p2_data, _pdf_path_p2)

        _p2_ai_task["pdf"] = _pdf_name_p2
        _p2_ai_task.update({"status": "done", "step": "done", "step_label": "완료"})
        await _emit({"phase": "p2_pipeline", "message": "P2 파이프라인 완료", "level": "success"})

    except Exception as exc:
        _p2_ai_task.update({"status": "error", "step": "error", "step_label": str(exc)[:300]})
        await _emit({"phase": "p2_pipeline", "message": f"P2 오류: {exc}", "level": "error"})


class UploadBody(BaseModel):
    filename: str
    content_b64: str  # base64 인코딩된 PDF 바이너리


@app.post("/api/p2/upload")
async def upload_p2_pdf(body: UploadBody) -> JSONResponse:
    """P2 파이프라인용 PDF 업로드 (base64 JSON — python-multipart 불필요)."""
    import base64
    import re as _re_up

    fname = body.filename or "upload.pdf"
    if not fname.lower().endswith(".pdf"):
        raise HTTPException(400, "PDF 파일(.pdf)만 업로드 가능합니다.")

    try:
        content = base64.b64decode(body.content_b64)
    except Exception:
        raise HTTPException(400, "base64 디코딩 실패 — 올바른 PDF 파일인지 확인하세요.")

    safe_fname = _re_up.sub(r"[^\w가-힣\-\.]", "_", fname)[:80]
    _reports_dir = ROOT / "reports"
    _reports_dir.mkdir(parents=True, exist_ok=True)
    dest = _reports_dir / f"upload_{safe_fname}"
    dest.write_bytes(content)

    return JSONResponse({"ok": True, "filename": dest.name})


class P2PipelineBody(BaseModel):
    report_filename: str = ""  # reports/ 내 파일명 (비어 있으면 최신 1공정 PDF 사용)
    market: str = "public"     # "public" | "private"


@app.post("/api/p2/pipeline")
async def trigger_p2_pipeline(body: P2PipelineBody) -> JSONResponse:
    """2공정 AI 파이프라인 실행."""
    global _p2_ai_task
    if _p2_ai_task.get("status") == "running":
        raise HTTPException(409, "P2 파이프라인이 이미 실행 중입니다.")

    if body.report_filename:
        report_path = ROOT / "reports" / Path(body.report_filename).name
    else:
        report_path = _latest_report_pdf()

    if not report_path or not Path(report_path).is_file():
        raise HTTPException(404, f"보고서 파일을 찾을 수 없습니다: {body.report_filename or '(최신 PDF 없음)'}")

    _p2_ai_task = {
        "status":        "running",
        "step":          "extract",
        "step_label":    "시작 중…",
        "extracted":     None,
        "exchange_rates": None,
        "analysis":      None,   # 하위호환
        "public":        None,
        "private":       None,
        "pdf":           None,
    }
    asyncio.create_task(_run_p2_ai_pipeline(str(report_path), body.market))
    return JSONResponse({"ok": True})


@app.get("/api/p2/pipeline/status")
async def p2_pipeline_status_ai() -> JSONResponse:
    if not _p2_ai_task:
        return JSONResponse({"status": "idle"})
    return JSONResponse({
        "status":     _p2_ai_task.get("status", "idle"),
        "step":       _p2_ai_task.get("step", ""),
        "step_label": _p2_ai_task.get("step_label", ""),
        "has_result": _p2_ai_task.get("public") is not None,
        "has_pdf":    bool(_p2_ai_task.get("pdf")),
    })


@app.get("/api/p2/pipeline/result")
async def p2_pipeline_result_ai() -> JSONResponse:
    if not _p2_ai_task:
        raise HTTPException(404, "P2 파이프라인 미실행")
    return JSONResponse({
        "status":         _p2_ai_task.get("status"),
        "extracted":      _p2_ai_task.get("extracted"),
        "exchange_rates": _p2_ai_task.get("exchange_rates"),
        "public":         _p2_ai_task.get("public"),
        "private":        _p2_ai_task.get("private"),
        "analysis":       _p2_ai_task.get("analysis"),   # 하위호환
        "pdf":            _p2_ai_task.get("pdf"),
    })


# ── 크롤링 파이프라인 ────────────────────────────────────────────────────────────

@app.post("/api/cl/crawl/prices")
async def trigger_price_crawl() -> JSONResponse:
    """Cruz Verde · Salcobrand · Ahumada · CENABAST · Mercado Público 가격 크롤링 트리거."""
    global _crawl_task
    if _crawl_task.get("status") == "running":
        return JSONResponse({"ok": False, "msg": "이미 실행 중입니다."})
    _crawl_task = {"status": "running", "started_at": datetime.utcnow().isoformat(), "rows_saved": 0, "error": None}

    async def _do_crawl():
        global _crawl_task
        try:
            from utils.cl_pricing_pipeline import run_all_crawlers
            from utils.db import upsert_cl_pricing
            from analysis.ch_export_analyzer import _FALLBACK_PRODUCT_META

            inn_names = list({m.get("inn", "").split()[0] for m in _FALLBACK_PRODUCT_META if m.get("inn")})
            rows = await run_all_crawlers(inn_names)
            saved = 0
            for row in rows:
                if "error" not in row and row.get("inn_name"):
                    if upsert_cl_pricing(row):
                        saved += 1
            _crawl_task.update({"status": "done", "rows_saved": saved, "finished_at": datetime.utcnow().isoformat()})
        except Exception as exc:
            _crawl_task.update({"status": "error", "error": str(exc)[:300]})

    asyncio.create_task(_do_crawl())
    return JSONResponse({"ok": True, "msg": "크롤링 시작됨"})


@app.get("/api/cl/crawl/status")
async def crawl_status() -> JSONResponse:
    return JSONResponse(_crawl_task if _crawl_task else {"status": "idle"})


# ── products 조회 ─────────────────────────────────────────────────────────────

@app.get("/api/products")
async def products() -> list[dict[str, Any]]:
    from utils.db import fetch_kup_products
    return fetch_kup_products("CL")


# ── API 키 상태 (U1) ──────────────────────────────────────────────────────────

@app.get("/api/keys/status")
async def keys_status() -> dict[str, Any]:
    """Claude·Perplexity API 키 설정 여부 반환 (실제 키 값은 노출하지 않음)."""
    import os
    claude_key     = os.environ.get("CLAUDE_API_KEY") or os.environ.get("ANTHROPIC_API_KEY", "")
    perplexity_key = os.environ.get("PERPLEXITY_API_KEY", "")
    return {
        "claude":     bool(claude_key.strip()),
        "perplexity": bool(perplexity_key.strip()),
    }


# ── 데이터 소스 상태 (U5·B1) ──────────────────────────────────────────────────

@app.get("/api/datasource/status")
async def datasource_status() -> JSONResponse:
    """Supabase 연결 상태, KUP 품목 수, ISP/CENABAST 컨텍스트 출처 반환."""
    try:
        from utils.db import get_client, fetch_kup_products
        kup_rows = fetch_kup_products("CL")
        kup_count = len(kup_rows)

        # CL 컨텍스트 테이블 점검
        sb = get_client()
        ctx_count = 0
        context_source = "없음"
        try:
            ctx_rows = (
                sb.table("cl_product_context")
                .select("product_id", count="exact")
                .execute()
            )
            ctx_count = ctx_rows.count or 0
            context_source = f"cl_product_context {ctx_count}건" if ctx_count else "products 테이블 폴백"
        except Exception:
            context_source = "조회 실패"

        return JSONResponse({
            "supabase":       "ok",
            "kup_count":      kup_count,
            "context_ok":     ctx_count > 0,
            "context_source": context_source,
            "country":        "CL",
            "message":        f"KUP (칠레) {kup_count}건 로드",
        })
    except Exception as exc:
        return JSONResponse({
            "supabase":       "error",
            "kup_count":      0,
            "context_ok":     False,
            "context_source": "연결 실패",
            "message":        str(exc)[:120],
        })


# ── 상태 / SSE 스트림 ─────────────────────────────────────────────────────────

@app.get("/api/status")
async def status() -> dict[str, Any]:
    lock = _state["lock"]
    assert lock is not None
    async with lock:
        n = len(_state["events"])
    return {"event_count": n}


@app.get("/api/health")
async def health() -> dict[str, Any]:
    """Render 헬스체크용 경량 엔드포인트."""
    return {"ok": True, "service": "cl-analysis-dashboard"}


@app.get("/api/stream")
async def stream() -> StreamingResponse:
    last = 0

    async def gen() -> Any:
        nonlocal last
        while True:
            await asyncio.sleep(0.12)
            chunk: list[dict[str, Any]] = []
            lock = _state["lock"]
            assert lock is not None
            async with lock:
                while last < len(_state["events"]):
                    chunk.append(_state["events"][last])
                    last += 1
            for ev in chunk:
                yield f"data: {json.dumps(ev, ensure_ascii=False)}\n\n"

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "Connection": "keep-alive", "X-Accel-Buffering": "no"},
    )


# ── 3공정: 바이어 발굴 파이프라인 ─────────────────────────────────────────────

_buyer_task: dict[str, Any] = {}

_PROD_LABELS: dict[str, str] = {
    "CL_cilostazol_cr_200":      "Cilostazol CR (Cilostazol 200mg SR · 1일 1회)",
    "CL_ciloduo_cilosta_rosuva": "Ciloduo (Cilostazol SR + Rosuvastatin 복합제)",
    "CL_rosumeg_combigel":       "Rosumeg Combigel (Rosuvastatin+Omega-3)",
    "CL_atmeg_combigel":         "Atmeg Combigel (Atorvastatin+Omega-3)",
    "CL_gastiin_cr_mosapride":   "Gastiin CR (Mosapride citrate 15mg SR)",
    "CL_omethyl_omega3_2g":      "Omethyl Cutielet (Omega-3 에틸에스테르 2g)",
}


class BuyerRunBody(BaseModel):
    product_key:     str = "CL_cilostazol_cr_200"
    active_criteria: list[str] | None = None
    target_country:  str = "Chile"
    target_region:   str = "Latin America"


async def _run_buyer_pipeline(
    product_key: str,
    active_criteria: list[str] | None = None,
    target_country: str = "Chile",
    target_region: str = "Latin America",
) -> None:
    global _buyer_task

    async def _log(msg: str, level: str = "info") -> None:
        await _emit({"phase": "buyer", "message": msg, "level": level})

    try:
        product_label = _PROD_LABELS.get(product_key, product_key)

        # ── Step 1: 1차 수집 (CPHI 크롤링 — 후보 최대 20개) ─────────────
        _buyer_task.update({"step": "crawl", "step_label": "CPHI 크롤링 중…"})
        await _log(f"바이어 발굴 시작 — 품목: {product_label} / 타깃: {target_country} ({target_region})")

        from utils.cphi_crawler import crawl as cphi_crawl
        companies = await cphi_crawl(
            product_key=product_key,
            candidate_pool=20,
            emit=_log,
        )
        _buyer_task["crawl_count"] = len(companies)
        await _log(f"1차 수집 완료 — {len(companies)}개 후보", "success")

        # ── Step 2: 심층조사 (CPHI 전체 텍스트 → Claude Haiku) ───────────
        _buyer_task.update({"step": "enrich", "step_label": "심층조사 중…"})
        await _log("심층조사 시작 (CPHI 페이지 텍스트 → Claude Haiku 파싱)")

        from utils.buyer_enricher import enrich_all
        enriched = await enrich_all(
            companies,
            product_label=product_label,
            target_country=target_country,
            target_region=target_region,
            emit=_log,
        )
        # 전체 후보 풀 저장 — 기준 변경 시 재선택에 사용
        _buyer_task["all_candidates"] = enriched
        await _log(f"심층조사 완료 — {len(enriched)}개", "success")

        # ── Step 3: 상위 10개 선택 ────────────────────────────────────────
        _buyer_task.update({"step": "rank", "step_label": "Top 10 선정 중…"})
        await _log("평가 기준 적용 → Top 10 선정")

        from analysis.buyer_scorer import rank_companies
        ranked = rank_companies(enriched, active_criteria=active_criteria, top_n=10)
        _buyer_task["buyers"] = ranked
        await _log(f"Top {len(ranked)}개 바이어 선정 완료", "success")

        # ── DB 적재: P3 바이어 분석 결과 → cl_analysis_p3 ───────────────────
        try:
            from utils.db import upsert_cl_analysis_p3
            _saved_p3 = 0
            for _rank_idx, _buyer in enumerate(ranked, start=1):
                _p3_row: dict[str, Any] = {
                    "company_name":    (_buyer.get("company_name") or _buyer.get("name", ""))[:255],
                    "company_country": _buyer.get("country", ""),
                    "product_label":   product_label,
                    "product_key":     product_key,
                    # 도메인: 기업 개요
                    "company_overview_kr": _buyer.get("company_overview_kr", ""),
                    "revenue":         _buyer.get("revenue", ""),
                    "employees":       _buyer.get("employees", ""),
                    "founded":         _buyer.get("founded", ""),
                    "territories":     _buyer.get("territories", []),
                    "certifications":  _buyer.get("certifications", []),
                    # 도메인: 자격 검증
                    "has_gmp":         _buyer.get("has_gmp"),
                    "import_history":  _buyer.get("import_history"),
                    "has_pharmacy_chain": _buyer.get("has_pharmacy_chain"),
                    "public_channel":  _buyer.get("public_channel"),
                    "private_channel": _buyer.get("private_channel"),
                    "mah_capable":     _buyer.get("mah_capable"),
                    "procurement_history": _buyer.get("procurement_history"),
                    "korea_experience": _buyer.get("korea_experience", ""),
                    "has_target_country_presence": _buyer.get("has_target_country_presence"),
                    # 도메인: 추천 근거
                    "recommendation_reason": _buyer.get("recommendation_reason", ""),
                    "score":           _buyer.get("score"),
                    "rank_position":   _rank_idx,
                    "source_urls":     _buyer.get("source_urls", []),
                }
                if await asyncio.to_thread(upsert_cl_analysis_p3, _p3_row):
                    _saved_p3 += 1
            await _log(f"P3 결과 DB 저장 완료 — {_saved_p3}개사 (cl_analysis_p3)", "info")
        except Exception as _e_p3db:
            await _log(f"P3 DB 저장 스킵: {_e_p3db}", "warn")

        # ── Step 4: PDF 보고서 생성 ───────────────────────────────────────
        _buyer_task.update({"step": "report", "step_label": "PDF 생성 중…"})
        await _log("바이어 보고서 PDF 생성 중…")

        from datetime import datetime, timezone as _tz_b
        from analysis.buyer_report_generator import build_buyer_pdf
        import re as _re_b

        _ts = datetime.now(_tz_b.utc).strftime("%Y%m%d_%H%M%S")
        _reports_dir = ROOT / "reports"
        _reports_dir.mkdir(parents=True, exist_ok=True)

        safe = _re_b.sub(r"[^\w가-힣]", "_", product_key)[:30]
        pdf_name = f"cl_buyers_{safe}_{_ts}.pdf"
        pdf_path = _reports_dir / pdf_name

        await asyncio.to_thread(build_buyer_pdf, ranked, product_label, pdf_path)
        _buyer_task["pdf"] = pdf_name
        _buyer_task.update({"status": "done", "step": "done", "step_label": "완료"})
        await _log("바이어 발굴 파이프라인 완료", "success")

    except Exception as exc:
        _buyer_task.update({"status": "error", "step": "error", "step_label": str(exc)})
        await _emit({"phase": "buyer", "message": f"오류: {exc}", "level": "error"})


@app.post("/api/buyers/run")
async def trigger_buyers(body: BuyerRunBody | None = None) -> JSONResponse:
    global _buyer_task
    req = body if body is not None else BuyerRunBody()
    if _buyer_task.get("status") == "running":
        raise HTTPException(409, "바이어 발굴이 이미 실행 중입니다.")
    _buyer_task = {
        "status": "running", "step": "crawl", "step_label": "시작 중…",
        "crawl_count": 0, "all_candidates": [], "buyers": [], "pdf": None,
    }
    asyncio.create_task(_run_buyer_pipeline(
        req.product_key,
        req.active_criteria,
        req.target_country,
        req.target_region,
    ))
    return JSONResponse({"ok": True})


@app.get("/api/buyers/status")
async def buyer_status() -> JSONResponse:
    if not _buyer_task:
        return JSONResponse({"status": "idle"})
    return JSONResponse({
        "status":          _buyer_task.get("status", "idle"),
        "step":            _buyer_task.get("step", ""),
        "step_label":      _buyer_task.get("step_label", ""),
        "crawl_count":     _buyer_task.get("crawl_count", 0),
        "buyer_count":     len(_buyer_task.get("buyers", [])),
        "candidate_count": len(_buyer_task.get("all_candidates", [])),
        "has_pdf":         bool(_buyer_task.get("pdf")),
    })


@app.get("/api/buyers/result")
async def buyer_result() -> JSONResponse:
    if not _buyer_task:
        raise HTTPException(404, "바이어 발굴 미실행")
    return JSONResponse({
        "status":  _buyer_task.get("status"),
        "buyers":  _buyer_task.get("buyers", []),
        "pdf":     _buyer_task.get("pdf"),
    })


@app.post("/api/buyers/rerank")
async def buyer_rerank(body: dict = None) -> JSONResponse:
    """기준 변경 시 전체 후보 풀(20개)에서 재선택."""
    all_candidates = _buyer_task.get("all_candidates", [])
    if not all_candidates:
        raise HTTPException(404, "후보 풀 없음. 파이프라인을 먼저 실행하세요.")
    criteria = (body or {}).get("criteria")
    from analysis.buyer_scorer import rank_companies
    ranked = rank_companies(all_candidates, active_criteria=criteria, top_n=10)
    _buyer_task["buyers"] = ranked
    return JSONResponse({"buyers": ranked})


@app.get("/api/buyers/report/download")
async def buyer_report_download(name: str | None = None) -> Any:
    reports_dir = ROOT / "reports"
    if name:
        target = reports_dir / Path(name).name
        if target.is_file():
            return FileResponse(
                str(target), media_type="application/pdf",
                filename=target.name, content_disposition_type="attachment",
            )
    # 최신 buyers PDF
    pdfs = sorted(reports_dir.glob("cl_buyers_*.pdf"), key=lambda p: p.stat().st_mtime, reverse=True)
    if not pdfs:
        pdfs = sorted(reports_dir.glob("sg_buyers_*.pdf"), key=lambda p: p.stat().st_mtime, reverse=True)
    if not pdfs:
        raise HTTPException(404, "바이어 보고서 없음")
    return FileResponse(
        str(pdfs[0]), media_type="application/pdf",
        filename=pdfs[0].name, content_disposition_type="attachment",
    )


# ── 칠레 거시지표 ─────────────────────────────────────────────────────────────

@app.get("/api/cl/macro")
async def api_cl_macro() -> JSONResponse:
    from utils.ch_macro import get_ch_macro
    return JSONResponse(get_ch_macro())


# 하위 호환 (UY 엔드포인트 유지)
@app.get("/api/uy/macro")
async def api_uy_macro() -> JSONResponse:
    from utils.uy_macro import get_uy_macro
    return JSONResponse(get_uy_macro())


# ── UYU/USD 환율 ──────────────────────────────────────────────────────────────

_uyu_exchange_cache: dict[str, Any] = {"data": None, "ts": 0.0}
_UYU_EXCHANGE_TTL = 300.0


@app.get("/api/exchange/uyu")
async def api_exchange_uyu() -> JSONResponse:
    import time as _time

    if _uyu_exchange_cache["data"] and _time.time() - _uyu_exchange_cache["ts"] < _UYU_EXCHANGE_TTL:
        return JSONResponse(_uyu_exchange_cache["data"])

    def _fetch_uyu() -> dict[str, Any]:
        import yfinance as yf  # type: ignore[import]
        uyu_usd = float(yf.Ticker("UYUUSD=X").fast_info.last_price)
        usd_krw = float(yf.Ticker("USDKRW=X").fast_info.last_price)
        return {
            "uyu_usd": round(uyu_usd, 6),
            "usd_krw": round(usd_krw, 2),
            "uyu_krw": round(uyu_usd * usd_krw, 4),
            "source": "Yahoo Finance",
            "fetched_at": _time.time(),
            "ok": True,
        }

    try:
        loop = asyncio.get_event_loop()
        data = await loop.run_in_executor(None, _fetch_uyu)
        _uyu_exchange_cache["data"] = data
        _uyu_exchange_cache["ts"] = _time.time()
        return JSONResponse(data)
    except Exception as exc:
        fallback: dict[str, Any] = {
            "uyu_usd": 0.02481,
            "usd_krw": 1393.0,
            "uyu_krw": 34.57,
            "source": "폴백 (Yahoo Finance 연결 실패)",
            "fetched_at": time.time(),
            "ok": False,
            "error": str(exc),
        }
        return JSONResponse(fallback)


# ── 우루과이 크롤링 파이프라인 ────────────────────────────────────────────────────

_uy_crawl_cache: dict[str, Any] = {"result": None, "running": False}


class UyCrawlBody(BaseModel):
    inn_names: list[str] = ["Cilostazol"]
    save_db: bool = True


@app.post("/api/uy/crawl")
async def trigger_uy_crawl(body: UyCrawlBody | None = None) -> JSONResponse:
    req = body if body is not None else UyCrawlBody()
    if _uy_crawl_cache["running"]:
        raise HTTPException(status_code=409, detail="UY 크롤링이 이미 실행 중입니다.")

    async def _run() -> None:
        _uy_crawl_cache["running"] = True
        try:
            from analysis.uy_export_analyzer import analyze_uy_market
            result = await analyze_uy_market(
                inn_names=req.inn_names,
                save_db=req.save_db,
                emit=_emit,
            )
            _uy_crawl_cache["result"] = result
        finally:
            _uy_crawl_cache["running"] = False

    asyncio.create_task(_run())
    return JSONResponse({"ok": True, "message": f"{req.inn_names} UY 크롤링 시작"})


@app.get("/api/uy/crawl/status")
async def uy_crawl_status() -> JSONResponse:
    return JSONResponse({
        "running": _uy_crawl_cache["running"],
        "has_result": _uy_crawl_cache["result"] is not None,
        "result": _uy_crawl_cache["result"],
    })


# ── 칠레 크롤링 파이프라인 ────────────────────────────────────────────────────

_cl_crawl_cache: dict[str, Any] = {"result": None, "running": False}


class ClCrawlBody(BaseModel):
    inn_names: list[str] = ["Cilostazol"]
    sources: list[str] = ["cruzverde", "salcobrand", "ahumada", "mercadopublico", "cenabast"]
    save_db: bool = True


@app.post("/api/cl/crawl")
async def trigger_cl_crawl(body: ClCrawlBody | None = None) -> JSONResponse:
    """칠레 5개 소스 크롤링 실행 (v2: INN 정규화 + 이상치 탐지 적용)."""
    req = body if body is not None else ClCrawlBody()
    if _cl_crawl_cache["running"]:
        raise HTTPException(status_code=409, detail="CL 크롤링이 이미 실행 중입니다.")

    async def _run() -> None:
        import time as _time
        _cl_crawl_cache["running"] = True
        results: list[dict[str, Any]] = []
        try:
            # ── 크롤링 ──────────────────────────────────────────────────────
            for src in req.sources:
                await _emit({"phase": "cl_crawl", "message": f"{src} 크롤링 시작", "level": "info"})
                try:
                    if src == "cruzverde":
                        from utils.cl_cruzverde_crawler import crawl
                    elif src == "salcobrand":
                        from utils.cl_salcobrand_crawler import crawl  # type: ignore[assignment]
                    elif src == "ahumada":
                        from utils.cl_ahumada_crawler import crawl  # type: ignore[assignment]
                    elif src == "mercadopublico":
                        from utils.cl_mercadopublico_crawler import crawl  # type: ignore[assignment]
                    elif src == "cenabast":
                        from utils.cl_cenabast_crawler import crawl  # type: ignore[assignment]
                    else:
                        continue
                    rows = await crawl(req.inn_names)
                    results.extend(rows)
                    ok_cnt  = sum(1 for r in rows if "error" not in r)
                    err_cnt = len(rows) - ok_cnt
                    msg     = f"{src} {ok_cnt}건 수집" + (f" ({err_cnt}건 오류)" if err_cnt else "")
                    await _emit({"phase": "cl_crawl", "message": msg,
                                 "level": "success" if ok_cnt else "warn"})
                except Exception as exc:
                    await _emit({"phase": "cl_crawl", "message": f"{src} 오류: {exc}", "level": "error"})

            # ── 후처리: INN 정규화 + 이상치 탐지 ───────────────────────────
            try:
                from utils.cl_inn_normalizer import get_normalizer as _get_norm
                from utils.cl_outlier_detector import flag_record as _flag_record

                inn_norm = _get_norm()
                # 정상 레코드만 후처리
                clean_raw = [
                    r for r in results
                    if "error" not in r and r.get("raw_price_clp") is not None
                ]

                # inn_name 그룹별 기존 가격 집계 (배치 내 비교)
                _price_groups: dict[str, list[float]] = {}
                for r in clean_raw:
                    key = (r.get("inn_name") or "").lower()
                    _price_groups.setdefault(key, [])

                enriched: list[dict[str, Any]] = []
                for r in clean_raw:
                    # INN 정규화
                    rec = inn_norm.normalize_record(r)
                    # crawled_at 타임스탬프 추가
                    rec["crawled_at"] = _time.strftime("%Y-%m-%dT%H:%M:%SZ", _time.gmtime())
                    # 이상치 탐지
                    key = (rec.get("inn_name") or "").lower()
                    existing = _price_groups.get(key, [])
                    rec = _flag_record(rec, existing)
                    # 이 레코드의 가격을 그룹에 추가 (다음 레코드 비교용)
                    price_f = rec.get("raw_price_clp")
                    if price_f is not None:
                        _price_groups.setdefault(key, []).append(float(price_f))
                    enriched.append(rec)

                flagged_cnt = sum(1 for r in enriched if r.get("outlier_flagged"))
                await _emit({
                    "phase": "cl_crawl",
                    "message": (
                        f"후처리 완료 — INN 정규화 {len(enriched)}건"
                        + (f", 이상치 플래그 {flagged_cnt}건" if flagged_cnt else "")
                    ),
                    "level": "info",
                })

            except Exception as exc_post:
                await _emit({"phase": "cl_crawl", "message": f"후처리 경고: {exc_post}", "level": "warn"})
                enriched = [
                    {**r, "crawled_at": _time.strftime("%Y-%m-%dT%H:%M:%SZ", _time.gmtime())}
                    for r in results
                    if "error" not in r and r.get("raw_price_clp") is not None
                ]

            # ── DB 저장 ─────────────────────────────────────────────────────
            if req.save_db and enriched:
                try:
                    from utils.db import get_supabase_client
                    sb = get_supabase_client()
                    # ocds_data 등 직렬화 불가 필드 제거
                    _REMOVE_FIELDS = {"ocds_data", "raw_text"}
                    db_rows = [
                        {k: v for k, v in r.items() if k not in _REMOVE_FIELDS}
                        for r in enriched
                    ]
                    sb.table("cl_pricing").insert(db_rows).execute()
                    await _emit({
                        "phase": "cl_crawl",
                        "message": f"DB 저장 완료 ({len(db_rows)}건)",
                        "level": "success",
                    })
                except Exception as exc_db:
                    await _emit({"phase": "cl_crawl", "message": f"DB 저장 실패: {exc_db}", "level": "warn"})

            _cl_crawl_cache["result"] = results
        finally:
            _cl_crawl_cache["running"] = False

    asyncio.create_task(_run())
    return JSONResponse({"ok": True, "message": f"{req.inn_names} CL 크롤링 시작"})


@app.get("/api/cl/crawl/status")
async def cl_crawl_status() -> JSONResponse:
    return JSONResponse({
        "running": _cl_crawl_cache["running"],
        "has_result": _cl_crawl_cache["result"] is not None,
        "result_count": len(_cl_crawl_cache["result"]) if _cl_crawl_cache["result"] else 0,
    })


@app.get("/api/cl/pricing")
async def api_cl_pricing(inn_name: str | None = None, limit: int = 100) -> JSONResponse:
    try:
        from utils.db import get_supabase_client
        sb = get_supabase_client()
        query = sb.table("cl_pricing").select("*").order("crawled_at", desc=True).limit(limit)
        if inn_name:
            query = query.ilike("inn_name", f"%{inn_name}%")
        result = query.execute()
        return JSONResponse({"ok": True, "count": len(result.data), "rows": result.data})
    except Exception as exc:
        return JSONResponse({"ok": False, "error": str(exc)[:200], "rows": []})


@app.get("/api/cl/cenabast")
async def api_cl_cenabast(inn_name: str | None = None) -> JSONResponse:
    """CENABAST 소매 상한가 조회."""
    try:
        from utils.db import get_supabase_client
        sb = get_supabase_client()
        query = sb.table("cl_cenabast_prices").select("*").order("scraped_at", desc=True).limit(50)
        if inn_name:
            query = query.ilike("inn_name", f"%{inn_name}%")
        result = query.execute()
        return JSONResponse({"ok": True, "count": len(result.data), "rows": result.data})
    except Exception as exc:
        return JSONResponse({"ok": False, "error": str(exc)[:200], "rows": []})


# ── CLP/KRW 환율 ──────────────────────────────────────────────────────────────

_clp_exchange_cache: dict[str, Any] = {"data": None, "ts": 0.0}
_CLP_EXCHANGE_TTL = 300.0


@app.get("/api/exchange/clp")
async def api_exchange_clp() -> JSONResponse:
    import time as _time

    if _clp_exchange_cache["data"] and _time.time() - _clp_exchange_cache["ts"] < _CLP_EXCHANGE_TTL:
        return JSONResponse(_clp_exchange_cache["data"])

    def _fetch_clp() -> dict[str, Any]:
        import yfinance as yf  # type: ignore[import]
        usd_clp = float(yf.Ticker("USDCLP=X").fast_info.last_price)
        usd_krw = float(yf.Ticker("USDKRW=X").fast_info.last_price)
        clp_usd = 1.0 / usd_clp if usd_clp else 0
        return {
            "usd_clp": round(usd_clp, 2),
            "clp_usd": round(clp_usd, 8),
            "usd_krw": round(usd_krw, 2),
            "clp_krw": round(clp_usd * usd_krw, 6),
            "source": "Yahoo Finance",
            "fetched_at": _time.time(),
            "ok": True,
        }

    try:
        loop = asyncio.get_event_loop()
        data = await loop.run_in_executor(None, _fetch_clp)
        _clp_exchange_cache["data"] = data
        _clp_exchange_cache["ts"] = _time.time()
        return JSONResponse(data)
    except Exception as exc:
        fallback: dict[str, Any] = {
            "usd_clp": 932.0,
            "clp_usd": 0.001073,
            "usd_krw": 1393.0,
            "clp_krw": 1.495,
            "source": "폴백 (Yahoo Finance 연결 실패)",
            "fetched_at": time.time(),
            "ok": False,
            "error": str(exc),
        }
        return JSONResponse(fallback)


@app.get("/api/uy/pricing")
async def api_uy_pricing(inn_name: str | None = None, limit: int = 100) -> JSONResponse:
    try:
        from utils.db import get_supabase_client
        sb = get_supabase_client()
        query = sb.table("uy_pricing").select("*").order("crawled_at", desc=True).limit(limit)
        if inn_name:
            query = query.ilike("inn_name", f"%{inn_name}%")
        result = query.execute()
        return JSONResponse({"ok": True, "count": len(result.data), "rows": result.data})
    except Exception as exc:
        return JSONResponse({"ok": False, "error": str(exc)[:200], "rows": []})


# ── FOB 역산기 ────────────────────────────────────────────────────────────────

class FobBody(BaseModel):
    price_usd: float
    market_segment: str = "private"
    inn_name: str = ""
    import_duty_pct: float | None = None


@app.post("/api/fob/calculate")
async def api_fob_calculate(body: FobBody) -> JSONResponse:
    from analysis.fob_calculator import (
        calc_logic_a, calc_logic_b, fob_result_to_dict, msp_copayment_check
    )
    from decimal import Decimal

    price = Decimal(str(body.price_usd))
    if body.market_segment == "public":
        duty = Decimal(str(body.import_duty_pct / 100)) if body.import_duty_pct else None
        result = calc_logic_a(price, import_duty_rate=duty, inn_name=body.inn_name)
    else:
        result = calc_logic_b(price, inn_name=body.inn_name)

    d = fob_result_to_dict(result)
    d["msp_check"] = msp_copayment_check(result.base.fob_usd)
    return JSONResponse({"ok": True, **d})


# ── 인도네시아 AHP 파트너 매칭 ────────────────────────────────────────────────────

@app.get("/api/ahp/partners")
async def api_ahp_partners() -> JSONResponse:
    from analysis.ahp_matcher import score_all_candidates, ahp_results_to_dicts
    results = score_all_candidates()
    return JSONResponse({"ok": True, "count": len(results), "partners": ahp_results_to_dicts(results)})


# ── 우루과이 시장 뉴스 (Perplexity) ────────────────────────────────────────────

_uy_news_cache: dict[str, Any] = {"data": None, "ts": 0.0}
_UY_NEWS_TTL = 1800


@app.get("/api/cl/news")
async def api_cl_news() -> JSONResponse:
    """Perplexity 기반 칠레 제약 시장 뉴스 (30분 캐시) — /api/news 와 동일."""
    return await api_news()


@app.get("/api/uy/news")
async def api_uy_news() -> JSONResponse:
    import time as _time
    import os
    import httpx

    if _uy_news_cache["data"] and _time.time() - _uy_news_cache["ts"] < _UY_NEWS_TTL:
        return JSONResponse(_uy_news_cache["data"])

    px_key = os.environ.get("PERPLEXITY_API_KEY", "").strip()
    if not px_key:
        return JSONResponse({"ok": False, "error": "PERPLEXITY_API_KEY 미설정", "items": []})

    try:
        payload = {
            "model": "sonar-pro",
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "You are a Uruguay pharmaceutical market analyst. "
                        "Return ONLY a JSON array with up to 6 recent news items. "
                        "All 'title' values MUST be written in Korean (한국어)."
                    ),
                },
                {
                    "role": "user",
                    "content": (
                        "Find the latest Uruguay pharmaceutical market, regulatory news, "
                        "and drug pricing policy (ASSE, MSP, ARCE). "
                        "Return strict JSON array. Each item: title (Korean), source, date, link."
                    ),
                },
            ],
            "max_tokens": 900,
            "temperature": 0.2,
        }
        headers = {"Authorization": f"Bearer {px_key}", "Content-Type": "application/json"}
        async with httpx.AsyncClient(timeout=20.0) as client:
            resp = await client.post(
                "https://api.perplexity.ai/chat/completions",
                headers=headers,
                json=payload,
            )
            resp.raise_for_status()
            raw = resp.json()

        content = str(raw.get("choices", [{}])[0].get("message", {}).get("content", ""))
        items = _parse_perplexity_news_items(content)
        data = {"ok": bool(items), "items": items}
        _uy_news_cache["data"] = data
        _uy_news_cache["ts"] = _time.time()
        return JSONResponse(data)
    except Exception as exc:
        return JSONResponse({"ok": False, "error": str(exc)[:120], "items": []})


@app.get("/")
async def index() -> FileResponse:
    index_path = STATIC / "index.html"
    if not index_path.is_file():
        raise HTTPException(status_code=404, detail="index.html 없음")
    return FileResponse(index_path)


@app.get("/frontend3")
async def frontend3() -> FileResponse:
    path = STATIC / "frontend3.html"
    if not path.is_file():
        raise HTTPException(status_code=404, detail="frontend3.html 없음")
    return FileResponse(path)


app.mount("/static", StaticFiles(directory=str(STATIC)), name="static")


def main() -> None:
    import uvicorn

    parser = argparse.ArgumentParser(description="SG 분석 대시보드")
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--open", action="store_true")
    args = parser.parse_args()

    if args.open:
        def _open_later() -> None:
            time.sleep(1.0)
            webbrowser.open(f"http://127.0.0.1:{args.port}/")
        threading.Thread(target=_open_later, daemon=True).start()

    print(f"\n  ▶ 칠레 대시보드: http://127.0.0.1:{args.port}/\n")
    uvicorn.run(app, host=args.host, port=args.port, reload=False)


if __name__ == "__main__":
    main()
