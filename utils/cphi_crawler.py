"""CPHI Japan 전시회 크롤러 — 1차 수집.

사이트: https://www.informa-japan.com/cphifcj/complist/en/
  index.php  → sidSuffix 취득 + 전체 목록
  searchlist.php → keyword / country 검색
  detail.php → 기업 상세 (성분·연락처·개요)

수집 전략:
  1단계: 품목 성분 키워드 + 치료군 키워드 검색 → ingredient_bucket
  2단계: 전체 exid 풀에서 랜덤 샘플링 → supplement_bucket (국가 필터 없음)
  최종: ingredient_bucket + supplement_bucket → candidate_pool개 확보
  ※ 국가/지역 적합성 판단은 Perplexity 실시간 검색에 위임
"""

from __future__ import annotations

import asyncio
import re
from typing import Any, Awaitable, Callable

import httpx

BASE = "https://www.informa-japan.com/cphifcj/complist/en"

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.informa-japan.com/",
}

_DELAY = 1.5

# 품목별 검색 키워드 맵
PRODUCT_SEARCH_MAP: dict[str, dict[str, list[str]]] = {
    "SG_sereterol_activair": {
        "ingredients": ["Fluticasone", "Salmeterol", "ICS LABA"],
        "therapeutic": ["respiratory", "asthma", "COPD", "inhaler"],
    },
    "SG_omethyl_omega3_2g": {
        "ingredients": ["Omega-3", "EPA", "DHA", "fish oil"],
        "therapeutic": ["cardiovascular", "lipid", "triglyceride"],
    },
    "SG_hydrine_hydroxyurea_500": {
        "ingredients": ["Hydroxyurea", "Hydroxycarbamide"],
        "therapeutic": ["oncology", "antineoplastic", "hematology"],
    },
    "SG_gadvoa_gadobutrol_604": {
        "ingredients": ["Gadobutrol", "gadolinium", "contrast"],
        "therapeutic": ["diagnostic imaging", "MRI", "radiology"],
    },
    "SG_rosumeg_combigel": {
        "ingredients": ["Rosuvastatin", "Omega-3", "statin"],
        "therapeutic": ["cardiovascular", "dyslipidemia"],
    },
    "SG_atmeg_combigel": {
        "ingredients": ["Atorvastatin", "Omega-3", "statin"],
        "therapeutic": ["cardiovascular", "dyslipidemia"],
    },
    "SG_ciloduo_cilosta_rosuva": {
        "ingredients": ["Cilostazol", "Rosuvastatin", "antiplatelet"],
        "therapeutic": ["cardiovascular", "peripheral arterial"],
    },
    "SG_gastiin_cr_mosapride": {
        "ingredients": ["Mosapride", "prokinetic", "gastroprokinetic"],
        "therapeutic": ["gastroenterology", "GERD", "gastroparesis"],
    },
}

# 보충 수집 시 국가 필터를 적용하지 않음.
# Perplexity 실시간 검색이 target_country 관련성을 판단하므로
# CPHI에서는 성분 매칭 기업을 최대한 확보하는 것이 목표.


# ── HTML 파싱 헬퍼 ────────────────────────────────────────────────────────────

def _extract_sid(html: str) -> str:
    m = re.search(r"sidSuffix=(s\d+)", html)
    return m.group(1) if m else ""


def _extract_exids(html: str) -> list[str]:
    return list(dict.fromkeys(re.findall(r"exid=(CF\w+)", html)))


