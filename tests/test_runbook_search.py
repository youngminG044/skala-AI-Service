"""유료 호출 없이 검색·캐시·근거 경계의 실제 실패 동작을 검증한다."""
import copy
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import httpx
from pydantic import ValidationError
from agent.contracts import RunbookSearchResult
from agent.grounding import prepare_report, validate_report
from agent.graph import build_graph
from agent.runbook_search import (OpenAIEmbedder, SearchError, build_index, build_service_query,
                                   document_hash, search_runbooks)
from agent.runbook_rerank import rerank_runbooks
import test_target_pipeline as support


class Vectors:
    model = "test-embedding"
    dimensions = 2

    def __init__(self):
        self.calls = []

    def embed(self, texts):
        self.calls.append(texts)
        return [[1, 0] if "CPU" in t else [0, 1] for t in texts], len(texts)


class IndexTests(unittest.TestCase):
    def setUp(self):
        self.rerank_env = patch.dict("os.environ", {"RUNBOOK_RERANK_ENABLED": "false"})
        self.rerank_env.start()
        self.addCleanup(self.rerank_env.stop)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.docs = root / "docs"
        self.docs.mkdir()
        (self.docs / "cpu-spike.md").write_text("CPU\n1. 부하 확인")
        (self.docs / "memory-leak.md").write_text("메모리\n1. 힙 확인")
        self.path, self.embedder = root / "index.json", Vectors()
        self.kwargs = {"directory": self.docs, "index_path": self.path, "embedder": self.embedder}

    def test_unchanged_index_is_reused_without_any_embedding_call(self):
        first = build_index(**self.kwargs)
        second = build_index(**self.kwargs)
        self.assertEqual(first["embedded_documents"], 2)
        self.assertEqual(second["embedding_tokens"], 0)
        self.assertEqual(second["reused_documents"], 2)
        self.assertEqual(len(self.embedder.calls), 1)

    def test_only_changed_document_is_embedded_and_deleted_document_is_removed(self):
        build_index(**self.kwargs)
        (self.docs / "cpu-spike.md").write_text("CPU 新\n1. 부하 확인")
        row = build_index(**self.kwargs)
        self.assertEqual(row["embedded_documents"], 1)
        (self.docs / "memory-leak.md").unlink()
        self.assertEqual(build_index(**self.kwargs)["document_count"], 1)

    def test_model_change_requires_explicit_rebuild(self):
        build_index(**self.kwargs)
        self.embedder.model = "other-model"
        before = len(self.embedder.calls)
        with self.assertRaises(SearchError):
            search_runbooks("CPU", **self.kwargs)
        self.assertEqual(len(self.embedder.calls), before)
        self.assertEqual(build_index(**self.kwargs)["embedded_documents"], 2)

    def test_document_change_stops_before_query_api_call(self):
        build_index(**self.kwargs)
        (self.docs / "cpu-spike.md").write_text("changed")
        with self.assertRaises(SearchError):
            search_runbooks("CPU", **self.kwargs)
        self.assertEqual(len(self.embedder.calls), 1)

    def test_missing_index_never_implicitly_builds(self):
        with self.assertRaises(SearchError):
            search_runbooks("CPU", **self.kwargs)
        self.assertEqual(self.embedder.calls, [])

    def test_top_two_candidates_and_selection_are_separate(self):
        build_index(**self.kwargs)
        row = search_runbooks("CPU", **self.kwargs, threshold=0.5, score_gap=0.1)
        self.assertEqual(len(row.candidates), 2)
        self.assertEqual(row.selected_ids, ("cpu-spike",))

    def test_no_match_retains_candidates_without_forcing_default(self):
        build_index(**self.kwargs)
        self.embedder.embed = lambda texts: ([[0.7, 0.7]], 1)
        row = search_runbooks("unrelated", **self.kwargs, threshold=0.9)
        self.assertEqual(row.status, "no_match")
        self.assertEqual(row.selected_ids, ())

    def test_broken_vector_is_rejected_before_api_call(self):
        build_index(**self.kwargs)
        data = json.loads(self.path.read_text())
        data["documents"][0]["embedding"] = [math.nan, 0]
        self.path.write_text(json.dumps(data))
        with self.assertRaises(SearchError):
            search_runbooks("CPU", **self.kwargs)
        self.assertEqual(len(self.embedder.calls), 1)

    def test_invalid_query_and_k_are_rejected(self):
        for query, k in (("", 2), ("CPU", 0), ("CPU", 3), ("CPU", True)):
            with self.assertRaises(SearchError):
                search_runbooks(query, k, **self.kwargs)


