"""후속 대상·원자 저장·대화 복원·대화 간 격리를 검증한다. 유료 호출 없음."""
import asyncio
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from agent.context import interpret_query, next_context
from agent.contracts import ConversationContext, PendingClarification
from agent.graph import build_graph
from agent.session_store import SqliteSessionStore
import test_target_pipeline as support


class ContextRulesTests(unittest.TestCase):
    def context(self, targets=("cache-service",), pending=None):
        return ConversationContext(thread_id="a", previous_targets=targets,
                                   last_request="cache-service 메모리 증가를 확인해줘", pending_clarification=pending)

    def test_single_followup_and_explicit_override(self):
        c = self.context()
        for query in ("그 서비스 다시 확인해줘", "지금은 정상화됐어?", "대응 절차는?"):
            row = interpret_query(query, c)
            self.assertEqual(row["target_resolution"]["resolved_targets"], ["cache-service"])
            self.assertEqual(row["mode"], "previous_target")
        row = interpret_query("payments-service 상태 확인", c)
        self.assertEqual(row["target_resolution"]["resolved_targets"], ["payments-service"])
        self.assertEqual(row["mode"], "explicit")

    def test_ambiguous_multiple_targets_ask_and_explicit_subset_works(self):
        c = self.context(("payments-service", "cache-service"))
        row = interpret_query("그 서비스 다시 확인해줘", c)
        self.assertEqual(row["mode"], "ambiguous_reference")
        self.assertEqual(row["target_resolution"]["resolved_targets"], [])
        self.assertEqual(row["pending_clarification"]["candidate_targets"], ["payments-service", "cache-service"])
        row = interpret_query("그중 cache-service만 다시 확인", c)
        self.assertEqual(row["target_resolution"]["resolved_targets"], ["cache-service"])

    def test_explicit_unknown_and_exclusion_never_rescue_old_target(self):
        for query in ("billing-service 상태", "cache-service 말고 상태 확인", "그 서비스 말고 다시 확인해줘"):
            row = interpret_query(query, self.context())
            self.assertEqual(row["target_resolution"]["resolved_targets"], [])
            self.assertIsNotNone(row["clarification_question"])

    def test_pending_target_only_answer_preserves_original_request(self):
        p = PendingClarification(original_query="메모리 증가 원인 확인", question="어느 서비스인가요?", reason="missing_target")
        row = interpret_query("cache-service요", self.context((), p))
        self.assertEqual(row["mode"], "clarification_answer")
        self.assertTrue(row["force_incident"])
        self.assertIn("메모리 증가 원인 확인", row["effective_query"])
        self.assertEqual(row["target_resolution"]["resolved_targets"], ["cache-service"])

    def test_pending_unknown_does_not_revert_to_older_known_target(self):
        p = PendingClarification(original_query="billing-service 상태", question="정확한 서비스명은?", reason="unknown_target")
        row = interpret_query("그 서비스 다시 확인해줘", self.context(pending=p))
        self.assertEqual(row["mode"], "pending_clarification")
        self.assertEqual(row["target_resolution"]["resolved_targets"], [])

    def test_both_targets_can_be_selected_after_ambiguous_question(self):
        p = PendingClarification(original_query="그 서비스 확인", question="어느 서비스?", reason="ambiguous_reference",
                                 candidate_targets=("payments-service", "cache-service"))
        row = interpret_query("둘 다", self.context(pending=p))
        self.assertEqual(row["target_resolution"]["resolved_targets"], ["payments-service", "cache-service"])

    def test_greeting_and_unrelated_question_do_not_inherit_targets(self):
        for query in ("안녕하세요", "고마워요", "다시 안녕", "지금 몇 시야?"):
            row = interpret_query(query, self.context())
            self.assertFalse(row["force_incident"])
            self.assertEqual(row["target_resolution"]["resolved_targets"], [])

    def test_new_thread_does_not_infer_target_and_removed_target_is_rechecked(self):
        row = interpret_query("지금은 정상화됐어?", self.context(()))
        self.assertEqual(row["target_resolution"]["resolved_targets"], [])
        c = self.context(("retired-service",))
        row = interpret_query("그 서비스 다시 확인", c)
        self.assertEqual(row["target_resolution"]["unresolved_targets"], ["retired-service"])


class StorageTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "test.db"
        self.store = SqliteSessionStore(self.path)
        self.addCleanup(lambda: self.store.close())

    def test_atomic_save_and_restart_preserve_context_status_and_order(self):
        c = ConversationContext(thread_id="a", previous_targets=["cache-service"], updated_at=datetime.now(timezone.utc))
        meta = {"processing_status": "validation_failed", "validation_status": "failed"}
        self.store.save_turn("a", "질문", "관측 사실만 제공", c, meta)
        self.store.close()
        self.store = SqliteSessionStore(self.path)
        self.assertEqual(self.store.load_context("a"), c)
        self.assertEqual([m["role"] for m in self.store.history("a")], ["user", "assistant"])
        self.assertEqual(self.store.history("a")[-1]["metadata"], meta)
        self.assertEqual(self.store.history("b"), [])
        self.assertEqual(self.store.load_context("b").previous_targets, ())

    def test_save_failure_rolls_back_both_messages_and_metadata(self):
        self.store.conn.execute("CREATE TRIGGER fail_context BEFORE INSERT ON conversation_context BEGIN SELECT RAISE(ABORT,'test failure'); END;")
        self.store.conn.commit()
        with self.assertRaises(sqlite3.Error):
            self.store.save_turn("a", "질문", "응답", ConversationContext(thread_id="a"), {})
        self.assertEqual(self.store.load("a"), [])
        self.assertEqual(self.store.conn.execute("SELECT COUNT(*) FROM message_metadata").fetchone()[0], 0)

    def test_invalid_context_or_nonfinite_metadata_does_not_write_messages(self):
        with self.assertRaises(ValueError):
            self.store.save_turn("a", "질문", "응답", ConversationContext(thread_id="b"), {})
        with self.assertRaises(ValueError):
            self.store.save_turn("a", "질문", "응답", None, {"number": float("nan")})
        self.assertEqual(self.store.load("a"), [])

    def test_old_messages_keep_content_without_invented_validation_status(self):
        self.store.close()
        old_path = Path(self.tmp.name) / "legacy.db"
        con = sqlite3.connect(old_path)
        con.execute("CREATE TABLE messages (thread_id TEXT, role TEXT, content TEXT, created TEXT)")
        con.execute("INSERT INTO messages VALUES (?,?,?,?)", ("legacy", "assistant", "예전 답변", "2026-10-20T10:00:00"))
        con.commit(); con.close()
        self.store = SqliteSessionStore(old_path)
        row = self.store.history("legacy")[0]
        self.assertEqual(row["content"], "예전 답변")
        self.assertIsNone(row["metadata"])


class ContextApiTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = support.ApiTests.asyncSetUp
    asyncTearDown = support.ApiTests.asyncTearDown

    async def post(self, query, thread="a"):
        response = await self.client.post("/chat", json={"message": query, "thread_id": thread})
        self.assertEqual(response.status_code, 200)
        return response.json()

    async def sse(self, query, thread="a"):
        response = await self.client.get("/chat/stream", params={"message": query, "thread_id": thread})
        events = [json.loads(line[6:]) for line in response.text.splitlines() if line.startswith('data: {')]
        self.assertFalse(any(e["type"] == "AnswerToken" for e in events))
        return next(e for e in events if e["type"] == "FinalAnswer")

    async def test_single_followup_recollects_sources_and_new_thread_asks(self):
        first = await self.post("cache-service 메모리 상태")
        second = await self.post("지금은 정상화됐어?")
        self.assertEqual(self.deps.mcp.calls, ["cache-service", "cache-service"])
        self.assertEqual(second["metadata"]["context_resolution"]["mode"], "previous_target")
        a = first["metadata"]["runbook_searches"][0]["monitoring_source_id"]
        b = second["metadata"]["runbook_searches"][0]["monitoring_source_id"]
        self.assertNotEqual(a, b)
        fresh = await self.post("지금은 정상화됐어?", "b")
        self.assertEqual(fresh["metadata"]["processing_status"], "needs_clarification")
        self.assertEqual(len(self.deps.mcp.calls), 2)

    async def test_multi_ambiguity_then_short_answer_runs_only_chosen_service(self):
        await self.post("payments-service와 cache-service 상태")
        ambiguous = await self.sse("그 서비스 다시 확인해줘")
        self.assertEqual(ambiguous["metadata"]["processing_status"], "needs_clarification")
        self.assertEqual(len(self.deps.mcp.calls), 2)
        answered = await self.sse("cache-service")
        self.assertEqual(answered["metadata"]["target_resolution"]["resolved_targets"], ["cache-service"])
        self.assertEqual(self.deps.mcp.calls, ["payments-service", "cache-service", "cache-service"])

    async def test_missing_target_answer_restores_symptom_to_planner(self):
        first = await self.post("메모리가 계속 증가해요")
        self.assertEqual(first["metadata"]["processing_status"], "needs_clarification")
        await self.post("cache-service")
        self.assertIn("메모리가 계속 증가해요", self.deps.mcp.prompts[-1]["symptom"])

    async def test_history_restore_never_calls_model_or_tools_and_keeps_metadata(self):
        body = await self.post("cache-service 상태")
        calls, writes = len(self.deps.mcp.calls), len(self.deps.quality_llm.inputs)
        for _ in range(2):
            response = await self.client.get("/sessions/a")
            history = response.json()
            self.assertEqual(response.headers["cache-control"], "no-store")
            self.assertEqual(history["messages"][-1]["metadata"], body["metadata"])
            self.assertEqual(history["messages"][-1]["content"], body["reply"])
        self.assertEqual(len(self.deps.mcp.calls), calls)
        self.assertEqual(len(self.deps.quality_llm.inputs), writes)

    async def test_restart_reopens_file_and_followup_preserves_scope(self):
        await self.post("cache-service 상태")
        self.store.close()
        self.store = SqliteSessionStore(Path(self.tmp.name) / "test.db")
        self.deps.session_store = self.store
        restored = (await self.client.get("/sessions/a")).json()
        self.assertEqual(len(restored["messages"]), 2)
        body = await self.sse("그 서비스 다시 확인해줘")
        self.assertEqual(body["metadata"]["target_resolution"]["resolved_targets"], ["cache-service"])

    async def test_failed_validation_status_survives_history_restore(self):
        from test_grounding_pipeline import SequenceWriter, BAD
        self.deps.quality_llm = SequenceWriter([BAD])
        body = await self.post("cache-service 상태")
        history = (await self.client.get("/sessions/a")).json()
        self.assertEqual(body["metadata"]["processing_status"], "validation_failed")
        self.assertEqual(history["messages"][-1]["metadata"]["validation_status"], "failed")
        self.assertNotIn("UNVERIFIED_DRAFT_MARKER123", json.dumps(history))

    async def test_corrupt_context_returns_clear_error_before_generation(self):
        self.store.conn.execute("INSERT INTO conversation_context VALUES (?,?)", ("a", '{"thread_id":"other"}'))
        self.store.conn.commit()
        response = await self.client.get("/sessions/a")
        self.assertEqual(response.status_code, 503)
        response = await self.client.post("/chat", json={"message": "cache-service 상태", "thread_id": "a"})
        self.assertEqual(response.status_code, 503)
        self.assertEqual(self.deps.mcp.calls, [])
        stream = await self.client.get("/chat/stream", params={"message": "cache-service 상태", "thread_id": "a"})
        self.assertIn('"type": "Error"', stream.text)
        self.assertNotIn('"type": "FinalAnswer"', stream.text)
        self.assertEqual(self.store.load("a"), [])

    async def test_store_failure_returns_error_without_partial_saved_turn(self):
        with patch.object(self.store, "save_turn", side_effect=sqlite3.OperationalError("test")):
            response = await self.client.post("/chat", json={"message": "cache-service 상태", "thread_id": "a"})
        self.assertEqual(response.status_code, 503)
        self.assertEqual(self.store.load("a"), [])

    async def test_same_thread_post_and_sse_are_serialized_before_loading_context(self):
        original = self.deps.quality_llm.write
        started, release = asyncio.Event(), asyncio.Event()
        async def delayed(*args, **kwargs):
            if not started.is_set():
                started.set()
                await release.wait()
            return await original(*args, **kwargs)
        self.deps.quality_llm.write = delayed
        first = asyncio.create_task(self.post("cache-service 상태"))
        await started.wait()
        second = asyncio.create_task(self.sse("지금은 정상화됐어?"))
        asyncio.get_running_loop().call_later(.01, release.set)
        _, answer = await asyncio.gather(first, second)
        self.assertEqual(answer["metadata"]["context_resolution"]["mode"], "previous_target")
        self.assertEqual(self.deps.mcp.calls, ["cache-service", "cache-service"])
        self.assertEqual(len(self.store.history("a")), 4)

    async def test_chat_does_not_replace_operational_context_or_pending_question(self):
        await self.post("cache-service 상태")
        before = self.store.load_context("a")
        await self.post("안녕하세요")
        after = self.store.load_context("a")
        self.assertEqual(after.previous_targets, before.previous_targets)
        self.assertEqual(after.last_request, before.last_request)
        await self.post("billing-service 상태")
        await self.post("고마워요")
        self.assertEqual(self.store.load_context("a").pending_clarification.reason, "unknown_target")


if __name__ == "__main__":
    unittest.main()
