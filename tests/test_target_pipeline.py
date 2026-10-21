"""실제 그래프 및 POST/SSE 경로의 대상 누락 방지 검증. 모델/MCP는 주입한다."""
import asyncio
import copy
import importlib
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

os.environ["LANGCHAIN_TRACING_V2"] = "false"
os.environ["LANGSMITH_TRACING"] = "false"

import httpx
from agent.deps import SupervisorDeps
from agent.graph import build_graph
from agent.session_store import SqliteSessionStore
from agent.targets import RegistryError


class TestFastLLM:
    name = "injected-fast"

    def __init__(self):
        self.plan_calls = 0
        self.analysis_calls = 0

    def route(self, query):
        return {"route": "chat", "needs_analysis": False}, {"usd": 0}

    def plan(self, query, procedure):
        self.plan_calls += 1
        return {"needs_analysis": False, "tasks": [
            {"agent": "research", "target": "payments-service"},
            {"agent": "research", "target": "payments-service"},
            {"agent": "research", "target": "web-01"},
            {"agent": "analysis"}]}, {"usd": 0}

    async def chat(self, query, history, on_token=None):
        if on_token:
            on_token("안녕하세요")
        return "안녕하세요", {"usd": 0}

    def analyze(self, collected):
        self.analysis_calls += 1
        return "추가 분석", {"usd": 0}


class TestQualityLLM:
    name = "injected-quality"

    def __init__(self):
        self.inputs = []

    async def write(self, query, collected, attempt, feedback, on_token=None):
        self.inputs.append(copy.deepcopy(collected))
        names = ", ".join(c["target"] for c in collected if c["source"] == "monitoring")
        reply = f"1) {names} 모니터링 자료를 확인했습니다. 2) 수집된 알림을 확인하세요. 3) 대응 후 다시 상태를 확인하세요."
        if on_token:
            on_token(reply)
        return reply, {"usd": 0}


class TestMCP:
    def __init__(self, behavior=None):
        self.behavior = behavior or {}
        self.calls = []
        self.prompts = []
        self.resources = []

    async def find_prompt(self, name):
        return "ops"

    async def get_prompt(self, name, arguments, server=None):
        self.prompts.append(arguments)
        return "확인된 서비스를 모니터링하고 대응 절차를 조회하세요"

    async def call_tool(self, name, arguments):
        target = arguments["system_name"]
        self.calls.append(target)
        action = self.behavior.get(target)
        if action == "error":
            raise ValueError("test permanent failure")
        if action == "timeout":
            raise asyncio.TimeoutError()
        if action == "once" and self.calls.count(target) == 1:
            raise ValueError("test first round failure")
        data = {"system_name": target, "status": "degraded", "cpu_percent": 81.5,
                "memory_percent": 52.0, "alerts": ["CPU high"], "last_checked": "2026-10-21T09:00:00+09:00"}
        if action == "unknown":
            data.update(status="unknown", cpu_percent=None, memory_percent=None)
        if action == "wrong_target":
            data["system_name"] = "auth-service"
        if action == "invalid_metric":
            data["cpu_percent"] = None
        if action == "nan_metric":
            data["cpu_percent"] = float("nan")
        if action == "non_json":
            data["extra"] = object()
        return data

    async def find_resource_template(self, name):
        if self.behavior.get("runbook") == "discovery_error":
            raise ValueError("test discovery failure")
        return "ops", "runbook://incident-response/{incident_type}"

    async def read_resource(self, uri, server=None):
        self.resources.append(uri)
        if self.behavior.get("runbook") == "error":
            raise ValueError("test runbook failure")
        return "# CPU 대응\n1. 프로세스 사용량 확인\n2. 부하 확인"


def make_deps(mcp=None, store=None):
    return SupervisorDeps(TestFastLLM(), TestQualityLLM(), mcp or TestMCP(), store, "v2")


class GraphTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.sleep_patch = patch("agent.graph.asyncio.sleep", new=AsyncMock())
        self.sleep_patch.start()
        self.addCleanup(self.sleep_patch.stop)

    async def run_graph(self, query, behavior=None):
        deps = make_deps(TestMCP(behavior))
        result = await build_graph(deps).ainvoke({"query": query, "thread_id": "test", "history": [],
                                                "events": [], "cost_usd": 0})
        return result, deps

    async def test_missing_duplicate_and_extra_plan_is_repaired_before_tools(self):
        result, deps = await self.run_graph("payments-service와 cache-service 상태 확인")
        self.assertEqual(deps.mcp.calls, ["payments-service", "cache-service"])
        self.assertEqual(result["monitoring_coverage"]["missing_targets"], [])
        self.assertEqual(result["response_state"]["status"], "completed")
        self.assertEqual(deps.fast_llm.analysis_calls, 0)
        self.assertEqual(deps.mcp.prompts[0]["system_name"], "payments-service, cache-service")

    async def test_unknown_missing_and_excluded_only_stop_before_any_tool_or_writer(self):
        for query in ("billing-service 장애", "서비스가 느려요", "auth-service 제외하고 상태 확인"):
            with self.subTest(query=query):
                result, deps = await self.run_graph(query)
                self.assertEqual(result["response_state"]["status"], "needs_clarification")
                self.assertEqual(deps.mcp.calls, [])
                self.assertEqual(deps.fast_llm.plan_calls, 0)
                self.assertEqual(deps.quality_llm.inputs, [])

    async def test_mixed_known_unknown_investigates_known_only_and_asks(self):
        result, deps = await self.run_graph("payments-service와 billing-service 상태 확인")
        self.assertEqual(deps.mcp.calls, ["payments-service"])
        self.assertEqual(result["response_state"]["status"], "partial")
        self.assertIn("billing-service", result["response_state"]["clarification_question"])

    async def test_excluded_target_is_never_queried(self):
        result, deps = await self.run_graph("auth-service 말고 cache-service만 진단")
        self.assertEqual(deps.mcp.calls, ["cache-service"])
        self.assertEqual(result["target_resolution"]["requested_targets"], ["cache-service"])

    async def test_instance_name_is_not_collapsed_to_parent(self):
        result, deps = await self.run_graph("payments-service-3 상태 확인")
        self.assertEqual(deps.mcp.calls, [])
        self.assertEqual(result["target_resolution"]["unresolved_targets"], ["payments-service-3"])

    async def test_one_failed_target_does_not_cancel_other_target_and_is_retried_once(self):
        result, deps = await self.run_graph("payments-service와 cache-service 상태", {"cache-service": "error"})
        self.assertEqual(deps.mcp.calls.count("cache-service"), 2)
        self.assertEqual(deps.mcp.calls.count("payments-service"), 1)
        self.assertEqual(result["monitoring_repair_rounds"], 1)
        self.assertEqual(result["monitoring_coverage"]["missing_targets"], ["cache-service"])
        self.assertEqual(result["response_state"]["status"], "partial")
        self.assertIn("모니터링 미수집: cache-service", result["final"])
        self.assertEqual([c["target"] for c in deps.quality_llm.inputs[0] if c["source"] == "monitoring"],
                         ["payments-service"])

    async def test_recovery_on_second_round_becomes_completed_without_duplicate_success(self):
        result, deps = await self.run_graph("cache-service 상태", {"cache-service": "once"})
        self.assertEqual(deps.mcp.calls, ["cache-service", "cache-service"])
        self.assertEqual(result["response_state"]["status"], "completed")
        self.assertEqual([e["status"] for e in result["evidence"]], ["error", "ok"])

    async def test_unknown_wrong_service_and_missing_metrics_are_not_observed_facts(self):
        for behavior in ("unknown", "wrong_target", "invalid_metric", "nan_metric", "non_json"):
            with self.subTest(behavior=behavior):
                result, deps = await self.run_graph("cache-service 상태", {"cache-service": behavior})
                self.assertEqual(result["collected"], [])
                self.assertEqual(deps.quality_llm.inputs, [])
                self.assertEqual(deps.mcp.resources, [])
                self.assertEqual(result["response_state"]["status"], "partial")
                self.assertNotIn("0.0%", result["final"])

    async def test_timeouts_have_two_rounds_with_three_attempts_each(self):
        result, deps = await self.run_graph("cache-service 상태", {"cache-service": "timeout"})
        self.assertEqual(deps.mcp.calls.count("cache-service"), 6)
        self.assertEqual(result["monitoring_repair_rounds"], 1)
        self.assertEqual(result["retry_count"], 4)

    async def test_runbook_failure_does_not_erase_monitoring_success(self):
        for behavior in ("error", "discovery_error"):
            with self.subTest(behavior=behavior):
                result, deps = await self.run_graph("cache-service 상태", {"runbook": behavior})
                self.assertEqual(result["monitoring_coverage"]["successful_targets"], ["cache-service"])
                self.assertEqual(result["response_state"]["status"], "partial")

    async def test_registry_failure_stops_without_invented_targets(self):
        deps = make_deps()
        with patch("agent.graph.resolve_targets", side_effect=RegistryError("등록 목록 확인 실패")):
            result = await build_graph(deps).ainvoke({"query": "cache-service 상태", "events": [], "cost_usd": 0})
        self.assertEqual(deps.mcp.calls, [])
        self.assertEqual(result["target_resolution"]["requested_targets"], [])
        self.assertEqual(result["response_state"]["status"], "partial")

    async def test_broken_planner_still_uses_confirmed_scope(self):
        deps = make_deps()
        with patch.object(deps.fast_llm, "plan", side_effect=ValueError("broken plan")):
            result = await build_graph(deps).ainvoke({"query": "cache-service 상태", "events": [], "cost_usd": 0})
        self.assertEqual(deps.mcp.calls, ["cache-service"])
        self.assertEqual(result["response_state"]["status"], "completed")

    async def test_greeting_stays_on_chat_path(self):
        result, deps = await self.run_graph("안녕하세요")
        self.assertEqual(result["route"], "chat")
        self.assertEqual(deps.mcp.calls, [])


class ApiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        # 서버 import가 개인 .env를 읽지 않도록 차단한다. lifespan은 실행하지 않는다.
        with patch("dotenv.load_dotenv"):
            self.server = importlib.import_module("server")
        self.tmp = tempfile.TemporaryDirectory()
        self.store = SqliteSessionStore(Path(self.tmp.name) / "test.db")
        self.deps = make_deps(store=self.store)
        state = self.server.app.state
        state.deps, state.graph = self.deps, build_graph(self.deps)
        state.llm_backend, state.instance_id = "injected", 1
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=self.server.app), base_url="http://test")
        self.sleep_patch = patch("agent.graph.asyncio.sleep", new=AsyncMock())
        self.sleep_patch.start()

    async def asyncTearDown(self):
        await self.client.aclose()
        self.store.close()
        self.tmp.cleanup()
        self.sleep_patch.stop()

    async def test_post_and_sse_have_same_status_scope_coverage_and_saved_reply(self):
        for query in ("payments-service와 cache-service 상태", "billing-service 장애",
                      "서비스가 느려요", "payments-service와 billing-service 상태",
                      "auth-service 말고 cache-service만 상태", "payments-service-3 상태"):
            with self.subTest(query=query):
                post = await self.client.post("/chat", json={"message": query, "thread_id": "post"})
                self.assertEqual(post.status_code, 200)
                post = post.json()
                stream = await self.client.get("/chat/stream", params={"message": query, "thread_id": "sse"})
                events = [json.loads(line[6:]) for line in stream.text.splitlines()
                          if line.startswith("data: ") and line[6:] != "[DONE]"]
                final = [event for event in events if event["type"] == "FinalAnswer"]
                self.assertEqual(len(final), 1)
                self.assertTrue(stream.text.rstrip().endswith("data: [DONE]"))
                self.assertEqual(post["reply"], final[0]["reply"])
                for key in ("processing_status", "validation_status", "target_resolution", "monitoring_coverage", "response_state"):
                    self.assertEqual(post["metadata"][key], final[0]["metadata"][key])
                self.assertEqual(self.store.load("sse")[-1]["content"], final[0]["reply"])
                if post["metadata"]["processing_status"] == "needs_clarification":
                    self.assertFalse(any(e["type"] == "AnswerToken" for e in events))

    async def test_post_and_sse_both_report_permanent_collection_failure_as_partial(self):
        self.deps.mcp.behavior["cache-service"] = "error"
        post = (await self.client.post("/chat", json={"message": "cache-service 상태"})).json()
        stream = await self.client.get("/chat/stream", params={"message": "cache-service 상태"})
        final = next(json.loads(line[6:]) for line in stream.text.splitlines()
                     if line.startswith('data: {"type": "FinalAnswer"'))
        self.assertEqual(post["metadata"]["processing_status"], "partial")
        self.assertEqual(final["metadata"]["monitoring_coverage"]["missing_targets"], ["cache-service"])
        self.assertEqual(post["reply"], final["reply"])


class MonitoringToolTests(unittest.TestCase):
    def test_unregistered_service_returns_null_metrics(self):
        from mcp_server import get_monitoring_status
        tool = getattr(get_monitoring_status, "fn", get_monitoring_status)
        status = tool("not-registered-service")
        self.assertEqual(status.status, "unknown")
        self.assertIsNone(status.cpu_percent)
        self.assertIsNone(status.memory_percent)


if __name__ == "__main__":
    unittest.main()
