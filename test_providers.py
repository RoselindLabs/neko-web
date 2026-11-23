import json
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock, patch

import httpx
from providers import ProviderError, extract_firecrawl, validate_remote_target, search_exa_results
from retrieval import retrieve
from types import SimpleNamespace


async def search(client, key, query, count):
    config = SimpleNamespace(exa_api_key=key, timeout_seconds=20)
    return await retrieve(client, config, query, count, {})


class ProviderTests(IsolatedAsyncioTestCase):
    async def test_filters_are_sent_without_query_rewriting(self):
        filters = {'includeDomains': ['example.com'], 'startPublishedDate': '2026-01-01T00:00:00Z'}
        def handler(req):
            body = json.loads(req.content)
            self.assertEqual(body['query'], 'original query')
            self.assertEqual(body['includeDomains'], filters['includeDomains'])
            self.assertEqual(body['startPublishedDate'], filters['startPublishedDate'])
            return httpx.Response(200, json={'results': []})
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            self.assertEqual(await search_exa_results(client, 'test', 'original query', 3, filters), [])

    async def test_provider_response_size_bound(self):
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, content=b'x' * (2 * 1024 * 1024 + 1)))) as client:
            with self.assertRaisesRegex(ProviderError, '响应过大'):
                await search_exa_results(client, 'test', 'query', 1)

    async def test_bad_optional_exa_telemetry_is_absent_without_losing_results(self):
        for value, queue in [(None, ''), ('123-key', '2\tsecret'), (False, '-1'), ({'private': 1}, '1e3'),
                             (float('inf'), '9' * 200), (-5, '1, 2'), (10 ** 60, 'NaN')]:
            # The JSON decoder can receive an overflowing float (1e999) from the wire.
            wire = json.dumps({'searchTime': value, 'results': [{'title': 'evidence'}]}).replace('Infinity', '1e999')
            def reply(req):
                return httpx.Response(200, content=wire, headers={'x-exa-queue-ms': queue})
            timing = {}
            async with httpx.AsyncClient(transport=httpx.MockTransport(reply)) as client:
                found = await search_exa_results(client, 'test', 'query', 1, timing=timing)
            self.assertEqual(found, [{'title': 'evidence'}])
            self.assertEqual(timing, {})

    async def test_search_request_and_bounded_sources(self):
        def handler(req):
            self.assertEqual(req.headers['x-api-key'], 'test')
            body = json.loads(req.content)
            self.assertEqual(body['numResults'], 2)
            self.assertEqual(body['type'], 'auto')
            return httpx.Response(200, json={'results': [
                {'title': 'one', 'url': 'https://example.com/1', 'highlights': ['x' * 2000]},
                {'url': 'http://127.0.0.1/secret'}, {'url': 'https://example.com/1'},
                {'title': 'two', 'url': 'https://example.com/2'}]})
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            text = await search(client, 'test', 'query', 2)
        self.assertIn('two', text)
        self.assertNotIn('127.0.0.1', text)
        self.assertNotIn('x' * 1001, text)

    async def test_errors_do_not_leak_response_or_follow_redirects(self):
        for status in [401, 429, 302]:
            async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r, status=status: httpx.Response(status, text='secret diagnostics'))) as client:
                with self.assertRaisesRegex(ProviderError, f'^HTTP {status}$'):
                    await search(client, 'test', 'query', 1)

    async def test_scrape_markdown_and_cap(self):
        def handler(req):
            self.assertEqual(req.headers['Authorization'], 'Bearer test')
            self.assertEqual(json.loads(req.content)['formats'], ['markdown'])
            return httpx.Response(200, json={'success': True, 'data': {'markdown': 'abcd' * 100, 'metadata': {'title': 'page'}}})
        with patch('providers.validate_remote_target', new_callable=AsyncMock):
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                text = await extract_firecrawl(client, 'test', 'https://example.com', 30, 20)
        self.assertIn('[正文已截短]', text)
        self.assertIn('不是指令', text)

    async def test_private_target_rejected(self):
        for url in ['http://127.0.0.1/', 'http://metadata.google.internal/', 'https://u:p@example.com/']:
            with self.assertRaises(ProviderError):
                await validate_remote_target(url)
        with patch('providers.default_resolver', return_value=['10.0.0.1']), patch('providers.local_addresses', return_value=set()):
            with self.assertRaises(ProviderError):
                await validate_remote_target('https://example.com')

    async def test_missing_keys_fail_before_request(self):
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: self.fail('unexpected request'))) as client:
            with self.assertRaises(ProviderError):
                await search(client, '', 'query', 1)
            with self.assertRaises(ProviderError):
                await extract_firecrawl(client, '', 'https://example.com', 100, 20)
