from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase, TestCase
from unittest.mock import AsyncMock, patch

import httpx

from texts import MAX_CAPTURE_CHARS, TextRegistry, capture_document, text_result
from page import read_public_page
from providers import extract_firecrawl_document
from retrieval import retrieve
from test_support import TestPlugin, plugin, host_context


class RegistryTests(TestCase):
    def doc(self, text='中文😀abc' * 1000):
        return capture_document('https://example.com/a', 'title', text, 'local')

    def test_exact_unicode_slices_reassemble_without_fetch(self):
        doc = self.doc()
        registry = TextRegistry()
        token = registry.create('chat-a', doc)
        chunks, offset = [], 0
        while True:
            result = text_result(registry.get('chat-a', token), token, offset=offset, limit=1000)
            chunks.append(doc.text[result['start']:result['end']])
            if result['next_offset'] is None:
                break
            offset = result['next_offset']
        self.assertEqual(''.join(chunks), doc.text)
        self.assertIn('不代表网站全文', result['content'])
        self.assertNotIn('沿用本次真实目标会话', result['content'])
        self.assertIn(f'text_id={token}', result['content'])

    def test_scope_missing_and_invalid_tokens_are_indistinguishable(self):
        store = TextRegistry()
        token = store.create('chat-a', self.doc())
        for scope, value in [('chat-b', token), ('', token), ('chat-a', 'missing'), ('chat-a', []), ([], token)]:
            self.assertIsNone(store.get(scope, value))
        self.assertEqual(store.create('', self.doc()), '')

    def test_expiry_is_fixed_and_reads_never_extend(self):
        now = [0]
        store = TextRegistry(clock=lambda: now[0], ttl=10)
        token = store.create('a', self.doc())
        now[0] = 9
        self.assertIsNotNone(store.get('a', token))
        now[0] = 10
        self.assertIsNone(store.get('a', token))
        self.assertEqual(store.bytes_used, 0)

    def test_entry_and_scope_eviction_and_clear(self):
        store = TextRegistry(max_entries=3, per_scope=2)
        old = store.create('a', self.doc())
        second = store.create('a', self.doc())
        store.create('b', self.doc())
        store.create('a', self.doc())
        self.assertIsNone(store.get('a', old))
        self.assertIsNotNone(store.get('a', second))
        store.create('c', self.doc())
        self.assertIsNone(store.get('a', second))
        store.clear()
        self.assertFalse(store.entries)
        self.assertEqual(store.bytes_used, 0)

    def test_utf8_byte_budget_includes_metadata(self):
        store = TextRegistry(max_bytes=1000)
        self.assertEqual(store.create('a', self.doc('😀' * 300)), '')
        first = store.create('a', self.doc('😀' * 150))
        store.create('b', self.doc('😀' * 150))
        self.assertIsNone(store.get('a', first))
        self.assertLessEqual(store.bytes_used, 1000)

    def test_capture_limit_distinct_from_display_limit(self):
        doc = self.doc('字' * (MAX_CAPTURE_CHARS + 20))
        self.assertEqual(len(doc.text), MAX_CAPTURE_CHARS)
        self.assertTrue(doc.capture_limited)
        result = text_result(doc, 'id', limit=4000)
        self.assertEqual(result['end'], 4000)
        self.assertTrue(result['has_more'])
        self.assertTrue(result['capture_limited'])
        exact = self.doc('x' * MAX_CAPTURE_CHARS)
        self.assertFalse(exact.capture_limited)

    def test_literal_keyword_window_and_not_found(self):
        doc = self.doc('a' * 5000 + '[x].*中文' + 'z' * 5000)
        result = text_result(doc, 'id', keyword='[x].*中文', limit=1000)
        self.assertEqual(result['match_offset'], 5000)
        self.assertIn('[x].*中文', result['content'])
        self.assertEqual(result['end'] - result['start'], 1000)
        missing = text_result(doc, 'id', keyword='not there')
        self.assertEqual(missing['status'], 'not_found')
        self.assertNotIn('aaaaa', missing['content'])
        self.assertIsNone(missing['next_offset'])
        self.assertEqual(text_result(doc, 'id', offset=6000, keyword='中文')['status'], 'not_found')

    def test_source_metadata_does_not_swallow_search_body_budget(self):
        doc = capture_document('https://example.com/' + 'x' * 10000, 't' * 300, '正文' * 3000, 'firecrawl')
        result = text_result(doc, 't' * 24, limit=4000)
        self.assertLessEqual(len(result['content']), 5100)


