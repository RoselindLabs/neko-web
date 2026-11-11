from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock
from test_support import TestPlugin, host_context


class RoutingTests(IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.p = TestPlugin()
        self.p.config.web.search_provider = 'exa'
        await self.p.on_load()
        self.p._provider_search = AsyncMock(return_value='results')
        self.p._call_api = AsyncMock(return_value='vertical')

    async def asyncTearDown(self):
        await self.p.on_unload()

    async def test_filters_and_read_pages_forwarded(self):
        result = await self.p.handle_search('query', sites=['example.com'], published_after='2026-01-01', read_pages=2, _host_context=host_context())
        self.assertEqual(result['content'], 'results')
        self.assertEqual(self.p._provider_search.call_args.args[2]['includeDomains'], ['example.com'])
        self.assertEqual(self.p._provider_search.call_args.args[3], 2)
        self.assertGreater(self.p._provider_search.call_args.kwargs['deadline'].remaining(), 0)

    async def test_invalid_and_vertical_options_fail_before_network(self):
        for kwargs in [{'read_pages': True}, {'read_pages': 3}, {'sites': ['https://example.com']},
                       {'domain': 'finance', 'sub_domain': 'stock', 'read_pages': 1}]:
            text = await self.p.handle_search('query', _host_context=host_context(), **kwargs)
            self.assertTrue(text['status'] in {'invalid_request', 'unsupported'})
        self.p._provider_search.assert_not_called()
        self.p._call_api.assert_not_called()

    async def test_vertical_keeps_anysearch(self):
        self.assertEqual((await self.p.handle_search('query', domain='finance', sub_domain='stock', _host_context=host_context()))['content'], 'vertical')
        self.p._provider_search.assert_not_called()

    async def test_duplicate_queries_only_search_once(self):
        text = await self.p.handle_batch_search(['one', ' one ', 'two'], _host_context=host_context())
        self.assertEqual(self.p._provider_search.await_count, 2)
        self.assertIn('查询：two', text['content'])

    async def test_anysearch_does_not_silently_ignore_batch_filters(self):
        self.p.config.web.search_provider = 'anysearch'
        text = await self.p.handle_batch_search(['one'], sites=['example.com'], _host_context=host_context())
        self.assertEqual(text['status'], 'unsupported')
        self.p._provider_search.assert_not_called()
