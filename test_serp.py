from unittest import IsolatedAsyncioTestCase, TestCase
from unittest.mock import AsyncMock, patch

import httpx

from providers import ProviderError, search_jina_results
from retrieval import organize_results, retrieve
from runtime import response_status
from test_support import TestPlugin, plugin, host_context
from neko_web_test import automation_retrieval


def payload(engine='google', query='query', results=None):
    return {'results': results if results is not None else [
        {'title': 'page', 'url': 'https://example.com', 'snippet': 'evidence', 'engine': engine}],
        'meta': {'engine': engine, 'query': query, 'credits': 1}}


class WireTests(IsolatedAsyncioTestCase):
    async def test_observed_contract_uses_engine_and_normalizes_snippet(self):
        for engine in ['google', 'bing']:
            def handler(req):
                self.assertEqual(str(req.url.copy_with(query=None)), 'https://serp.jina.ai/')
                self.assertEqual(req.method, 'GET')
                self.assertEqual(dict(req.url.params), {'q': 'query', 'engine': engine, 'num': '3'})
                self.assertEqual(req.headers['authorization'], 'Bearer test')
                return httpx.Response(200, json=payload(engine))
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                results = await search_jina_results(client, 'test', 'query', 3, engine)
            sources, stats = organize_results(results, 3)
            self.assertEqual(sources[0]['snippet'], 'evidence')
            self.assertEqual(sources[0]['published'], '')

    async def test_missing_key_and_invalid_engine_fail_before_network(self):
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: self.fail('no request'))) as client:
            for key, engine, count in [('', 'google', 3), ('test', 'unknown', 3), ('test', 'bing', True), ('test', 'bing', 11)]:
                with self.assertRaises(ProviderError):
                    await search_jina_results(client, key, 'query', count, engine)

    async def test_engine_query_and_item_engine_mismatch_rejected(self):
        for data in [payload('bing'), payload(query='rewritten'), payload(results=[
                {'url': 'https://example.com', 'engine': 'bing'}])]:
            async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, json=data))) as client:
                with self.assertRaises(ProviderError) as ctx:
                    await search_jina_results(client, 'test', 'query', 3, 'google')
            self.assertEqual(ctx.exception.code, 'provider_contract_mismatch')

    async def test_old_or_broken_shape_rejected_not_empty_success(self):
        for data in [[], {'data': []}, {'results': [], 'meta': []}, {'results': {}, 'meta': {}}]:
            async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, json=data))) as client:
                with self.assertRaises(ProviderError):
                    await search_jina_results(client, 'test', 'query', 3, 'google')

    async def test_error_size_transport_and_redirect_no_retry(self):
        for status, content, expected in [(401, b'SECRET', 'auth_error'), (429, b'SECRET', 'quota_or_rate_limit'),
                (302, b'SECRET', 'provider_error'), (200, b'not json', 'invalid_response'),
                (200, b'x' * (2 * 1024 * 1024 + 1), 'response_too_large')]:
            calls = []
            def handler(req):
                calls.append(req)
                return httpx.Response(status, content=content, headers={'Location': 'https://other.example'})
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                with self.assertRaises(ProviderError) as ctx:
                    await search_jina_results(client, 'test', 'query', 3, 'google')
            self.assertEqual(ctx.exception.code, expected)
            self.assertNotIn('SECRET', str(ctx.exception))
            self.assertEqual(len(calls), 1)

    async def test_invalid_sources_and_duplicate_accounting(self):
        data = payload(results=[None, {'url': 'http://127.0.0.1'},
                                {'url': 'https://example.com', 'snippet': 'a'}, {'url': 'https://example.com'}])
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, json=data))) as client:
            raw = await search_jina_results(client, 'test', 'query', 3, 'google')
        sources, stats = organize_results(raw, 3)
        self.assertEqual(len(sources), 1)
        self.assertEqual(stats['invalid'], 2)
        self.assertEqual(stats['duplicates'], 1)