class CaptureTests(IsolatedAsyncioTestCase):
    async def test_local_capture_preserves_tail_and_keeps_display_small(self):
        body = '<article>' + 'a' * 10000 + 'TAIL_MARKER</article>'
        with patch('page.download_public', AsyncMock(return_value=(body.encode(), 'text/html', 'https://example.com'))):
            page = await read_public_page(None, 'https://example.com', text_limit=1000, preview_only=True, capture_text=True)
        self.assertNotIn('TAIL_MARKER', page.text)
        self.assertIn('TAIL_MARKER', page.captured_text)
        self.assertFalse(page.capture_limited)

    async def test_local_capture_marks_upper_bound_without_storing_notice(self):
        body = '字' * (MAX_CAPTURE_CHARS + 100)
        with patch('page.download_public', AsyncMock(return_value=(body.encode(), 'text/plain', 'https://example.com'))):
            page = await read_public_page(None, 'https://example.com', capture_text=True, preview_only=True)
        self.assertEqual(page.captured_text, '字' * MAX_CAPTURE_CHARS)
        self.assertTrue(page.capture_limited)

    async def test_firecrawl_retains_body_before_output_formatting(self):
        body = 'b' * 10000 + 'TAIL_MARKER'
        with patch('providers.validate_remote_target', AsyncMock()):
            async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, json={
                    'success': True, 'data': {'markdown': body, 'metadata': {'title': 'page'}}}))) as client:
                doc = await extract_firecrawl_document(client, 'test', 'https://example.com', 20)
        self.assertEqual(doc.text, body)
        self.assertFalse(doc.capture_limited)

    async def test_firecrawl_capture_has_its_own_upper_bound(self):
        with patch('providers.validate_remote_target', AsyncMock()):
            async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, json={
                    'success': True, 'data': {'markdown': 'x' * (MAX_CAPTURE_CHARS + 1)}}))) as client:
                doc = await extract_firecrawl_document(client, 'test', 'https://example.com', 20)
        self.assertEqual(len(doc.text), MAX_CAPTURE_CHARS)
        self.assertTrue(doc.capture_limited)

    async def test_search_read_registers_body_and_search_only_does_not(self):
        config = SimpleNamespace(exa_api_key='test', firecrawl_api_key='test', extract_provider='firecrawl', timeout_seconds=5)
        document = capture_document('https://example.com', 'page', 'z' * 9000 + 'TAIL', 'firecrawl')
        store = TextRegistry()
        def render(doc, limit):
            return text_result(doc, store.create('chat-a', doc), limit=limit)['content']
        with patch('retrieval.search_exa_results', AsyncMock(return_value=[{'url': document.url}])), \
                patch('retrieval.extract_firecrawl_document', AsyncMock(return_value=document)) as extract:
            result = await retrieve(None, config, 'query', 2, {}, 1, render_document=render)
            self.assertIn('neko_web_text', result)
            token = next(iter(store.entries))
            self.assertIn('TAIL', store.get('chat-a', token).text)
            await retrieve(None, config, 'query', 2, {}, 0, render_document=render)
            extract.assert_awaited_once()

    async def test_search_capture_failure_keeps_evidence_without_cache(self):
        from providers import ProviderError
        config = SimpleNamespace(exa_api_key='test', firecrawl_api_key='test', extract_provider='firecrawl', timeout_seconds=5)
        render = AsyncMock(side_effect=AssertionError('must not register failed extraction'))
        with patch('retrieval.search_exa_results', AsyncMock(return_value=[{'url': 'https://example.com', 'highlights': ['evidence']}])), \
                patch('retrieval.extract_firecrawl_document', AsyncMock(side_effect=ProviderError('HTTP 429'))):
            result = await retrieve(None, config, 'query', 2, {}, 1, render_document=render)
        self.assertIn('状态：partial', result)
        self.assertIn('evidence', result)
        self.assertIn('quota_or_rate_limit', result)
        render.assert_not_called()


