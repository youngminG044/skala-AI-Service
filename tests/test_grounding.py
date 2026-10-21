"""원본 대조 검증. 잘못된 수치·인용·서술을 명시적으로 주입한다."""
import copy
import json
import unittest
from unittest.mock import AsyncMock, Mock, patch

import httpx
from pydantic import ValidationError

from agent.contracts import DraftAnnotations, DraftReport, RunbookCitation, RunbookEvidence
from agent.grounding import canonical_facts, prepare_report, render_report, validate_report


def evidence():
    return [
        {"source_id": "mon-payments", "target": "payments-service", "source_type": "monitoring",
         "reference": "get_monitoring_status", "observed_at": "2026-10-21T09:00:00+09:00", "status": "ok",
         "raw_data": {"system_name": "payments-service", "status": "degraded", "cpu_percent": 91.7,
                      "memory_percent": 55.0, "alerts": ["CPU high"]}},
        {"source_id": "book-cpu", "source_type": "runbook", "reference": "runbook://incident-response/cpu-spike",
         "observed_at": "2026-10-21T09:00:01+09:00", "status": "ok", "applicable_targets": [],
         "raw_data": "# CPU 대응\n\n1. 프로세스를 확인한다.\n2. 부하를 확인한다.\n"},
    ]


TARGETS = ["payments-service"]


def valid_report():
    return prepare_report({"hypotheses": ["부하 증가 가능성"], "missing_checks": ["프로세스 상세 확인 필요"],
                           "recommendations": [{"source_id": "book-cpu", "step": 1,
                                                "target": None}]}, evidence(), TARGETS).model_dump(mode="json")


