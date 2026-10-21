"""서비스 대상 식별과 모델 계획 정규화의 기능 테스트."""
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

os.environ["LANGCHAIN_TRACING_V2"] = "false"
os.environ["LANGSMITH_TRACING"] = "false"

from agent.targets import RegistryError, load_registry, normalize_plan, resolve_targets

KNOWN = frozenset({"auth-service", "payments-service", "cache-service", "web-01"})


class TargetTests(unittest.TestCase):
    def test_all_explicit_targets_are_kept_in_request_order_once(self):
        result = resolve_targets("cache-service와 payments-service 그리고 cache-service 상태", KNOWN)
        self.assertEqual(result.resolved_targets, ("cache-service", "payments-service"))

    def test_unknown_target_is_not_substituted(self):
        result = resolve_targets("billing-service 장애 진단", KNOWN)
        self.assertEqual(result.resolved_targets, ())
        self.assertEqual(result.unresolved_targets, ("billing-service",))

    def test_missing_target_has_no_default(self):
        self.assertEqual(resolve_targets("서비스가 느려요", KNOWN).requested_targets, ())

    def test_known_and_unknown_targets_are_partitioned(self):
        result = resolve_targets("payments-service와 billing-service 조사", KNOWN)
        self.assertEqual(result.requested_targets, ("payments-service", "billing-service"))
        self.assertEqual(result.resolved_targets, ("payments-service",))
        self.assertEqual(result.unresolved_targets, ("billing-service",))

    def test_exclusion_forms_and_connected_exclusion_list(self):
        for query in ("auth-service 말고 cache-service만 진단", "auth-service는 제외하고 cache-service 상태",
                      "auth-service와 payments-service는 빼고 cache-service만 조사",
                      "auth-service, payments-service 제외하고 cache-service 상태",
                      "billing-service 대신 cache-service 상태"):
            with self.subTest(query=query):
                self.assertEqual(resolve_targets(query, KNOWN).requested_targets, ("cache-service",))

    def test_all_excluded_means_no_investigation(self):
        self.assertEqual(resolve_targets("auth-service 제외", KNOWN).requested_targets, ())

    def test_instance_or_longer_identifier_does_not_match_parent(self):
        result = resolve_targets("payments-service-3과 mypayments-service, web-010 상태", KNOWN)
        self.assertEqual(result.resolved_targets, ())
        self.assertEqual(result.unresolved_targets, ("payments-service-3", "mypayments-service", "web-010"))

    def test_valid_new_registry_name_needs_no_code_change(self):
        self.assertEqual(resolve_targets("custom-api 상태", frozenset({"custom-api"})).resolved_targets,
                         ("custom-api",))

    def test_registry_failure_and_invalid_formats_do_not_use_fallback_list(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "registry.json"
            with self.assertRaises(RegistryError):
                load_registry(path)
            for text in ("{bad", "[]", "{}", '{"auth-service": null}', '{"bad name": {}}'):
                path.write_text(text)
                with self.subTest(text=text), self.assertRaises(RegistryError):
                    load_registry(path)

    def test_registry_uses_actual_data_file(self):
        registry = load_registry()
        self.assertIn("payments-service", registry)
        self.assertIn("cache-service", registry)


class PlanTests(unittest.TestCase):
    def test_partial_duplicate_and_extra_plan_is_replaced_with_exact_scope(self):
        resolution = resolve_targets("payments-service와 cache-service 진단", KNOWN)
        raw = {"needs_analysis": False, "tasks": [
            {"agent": "research", "target": "payments-service", "status": "done"},
            {"agent": "research", "target": "payments-service"},
            {"agent": "research", "target": "auth-service"},
            {"agent": "analysis"}, {"agent": "analysis"},
        ]}
        result = normalize_plan(raw, resolution)
        monitoring = [t for t in result["tasks"] if t["kind"] == "monitoring"]
        self.assertEqual([t["target"] for t in monitoring], ["payments-service", "cache-service"])
        self.assertTrue(all(t["status"] == "pending" for t in monitoring))
        self.assertEqual(result["plan_adjustments"], {"added_targets": ["cache-service"],
                         "removed_targets": ["auth-service"], "duplicate_targets": ["payments-service"]})
        self.assertEqual(sum(t["agent"] == "analysis" for t in result["tasks"]), 1)

    def test_malformed_model_output_still_produces_exact_monitoring_scope(self):
        resolution = resolve_targets("cache-service 상태", KNOWN)
        for raw in (None, [], "bad", {}, {"tasks": None}, {"tasks": [None, [], "bad"]}):
            with self.subTest(raw=raw):
                result = normalize_plan(raw, resolution)
                self.assertEqual([t["target"] for t in result["tasks"]], ["cache-service"])

    def test_unknown_and_missing_targets_never_generate_tasks(self):
        for query in ("billing-service 장애", "서비스가 느려요"):
            with self.subTest(query=query):
                result = normalize_plan({"tasks": [{"agent": "research", "target": "auth-service"}]},
                                        resolve_targets(query, KNOWN))
                self.assertEqual(result["tasks"], [])

    def test_ollama_and_openai_plan_use_shared_rules_without_network(self):
        from agent.ollama_llm import OllamaFastLLM
        from agent.openai_llm import OpenAIFastLLM
        for cls in (OllamaFastLLM, OpenAIFastLLM):
            with self.subTest(backend=cls.__name__):
                llm = cls("test-only", 0)
                with patch.object(llm, "_chat", return_value=(json.dumps({"tasks": [
                        {"agent": "research", "target": "payments-service"}]}), {"usd": 0})) as call:
                    result, _ = llm.plan("payments-service와 cache-service 장애", "표준 절차")
                    self.assertEqual([t["target"] for t in result["tasks"]], ["payments-service", "cache-service"])
                    self.assertNotIn("기본으로 조사", call.call_args.args[0])
                with patch.object(llm, "_chat") as call:
                    result, _ = llm.plan("billing-service 장애", "표준 절차")
                    self.assertEqual(result["tasks"], [])
                    call.assert_not_called()


if __name__ == "__main__":
    unittest.main()
