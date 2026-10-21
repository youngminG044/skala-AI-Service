"""재작성 제한, 안전한 제한 응답, POST/SSE·저장소의 초안 비노출 검증."""
import json
import unittest
from unittest.mock import patch

from agent.graph import build_graph
import test_target_pipeline as support


GOOD = {"hypotheses": ["부하 증가 가능성"], "missing_checks": ["프로세스 상세 확인 필요"], "recommendations": []}
BAD = {"hypotheses": ["UNVERIFIED_DRAFT_MARKER123 로그를 분석한 결과 원인 확인됨"],
       "missing_checks": [], "recommendations": []}


class SequenceWriter:
    name = "injected-grounding-writer"

    def __init__(self, outputs):
        self.outputs = outputs
        self.attempts = []
        self.feedback = []
        self.callbacks = []

    async def write(self, query, evidence, attempt, feedback, on_token=None):
        self.attempts.append(attempt)
        self.feedback.append(feedback)
        self.callbacks.append(on_token)
        output = self.outputs[min(len(self.attempts) - 1, len(self.outputs) - 1)]
        if isinstance(output, Exception):
            raise output
        if on_token:
            on_token("UNVERIFIED_DRAFT_MARKER123")
        return output, {"usd": 0, "prompt": "test prompt"}


class GroundingGraphTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = support.GraphTests.asyncSetUp

    async def invoke(self, outputs, behavior=None, stream=False):
        deps = support.make_deps(support.TestMCP(behavior))
        deps.quality_llm = SequenceWriter(outputs)
        state = {"query": "payments-service 상태", "thread_id": "grounding", "history": [], "events": [], "cost_usd": 0}
        graph = build_graph(deps)
        if not stream:
            return await graph.ainvoke(state), deps
        updates, custom = {}, []
        async for mode, payload in graph.astream(state, stream_mode=["updates", "custom"]):
            if mode == "custom":
                custom.append(payload)
            else:
                for delta in payload.values():
                    updates.update(delta)
        return updates, deps, custom

    async def test_invalid_schema_is_rewritten_then_passes_with_original_facts(self):
        result, deps = await self.invoke(["bad JSON", GOOD])
        self.assertEqual(deps.quality_llm.attempts, [1, 2])
        self.assertIn("draft_schema", deps.quality_llm.feedback[1])
        self.assertEqual([v["status"] for v in result["validation_history"]], ["failed", "passed"])
        self.assertEqual(result["response_state"]["validation"]["status"], "passed")
        self.assertIn("CPU=81.5%", result["final"])

    async def test_three_failures_remain_failed_and_do_not_display_bad_sections(self):
        result, deps = await self.invoke([BAD])
        self.assertEqual(deps.quality_llm.attempts, [1, 2, 3])
        self.assertEqual(result["response_state"]["status"], "validation_failed")
        self.assertEqual(result["validation_result"]["status"], "failed")
        self.assertEqual(len(result["validation_history"]), 3)
        self.assertIn("CPU=81.5%", result["final"])
        self.assertIn("답변 검증에 실패", result["final"])
        self.assertNotIn("UNVERIFIED_DRAFT_MARKER123", result["final"])
        self.assertTrue(all(v["status"] == "failed" for v in result["validation_history"]))

    async def test_fake_fact_field_cannot_replace_canonical_facts(self):
        result, deps = await self.invoke([{"facts": [{"value": 70}]}])
        self.assertEqual(result["response_state"]["status"], "validation_failed")
        self.assertIn("CPU=81.5%", result["final"])
        self.assertNotIn("CPU=70", result["final"])

    async def test_forged_citation_rewrites_and_failure_preserves_empty_actions(self):
        bad = {"recommendations": [{"source_id": "fake-book", "step": 1}]}
        result, deps = await self.invoke([bad])
        self.assertEqual(result["response_state"]["status"], "validation_failed")
        self.assertTrue(any(i["code"] == "runbook_source" for i in result["validation_result"]["issues"]))
        self.assertNotIn("서비스 재시작", result["final"])

    async def test_generation_error_is_bounded_and_returns_facts(self):
        result, deps = await self.invoke([RuntimeError("test writer unavailable")])
        self.assertEqual(deps.quality_llm.attempts, [1, 2, 3])
        self.assertEqual(result["validation_result"]["issues"][0]["code"], "draft_generation")
        self.assertIn("CPU=81.5%", result["final"])

    async def test_stream_contains_progress_but_no_incident_draft_tokens(self):
        result, deps, custom = await self.invoke([BAD, GOOD], stream=True)
        self.assertEqual([e["type"] for e in custom], ["DraftStarted", "DraftStarted"])
        self.assertTrue(all(callback is None for callback in deps.quality_llm.callbacks))
        self.assertEqual(result["validation_result"]["status"], "passed")

    async def test_no_observations_skip_writer_and_do_not_claim_validation_pass(self):
        result, deps = await self.invoke([GOOD], {"payments-service": "error"})
        self.assertEqual(deps.quality_llm.attempts, [])
        self.assertEqual(result["response_state"]["status"], "partial")
        self.assertEqual(result["response_state"]["validation"]["status"], "not_run")

    async def test_analysis_remains_separate_from_observed_facts(self):
        deps = support.make_deps()
        deps.quality_llm = SequenceWriter([GOOD])
        with patch.object(deps.fast_llm, "plan", return_value=({"needs_analysis": True, "tasks": [{"agent": "analysis"}]}, {"usd": 0})), \
             patch.object(deps.fast_llm, "analyze", return_value=("CPU 15%로 추정", {"usd": 0})):
            result = await build_graph(deps).ainvoke({"query": "payments-service 상태", "events": [], "cost_usd": 0})
        self.assertIn("15%", result["analysis_text"])
        self.assertFalse(any(c["source"] == "analysis" for c in result["collected"]))
        self.assertNotIn("15%", result["final"])
        self.assertIn("CPU=81.5%", result["final"])


class GroundingApiTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = support.ApiTests.asyncSetUp
    asyncTearDown = support.ApiTests.asyncTearDown

    async def test_failed_raw_draft_is_absent_from_post_sse_and_saved_messages(self):
        self.deps.quality_llm = SequenceWriter([BAD])
        post = await self.client.post("/chat", json={"message": "payments-service 상태", "thread_id": "post-failure"})
        self.assertEqual(post.status_code, 200)
        body = post.json()
        self.assertEqual(body["metadata"]["validation_attempts"], 3)
        self.assertEqual(body["metadata"]["processing_status"], "validation_failed")
        self.assertEqual(body["metadata"]["validation_status"], "failed")
        self.assertEqual(len(body["metadata"]["validation_history"]), 3)
        self.deps.quality_llm = SequenceWriter([BAD])
        stream = await self.client.get("/chat/stream", params={"message": "payments-service 상태", "thread_id": "sse-failure"})
        events = [json.loads(line[6:]) for line in stream.text.splitlines() if line.startswith('data: {')]
        final = [e for e in events if e["type"] == "FinalAnswer"]
        self.assertEqual(len(final), 1)
        self.assertEqual(final[0]["metadata"]["validation_status"], "failed")
        self.assertEqual(final[0]["metadata"]["response_state"], body["metadata"]["response_state"])
        self.assertIsNone(final[0]["prompt"])
        self.assertFalse(any(e["type"] == "AnswerToken" for e in events))
        self.assertTrue(stream.text.rstrip().endswith('data: [DONE]'))
        self.assertNotIn("UNVERIFIED_DRAFT_MARKER123", post.text + stream.text)
        self.assertNotIn("UNVERIFIED_DRAFT_MARKER123", str(self.store.load("post-failure")) + str(self.store.load("sse-failure")))

    async def test_success_after_rewrite_reports_history_without_leaking_rejected_content(self):
        self.deps.quality_llm = SequenceWriter([BAD, GOOD])
        response = await self.client.get("/chat/stream", params={"message": "payments-service 상태"})
        events = [json.loads(line[6:]) for line in response.text.splitlines() if line.startswith('data: {')]
        final = next(e for e in events if e["type"] == "FinalAnswer")
        self.assertEqual(final["metadata"]["validation_status"], "passed")
        self.assertEqual(final["metadata"]["validation_attempts"], 2)
        self.assertEqual([v["status"] for v in final["metadata"]["validation_history"]], ["failed", "passed"])
        self.assertNotIn("UNVERIFIED_DRAFT_MARKER123", response.text)


if __name__ == "__main__":
    unittest.main()