class OpenAIEmbeddingTests(unittest.TestCase):
    @patch.dict("os.environ", {"OPENAI_API_KEY": "test-key"})
    def test_request_dimensions_order_validation_and_real_usage(self):
        response = httpx.Response(200, request=httpx.Request("POST", "https://api.openai.com/v1/embeddings"),
            json={"model": "test", "data": [{"index": 1, "embedding": [0, 2]}, {"index": 0, "embedding": [3, 0]}],
                  "usage": {"total_tokens": 7}})
        with patch("agent.runbook_search.httpx.post", return_value=response) as post:
            vectors, tokens = OpenAIEmbedder("test", 2).embed(["a", "b"])
        self.assertEqual(vectors, [[1, 0], [0, 1]])
        self.assertEqual(tokens, 7)
        self.assertEqual(post.call_args.kwargs["json"]["dimensions"], 2)

    @patch.dict("os.environ", {"OPENAI_API_KEY": "test-key"})
    def test_api_error_does_not_include_key_or_response_body(self):
        response = httpx.Response(401, request=httpx.Request("POST", "https://api.openai.com/v1/embeddings"), text="test-key")
        with patch("agent.runbook_search.httpx.post", return_value=response):
            with self.assertRaises(SearchError) as error:
                OpenAIEmbedder("test", 2).embed(["a"])
        self.assertNotIn("test-key", str(error.exception))


class RerankingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.docs = Path(self.tmp.name)
        self.text = "CPU\n1. 프로세스별 부하를 확인한다."
        (self.docs / "cpu-spike.md").write_text(self.text)
        self.candidate = {"document_id": "cpu-spike", "reference": "runbook://incident-response/cpu-spike",
                          "document_sha256": document_hash(self.text), "score": 0.9}

    def rerank(self, raw):
        from agent.contracts import RunbookCandidate
        class LLM:
            model = "test-model"
            def _chat(self, system, user, json_mode=False):
                return json.dumps(raw), {"response_model": "test-snapshot", "prompt_tokens": 10, "completion_tokens": 3, "tokens": 13}
        return rerank_runbooks("CPU high", [RunbookCandidate.model_validate(self.candidate)], directory=self.docs, llm=LLM())

    def test_empty_selection_and_real_token_fields_are_retained(self):
        result = self.rerank({"ranked_ids": []})
        self.assertEqual(result.ranked_ids, ())
        self.assertEqual(result.total_tokens, 13)

    def test_unknown_duplicate_and_extra_fields_are_rejected(self):
        for raw in ({"ranked_ids": ["db-down"]}, {"ranked_ids": ["cpu-spike", "cpu-spike"]},
                    {"ranked_ids": ["cpu-spike"], "quote": "invented"}):
            with self.assertRaises(SearchError):
                self.rerank(raw)

    def test_changed_document_is_rejected_before_model_call(self):
        (self.docs / "cpu-spike.md").write_text("changed")
        with self.assertRaises(SearchError):
            self.rerank({"ranked_ids": ["cpu-spike"]})

    def test_search_contract_disallows_missing_candidate_and_inconsistent_ranker(self):
        row = {"query": "CPU high", "status": "matched", "embedding_model": "test", "dimensions": 2,
               "candidates": [self.candidate], "selected_ids": ["db-down"]}
        with self.assertRaises(ValidationError):
            RunbookSearchResult.model_validate(row)
        row["selected_ids"] = ["cpu-spike"]
        row["reranking"] = {"ranked_ids": [], "model": "test", "prompt_tokens": 10,
                            "completion_tokens": 1, "total_tokens": 11, "elapsed_seconds": 1}
        with self.assertRaises(ValidationError):
            RunbookSearchResult.model_validate(row)


class QueryTests(unittest.TestCase):
    def test_observations_are_preserved_without_unobserved_cause(self):
        data = {"system_name": "cache-service", "status": "degraded", "cpu_percent": 42,
                "memory_percent": 89, "alerts": ["heap near limit"]}
        query = build_service_query("cache-service", data)
        self.assertIn("heap near limit", query)
        self.assertNotIn("누수", query)
        self.assertNotIn("payments-service", query)

    def test_healthy_without_alerts_needs_no_search(self):
        self.assertIsNone(build_service_query("web-01", {"system_name": "web-01", "status": "healthy", "alerts": []}))

    def test_wrong_service_cannot_form_query(self):
        with self.assertRaises(SearchError):
            build_service_query("cache-service", {"system_name": "payments-service"})


class SearchMCP(support.TestMCP):
    def __init__(self, behavior=None):
        super().__init__(behavior)
        self.searches = []

    async def call_tool(self, name, arguments, server=None):
        if name != "search_runbooks":
            data = await super().call_tool(name, arguments, server)
            if arguments["system_name"] == "cache-service":
                data["alerts"] = ["heap near limit"]
            if self.behavior.get("healthy"):
                data.update(status="healthy", alerts=[])
            return data
        self.searches.append(arguments["query"])
        if self.behavior.get("search") == "error":
            return {"status": "error", "reason": "test API unavailable"}
        row = await super().call_tool(name, arguments, server)
        if "heap near limit" in arguments["query"]:
            row["candidates"][0].update(document_id="memory-leak", reference="runbook://incident-response/memory-leak")
            row["selected_ids"] = ["memory-leak"]
        if self.behavior.get("search") == "no_match":
            row.update(status="no_match", selected_ids=[])
        if self.behavior.get("search") == "wrong_query":
            row["query"] = "another service"
        return row


class RetrievalGraphTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = support.GraphTests.asyncSetUp

    async def run_case(self, query="payments-service와 cache-service 상태", behavior=None):
        deps = support.make_deps(SearchMCP(behavior))
        result = await build_graph(deps).ainvoke({"query": query, "events": [], "cost_usd": 0})
        return result, deps

    async def test_per_service_search_and_citations_do_not_mix(self):
        result, deps = await self.run_case()
        self.assertEqual(len(deps.mcp.searches), 2)
        self.assertNotIn("heap near limit", deps.mcp.searches[0])
        self.assertNotIn("CPU high", deps.mcp.searches[1])
        self.assertEqual([s["selected_ids"] for s in result["runbook_searches"]], [["cpu-spike"], ["memory-leak"]])
        self.assertEqual(result["validation_result"]["status"], "passed")
        self.assertIn("[payments-service] runbook://incident-response/cpu-spike", result["final"])
        self.assertIn("[cache-service] runbook://incident-response/memory-leak", result["final"])

    async def test_healthy_skips_embedding_and_finishes(self):
        result, deps = await self.run_case("web-01 상태", {"healthy": True})
        self.assertEqual(deps.mcp.searches, [])
        self.assertEqual(result["response_state"]["status"], "completed")
        self.assertEqual(result["runbook_coverage"]["services"][0]["status"], "not_needed")

    async def test_no_match_api_error_and_wrong_response_keep_facts_without_default(self):
        for mode in ("no_match", "error", "wrong_query"):
            result, deps = await self.run_case("cache-service 상태", {"search": mode})
            self.assertEqual(result["response_state"]["status"], "partial")
            self.assertIn("CPU=81.5%", result["final"])
            self.assertEqual(deps.mcp.resources, [])
            self.assertNotIn("db-down", result["final"])
            self.assertEqual(len(deps.mcp.searches), 1)

    async def test_changed_resource_is_not_accepted_as_search_evidence(self):
        deps = support.make_deps(SearchMCP())
        with patch.object(deps.mcp, "read_resource", return_value="# changed\n1. altered"):
            result = await build_graph(deps).ainvoke({"query": "cache-service 상태", "events": [], "cost_usd": 0})
        self.assertEqual(result["runbook_coverage"]["services"][0]["status"], "fetch_failed")
        self.assertEqual(result["response_state"]["status"], "partial")

    async def test_missing_and_cross_service_citations_are_rejected(self):
        result, _ = await self.run_case()
        evidence, targets = result["evidence"], ["payments-service", "cache-service"]
        report = copy.deepcopy(result["draft_report"])
        report["recommendations"] = report["recommendations"][:1]
        codes = {i.code for i in validate_report(report, evidence, targets, 1).issues}
        self.assertIn("runbook_citation_missing", codes)
        report = copy.deepcopy(result["draft_report"])
        report["recommendations"][0]["target"] = "cache-service"
        self.assertIn("runbook_target", {i.code for i in validate_report(report, evidence, targets, 1).issues})

    async def test_trace_with_another_observation_is_rejected(self):
        result, _ = await self.run_case()
        evidence = copy.deepcopy(result["evidence"])
        book = next(e for e in evidence if e["source_type"] == "runbook")
        book["retrieval"]["monitoring_source_id"] = "forged-observation"
        self.assertIn("retrieval_evidence", {i.code for i in validate_report(result["draft_report"], evidence,
                     ["payments-service", "cache-service"], 1).issues})


class RetrievalApiTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = support.ApiTests.asyncSetUp
    asyncTearDown = support.ApiTests.asyncTearDown

    async def test_post_and_sse_expose_same_service_scope_and_do_not_stream_drafts(self):
        self.deps.mcp = SearchMCP()
        self.server.app.state.graph = build_graph(self.deps)
        query = "payments-service와 cache-service 상태"
        post = (await self.client.post("/chat", json={"message": query, "thread_id": "post-rag"})).json()
        stream = await self.client.get("/chat/stream", params={"message": query, "thread_id": "sse-rag"})
        events = [json.loads(line[6:]) for line in stream.text.splitlines() if line.startswith('data: {')]
        final = next(e for e in events if e["type"] == "FinalAnswer")
        for body in (post, final):
            meta = body["metadata"]
            self.assertEqual([s["target"] for s in meta["runbook_searches"]], ["payments-service", "cache-service"])
            self.assertEqual([s["selected_ids"] for s in meta["runbook_searches"]], [["cpu-spike"], ["memory-leak"]])
            self.assertTrue(all(s["status"] == "fetched" for s in meta["runbook_coverage"]["services"]))
            self.assertEqual(meta["validation_status"], "passed")
        self.assertFalse(any(e["type"] == "AnswerToken" for e in events))
        self.assertIsNone(final["prompt"])
        self.assertEqual(self.store.load("sse-rag")[-1]["content"], final["reply"])


if __name__ == "__main__":
    unittest.main()