class ToolTests(IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.p = TestPlugin()
        await self.p.on_load()
        self.p.config.web.search_provider = 'exa'

    async def asyncTearDown(self):
        await self.p.on_unload()

    async def test_default_manual_inherits_existing_provider(self):
        self.assertEqual(self.p.config.web.manual_search_provider, 'configured')
        with patch('neko_web_test.retrieval.search_exa_results', AsyncMock(return_value=[])) as exa, \
                patch('neko_web_test.retrieval.search_jina_results', AsyncMock()) as jina:
            result = await self.p.handle_search('query', _host_context=host_context())
            self.assertEqual(result['status'], 'no_results')
            exa.assert_awaited_once()
            jina.assert_not_called()

    async def test_manual_google_and_bing_route_and_metadata(self):
        self.p.config.web.manual_search_provider = 'jina_serp'
        for engine in ['google', 'bing']:
            self.p.config.web.jina_search_engine = engine
            with patch('neko_web_test.retrieval.search_jina_results', AsyncMock(return_value=[{'url': 'https://example.com'}])) as jina, \
                    patch('neko_web_test.retrieval.search_exa_results', AsyncMock()) as exa:
                result = await self.p.handle_search('query', _host_context=host_context())
                self.assertTrue(result['success'])
                self.assertIn(f'Jina SERP ({engine})', result['content'])
                self.assertEqual(jina.call_args.args[-1], engine)
                exa.assert_not_called()

    async def test_filters_fail_before_search_without_silently_ignoring(self):
        self.p.config.web.manual_search_provider = 'jina_serp'
        with patch('neko_web_test.retrieval.search_jina_results', AsyncMock()) as jina:
            for call in [self.p.handle_search('query', sites=['example.com'], _host_context=host_context()),
                         self.p.handle_batch_search(['query'], published_after='2026-01-01', _host_context=host_context())]:
                result = await call
                self.assertEqual(result['status'], 'unsupported')
            jina.assert_not_called()

    async def test_batch_preserves_queries_and_no_reading(self):
        self.p.config.web.manual_search_provider = 'jina_serp'
        with patch('neko_web_test.retrieval.search_jina_results', AsyncMock(return_value=[])) as jina, \
                patch('neko_web_test.retrieval.extract_jina_document', AsyncMock()) as extract:
            result = await self.p.handle_batch_search(['a', 'b'], _host_context=host_context())
            self.assertTrue(result['success'])
            self.assertEqual(jina.await_count, 2)
            extract.assert_not_called()

    async def test_provider_failure_no_exa_fallback(self):
        self.p.config.web.manual_search_provider = 'jina_serp'
        with patch('neko_web_test.retrieval.search_jina_results', AsyncMock(side_effect=plugin.ProviderError('mismatch', 'provider_contract_mismatch'))), \
                patch('neko_web_test.retrieval.search_exa_results', AsyncMock()) as exa:
            result = await self.p.handle_search('query', _host_context=host_context())
            self.assertFalse(result['success'])
            self.assertEqual(result['status'], 'provider_contract_mismatch')
            exa.assert_not_called()

    async def test_life_background_still_exa_even_manual_override_selected(self):
        self.p.config.web.manual_search_provider = 'jina_serp'
        with patch.object(automation_retrieval, 'search_sources', AsyncMock(return_value=([], {'invalid': 0, 'omitted': 0}))) as search:
            result = await automation_retrieval.search_data(self.p.config.web, {'trust_env': False}, 'query')
        self.assertTrue(result['success'])
        self.assertNotIn('backend', search.call_args.kwargs)
        self.assertEqual(self.p.config.web.search_provider, 'exa')

    async def test_vertical_still_anysearch(self):
        self.p.config.web.manual_search_provider = 'jina_serp'
        self.p._call_api = AsyncMock(return_value='vertical')
        with patch('neko_web_test.retrieval.search_jina_results', AsyncMock()) as jina:
            await self.p.handle_search('query', domain='finance', sub_domain='stock', _host_context=host_context())
            self.p._call_api.assert_awaited_once()
            jina.assert_not_called()

    async def test_jina_dedicated_proxy_leaves_other_options_unchanged(self):
        self.p.config.web.jina_proxy_url = 'http://127.0.0.1:7890'
        self.assertEqual(self.p._build_client_options(), {'trust_env': False})
        self.assertEqual(self.p._build_client_options(jina=True), {'proxy': 'http://127.0.0.1:7890', 'trust_env': False})
        self.assertEqual(self.p.config.web.proxy_mode, 'none')
        self.assertEqual(self.p.config.web.proxy_url, '')
        self.p.config.web.jina_proxy_url = 'file:///tmp/proxy'
        with self.assertRaises(plugin.AnySearchConfigError):
            self.p._build_client_options(jina=True)

    async def test_search_and_extraction_use_separate_clients(self):
        self.p.config.web.manual_search_provider = 'jina_serp'
        self.p.config.web.extract_provider = 'firecrawl'
        with patch.object(plugin, 'retrieve', AsyncMock(return_value='搜索状态：ok')) as retrieve_mock:
            await self.p.handle_search('query', read_pages=1, _host_context=host_context('a'))
            args = retrieve_mock.call_args
            self.assertIsNot(args.args[0], args.kwargs['body_client'])
            self.assertEqual(args.kwargs['backend'], 'jina_serp')


class StatusTests(TestCase):
    def test_safe_statuses_with_engine_label(self):
        for engine in ['google', 'bing']:
            for status in ['ok', 'no_results', 'partial', 'provider_contract_mismatch', 'auth_error']:
                self.assertEqual(response_status(f'Jina SERP ({engine}) 搜索状态：{status}；test'), status)
