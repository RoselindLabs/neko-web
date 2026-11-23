import json
from unittest import IsolatedAsyncioTestCase

import httpx

from providers import ProviderError, search_linkup_results
from retrieval import organize_results, search_filters


KEY = 'mock-linkup-token'
QUERY = '原样查询 2024?'
SOURCE = {'name': 'Quarterly report', 'url': 'https://news.example.org/report', 'snippet': 'Revenue increased.'}


class LinkupProviderTests(IsolatedAsyncioTestCase):
    async def search(self, response, *, count=3, filters=None):
        def handler(request):
            return httpx.Response(200, json=response)

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await search_linkup_results(client, KEY, QUERY, count, filters)

    async def test_exact_request_and_organizable_sources(self):
        def handler(request):
            self.assertEqual((request.method, str(request.url)), ('POST', 'https://api.linkup.so/v1/search'))
            self.assertEqual(request.headers['Authorization'], 'Bearer ' + KEY)
            self.assertEqual(json.loads(request.content), {
                'q': QUERY, 'depth': 'fast', 'outputType': 'sourcedAnswer', 'maxResults': 2,
                'includeInlineCitations': False, 'includeDomains': ['example.org'],
                'fromDate': '2024-02-28', 'toDate': '2024-03-01'})
            return httpx.Response(200, json={'answer': 'Summary.', 'sources': [
                dict(SOURCE, publishedAt='2024-02-29'), dict(SOURCE, url='https://example.org/second')]})

        filters = search_filters(['Example.org.'], '2024-02-28', '2024-03-01')
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            raw, answer = await search_linkup_results(client, KEY, QUERY, 2, filters)
        self.assertEqual(answer, 'Summary.')
        sources, stats = organize_results(raw, 2)
        self.assertEqual(stats['invalid'], 0)
        self.assertEqual(sources[0], {'url': SOURCE['url'], 'title': SOURCE['name'], 'published': '2024-02-29',
                                     'source': 'news.example.org', 'snippet': SOURCE['snippet'], 'provider_rank': 1})
        self.assertEqual(sources[1]['published'], '')

    async def test_one_sided_dates_do_not_add_or_move_boundaries(self):
        for filters, expected in [(search_filters(published_after='1970-01-02'), {'fromDate': '1970-01-02'}),
                                  (search_filters(published_before='2026-11-23'), {'toDate': '2026-11-23'})]:
            def handler(request):
                dates = {k: v for k, v in json.loads(request.content).items() if k.endswith('Date')}
                self.assertEqual(dates, expected)
                return httpx.Response(200, json={'answer': '', 'sources': []})

            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                self.assertEqual(await search_linkup_results(client, KEY, QUERY, 1, filters), ([], ''))

    async def test_unsupported_boundaries_and_invalid_config_never_send(self):
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: self.fail('unexpected request'))) as client:
            for filters in [search_filters([], '2024-01-01', '2024-01-01'), search_filters([], '1970-01-01'),
                            search_filters([], '', '1969-12-31'), {'startPublishedDate': '2024-01-01T12:00:00Z'},
                            {'excludeDomains': ['example.org']}]:
                with self.assertRaises(ProviderError) as error:
                    await search_linkup_results(client, KEY, QUERY, 1, filters)
                self.assertEqual(error.exception.code, 'unsupported')
            for key, count, code in [('', 1, 'config_error'), (KEY + '\nX', 1, 'config_error'),
                                     (KEY, True, 'invalid_request'), (KEY, 0, 'invalid_request')]:
                with self.assertRaises(ProviderError) as error:
                    await search_linkup_results(client, key, QUERY, count, {})
                self.assertEqual(error.exception.code, code)
                self.assertNotIn(KEY, str(error.exception))

    async def test_rejected_source_invalidates_synthesis_but_preserves_valid_evidence(self):
        filters = search_filters(['example.org'], '2024-01-01', '2024-12-31')
        for item in [None, dict(SOURCE, url='https://notexample.org/'),
                     dict(SOURCE, url='https://example.org.other.net/'),
                     dict(SOURCE, url='https://other.net\\news.example.org/'),
                     dict(SOURCE, url='https://example.org@other.net/'), dict(SOURCE, url='http://127.0.0.1/'),
                     dict(SOURCE, publishedAt='2025-01-01'), dict(SOURCE, publishedAt='2023-12-31')]:
            with self.subTest(item=item):
                raw, answer = await self.search({'answer': 'Contains rejected material', 'sources': [item, SOURCE]},
                                                filters=filters)
                self.assertEqual(answer, '')
                self.assertIsNone(raw[0])
                sources, stats = organize_results(raw, 3)
                self.assertEqual([s['url'] for s in sources], [SOURCE['url']])
                self.assertEqual(stats['invalid'], 1)

    async def test_empty_sources_distinct_from_wrong_shape_or_all_bad_rows(self):
        self.assertEqual(await self.search({'answer': 'Unsupported claim', 'sources': []}), ([], ''))
        for data in [[], {}, {'answer': None, 'sources': []}, {'answer': '', 'sources': {}},
                     {'error': 'private diagnostic', 'answer': 'private diagnostic', 'sources': [SOURCE]},
                     {'answer': 'private diagnostic', 'sources': [SOURCE] * 4}]:
            with self.subTest(data=data), self.assertRaises(ProviderError) as error:
                await self.search(data)
            self.assertEqual(error.exception.code, 'invalid_response')
            self.assertNotIn('private diagnostic', str(error.exception))
        for item in [None, {}, dict(SOURCE, snippet='  '), dict(SOURCE, url=123), dict(SOURCE, name=[]),
                     dict(SOURCE, publishedAt=None), dict(SOURCE, publishedAt='2025-02-29'),
                     dict(SOURCE, publishedAt='2024-02-29T12:00:00Z')]:
            with self.subTest(item=item), self.assertRaises(ProviderError) as error:
                await self.search({'answer': 'private diagnostic', 'sources': [item]})
            self.assertEqual(error.exception.code, 'invalid_response')
            self.assertNotIn('private diagnostic', str(error.exception))

    async def test_success_payload_cannot_echo_bearer_key(self):
        raw, answer = await self.search({'answer': 'Bearer ' + KEY, 'sources': [SOURCE]})
        self.assertEqual((len(raw), answer), (1, ''))
        for field, value in [('name', KEY), ('snippet', KEY), ('url', SOURCE['url'] + '?token=' + KEY)]:
            raw, answer = await self.search({'answer': 'Summary.', 'sources': [dict(SOURCE, **{field: value}), SOURCE]})
            self.assertNotIn(KEY, repr((raw, answer)))
            self.assertEqual((raw[0], answer), (None, ''))

    async def test_safe_bounded_errors_without_retry_or_redirect(self):
        diagnostic = f'server diagnostic: Bearer {KEY}'
        for status, code in [(400, 'invalid_request'), (401, 'auth_error'), (402, 'quota_or_rate_limit'),
                             (429, 'quota_or_rate_limit'), (307, 'provider_error'), (503, 'provider_error'),
                             ('timeout', 'timeout'), ('bad_json', 'invalid_response'), ('large', 'response_too_large')]:
            calls = []

            def handler(request):
                calls.append(str(request.url))
                if status == 'timeout':
                    raise httpx.ReadTimeout(diagnostic)
                if status == 'large':
                    return httpx.Response(200, content=b'x' * (2 * 1024 * 1024 + 1))
                return httpx.Response(200 if status == 'bad_json' else status, text=diagnostic,
                                      headers={'Location': 'https://other.example/'})

            async with httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=True) as client:
                with self.subTest(status=status), self.assertRaises(ProviderError) as error:
                    await search_linkup_results(client, KEY, QUERY, 1, {})
                self.assertEqual(error.exception.code, code)
                self.assertNotIn(KEY, str(error.exception))
                self.assertNotIn('server diagnostic', str(error.exception))
                self.assertEqual(calls, ['https://api.linkup.so/v1/search'])
