"""공통 데이터 구조의 무결성·JSON 복원·대화 간 분리 검증. 모델 호출 없음."""
import json
from datetime import datetime, timezone
from pathlib import Path
import tempfile
import unittest

from pydantic import ValidationError

from agent.contracts import (
    ConversationContext,
    DraftReport,
    EvidenceRecord,
    Fact,
    PendingClarification,
    ResponseState,
    TargetResolution,
    ValidationIssue,
    ValidationResult,
)


def monitoring_payload() -> dict:
    return {
        "source_id": "monitoring-payments-1",
        "target": "payments-service",
        "source_type": "monitoring",
        "reference": "get_monitoring_status",
        "observed_at": datetime(2026, 10, 21, 9, tzinfo=timezone.utc),
        "status": "ok",
        "raw_data": {"cpu_percent": 91.0, "alerts": ["CPU high"]},
    }


def cpu_fact(**overrides) -> Fact:
    return Fact(**{
        "target": "payments-service", "field": "cpu_percent", "value": 91.0,
        "unit": "%", "source_id": "monitoring-payments-1", **overrides,
    })


def failed_validation() -> ValidationResult:
    return ValidationResult(
        status="failed", attempts=1,
        issues=(ValidationIssue(code="value_mismatch", message="CPU 수치가 근거와 다릅니다",
                                target="payments-service", field="cpu_percent"),),
    )


class TargetResolutionTests(unittest.TestCase):
    def test_no_target_requests_clarification_without_a_default(self):
        resolution = TargetResolution()
        self.assertEqual(resolution.requested_targets, ())
        self.assertEqual(resolution.resolved_targets, ())
        self.assertTrue(resolution.needs_clarification)

    def test_known_and_unknown_targets_are_preserved_in_request_order(self):
        resolution = TargetResolution(
            requested_targets=["payments-service", "billing-service", "cache-service"],
            resolved_targets=["payments-service", "cache-service"],
            unresolved_targets=["billing-service"],
        )
        self.assertEqual(resolution.resolved_targets, ("payments-service", "cache-service"))
        self.assertEqual(resolution.unresolved_targets, ("billing-service",))
        self.assertTrue(resolution.needs_clarification)

    def test_fully_resolved_targets_need_no_clarification(self):
        resolution = TargetResolution(
            requested_targets=("payments-service",), resolved_targets=("payments-service",))
        self.assertFalse(resolution.needs_clarification)

    def test_missing_extra_overlapping_and_duplicate_targets_are_rejected(self):
        invalid = [
            {"requested_targets": ["payments-service", "cache-service"],
             "resolved_targets": ["payments-service"]},
            {"requested_targets": ["payments-service"], "resolved_targets": ["auth-service"]},
            {"requested_targets": ["payments-service"], "resolved_targets": ["payments-service"],
             "unresolved_targets": ["payments-service"]},
            {"requested_targets": ["payments-service", "payments-service"],
             "resolved_targets": ["payments-service"]},
            {"requested_targets": ["payments-service"],
             "resolved_targets": ["payments-service", "payments-service"]},
            {"requested_targets": ["billing-service"],
             "unresolved_targets": ["billing-service", "billing-service"]},
        ]
        for payload in invalid:
            with self.subTest(payload=payload), self.assertRaises(ValidationError):
                TargetResolution(**payload)


