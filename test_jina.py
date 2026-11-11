import json
from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock, patch

import httpx

from providers import ProviderError, extract_jina_document
from retrieval import retrieve
from texts import MAX_CAPTURE_CHARS, capture_document
from test_support import TestPlugin, plugin, host_context


def envelope(content='正文', **page):
    return {'code': 200, 'status': 20000,
            'data': {'url': 'https://example.com/path?q=x#route', 'title': 'page', 'content': content, **page}}


class ReaderTests(IsolatedAsyncioTestCase):
    async def call(self, response, key=''):
        with patch('providers.validate_remote_target', AsyncMock()):
            async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: response)) as client:
                return await extract_jina_document(client, key, 'https://example.com/path?q=x#route', 25)

    async def test_fixed_post_body_headers_and_optional_auth(self):
        for key in ['', 'test-key']:
            def handler(req):
                self.assertEqual(str(req.url), 'https://r.jina.ai/')
                self.assertEqual(req.method, 'POST')
                self.assertEqual(req.headers['accept'], 'application/json')
                self.assertEqual(req.headers.get('authorization'), 'Bearer test-key' if key else None)
                self.assertEqual(json.loads(req.content), {'url': 'https://example.com/path?q=x#route',
                    'respondWith': 'markdown', 'assertStatusCode': 200, 'timeout': 25})
                return httpx.Response(200, json=envelope())
            with patch('providers.validate_remote_target', AsyncMock()) as validate:
                async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                    doc = await extract_jina_document(client, key, 'https://example.com/path?q=x#route', 25)
            self.assertEqual(doc.provider, 'jina')
            self.assertEqual(doc.text, '正文')
            validate.assert_awaited_once()

    async def test_target_rejected_before_request(self):
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: self.fail('must not request'))) as client:
            for url in ['http://127.0.0.1', 'https://user:secret@example.com', 'http://metadata.google.internal']:
                with self.assertRaises(ProviderError) as ctx:
                    await extract_jina_document(client, '', url, 20)
                self.assertEqual(ctx.exception.code, 'blocked_target')

    async def test_errors_safe_no_retry_or_redirect(self):
        for status, code in [(401, 'auth_error'), (402, 'quota_or_rate_limit'), (429, 'quota_or_rate_limit'),
                             (422, 'page_or_request_error'), (302, 'provider_error'), (503, 'provider_error')]:
            count = 0
            def handler(req):
                nonlocal count
                count += 1
                return httpx.Response(status, headers={'Location': 'https://other.test'}, text='private diagnostics')
            with patch('providers.validate_remote_target', AsyncMock()):
                async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                    with self.assertRaises(ProviderError) as ctx:
                        await extract_jina_document(client, '', 'https://example.com', 20)
            self.assertEqual(ctx.exception.code, code)
            self.assertNotIn('private', str(ctx.exception))
            self.assertEqual(count, 1)

    async def test_envelope_failure_and_bad_types_never_become_content(self):
        for payload in [{}, [], {'code': True, 'status': 20000}, {'code': 200, 'status': 20001, 'data': {}},
                        {'code': 200, 'status': 20000, 'data': 'text'},
                        envelope(content=None), envelope(title=[]), envelope(url=[]),
                        {'code': 401, 'status': 40101, 'message': 'SECRET'},
                        {'code': 422, 'status': 42201, 'message': 'SECRET'}]:
            with self.assertRaises(ProviderError) as ctx:
                await self.call(httpx.Response(200, json=payload))
            self.assertNotIn('SECRET', str(ctx.exception))

    async def test_empty_invalid_and_oversized_responses(self):
        for response, code in [(httpx.Response(200, json=envelope('  ')), 'no_content'),
                               (httpx.Response(200, text='<html>not JSON</html>'), 'invalid_response'),
                               (httpx.Response(200, content=b'x' * (2 * 1024 * 1024 + 1)), 'response_too_large')]:
            with self.assertRaises(ProviderError) as ctx:
                await self.call(response)
            self.assertEqual(ctx.exception.code, code)

    async def test_transport_timeout_and_cancellation_never_retry(self):
        for exception in [httpx.ReadTimeout('SECRET'), httpx.ConnectError('SECRET')]:
            count = 0
            def handler(req):
                nonlocal count
                count += 1
                raise exception
            with patch('providers.validate_remote_target', AsyncMock()):
                async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                    with self.assertRaises(ProviderError) as ctx:
                        await extract_jina_document(client, '', 'https://example.com', 20)
            self.assertNotIn('SECRET', str(ctx.exception))
            self.assertEqual(count, 1)
        async def cancelled(*args, **kwargs):
            raise asyncio.CancelledError
        with patch('providers.validate_remote_target', AsyncMock()), patch('providers.post_json', side_effect=cancelled):
            with self.assertRaises(asyncio.CancelledError):
                await extract_jina_document(None, '', 'https://example.com', 20)

    async def test_capture_limit_and_final_source_checked(self):
        doc = await self.call(httpx.Response(200, json=envelope('字' * (MAX_CAPTURE_CHARS + 1))))
        self.assertTrue(doc.capture_limited)
        self.assertEqual(len(doc.text), MAX_CAPTURE_CHARS)
        # Returned target URLs are metadata, never followed locally; still validate
        # them before retaining a source (remote redirects remain provider-owned).
        async def validate(url):
            if url != 'https://example.com/path?q=x#route':
                raise ProviderError('blocked', 'blocked_target')
        with patch('providers.validate_remote_target', side_effect=validate):
            async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200,
                    json=envelope(url='http://127.0.0.1')))) as client:
                with self.assertRaises(ProviderError):
                    await extract_jina_document(client, '', 'https://example.com/path?q=x#route', 20)

    async def test_reported_target_http_status_not_ignored(self):
        for target, code in [(404, 'page_error'), (True, 'invalid_response'), ('200', 'invalid_response')]:
            with self.assertRaises(ProviderError) as ctx:
                await self.call(httpx.Response(200, json=envelope(httpStatus=target)))
            self.assertEqual(ctx.exception.code, code)


