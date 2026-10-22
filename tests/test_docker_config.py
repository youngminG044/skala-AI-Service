"""Docker 사전 점검과 프록시 주소·SSE 전달을 유료 호출 없이 검증한다."""
import copy
import importlib
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import httpx
from agent.runbook_search import document_hash

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location('ops_docker_entrypoint', ROOT / 'docker/entrypoint.py')
entry = importlib.util.module_from_spec(spec)
spec.loader.exec_module(entry)


class DockerStartupTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.docs = self.root / 'runbooks'; self.docs.mkdir()
        text = '# CPU\n1. 부하를 확인한다.\n'
        (self.docs / 'cpu-spike.md').write_text(text)
        self.index = self.root / 'index.json'
        self.valid = {'version': 2, 'model': 'test-embedding', 'dimensions': 2,
                      'documents': [{'document_id': 'cpu-spike', 'sha256': document_hash(text), 'embedding': [1.0, 0.0]}]}
        self.index.write_text(json.dumps(self.valid))
        self.env = patch.dict(os.environ, {'OPENAI_API_KEY': 'test-only-key', 'OPENAI_EMBEDDING_MODEL': 'test-embedding',
                       'OPENAI_EMBEDDING_DIMENSIONS': '2', 'RUNBOOK_INDEX_PATH': str(self.index),
                       'SESSION_DB_PATH': str(self.root / 'sessions/session.db')})
        self.env.start(); self.addCleanup(self.env.stop)

    def test_missing_key_stops_before_creating_session_or_querying_index(self):
        for value in ('', 'sk-...', 'your_api_key_here'):
            with patch.dict(os.environ, {'OPENAI_API_KEY': value}), patch.object(entry, 'check_index') as check:
                with self.assertRaises(entry.SetupError): entry.prepare('backend')
                check.assert_not_called()
        self.assertFalse((self.root / 'sessions').exists())

    def test_local_index_validation_rejects_stale_or_corrupt_data_without_network(self):
        mutations = []
        for key, value in [('version', 1), ('model', 'another-model'), ('dimensions', 3)]:
            row = copy.deepcopy(self.valid); row[key] = value; mutations.append(row)
        for key, value in [('sha256', 'wrong'), ('embedding', [0, 0]), ('embedding', [float('nan'), 0])]:
            row = copy.deepcopy(self.valid); row['documents'][0][key] = value; mutations.append(row)
        with patch('httpx.post', side_effect=AssertionError('network must not run')) as post:
            entry.check_index(self.index, self.docs)
            for row in mutations:
                self.index.write_text(json.dumps(row))
                with self.assertRaises(entry.SetupError): entry.check_index(self.index, self.docs)
            with self.assertRaises(entry.SetupError): entry.check_index(self.root / 'missing.json', self.docs)
            post.assert_not_called()

    def test_backend_preparation_preserves_index_and_uses_one_worker(self):
        original = self.index.read_bytes(); check = entry.check_index
        with patch.object(entry, 'check_index', side_effect=lambda p: check(p, self.docs)), patch('httpx.post') as post:
            args = entry.prepare('backend'); entry.prepare('backend'); post.assert_not_called()
        self.assertEqual(self.index.read_bytes(), original)
        self.assertEqual(args[args.index('--workers')+1], '1')
        self.assertFalse((self.root / 'sessions/session.db').exists())
        self.assertTrue((self.root / 'sessions').is_dir())

    def test_indexer_preparation_does_not_implicitly_embed_documents(self):
        path = self.root / 'new/index.json'
        with patch.dict(os.environ, {'RUNBOOK_INDEX_PATH': str(path)}), patch('httpx.post') as post:
            args = entry.prepare('indexer')
        post.assert_not_called()
        self.assertTrue(path.parent.is_dir()); self.assertFalse(path.exists())
        self.assertEqual(args, ['sh/build_runbook_index.py'])

    def test_frontend_bad_url_does_not_echo_embedded_credentials(self):
        with patch.dict(os.environ, {'BACKEND_URL': 'https://private-token@example.com'}):
            with self.assertRaises(entry.SetupError) as error: entry.prepare('frontend')
        self.assertNotIn('private-token', str(error.exception))
        with patch.dict(os.environ, {'BACKEND_URL': 'http://backend:8100'}):
            self.assertIn('web_demo_server:app', entry.prepare('frontend'))


class DockerProxyTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.web = importlib.import_module('web_demo_server'); self.original_backend = self.web.BACKEND
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=self.web.app), base_url='http://frontend')
        self.real_client = httpx.AsyncClient; self.calls = []

    async def asyncTearDown(self):
        await self.client.aclose(); self.web.BACKEND = self.original_backend

    def outbound(self, **kwargs):
        def handler(request):
            self.calls.append(request)
            if request.url.path == '/chat/stream':
                return httpx.Response(200, headers={'content-type': 'text/event-stream'},
                                      content='data: {"type":"FinalAnswer","reply":"확인"}\n\ndata: [DONE]\n\n'.encode())
            return httpx.Response(200, json={'ok': True})
        return self.real_client(transport=httpx.MockTransport(handler), **kwargs)

    async def test_env_address_and_local_default_work_for_chat_and_history(self):
        for setting, expected in [('http://backend:8100/', 'backend'), (None, '127.0.0.1')]:
            values = dict(os.environ)
            if setting is None: values.pop('BACKEND_URL', None)
            else: values['BACKEND_URL'] = setting
            with patch.dict(os.environ, values, clear=True): importlib.reload(self.web)
            with patch.object(self.web.httpx, 'AsyncClient', side_effect=self.outbound):
                response = await self.client.post('/chat', json={'message': 'test', 'thread_id': 'a'})
                self.assertEqual(response.status_code, 200)
                response = await self.client.get('/sessions/a')
                self.assertEqual(response.headers['cache-control'], 'no-store')
            self.assertEqual([str(r.url.host) for r in self.calls[-2:]], [expected, expected])
            self.assertEqual(json.loads(self.calls[-2].content)['thread_id'], 'a')

    async def test_sse_uses_service_dns_and_preserves_final_event_and_done(self):
        self.web.BACKEND = 'http://backend:8100'
        with patch.object(self.web.httpx, 'AsyncClient', side_effect=self.outbound):
            response = await self.client.get('/chat/stream', params={'message': '캐시 확인', 'thread_id': 'a'})
        self.assertEqual(self.calls[-1].url.host, 'backend')
        self.assertEqual(self.calls[-1].url.params['thread_id'], 'a')
        self.assertIn('FinalAnswer', response.text)
        self.assertTrue(response.text.rstrip().endswith('data: [DONE]'))