def _clean_tag(s: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", s)).strip()


def _field(html: str, *labels: str) -> str:
    for label in labels:
        patterns = [
            rf"<th[^>]*>\s*{re.escape(label)}\s*</th>\s*<td[^>]*>(.*?)</td>",
            rf"{re.escape(label)}\s*</[^>]+>\s*<[^>]+>(.*?)</[^>]+>",
            rf"<td[^>]*>\s*{re.escape(label)}\s*</td>\s*<td[^>]*>(.*?)</td>",
        ]
        for pat in patterns:
            m = re.search(pat, html, re.S | re.I)
            if m:
                val = _clean_tag(m.group(1))
                if val:
                    return val
    return "-"


def _extract_overview(html: str) -> str:
    """기업 개요/소개 텍스트 추출 (최대 1500자).

    CPHI Japan 실제 HTML 구조:
      - 기업 설명: <div class="productHighlight">...</div>
      - 제품 목록: <div class="product-info pc-only">
    """
    # ── 1순위: CPHI Japan 전용 — productHighlight div ─────────────────────
    m = re.search(r'<div[^>]*class="productHighlight"[^>]*>(.*?)</div>', html, re.S)
    if m:
        text = _clean_tag(m.group(1))
        if len(text) > 50:
            return text[:1500]

    # ── 2순위: 일반 패턴 — 클래스명에 의미있는 키워드 포함 ─────────────────
    for pat in [
        r'<div[^>]*class="[^"]*(?:company-desc|overview|profile|description|about)[^"]*"[^>]*>(.*?)</div>',
        r'<section[^>]*class="[^"]*(?:overview|profile|about)[^"]*"[^>]*>(.*?)</section>',
    ]:
        for m2 in re.finditer(pat, html, re.S | re.I):
            text = _clean_tag(m2.group(1))
            if len(text) > 80:
                return text[:1500]

    # ── 3순위: "Company Profile" / "About" 헤더 이후 <p> 블록 ──────────────
    header_pat = re.compile(
        r'(?:Company\s*Profile|About\s*(?:Us|Company)|Overview|Profile)'
        r'[^<]*</[^>]+>(.*?)(?=<h[1-4]|</(?:section|article|div))',
        re.S | re.I,
    )
    for m3 in header_pat.finditer(html):
        chunks = re.findall(r"<p[^>]*>(.*?)</p>", m3.group(1), re.S)
        combined = " ".join(_clean_tag(c) for c in chunks if len(_clean_tag(c)) > 40)
        if len(combined) > 80:
            return combined[:1500]

    return ""


def _parse_detail(html: str, exid: str) -> dict[str, Any]:
    """detail.php HTML → 구조화된 기업 정보."""

    name = "-"
    for pat in [
        r'<h[12][^>]*class="[^"]*company[^"]*"[^>]*>(.*?)</h[12]>',
        r'<div[^>]*class="[^"]*company[-_]?name[^"]*"[^>]*>(.*?)</div>',
        r'<h[12][^>]*>(.*?)</h[12]>',
        r'<title[^>]*>(.*?)(?:\s*[-|].*)?</title>',
    ]:
        m = re.search(pat, html, re.S | re.I)
        if m:
            candidate = _clean_tag(m.group(1))
            if candidate and len(candidate) > 2:
                name = candidate
                break

    # ── Booth / Category ────────────────────────────────────────────────────
    # CPHI Japan: <div class="title">Booth No.： 2C-05</div>
    booth = "-"
    m_booth = re.search(r'Booth\s*No\.?\s*[：:]\s*([\w-]+)', html, re.I)
    if m_booth:
        booth = m_booth.group(1).strip()
    if booth == "-":
        booth = _field(html, "Booth No.", "Booth", "ブース番号")

    # CPHI Japan: <div class="exhibition-kbn">Category： Ingredients</div>
    category = "-"
    m_cat = re.search(r'Category\s*[：:]\s*</?\w[^>]*>\s*([^\s<]{2,50})', html, re.I)
    if m_cat:
        category = m_cat.group(1).strip()
    if category == "-":
        category = _field(html, "Category", "カテゴリ")

    # ── Address / Country ────────────────────────────────────────────────────
    # CPHI Japan: <th>Address</th><td>주소 COUNTRY</td> 구조
    address = _field(html, "Address", "住所")

    # Country: 전용 필드 없음 → address 끝 대문자 단어에서 추출
    country = "-"
    if address != "-":
        # "Mumbai - 400083, India  INDIA" → INDIA
        m_c = re.search(r'\b([A-Z]{3,})\s*$', address.strip())
        if m_c:
            country = m_c.group(1).capitalize()
        else:
            # "..., India INDIA" 또는 "... India" 형태
            m_c2 = re.search(r',\s*([A-Z][a-z]{2,})\s*(?:[A-Z]+)?\s*$', address.strip())
            if m_c2:
                country = m_c2.group(1)
    # 위에서 못 잡으면 기존 패턴 시도
    if country == "-":
        country = _field(html, "Country", "国", "Nation")
    if country == "-":
        m_c3 = re.search(r"country[^>]*>\s*([A-Z][a-zA-Z ]{2,30})\s*<", html)
        if m_c3:
            country = m_c3.group(1).strip()

    # ── Phone / Fax ──────────────────────────────────────────────────────────
    phone = _field(html, "TEL", "Tel", "Phone", "電話")
    fax   = _field(html, "FAX", "Fax")

    # ── Email ────────────────────────────────────────────────────────────────
    email = _field(html, "E-mail", "Email", "E mail", "メール")
    if email == "-":
        m_e = re.search(r"[\w.+-]+@[\w.-]+\.\w{2,}", html)
        if m_e:
            email = m_e.group(0)

    # ── Website ──────────────────────────────────────────────────────────────
    website = _field(html, "URL", "Website", "Web", "ウェブサイト")
    if website == "-":
        # <a href="http://...">http://... 패턴
        m_w = re.search(r'href="(https?://[^"]{5,100})"', html, re.I)
        if m_w:
            url_cand = m_w.group(1)
            # mailto / 내부 경로 제외
            if "mailto" not in url_cand and "informa-japan" not in url_cand:
                website = url_cand

    # ── Products ─────────────────────────────────────────────────────────────
    # CPHI Japan 실제 구조: <span class="product-detail">제품명<img .../></span>
    products: list[str] = []

    prod_spans = re.findall(
        r'<span[^>]*class="product-detail"[^>]*>\s*(.*?)(?:<img|</span>)',
        html, re.S,
    )
    for raw in prod_spans:
        name_clean = _clean_tag(raw)
        if name_clean and 2 < len(name_clean) < 120 and name_clean not in products:
            products.append(name_clean)

    # 폴백: <th>Product introduction</th> 옆 <td> 스페이스 구분
    if not products:
        m_prod = re.search(
            r'<th[^>]*>\s*Product\s+introduction\s*</th>\s*<td[^>]*>(.*?)</td>',
            html, re.S | re.I,
        )
        if m_prod:
            raw_text = _clean_tag(m_prod.group(1))
            # 스페이스로 구분된 제품명 분리 (대문자 시작 단어 기준)
            parts = re.split(r'\s{2,}', raw_text)
            for p in parts:
                p = p.strip()
                if 3 < len(p) < 100:
                    products.append(p)

    # 폴백2: 기존 <ul><li> 구조
    if not products:
        for ul_m in re.finditer(r"<ul[^>]*>(.*?)</ul>", html, re.S):
            items = re.findall(r"<li[^>]*>(.*?)</li>", ul_m.group(1), re.S)
            for c in [_clean_tag(i) for i in items if _clean_tag(i)]:
                if 3 < len(c) < 100 and c not in products:
                    products.append(c)

    overview_text = _extract_overview(html)

    # HTML 태그 제거 후 전체 페이지 순수 텍스트 — Claude Haiku가 직접 파싱
    full_page_text = re.sub(r"<[^>]+>", " ", html)
    full_page_text = re.sub(r"\s+", " ", full_page_text).strip()[:6000]

    return {
        "exid":          exid,
        "company_name":  name,
        "country":       country,
        "address":       address,
        "phone":         phone,
        "fax":           fax,
        "email":         email,
        "website":       website,
        "booth":         booth,
        "category":      category,
        "products_cphi": products[:30],
        "overview_text": overview_text,
        "full_page_text": full_page_text,
    }


# ── 메인 크롤링 함수 ──────────────────────────────────────────────────────────

async def crawl(
    product_key: str = "SG_sereterol_activair",
    candidate_pool: int = 20,
    min_ingredient: int = 4,
    emit: Callable[[str], Awaitable[None]] | None = None,
    delay: float = _DELAY,
) -> list[dict[str, Any]]:
    """
    CPHI Japan 전시회 크롤링.

    전략:
      Step1: 성분/치료군 키워드 검색 → ingredient_bucket
      Step2: 전체 573개 exid 확보 (빈 POST로 한 번에 가능)
      Step3: ingredient_bucket 외 나머지에서 랜덤 샘플링 → detail 조회
             아시아/관련 국가 기업 우선 선택하여 supplement_bucket 구성
      Step4: ingredient_bucket + supplement_bucket → 최대 candidate_pool개
    """
    import random

    async def _log(msg: str) -> None:
        if emit:
            await emit(msg)

    search_conf = PRODUCT_SEARCH_MAP.get(product_key, {
        "ingredients": [],
        "therapeutic": [],
    })
    ingredient_kws  = search_conf["ingredients"]
    therapeutic_kws = search_conf["therapeutic"]

    # 보충 목표: candidate_pool - ingredient 수
    supplement_target = candidate_pool - len(ingredient_kws)  # 동적 계산은 아래서

    async with httpx.AsyncClient(
        headers=_HEADERS,
        follow_redirects=True,
        timeout=20.0,
    ) as client:

        # ── Step 1: sidSuffix + 전체 exid 목록 확보 ──────────────────────
        await _log("CPHI: 세션 및 전체 기업 목록 수집 중…")
        try:
            r = await client.get(f"{BASE}/index.php")
            sid = _extract_sid(r.text)
        except Exception as e:
            await _log(f"CPHI: index 접속 실패 — {e}")
            sid = ""
        await _log(f"CPHI: sidSuffix={sid or '(없음)'}")

        # 빈 POST → 전체 기업 목록 (573개) 한 번에 반환
        try:
            r_all = await client.post(
                f"{BASE}/searchlist.php",
                data={"sidSuffix": sid} if sid else {},
            )
            all_site_exids = _extract_exids(r_all.text)
            await _log(f"CPHI: 전체 등록 기업 {len(all_site_exids)}개 확인")
        except Exception as e:
            await _log(f"CPHI: 전체 목록 수집 실패 — {e}")
            all_site_exids = []

        async def _do_search(data: dict) -> list[str]:
            if sid:
                data["sidSuffix"] = sid
            try:
                r_post = await client.post(f"{BASE}/searchlist.php", data=data)
                return _extract_exids(r_post.text)
            except Exception as e:
                await _log(f"  검색 오류: {e}")
                return []

        # ── Step 2: 성분/치료군 키워드 검색 → ingredient_bucket ──────────
        ingredient_exids: set[str] = set()

        for kw in ingredient_kws:
            await _log(f"CPHI: 성분 키워드 검색 '{kw}'…")
            await asyncio.sleep(delay)
            exids = await _do_search({"Keyword": kw})
            ingredient_exids.update(exids)
            await _log(f"  → {len(exids)}개 발견 (누적: {len(ingredient_exids)})")

        # 성분 매칭 부족 시 치료군 키워드로 보충
        if len(ingredient_exids) < min_ingredient:
            await _log(f"CPHI: 성분 매칭 {len(ingredient_exids)}개 — 치료군 키워드로 보충")
            for kw in therapeutic_kws:
                await _log(f"CPHI: 치료군 키워드 '{kw}'…")
                await asyncio.sleep(delay)
                exids = await _do_search({"Keyword": kw})
                ingredient_exids.update(exids)
                await _log(f"  → {len(exids)}개 발견 (누적: {len(ingredient_exids)})")
                if len(ingredient_exids) >= min_ingredient:
                    break

        await _log(f"CPHI: 성분 버킷 — {len(ingredient_exids)}개")

        # ── Step 3: 보충 — 전체 풀에서 랜덤 샘플 detail 조회 ────────────
        # ingredient 미포함 exid를 랜덤 샘플링 → detail 조회 → 아시아 국가 우선
        remaining_exids = [e for e in all_site_exids if e not in ingredient_exids]
        random.shuffle(remaining_exids)

        supplement_needed = max(candidate_pool - len(ingredient_exids), 8)
        # 샘플 조회 수: 목표의 3배 (아시아 필터 후 충분히 남도록)
        sample_size = min(supplement_needed * 3, len(remaining_exids), 60)
        sample_exids = remaining_exids[:sample_size]

        await _log(f"CPHI: 보충 후보 {len(remaining_exids)}개 중 {sample_size}개 샘플 조회 시작")

        supplement_bucket: list[dict[str, Any]] = []
        for i, exid in enumerate(sample_exids, 1):
            await asyncio.sleep(delay * 0.8)
            url = f"{BASE}/detail.php?exid={exid}"
            if sid:
                url += f"&sidSuffix={sid}&previous="
            try:
                rd = await client.get(url)
                detail = _parse_detail(rd.text, exid)
            except Exception as e:
                await _log(f"  [보충 {i}/{sample_size}] {exid} 오류: {e}")
                continue

            detail["ingredient_match"]    = False
            detail["matched_ingredients"] = []
            detail["source_region"]       = "supplement"

            supplement_bucket.append(detail)
            country_val = detail.get("country", "-")
            await _log(f"  [보충] {detail.get('company_name', exid)} ({country_val}) ✓")

            if len(supplement_bucket) >= supplement_needed:
                break

        await _log(f"CPHI: 보충 버킷 — {len(supplement_bucket)}개 (국가 필터 없음)")

        # ── Step 4: 성분 버킷 상세 조회 (별도 클라이언트 — 연결 풀 재사용 방지) ──
        ingredient_companies: list[dict[str, Any]] = []
        ing_list = list(ingredient_exids)
        await _log(f"CPHI: 성분 매칭 {len(ing_list)}개 상세 수집…")

    async with httpx.AsyncClient(
        headers=_HEADERS,
        follow_redirects=True,
        timeout=20.0,
    ) as ing_client:
        for i, exid in enumerate(ing_list, 1):
            await asyncio.sleep(delay)
            url = f"{BASE}/detail.php?exid={exid}"
            if sid:
                url += f"&sidSuffix={sid}&previous="

            detail = None
            last_err: Exception | None = None
            for attempt in range(2):
                try:
                    if attempt > 0:
                        await asyncio.sleep(3.0)
                        async with httpx.AsyncClient(
                            headers=_HEADERS, follow_redirects=True, timeout=20.0
                        ) as retry_c:
                            rd = await retry_c.get(url)
                    else:
                        rd = await ing_client.get(url)
                    detail = _parse_detail(rd.text, exid)
                    break
                except Exception as e:
                    last_err = e
                    if attempt == 0:
                        await _log(f"  [{i}/{len(ing_list)}] {exid} 재시도 중…")

            if detail is None:
                await _log(f"  [{i}/{len(ing_list)}] {exid} 오류: {last_err}")
                detail = {
                    "exid": exid, "company_name": exid,
                    "country": "-", "address": "-", "phone": "-",
                    "fax": "-", "email": "-", "website": "-",
                    "booth": "-", "category": "-",
                    "products_cphi": [], "overview_text": "", "full_page_text": "",
                }

            detail["ingredient_match"] = True
            detail["source_region"]    = "ingredient"
            prods_lower = " ".join(detail["products_cphi"]).lower()
            detail["matched_ingredients"] = [
                kw for kw in ingredient_kws if kw.lower() in prods_lower
            ]

            ingredient_companies.append(detail)
            await _log(f"  [{i}/{len(ing_list)}] {detail.get('company_name', exid)} (성분매칭)")

        # ── Step 5: 최종 풀 합산 ─────────────────────────────────────────
        companies = ingredient_companies + supplement_bucket
        await _log(
            f"CPHI: 1차 수집 완료 — 성분매칭 {len(ingredient_companies)}개 "
            f"+ 아시아보충 {len(supplement_bucket)}개 = 총 {len(companies)}개"
        )
        return companies[:candidate_pool]