class GroundingTests(unittest.TestCase):
    def codes(self, report=None, sources=None, targets=None):
        return {issue.code for issue in validate_report(report if report is not None else valid_report(),
            sources if sources is not None else evidence(), targets or TARGETS, 1).issues}

    def test_normal_report_passes_and_preserves_original_sources(self):
        sources = evidence()
        snapshot = copy.deepcopy(sources)
        report = prepare_report({}, sources, TARGETS)
        self.assertEqual(validate_report(report, sources, TARGETS, 1).status, "passed")
        self.assertEqual(sources, snapshot)
        self.assertEqual(len(report.facts), 4)

    def test_model_cannot_supply_or_replace_facts_in_annotations(self):
        with self.assertRaises(ValidationError):
            prepare_report({"facts": []}, evidence(), TARGETS)
        report = prepare_report({}, evidence(), TARGETS)
        self.assertEqual(report.facts[1].value, 91.7)

    def test_model_selects_step_and_code_preserves_exact_original_quote(self):
        report = prepare_report({"recommendations": [{"source_id": "book-cpu", "step": 2}]}, evidence(), TARGETS)
        self.assertEqual(report.recommendations[0].quote, "부하를 확인한다.")
        self.assertEqual(validate_report(report, evidence(), TARGETS, 1).status, "passed")
        with self.assertRaises(ValidationError):
            prepare_report({"recommendations": [{"source_id": "book-cpu", "step": 2,
                                                "quote": "임의로 바꾼 조치"}]}, evidence(), TARGETS)

    def test_monitoring_alert_whitespace_is_preserved_and_empty_alert_is_not_a_fact(self):
        sources = evidence()
        sources[0]["raw_data"]["alerts"] = ["  CPU high  ", ""]
        report = prepare_report({}, sources, TARGETS)
        self.assertEqual(report.facts[-1].value, "  CPU high  ")
        self.assertEqual(len(report.facts), 4)
        self.assertEqual(validate_report(report, sources, TARGETS, 1).status, "passed")

    def test_wrong_numeric_value_type_and_unit_are_rejected(self):
        for changes, code in (({"value": 70}, "value_mismatch"), ({"value": "91.7"}, "value_mismatch"),
                              ({"value": True}, "value_mismatch"), ({"unit": "ms"}, "unit_mismatch"),
                              ({"unit": None}, "unit_mismatch")):
            report = valid_report()
            report["facts"][1].update(changes)
            with self.subTest(changes=changes):
                self.assertIn(code, self.codes(report))

    def test_missing_source_wrong_service_and_uncollected_field_are_rejected(self):
        for changes, code in (({"source_id": "missing"}, "source_missing"),
                              ({"target": "cache-service"}, "target_mismatch"),
                              ({"field": "p95_latency"}, "field_missing")):
            report = valid_report()
            report["facts"][1].update(changes)
            with self.subTest(changes=changes):
                self.assertIn(code, self.codes(report))

    def test_unknown_error_analysis_and_runbook_are_not_monitoring_facts(self):
        for mode in ("unknown", "error", "analysis", "runbook"):
            sources = evidence()
            report = valid_report()
            if mode == "runbook":
                report["facts"][1]["source_id"] = "book-cpu"
            elif mode == "analysis":
                sources[0].update(source_type="analysis", raw_data="CPU 91.7%로 추정")
            else:
                sources[0].update(status=mode, reason="test failure")
            with self.subTest(mode=mode):
                self.assertIn("source_not_observation", self.codes(report, sources))

    def test_ok_wrapper_cannot_turn_unknown_zero_placeholders_into_observations(self):
        sources = evidence()
        sources[0]["raw_data"].update(status="unknown", cpu_percent=0, memory_percent=0)
        self.assertEqual(canonical_facts(sources, TARGETS), ())
        self.assertIn("source_not_observation", self.codes(sources=sources))

    def test_missing_and_duplicate_facts_are_rejected(self):
        report = valid_report()
        report["facts"].pop()
        self.assertIn("fact_missing", self.codes(report))
        report = valid_report()
        report["facts"].append(copy.deepcopy(report["facts"][0]))
        self.assertIn("fact_duplicate", self.codes(report))

    def test_duplicate_or_malformed_source_records_fail_closed(self):
        sources = evidence()
        sources.append(copy.deepcopy(sources[0]))
        self.assertIn("source_duplicate", self.codes(sources=sources))
        sources = evidence()
        del sources[0]["observed_at"]
        self.assertIn("source_schema", self.codes(sources=sources))

    def test_superseded_source_is_not_used_as_current_fact(self):
        sources = evidence()
        newer = copy.deepcopy(sources[0])
        newer.update(source_id="newer")
        sources.append(newer)
        self.assertIn("source_superseded", self.codes(sources=sources))

    def test_citation_wrong_step_or_modified_text_is_rejected(self):
        for changes in ({"step": 9}, {"quote": "메모리를 증설한다."}, {"quote": "프로세스를 확인한다.변조"}):
            report = valid_report()
            report["recommendations"][0].update(changes)
            with self.subTest(changes=changes):
                self.assertIn("runbook_quote", self.codes(report))

    def test_monitoring_or_failed_document_cannot_be_cited_as_runbook(self):
        report = valid_report()
        report["recommendations"][0]["source_id"] = "mon-payments"
        self.assertIn("runbook_source", self.codes(report))
        sources = evidence()
        sources[1].update(status="error", reason="read failed", raw_data=None)
        self.assertIn("runbook_source", self.codes(sources=sources))

    def test_unbound_document_cannot_be_assigned_to_service(self):
        report = valid_report()
        report["recommendations"][0]["target"] = "payments-service"
        self.assertIn("runbook_target", self.codes(report))
        sources = evidence()
        sources[1]["applicable_targets"] = TARGETS
        self.assertEqual(self.codes(report, sources), set())

    def test_common_reference_is_labeled_without_service_suitability_claim(self):
        report = DraftReport.model_validate(valid_report())
        text = render_report(report, evidence(), TARGETS)
        self.assertIn("공통 참고 · 서비스 적합성 미확인", text)
        self.assertIn("runbook://incident-response/cpu-spike 1단계", text)
        self.assertIn("가설 (미검증 추정)", text)

    def test_unsupported_log_claims_numbers_other_services_and_uncited_actions_are_rejected(self):
        cases = [("로그를 분석한 결과 원인을 확인했습니다", "unsupported_observation"),
                 ("로그 점검 완료", "unsupported_observation"),
                 ("We analyzed logs and found the cause", "unsupported_observation"),
                 ("원인이 확인됩니다", "unsupported_observation"),
                 ("CPU 70%로 추정", "text_numeric_claim"),
                 ("cache-service 부하 가능성", "text_target"),
                 ("즉시 재시작하세요", "uncited_action")]
        for text, code in cases:
            report = valid_report()
            report["hypotheses"] = [text]
            with self.subTest(text=text):
                self.assertIn(code, self.codes(report))

    def test_next_step_log_check_is_allowed_without_claiming_it_happened(self):
        report = valid_report()
        report["missing_checks"] = ["최근 로그를 확인할 필요가 있습니다"]
        self.assertEqual(self.codes(report), set())

    def test_identifiers_with_digits_are_not_confused_with_numeric_claims(self):
        sources = evidence()
        sources[0]["target"] = sources[0]["raw_data"]["system_name"] = "web-01"
        report = prepare_report({"hypotheses": ["web-01 부하 증가 가능성"]}, sources, ["web-01"])
        self.assertEqual(validate_report(report, sources, ["web-01"], 1).status, "passed")

    def test_limited_response_uses_original_values_and_omits_model_sections(self):
        report = DraftReport.model_validate(valid_report())
        text = render_report(report, evidence(), TARGETS, limited=True)
        self.assertIn("CPU=91.7%", text)
        self.assertIn("답변 검증에 실패", text)
        self.assertNotIn(report.hypotheses[0], text)
        self.assertNotIn(report.recommendations[0].quote, text)

    def test_source_document_preserves_whitespace_and_schema_rejects_invalid_citation(self):
        source = RunbookEvidence.model_validate(evidence()[1])
        self.assertEqual(source.raw_data, evidence()[1]["raw_data"])
        for step in (0, True, 1.0, "1"):
            with self.subTest(step=step), self.assertRaises(ValidationError):
                RunbookCitation(source_id="book", step=step, quote="test")
        with self.assertRaises(ValidationError):
            DraftAnnotations(facts=[])


