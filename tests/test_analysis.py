"""분석 엔진 단위 테스트 — 네트워크 없이 정적 폴백 경로 검증."""

from __future__ import annotations

import asyncio
import unittest
from pathlib import Path


class TestExportAnalyzerStatic(unittest.TestCase):
    """API 키 없이 정적 폴백으로 analyze_product 동작 확인."""

    def setUp(self) -> None:
        import sys
        sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
        # API 키 제거 (정적 폴백 강제)
        import os
        self._orig = {
            k: os.environ.pop(k, None)
            for k in ("CLAUDE_API_KEY", "ANTHROPIC_API_KEY", "PERPLEXITY_API_KEY", "PBS_FETCH")
        }
        os.environ["PBS_FETCH"] = "0"

    def tearDown(self) -> None:
        import os
        for k, v in self._orig.items():
            if v is not None:
                os.environ[k] = v

    def _run(self, coro):
        return asyncio.get_event_loop().run_until_complete(coro)

    def test_analyze_all_returns_results(self) -> None:
        """analyze_all이 결과를 반환해야 함 (Supabase에 품목 있으면 8건)."""
        from analysis.sg_export_analyzer import analyze_all
        results = self._run(analyze_all(use_perplexity=False))
        self.assertIsInstance(results, list)

    def test_result_has_required_fields(self) -> None:
        """모든 결과에 필수 필드 존재."""
        from analysis.sg_export_analyzer import analyze_all
        results = self._run(analyze_all(use_perplexity=False))
        required = [
            "product_id",
            "trade_name",
            "verdict",
            "verdict_en",
            "rationale",
            "key_factors",
            "sources",
            "analyzed_at",
            "analysis_error",
            "claude_model_id",
            "price_positioning_pbs",
            "pbs_methodology_label_ko",
        ]
        for r in results:
            for field in required:
                self.assertIn(field, r, f"{r.get('product_id')}: '{field}' 필드 없음")

    def test_verdict_values_valid(self) -> None:
        """verdict는 적합/부적합/조건부 또는 None(API 미설정) 이어야 함."""
        from analysis.sg_export_analyzer import analyze_all
        results = self._run(analyze_all(use_perplexity=False))
        valid = {"적합", "부적합", "조건부", None}
        for r in results:
            self.assertIn(r["verdict"], valid,
                          f"{r.get('product_id')}: verdict={r.get('verdict')!r}")

    def test_verdict_en_values_valid(self) -> None:
        """verdict_en은 SUITABLE/UNSUITABLE/CONDITIONAL 또는 None(API 미설정)."""
        from analysis.sg_export_analyzer import analyze_all
        results = self._run(analyze_all(use_perplexity=False))
        valid = {"SUITABLE", "UNSUITABLE", "CONDITIONAL", None}
        for r in results:
            self.assertIn(r["verdict_en"], valid,
                          f"{r.get('product_id')}: verdict_en={r.get('verdict_en')!r}")

    def test_rationale_not_empty(self) -> None:
        """rationale은 비어있지 않아야 함."""
        from analysis.sg_export_analyzer import analyze_all
        results = self._run(analyze_all(use_perplexity=False))
        for r in results:
            self.assertTrue(
                len(r.get("rationale", "")) > 10,
                f"{r.get('product_id')}: rationale 너무 짧음"
            )

    def test_fallback_model_label(self) -> None:
        """API 키 없으면 analysis_model이 static_fallback이어야 함."""
        from analysis.sg_export_analyzer import analyze_all
        results = self._run(analyze_all(use_perplexity=False))
        for r in results:
            self.assertEqual(
                r.get("analysis_model"), "static_fallback",
                f"{r.get('product_id')}: model={r.get('analysis_model')!r}"
            )
            self.assertEqual(
                r.get("analysis_error"), "no_api_key",
                f"{r.get('product_id')}: analysis_error={r.get('analysis_error')!r}"
            )
            self.assertTrue(
                str(r.get("claude_model_id", "")).startswith("claude-"),
                f"{r.get('product_id')}: claude_model_id={r.get('claude_model_id')!r}"
            )

    def test_extract_assistant_text_skips_thinking_blocks(self) -> None:
        """thinking 블록 뒤의 text 블록만 모아 JSON 본문을 복원해야 함."""
        from analysis.sg_export_analyzer import _extract_assistant_text

        class _Think:
            type = "thinking"

        class _Text:
            type = "text"
            text = '{"verdict": "적합", "verdict_en": "SUITABLE"}'

        class _Msg:
            content = [_Think(), _Text()]

        raw = _extract_assistant_text(_Msg())
        self.assertIn("적합", raw)

    def test_parse_claude_analysis_json_with_preamble(self) -> None:
        """서두 문장이 붙어도 첫 JSON 객체를 파싱해야 함."""
        from analysis.sg_export_analyzer import _parse_claude_analysis_json

        raw = (
            '분석 결과입니다.\n{"verdict": "조건부", "verdict_en": "CONDITIONAL", '
            '"rationale": "x", "key_factors": [], "sources": [], "confidence_note": "n"}\n'
        )
        obj = _parse_claude_analysis_json(raw)
        self.assertIsNotNone(obj)
        assert obj is not None
        self.assertEqual(obj.get("verdict"), "조건부")

    def test_parse_claude_analysis_json_fenced(self) -> None:
        """마크다운 코드펜스 안의 JSON을 파싱해야 함."""
        from analysis.sg_export_analyzer import _parse_claude_analysis_json

        raw = "```json\n{\"verdict\": \"부적합\", \"verdict_en\": \"UNSUITABLE\"}\n```"
        obj = _parse_claude_analysis_json(raw)
        self.assertIsNotNone(obj)
        assert obj is not None
        self.assertEqual(obj.get("verdict"), "부적합")

    def test_parse_claude_analysis_json_verdict_key_case(self) -> None:
        """키가 Verdict처럼 달라도 수용해야 함."""
        from analysis.sg_export_analyzer import _parse_claude_analysis_json

        raw = '{"Verdict": "적합", "verdict_en": "SUITABLE"}'
        obj = _parse_claude_analysis_json(raw)
        self.assertIsNotNone(obj)
        assert obj is not None
        self.assertEqual(obj.get("verdict"), "적합")

    def test_unknown_product_id_returns_error(self) -> None:
        """알 수 없는 product_id는 error 필드를 반환해야 함."""
        from analysis.sg_export_analyzer import analyze_product
        result = self._run(analyze_product("UNKNOWN_PID"))
        self.assertIn("error", result)

    def test_all_product_ids_covered(self) -> None:
        """analyze_all 결과와 _get_product_meta() 품목이 일치."""
        from analysis.sg_export_analyzer import analyze_all, _get_product_meta
        results = self._run(analyze_all(use_perplexity=False))
        result_pids = {r["product_id"] for r in results}
        expected_pids = {m["product_id"] for m in _get_product_meta()}
        self.assertEqual(result_pids, expected_pids)

    def test_gastiin_returns_result(self) -> None:
        """Gastiin CR 분석 결과가 반환되어야 함 (API 미설정 시 verdict=None)."""
        from analysis.sg_export_analyzer import analyze_product
        r = self._run(analyze_product("SG_gastiin_cr_mosapride", use_perplexity=False))
        self.assertIn("product_id", r)
        self.assertIn("hsa_reg", r)
        self.assertIn("entry_pathway", r)

    def test_sereterol_returns_result(self) -> None:
        """Sereterol Activair 분석 결과가 반환되어야 함 (API 미설정 시 verdict=None)."""
        from analysis.sg_export_analyzer import analyze_product
        r = self._run(analyze_product("SG_sereterol_activair", use_perplexity=False))
        self.assertIn("product_id", r)
        self.assertIn("hsa_reg", r)
        self.assertIn("entry_pathway", r)

    def test_key_factors_is_list(self) -> None:
        """key_factors는 리스트여야 함."""
        from analysis.sg_export_analyzer import analyze_all
        results = self._run(analyze_all(use_perplexity=False))
        for r in results:
            self.assertIsInstance(r["key_factors"], list,
                                  f"{r.get('product_id')}: key_factors가 리스트가 아님")

    def test_sources_is_list(self) -> None:
        """sources는 리스트여야 함."""
        from analysis.sg_export_analyzer import analyze_all
        results = self._run(analyze_all(use_perplexity=False))
        for r in results:
            self.assertIsInstance(r["sources"], list,
                                  f"{r.get('product_id')}: sources가 리스트가 아님")

    def test_with_db_row_hsa_reg_present(self) -> None:
        """db_row 제공 시에도 hsa_reg 필드가 반환 결과에 포함."""
        from analysis.sg_export_analyzer import analyze_product
        fake_row = {"price_local": 52.3, "confidence": 0.72}
        r = self._run(
            analyze_product("SG_sereterol_activair", db_row=fake_row, use_perplexity=False)
        )
        self.assertIn("hsa_reg", r)
        self.assertIn("product_type", r)


if __name__ == "__main__":
    unittest.main(verbosity=2)