class RoutingTests(IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.p = TestPlugin()
        await self.p.on_load()
        self.doc = capture_document('https://example.com', 'page', '前文' * 6000 + '目标段落', 'jina')

    async def asyncTearDown(self):
        await self.p.on_unload()

    async def test_default_and_existing_provider_unchanged(self):
        self.assertEqual(plugin.WebConfig().extract_provider, 'anysearch')
        self.assertEqual(plugin.WebConfig().jina_api_key, '')
        self.p.config.web.extract_provider = 'firecrawl'
        with patch.object(plugin, 'extract_firecrawl_document', AsyncMock(return_value=self.doc)) as fire, \
                patch.object(plugin, 'extract_jina_document', AsyncMock()) as jina:
            await self.p.handle_extract(self.doc.url, _host_context=host_context('a'))
            fire.assert_awaited_once()
            jina.assert_not_called()

    async def test_explicit_jina_extract_reuses_text_store(self):
        self.p.config.web.extract_provider = 'jina'
        with patch.object(plugin, 'extract_jina_document', AsyncMock(return_value=self.doc)) as jina, \
                patch.object(plugin, 'extract_firecrawl_document', AsyncMock()) as fire:
            result = await self.p.handle_extract(self.doc.url, _host_context=host_context('a'))
            self.assertTrue(result['success'])
            self.assertEqual(result['total_chars'], len(self.doc.text))
            follow = await self.p.handle_text(result['text_id'], keyword='目标', _host_context=host_context('a'))
            self.assertEqual(follow['match_offset'], 12000)
            fire.assert_not_called()
            jina.assert_awaited_once()

    async def test_jina_failure_not_success_or_fallback_or_cache(self):
        self.p.config.web.extract_provider = 'jina'
        for exc, code in [(plugin.ProviderError('HTTP 429'), 'quota_or_rate_limit'), (TimeoutError(), 'timeout')]:
            with patch.object(plugin, 'extract_jina_document', AsyncMock(side_effect=exc)), \
                    patch.object(plugin, 'extract_firecrawl_document', AsyncMock()) as fire:
                result = await self.p.handle_extract(self.doc.url, _host_context=host_context('a'))
                self.assertFalse(result['success'])
                self.assertEqual(result['status'], code)
                self.assertFalse(self.p._texts.entries)
                fire.assert_not_called()

    async def test_search_explicit_read_uses_jina_only(self):
        config = SimpleNamespace(exa_api_key='test', extract_provider='jina', jina_api_key='', timeout_seconds=5)
        def render(doc, limit):
            return self.p._document_result(doc, 'a', limit)['content']
        with patch('retrieval.search_exa_results', AsyncMock(return_value=[{'url': self.doc.url, 'highlights': ['evidence']}])), \
                patch('retrieval.extract_jina_document', AsyncMock(return_value=self.doc)) as jina, \
                patch('retrieval.extract_firecrawl', AsyncMock()) as fire:
            result = await retrieve(None, config, 'query', 3, {}, read_pages=1, render_document=render)
            self.assertIn('正文编号', result)
            self.assertIn('提取来源：jina', result)
            self.assertLessEqual(len(result), 16000)
            await retrieve(None, config, 'query', 3, {})
            jina.assert_awaited_once()
            fire.assert_not_called()

    async def test_search_failure_retains_search_evidence_no_fallback(self):
        config = SimpleNamespace(exa_api_key='test', extract_provider='jina', jina_api_key='', timeout_seconds=5)
        with patch('retrieval.search_exa_results', AsyncMock(return_value=[{'url': self.doc.url, 'highlights': ['evidence']}])), \
                patch('retrieval.extract_jina_document', AsyncMock(side_effect=ProviderError('HTTP 429'))), \
                patch('retrieval.extract_firecrawl', AsyncMock()) as fire:
            result = await retrieve(None, config, 'query', 3, {}, read_pages=1)
            self.assertIn('partial', result)
            self.assertIn('evidence', result)
            fire.assert_not_called()
import asyncio