class EvidenceTests(unittest.TestCase):
    def test_required_provenance_cannot_be_omitted(self):
        for field in ("source_id", "target", "source_type", "reference", "observed_at",
                      "status", "raw_data"):
            payload = monitoring_payload()
            del payload[field]
            with self.subTest(field=field), self.assertRaises(ValidationError):
                EvidenceRecord(**payload)

    def test_runbook_is_associated_with_a_service_and_preserves_text(self):
        payload = {**monitoring_payload(), "source_id": "runbook-payments-1",
                   "source_type": "runbook", "reference": "runbook://incident-response/cpu-spike",
                   "raw_data": "# CPU 대응\n1. 프로세스별 CPU 사용량 확인\n"}
        evidence = EvidenceRecord(**payload)
        restored = EvidenceRecord.model_validate_json(evidence.model_dump_json())
        self.assertEqual(restored, evidence)
        self.assertEqual(restored.target, "payments-service")
        self.assertEqual(restored.raw_data, payload["raw_data"])

    def test_unknown_and_error_require_reasons_and_remain_distinct(self):
        for status in ("unknown", "error"):
            payload = {**monitoring_payload(), "status": status, "raw_data": None}
            with self.subTest(status=status):
                with self.assertRaises(ValidationError):
                    EvidenceRecord(**payload)
                evidence = EvidenceRecord(**payload, reason="조회 대상 미등록 또는 도구 호출 실패")
                restored = EvidenceRecord.model_validate_json(evidence.model_dump_json())
                self.assertEqual(restored.status, status)
                self.assertIsNone(restored.raw_data)

    def test_success_without_data_or_timestamp_without_timezone_is_rejected(self):
        for changes in ({"raw_data": None}, {"observed_at": "2026-10-21T09:00:00"}):
            with self.subTest(changes=changes), self.assertRaises(ValidationError):
                EvidenceRecord(**{**monitoring_payload(), **changes})

    def test_analysis_has_its_own_source_type(self):
        evidence = EvidenceRecord(**{**monitoring_payload(), "source_type": "analysis",
                                    "reference": "analysis_node", "raw_data": "부하 증가 추정"})
        self.assertEqual(evidence.source_type, "analysis")

    def test_raw_data_must_be_finite_json_data(self):
        for value in (object(), {"cpu_percent": float("nan")}, {"cpu_percent": float("inf")}):
            with self.subTest(value=value), self.assertRaises(ValidationError):
                EvidenceRecord(**{**monitoring_payload(), "raw_data": value})

    def test_nested_raw_data_can_be_copied_without_cross_session_mutation(self):
        original_input = monitoring_payload()
        first = EvidenceRecord(**original_input)
        original_input["raw_data"]["alerts"].append("input changed")
        self.assertEqual(first.raw_data["alerts"], ["CPU high"])
        second = first.model_copy(deep=True)
        second.raw_data["alerts"].append("second investigation only")
        self.assertEqual(first.raw_data["alerts"], ["CPU high"])


class DraftTests(unittest.TestCase):
    def test_facts_hypotheses_missing_checks_and_actions_stay_separate(self):
        draft = DraftReport(
            facts=[cpu_fact()], hypotheses=["특정 프로세스 부하 증가 가능성"],
            missing_checks=["프로세스별 CPU 수치 미수집"],
            recommendations=["프로세스별 사용량 확인"],
        )
        restored = DraftReport.model_validate_json(draft.model_dump_json())
        self.assertEqual(restored, draft)
        self.assertIsInstance(restored.facts[0], Fact)
        self.assertEqual(restored.facts[0].value, 91.0)
        self.assertEqual(restored.hypotheses, ("특정 프로세스 부하 증가 가능성",))

    def test_fact_requires_target_field_value_and_source(self):
        for field in ("target", "field", "value", "source_id"):
            payload = cpu_fact().model_dump()
            del payload[field]
            with self.subTest(field=field), self.assertRaises(ValidationError):
                Fact(**payload)

    def test_boolean_number_and_numeric_string_are_not_silently_interchanged(self):
        for value in (True, 91, 91.0, "91.0"):
            with self.subTest(value=value):
                fact = cpu_fact(value=value)
                restored = Fact.model_validate_json(fact.model_dump_json())
                self.assertIs(type(restored.value), type(value))
                self.assertEqual(restored.value, value)

    def test_unsupported_fact_values_and_free_text_facts_are_rejected(self):
        for value in (None, [], {}, float("nan"), float("inf")):
            with self.subTest(value=value), self.assertRaises(ValidationError):
                cpu_fact(value=value)
        with self.assertRaises(ValidationError):
            DraftReport(facts=["로그를 분석했습니다"])


