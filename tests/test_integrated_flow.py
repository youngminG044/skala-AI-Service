"""네 개선 기능을 API·저장까지 연결한다. 모델/MCP 주입, 유료 호출 없음."""
import json
import unittest
import test_target_pipeline as support
from test_runbook_search import SearchMCP
from test_grounding_pipeline import SequenceWriter, BAD, GOOD


class IntegratedFlowTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = support.ApiTests.asyncSetUp
    asyncTearDown = support.ApiTests.asyncTearDown

    async def post(self, text):
        response = await self.client.post('/chat', json={'message': text, 'thread_id': 'integrated'})
        self.assertEqual(response.status_code, 200)
        return response.json()

    async def stream(self, text):
        response = await self.client.get('/chat/stream', params={'message': text, 'thread_id': 'integrated'})
        events = [json.loads(line[6:]) for line in response.text.splitlines() if line.startswith('data: {')]
        self.assertFalse(any(e['type'] in ('AnswerToken', 'Error') for e in events))
        self.assertTrue(response.text.rstrip().endswith('data: [DONE]'))
        return next(e for e in events if e['type'] == 'FinalAnswer')

    async def test_clarify_search_validate_refresh_and_restore(self):
        self.deps.mcp = SearchMCP()
        first = await self.post('메모리가 계속 증가해요')
        self.assertEqual(first['metadata']['processing_status'], 'needs_clarification')
        self.assertEqual(self.deps.mcp.calls, [])
        answer = await self.post('cache-service')
        self.assertEqual(answer['metadata']['context_resolution']['mode'], 'clarification_answer')
        self.assertEqual(answer['metadata']['runbook_searches'][0]['selected_ids'], ['memory-leak'])
        self.assertIn('[cache-service] runbook://incident-response/memory-leak', answer['reply'])
        followup = await self.stream('지금은 정상화됐어?')
        for body in (answer, followup):
            self.assertEqual(body['metadata']['validation_status'], 'passed')
            self.assertEqual(body['metadata']['target_resolution']['resolved_targets'], ['cache-service'])
        self.assertNotEqual(answer['metadata']['runbook_searches'][0]['monitoring_source_id'],
                            followup['metadata']['runbook_searches'][0]['monitoring_source_id'])
        counts = (len(self.deps.mcp.calls), len(self.deps.mcp.searches), len(self.deps.quality_llm.inputs))
        history = (await self.client.get('/sessions/integrated')).json()
        self.assertEqual(len(history['messages']), 6)
        self.assertEqual(history['messages'][-1]['content'], followup['reply'])
        self.assertEqual(history['messages'][-1]['metadata'], followup['metadata'])
        self.assertEqual(counts, (len(self.deps.mcp.calls), len(self.deps.mcp.searches), len(self.deps.quality_llm.inputs)))

    async def test_search_failure_is_saved_then_followup_recovers_with_new_sources(self):
        self.deps.mcp = SearchMCP({'search': 'error'})
        failed = await self.post('cache-service 상태와 대응 절차')
        self.assertEqual(failed['metadata']['processing_status'], 'partial')
        self.assertEqual(failed['metadata']['runbook_coverage']['services'][0]['status'], 'error')
        self.assertNotIn('runbook://incident-response/memory-leak', failed['reply'])
        saved = (await self.client.get('/sessions/integrated')).json()['messages'][-1]
        self.assertEqual(saved['metadata'], failed['metadata'])
        self.deps.mcp.behavior.clear()
        recovered = await self.stream('그 서비스 다시 확인해줘')
        self.assertEqual(recovered['metadata']['processing_status'], 'completed')
        self.assertEqual(recovered['metadata']['validation_status'], 'passed')
        self.assertEqual(recovered['metadata']['runbook_searches'][0]['selected_ids'], ['memory-leak'])
        self.assertEqual(self.deps.mcp.calls, ['cache-service', 'cache-service'])
        self.assertNotEqual(failed['metadata']['runbook_searches'][0]['monitoring_source_id'],
                            recovered['metadata']['runbook_searches'][0]['monitoring_source_id'])

    async def test_failed_multi_draft_ambiguity_and_chosen_target_recovery(self):
        self.deps.mcp = SearchMCP()
        self.deps.quality_llm = SequenceWriter([BAD])
        failed = await self.stream('payments-service와 cache-service 상태 확인')
        self.assertEqual(failed['metadata']['processing_status'], 'validation_failed')
        self.assertEqual(self.deps.quality_llm.attempts, [1, 2, 3])
        self.assertNotIn('UNVERIFIED_DRAFT_MARKER123', failed['reply'])
        ambiguous = await self.post('그 서비스 다시 확인해줘')
        self.assertEqual(ambiguous['metadata']['processing_status'], 'needs_clarification')
        self.assertEqual(self.deps.mcp.calls, ['payments-service', 'cache-service'])
        self.deps.quality_llm = SequenceWriter([GOOD])
        recovered = await self.stream('cache-service')
        self.assertEqual(recovered['metadata']['target_resolution']['resolved_targets'], ['cache-service'])
        self.assertEqual(recovered['metadata']['validation_status'], 'passed')
        self.assertNotIn('[payments-service] runbook://', recovered['reply'])
        history = (await self.client.get('/sessions/integrated')).json()
        self.assertNotIn('UNVERIFIED_DRAFT_MARKER123', json.dumps(history))
        self.assertEqual(history['messages'][1]['metadata']['validation_status'], 'failed')
        self.assertEqual(history['messages'][-1]['metadata']['validation_status'], 'passed')