class ToolTests(IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.p = TestPlugin()
        await self.p.on_load()
        self.doc = capture_document('https://example.com', 'page', 'a' * 9000 + '目标😀' + 'b' * 9000, 'firecrawl')
        self.token = self.p._texts.create('chat-a', self.doc)

    async def asyncTearDown(self):
        await self.p.on_unload()

    async def test_read_and_keyword_no_network_no_send(self):
        with patch.object(plugin.httpx, 'AsyncClient', side_effect=AssertionError('network forbidden')):
            result = await self.p.handle_text(self.token, offset=8000, limit=4000, _host_context=host_context('chat-a'))
            self.assertEqual(result['next_offset'], 12000)
            self.assertIn('目标😀', result['content'])
            found = await self.p.handle_text(self.token, keyword='目标', _host_context=host_context('chat-a'))
            self.assertEqual(found['match_offset'], 9000)
        self.p.ctx.send.image.assert_not_called()

    async def test_scope_and_missing_do_not_leak_source(self):
        for kwargs in [{}, {'stream_id': 'chat-b'}, {'stream_id': []},
                       {'_host_context': host_context('chat-b')}]:
            result = await self.p.handle_text(self.token, **kwargs)
            self.assertFalse(result['success'])
            self.assertEqual(result['status'], 'unavailable' if '_host_context' in kwargs else 'invalid_context')
            self.assertNotIn('example.com', result['content'])

    async def test_expiry_and_unload_clear_without_network_retry(self):
        now = [0]
        self.p._texts = plugin.TextRegistry(ttl=1, clock=lambda: now[0])
        token = self.p._texts.create('chat-a', self.doc)
        now[0] = 1
        with patch.object(plugin.httpx, 'AsyncClient', side_effect=AssertionError('must not refetch')):
            self.assertEqual((await self.p.handle_text(token, _host_context=host_context('chat-a')))['status'], 'unavailable')
        self.p._texts.create('chat-a', self.doc)
        await self.p.on_unload()
        self.assertEqual(self.p._texts.bytes_used, 0)
        self.assertFalse(self.p._texts.entries)

    async def test_bad_parameters_rejected(self):
        for args in [{'offset': True}, {'offset': -1}, {'offset': 999999}, {'offset': '10'},
                     {'keyword': []}, {'keyword': 'x' * 101}, {'limit': True}, {'limit': 99},
                     {'limit': 8001}, {'text_id': []}]:
            values = {'text_id': self.token, '_host_context': host_context('chat-a'), **args}
            result = await self.p.handle_text(**values)
            self.assertEqual(result['status'], 'invalid_request')

    async def test_end_empty_keyword_and_not_found_are_explicit(self):
        result = await self.p.handle_text(self.token, offset=len(self.doc.text), _host_context=host_context('chat-a'))
        self.assertFalse(result['has_more'])
        result = await self.p.handle_text(self.token, keyword='MISSING', _host_context=host_context('chat-a'))
        self.assertTrue(result['success'])
        self.assertEqual(result['status'], 'not_found')

    async def test_config_update_and_disable(self):
        await self.p.on_config_update(plugin.CONFIG_RELOAD_SCOPE_SELF, {}, 'new')
        self.assertIsNone(self.p._texts.get('chat-a', self.token))
        self.p.config.plugin.enabled = False
        self.assertEqual((await self.p.handle_text(self.token, _host_context=host_context('chat-a')))['status'], 'disabled')

    async def test_extract_wires_capture_to_tool(self):
        self.p.config.web.extract_provider = 'firecrawl'
        with patch.object(plugin, 'extract_firecrawl_document', AsyncMock(return_value=self.doc)) as extract:
            result = await self.p.handle_extract(self.doc.url, _host_context=host_context('chat-a'))
            self.assertEqual(result['total_chars'], len(self.doc.text))
            self.assertEqual(result['end'], 8000)
            follow = await self.p.handle_text(result['text_id'], keyword='目标', _host_context=host_context('chat-a'))
            self.assertEqual(follow['match_offset'], 9000)
            extract.assert_awaited_once()

    async def test_read_preserves_image_pagination_metadata(self):
        page = plugin.read_public_page.__globals__['PageRead']('https://example.com', 'title', 'short', [], [], [], False,
                                                              self.doc.text, False)
        self.p._pager.create = lambda *a, **kw: 'preview'
        self.p._pager.queues['preview'] = SimpleNamespace()
        self.p._pager.batch = AsyncMock(return_value={})
        self.p._batch_preview_result = lambda *a: {'content': 'images', 'has_more': False, 'next_cursor': '', 'content_items': []}
        async def run(action, failure):
            class Client:
                neko_blocked_ips = set()
            return await action(Client(), plugin.Deadline.after(30))
        self.p._run_public = run
        with patch.object(plugin, 'read_public_page', AsyncMock(return_value=page)):
            result = await self.p.handle_read(self.doc.url, _host_context=host_context('chat-a'))
        self.assertFalse(result['has_more'])
        self.assertTrue(result['text_has_more'])
        self.assertTrue(result['text_id'])
