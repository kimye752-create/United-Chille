"""
cl_inn_normalizer.py — 칠레 시장 의약품명 INN 정규화 (preon 불필요 경량 버전)

Saudi pharma crawler inn_normalizer.py 기반 — preon 의존성 제거, 칠레 시장 브랜드 추가.

파이프라인 위치:
  크롤링 → [cl_inn_normalizer.py] → INN 표준화 → DB 저장

사용법:
    from utils.cl_inn_normalizer import normalize_to_inn, INNNormalizer
    result = normalize_to_inn("Panadol 500mg")
    # result.inn_name = "paracetamol"
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger("cl_inn_normalizer")


@dataclass
class INNResult:
    """INN 정규화 결과"""
    success: bool
    input_name: str
    inn_name: Optional[str] = None
    inn_id: Optional[str] = None          # ATC 코드
    match_type: str = "none"              # exact / salt_strip / brand / fuzzy / none
    confidence_modifier: float = 0.0
    all_matches: list = field(default_factory=list)

    @property
    def confidence_bonus(self) -> float:
        bonuses = {
            "exact":       0.05,
            "salt_strip":  0.04,
            "brand":       0.03,
            "fuzzy":       0.00,
            "none":       -0.03,
        }
        return bonuses.get(self.match_type, 0.0)


# ─── 칠레 + 글로벌 브랜드 → INN 매핑 ────────────────────
BRAND_TO_INN: dict[str, str] = {
    # 칠레 인기 브랜드 (Cruz Verde / Salcobrand / Ahumada 기준)
    "tapsin":        "paracetamol",          # 칠레 1위 진통제 브랜드
    "tempra":        "paracetamol",
    "karinat":       "paracetamol",
    "itamol":        "paracetamol",
    "acetatil":      "paracetamol",
    "optalidon":     "paracetamol",          # 복합제이지만 주성분
    "cafiaspirina":  "acetylsalicylic acid",
    "kitadol":       "paracetamol",
    "geniol":        "paracetamol",
    "dolex":         "paracetamol",
    # 글로벌 OTC
    "panadol":       "paracetamol",
    "tylenol":       "paracetamol",
    "adol":          "paracetamol",
    "brufen":        "ibuprofen",
    "advil":         "ibuprofen",
    "nurofen":       "ibuprofen",
    "motrin":        "ibuprofen",
    "aspirin":       "acetylsalicylic acid",
    "aspirina":      "acetylsalicylic acid",  # 스페인어 브랜드명
    "voltaren":      "diclofenac",
    "voltarene":     "diclofenac",
    "cataflam":      "diclofenac",
    "celebrex":      "celecoxib",
    "arcoxia":       "etoricoxib",
    "naprosin":      "naproxen",
    "naprosyn":      "naproxen",
    "aleve":         "naproxen",
    # 항생제
    "augmentin":     "amoxicillin",
    "amoxil":        "amoxicillin",
    "amoxicilina":   "amoxicillin",
    "zithromax":     "azithromycin",
    "azitromicina":  "azithromycin",
    "cipro":         "ciprofloxacin",
    "ciprofloxacina":"ciprofloxacin",
    "vibramycin":    "doxycycline",
    "klamycin":      "clarithromycin",
    # 심혈관
    "lipitor":       "atorvastatin",
    "atorvastatina": "atorvastatin",
    "crestor":       "rosuvastatin",
    "rosuvastatina": "rosuvastatin",
    "zocor":         "simvastatin",
    "plavix":        "clopidogrel",
    "clopidogrel":   "clopidogrel",
    "norvasc":       "amlodipine",
    "amlodipino":    "amlodipine",
    "cozaar":        "losartan",
    "losartan":      "losartan",
    "diovan":        "valsartan",
    "micardis":      "telmisartan",
    "concor":        "bisoprolol",
    "bisoprolol":    "bisoprolol",
    "zestril":       "lisinopril",
    "lisinopril":    "lisinopril",
    "coumadin":      "warfarin",
    "warfarina":     "warfarin",
    "lasix":         "furosemide",
    "furosemida":    "furosemide",
    "aldactone":     "spironolactone",
    "espironolactona":"spironolactone",
    # 소화기
    "nexium":        "esomeprazole",
    "losec":         "omeprazole",
    "omeprazol":     "omeprazole",
    "pantoloc":      "pantoprazole",
    "pantoprazol":   "pantoprazole",
    "prevacid":      "lansoprazole",
    "lansoprazol":   "lansoprazole",
    # 내분비
    "glucophage":    "metformin",
    "metformina":    "metformin",
    "lantus":        "insulin glargine",
    "eltroxin":      "levothyroxine",
    "eutirox":       "levothyroxine",    # 칠레에서 흔한 브랜드
    "levotiroxina":  "levothyroxine",
    # 호흡기
    "ventolin":      "salbutamol",
    "salbutamol":    "salbutamol",
    "aerolin":       "salbutamol",       # 칠레 현지
    "flixotide":     "fluticasone",
    "singulair":     "montelukast",
    "montelukast":   "montelukast",
    # 신경계
    "neurontin":     "gabapentin",
    "gabapentina":   "gabapentin",
    "lyrica":        "pregabalin",
    "pregabalina":   "pregabalin",
    "tramal":        "tramadol",
    "tramadol":      "tramadol",
    # 콜레스테롤
    "pravastatina":  "pravastatin",
    "omega-3":       "omega-3 fatty acids",
    # 실로스타졸 (KUP 핵심 품목)
    "cilostazol":    "cilostazol",
    "pletal":        "cilostazol",
    "mosapride":     "mosapride",
    "gastiin":       "mosapride",
    "rosuvastatin":  "rosuvastatin",
    "atorvastatin":  "atorvastatin",
}

# ATC 코드 매핑 (핵심 INN)
INN_TO_ATC: dict[str, str] = {
    "paracetamol":            "N02BE01",
    "ibuprofen":              "M01AE01",
    "acetylsalicylic acid":   "N02BA01",
    "diclofenac":             "M01AB05",
    "celecoxib":              "M01AH01",
    "etoricoxib":             "M01AH05",
    "naproxen":               "M01AE02",
    "amoxicillin":            "J01CA04",
    "azithromycin":           "J01FA10",
    "ciprofloxacin":          "J01MA02",
    "doxycycline":            "J01AA02",
    "clarithromycin":         "J01FA09",
    "metformin":              "A10BA02",
    "atorvastatin":           "C10AA05",
    "rosuvastatin":           "C10AA07",
    "simvastatin":            "C10AA01",
    "clopidogrel":            "B01AC04",
    "amlodipine":             "C08CA01",
    "losartan":               "C09CA01",
    "valsartan":              "C09CA03",
    "telmisartan":            "C09CA07",
    "bisoprolol":             "C07AB07",
    "lisinopril":             "C09AA03",
    "warfarin":               "B01AA03",
    "furosemide":             "C03CA01",
    "spironolactone":         "C03DA01",
    "omeprazole":             "A02BC01",
    "esomeprazole":           "A02BC05",
    "pantoprazole":           "A02BC02",
    "lansoprazole":           "A02BC03",
    "levothyroxine":          "H03AA01",
    "insulin glargine":       "A10AE04",
    "salbutamol":             "R03AC02",
    "fluticasone":            "R03BA05",
    "montelukast":            "R03DC03",
    "gabapentin":             "N03AX12",
    "pregabalin":             "N03AX16",
    "tramadol":               "N02AX02",
    "cilostazol":             "B01AC23",
    "mosapride":              "A03FA",
    "omega-3 fatty acids":    "C10AX06",
}


# ─── 염/수화물/함량 제거 정규식 ───────────────────────────
_SALT_PATTERN = re.compile(
    r'\s+(?:hydrochloride|hcl|besylate|maleate|fumarate|succinate|'
    r'tartrate|sulfate|sulphate|phosphate|acetate|calcium|potassium|'
    r'sodium|magnesium|trihydrate|dihydrate|monohydrate|mesylate|citrate|'
    r'clorhidrato|clorhidratado)\b',
    re.IGNORECASE,
)
_STRENGTH_PATTERN = re.compile(
    r'\s+\d+(?:[.,]\d+)?\s*(?:mg|ml|mcg|ug|µg|g|iu|ui|%)\b.*$',
    re.IGNORECASE,
)
_PARENS_PATTERN = re.compile(r'\s*\([^)]*\)', re.IGNORECASE)


def _strip_extras(name: str) -> str:
    """염, 수화물, 함량, 괄호 제거."""
    text = _PARENS_PATTERN.sub("", name)
    text = _STRENGTH_PATTERN.sub("", text)
    text = _SALT_PATTERN.sub("", text)
    return text.strip().lower()


# ─── 메인 정규화 클래스 ──────────────────────────────────
class INNNormalizer:
    """칠레 시장용 WHO INN 정규화기 (preon 불필요 경량 버전)."""

    def __init__(self, extra_brand_map: dict[str, str] | None = None):
        self._brand_map: dict[str, str] = {k.lower(): v.lower() for k, v in BRAND_TO_INN.items()}
        if extra_brand_map:
            self._brand_map.update({k.lower(): v.lower() for k, v in extra_brand_map.items()})
        # 정방향 INN 셋 (정확히 일치 체크용)
        self._inn_set: set[str] = set(INN_TO_ATC.keys())

    def normalize(self, name: str) -> INNResult:
        """약품명 → INN 정규화.

        매칭 순서:
          1. 브랜드 매핑 테이블 (BRAND_TO_INN)
          2. 염/함량 제거 후 INN 직접 일치
          3. 접두어 부분 일치 (3글자 이상)
        """
        if not name or not name.strip():
            return INNResult(success=False, input_name=name or "", match_type="none")

        clean = name.strip()
        stripped = _strip_extras(clean)

        # ── Step 1: 브랜드 매핑 ──
        # 첫 단어로 시도 (e.g. "Panadol Extra" → "panadol")
        first_word = stripped.split()[0] if stripped else ""
        for key in (stripped, first_word, clean.lower()):
            inn = self._brand_map.get(key)
            if inn:
                atc = INN_TO_ATC.get(inn)
                return INNResult(
                    success=True,
                    input_name=clean,
                    inn_name=inn,
                    inn_id=atc,
                    match_type="brand",
                )

        # ── Step 2: INN 직접 일치 (염/함량 제거 후) ──
        if stripped in self._inn_set:
            return INNResult(
                success=True,
                input_name=clean,
                inn_name=stripped,
                inn_id=INN_TO_ATC.get(stripped),
                match_type="exact",
            )

        # 원본 소문자로도 시도
        clean_lower = clean.lower()
        if clean_lower in self._inn_set:
            return INNResult(
                success=True,
                input_name=clean,
                inn_name=clean_lower,
                inn_id=INN_TO_ATC.get(clean_lower),
                match_type="exact",
            )

        # ── Step 3: 염 제거 → INN 재확인 ──
        # e.g. "losartan potassium" → stripped = "losartan" → 일치
        if stripped and stripped in self._inn_set:
            return INNResult(
                success=True,
                input_name=clean,
                inn_name=stripped,
                inn_id=INN_TO_ATC.get(stripped),
                match_type="salt_strip",
            )

        # ── Step 4: 부분 일치 (접두어 3자 이상) ──
        if len(stripped) >= 4:
            for inn in sorted(self._inn_set):
                if inn.startswith(stripped[:4]) or stripped.startswith(inn[:4]):
                    # 더 구체적인 일치: stripped가 inn의 prefix이거나 반대
                    if inn.startswith(stripped) or stripped.startswith(inn):
                        return INNResult(
                            success=True,
                            input_name=clean,
                            inn_name=inn,
                            inn_id=INN_TO_ATC.get(inn),
                            match_type="fuzzy",
                        )

        return INNResult(success=False, input_name=clean, match_type="none")

    def normalize_record(self, record: dict) -> dict:
        """크롤링 레코드에 INN 정규화 결과 필드 추가.

        추가 필드: inn_name, inn_id, inn_match_type, (confidence 조정)
        원본 필드는 수정하지 않음.
        """
        out = dict(record)

        name_to_check = out.get("scientific_name") or out.get("brand_name") or out.get("inn_name")
        if not name_to_check:
            out["inn_match_type"] = "none"
            return out

        result = self.normalize(str(name_to_check))

        # inn_name 필드가 이미 있으면 덮어쓰지 않고 inn_normalized 사용
        if "inn_name" in out and out["inn_name"]:
            out["inn_normalized"] = result.inn_name
        else:
            out["inn_name"] = result.inn_name or out.get("inn_name", "")

        out["inn_id"]         = result.inn_id
        out["inn_match_type"] = result.match_type

        if "confidence" in out:
            try:
                conf = float(out["confidence"])
                conf = max(0.30, min(0.95, conf + result.confidence_bonus))
                out["confidence"] = conf
            except (TypeError, ValueError):
                pass

        return out


# ─── 싱글턴 편의 함수 ────────────────────────────────────
_default_normalizer: Optional[INNNormalizer] = None


def get_normalizer() -> INNNormalizer:
    global _default_normalizer
    if _default_normalizer is None:
        _default_normalizer = INNNormalizer()
    return _default_normalizer


def normalize_to_inn(name: str) -> INNResult:
    """편의 함수: 약품명 → INN 정규화."""
    return get_normalizer().normalize(name)


# ─── 자가 테스트 ──────────────────────────────────────────
if __name__ == "__main__":
    norm = INNNormalizer()

    # 칠레 브랜드
    r = norm.normalize("Tapsin 500mg")
    assert r.success and r.inn_name == "paracetamol", f"Failed: {r}"

    r = norm.normalize("Panadol 1g")
    assert r.success and r.inn_name == "paracetamol", f"Failed: {r}"

    # 스페인어 INN
    r = norm.normalize("Amoxicilina 500mg")
    assert r.success and r.inn_name == "amoxicillin", f"Failed: {r}"

    # 직접 INN
    r = norm.normalize("cilostazol 100mg")
    assert r.success and r.inn_name == "cilostazol", f"Failed: {r}"

    # INN 소문자
    r = norm.normalize("Losartan Potassium 50mg")
    assert r.success and "losartan" in r.inn_name, f"Failed: {r}"

    print("cl_inn_normalizer 자가 테스트 통과")
