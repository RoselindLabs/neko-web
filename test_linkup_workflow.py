"""Exercise the public SDK search tool; never contact a real provider."""
import json
import re
from unittest import IsolatedAsyncioTestCase
from unittest.mock import patch

import httpx

from test_support import TestPlugin, host_context


class LinkupWorkflowTests(IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.p = TestPlugin()
        await self.p.on_load()
        self.addAsyncCleanup(self.p.on_unload)
        self.web = self.p.config.web
        self.web.search_provider = 'exa'
        self.web.linkup_api_key = 'LINKUP_FIXTURE_ONLY'
        self.web.manual_search_provider = 'linkup_fast'
        self.ctx = {'_host_context': host_context('owner')}

    async def test_fast_answer_and_sources_stay_with_their_question(self):
        calls = []
        def server(req):
            self.assertEqual(str(req.url), 'https://api.linkup.so/v1/search')
            body = json.loads(req.content)
            q = body['q']
            self.assertEqual(req.headers['authorization'], 'Bearer LINKUP_FIXTURE_ONLY')
            self.assertEqual((body['depth'], body['outputType'], body['maxResults']), ('fast', 'sourcedAnswer', 4))
            calls.append(q)
            if q == 'rate':
                return httpx.Response(429, text='private error')
            return httpx.Response(200, json={'answer': 'The synthetic release date is November 3.', 'sources': [
                {'name': 'Release notes', 'url': f'https://s{i}.example/r', 'snippet': f'November 3. Source detail {i}.'}
                for i in range(4)]})
        with patch.object(self.p, '_build_client_options', return_value={'transport':httpx.MockTransport(server)}):
            r = await self.p.handle_search_tool(['release','rate','release'], max_results=4, **self.ctx)
            self.assertEqual(r['status'], 'partial')
            t = r['content']
            self.assertIn('Linkup', t)
            self.assertEqual(t.count('The synthetic release date'), 1)
            self.assertIn('https://s0.example/r', t)
            self.assertIn('quota_or_rate_limit', t)
            self.assertNotIn('private error', t)
            token = re.search(r'text_id="([\w-]+)"', t)[1]
            extra = await self.p.handle_text(token, keyword='Source detail 3', **self.ctx)
            self.assertIn('Source detail 3', extra['content'])
            self.assertFalse((await self.p.handle_text(token, _host_context=host_context('other')))['success'])
        self.assertCountEqual(calls, ['release','rate'])

    async def test_no_key_and_read_pages_do_not_make_a_request_or_fall_back(self):
        self.web.linkup_api_key = ''
        with patch.object(self.p, '_build_client_options', return_value={'transport':httpx.MockTransport(
                lambda req: self.fail('must not make a network request'))}):
            no_key = await self.p.handle_search_tool(['simple'], **self.ctx)
            self.assertFalse(no_key['success'])
            self.assertEqual(no_key['status'], 'config_error')
            self.web.linkup_api_key = 'LINKUP_FIXTURE_ONLY'
            self.assertEqual((await self.p.handle_search_tool(['simple'], read_pages=1, **self.ctx))['status'], 'unsupported')

    async def test_entering_key_does_not_select_linkup(self):
        self.web.manual_search_provider = 'configured'
        self.web.exa_api_key = 'EXA_FIXTURE_ONLY'
        def exa(req):
            self.assertEqual(req.url.host, 'api.exa.ai')
            self.assertNotIn('authorization', req.headers)
            return httpx.Response(200, json={'results':[]})
        with patch.object(self.p, '_build_client_options', return_value={'transport':httpx.MockTransport(exa)}):
            self.assertEqual((await self.p.handle_search_tool(['one'], **self.ctx))['status'], 'no_results')