class ContextTests(unittest.TestCase):
    def test_saved_json_restores_original_question_targets_and_symptoms(self):
        context = ConversationContext(
            thread_id="session-a", previous_targets=["payments-service", "cache-service"],
            symptoms=["CPU 급증", "메모리 증가"],
            pending_clarification=PendingClarification(
                original_query="그 서비스 현재 상태를 다시 확인해줘",
                question="두 서비스 중 어느 서비스를 확인할까요?",
                reason="ambiguous_reference", candidate_targets=["payments-service", "cache-service"],
            ),
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "context.json"
            path.write_text(context.model_dump_json(), encoding="utf-8")
            restored = ConversationContext.model_validate_json(path.read_text(encoding="utf-8"))
        self.assertEqual(restored, context)
        self.assertEqual(restored.pending_clarification.original_query,
                         "그 서비스 현재 상태를 다시 확인해줘")

    def test_two_sessions_keep_independent_defaults_and_restored_contexts(self):
        first = ConversationContext(thread_id="session-a", previous_targets=["payments-service"])
        second = ConversationContext(thread_id="session-b")
        saved = {c.thread_id: c.model_dump_json() for c in (first, second)}
        restored_a = ConversationContext.model_validate_json(saved["session-a"])
        restored_b = ConversationContext.model_validate_json(saved["session-b"])
        self.assertEqual(restored_a.previous_targets, ("payments-service",))
        self.assertEqual(restored_b.previous_targets, ())
        self.assertIsNone(restored_b.pending_clarification)
        with self.assertRaises(ValidationError):
            restored_a.previous_targets = ("cache-service",)
        self.assertEqual(first.previous_targets, ("payments-service",))
        self.assertEqual(second.previous_targets, ())


class StatusTests(unittest.TestCase):
    def test_completed_does_not_implicitly_mean_validation_passed(self):
        response = ResponseState(status="completed")
        self.assertEqual(response.validation.status, "not_run")
        self.assertEqual(response.validation.attempts, 0)
        passed = ResponseState(status="completed",
                               validation=ValidationResult(status="passed", attempts=1))
        self.assertEqual(passed.validation.status, "passed")

    def test_partial_can_keep_clarification_alongside_passed_known_facts(self):
        response = ResponseState(status="partial", clarification_question="billing-service의 정확한 이름은?",
                                 validation=ValidationResult(status="passed", attempts=1))
        self.assertEqual(ResponseState.model_validate_json(response.model_dump_json()), response)

    def test_clarification_requires_a_question(self):
        with self.assertRaises(ValidationError):
            ResponseState(status="needs_clarification")
        response = ResponseState(status="needs_clarification", clarification_question="어느 서비스인가요?")
        self.assertEqual(response.validation.status, "not_run")

    def test_failed_validation_cannot_be_presented_as_completed_or_partial(self):
        failure = failed_validation()
        for status in ("completed", "partial", "needs_clarification"):
            with self.subTest(status=status), self.assertRaises(ValidationError):
                ResponseState(status=status, validation=failure, clarification_question="대상은?")
        response = ResponseState(status="validation_failed", validation=failure)
        self.assertEqual(response.validation.issues[0].code, "value_mismatch")
        with self.assertRaises(ValidationError):
            ResponseState(status="validation_failed")

    def test_invalid_validation_status_attempts_and_issue_combinations_are_rejected(self):
        issue = ValidationIssue(code="source_missing", message="근거를 찾을 수 없습니다")
        invalid = [
            {"status": "not_run", "attempts": 1},
            {"status": "not_run", "issues": [issue]},
            {"status": "passed", "attempts": 0},
            {"status": "passed", "attempts": 1, "issues": [issue]},
            {"status": "failed", "attempts": 1},
            {"status": "failed", "attempts": 0, "issues": [issue]},
            {"attempts": -1}, {"attempts": True}, {"attempts": "1"},
            {"status": "forced_pass", "attempts": 3},
        ]
        for payload in invalid:
            with self.subTest(payload=payload), self.assertRaises(ValidationError):
                ValidationResult(**payload)
        with self.assertRaises(ValidationError):
            ResponseState(status="success")


class ContractBoundaryTests(unittest.TestCase):
    def test_blank_identifiers_unknown_fields_and_accidental_coercion_are_rejected(self):
        invalid_contexts = [
            {"thread_id": "  "}, {"thread_id": 123},
            {"thread_id": "session-a", "previous_target": "payments-service"},
            {"thread_id": "session-a", "previous_targets": ["payments-service", " "]},
        ]
        for payload in invalid_contexts:
            with self.subTest(payload=payload), self.assertRaises(ValidationError):
                ConversationContext(**payload)

    def test_json_schema_exports_the_common_contract(self):
        models = (TargetResolution, EvidenceRecord, Fact, DraftReport, PendingClarification,
                  ConversationContext, ValidationIssue, ValidationResult, ResponseState)
        for model in models:
            with self.subTest(model=model.__name__):
                schema = json.loads(json.dumps(model.model_json_schema(), allow_nan=False))
                self.assertEqual(schema["title"], model.__name__)
                self.assertFalse(schema["additionalProperties"])
        self.assertEqual(ResponseState.model_json_schema()["properties"]["status"]["enum"],
                         ["needs_clarification", "completed", "partial", "validation_failed"])


if __name__ == "__main__":
    unittest.main()