class ProviderWriterTests(unittest.IsolatedAsyncioTestCase):
    async def test_ollama_writer_requests_json_and_never_calls_token_callback(self):
        from agent.ollama_llm import OllamaQualityLLM
        callback = Mock()
        client = AsyncMock()
        client.__aenter__.return_value = client
        client.post.return_value = httpx.Response(200, request=httpx.Request("POST", "http://test"),
            json={"message": {"content": '{"hypotheses":[],"missing_checks":[],"recommendations":[]}'},
                  "prompt_eval_count": 10, "eval_count": 20})
        with patch("agent.ollama_llm.httpx.AsyncClient", return_value=client):
            content, usage = await OllamaQualityLLM("test", 0).write("상태 확인", evidence(), 1, None, callback)
        self.assertEqual(json.loads(content)["hypotheses"], [])
        self.assertEqual(usage["tokens"], 30)
        self.assertFalse(client.post.call_args.kwargs["json"]["stream"])
        self.assertEqual(client.post.call_args.kwargs["json"]["format"]["title"], "DraftAnnotations")
        callback.assert_not_called()

    async def test_openai_writer_uses_existing_json_mode_without_network_or_callback(self):
        from agent.openai_llm import OpenAIQualityLLM
        llm, callback = OpenAIQualityLLM("test", 0), Mock()
        with patch.object(llm, "_chat", return_value=('{}', {"usd": 0})) as call:
            content, usage = await llm.write("상태 확인", evidence(), 1, None, callback)
        self.assertEqual(content, '{}')
        self.assertTrue(call.call_args.kwargs["json_mode"])
        callback.assert_not_called()


if __name__ == "__main__":
    unittest.main()
