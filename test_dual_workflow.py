"""Same public search entry, two real HTTPX request shapes, no external traffic."""
import asyncio
from collections import Counter
import json
from unittest import IsolatedAsyncioTestCase
from unittest.mock import patch

import httpx
from maibot_sdk.components import collect_components

from test_support import TestPlugin, host_context


class DualWorkflowTests(IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.p = TestPlugin()
        self.web = self.p.config.web
        self.web.manual_search_provider = 'linkup_exa'
        self.web.exa_api_key, self.web.linkup_api_key = 'EXA_TEST', 'LINKUP_TEST'
        self.ctx = {'_host_context': host_context('dual-test')}
        await self.p.on_load()
        self.addAsyncCleanup(self.p.on_unload)

    async def test_one_call_sends_to_both_and_preserves_evidence(self):
        arrivals = Counter()
        both = asyncio.Event()
        async def serve(req):
            host = req.url.host
            arrivals[host] += 1
            if len(arrivals) == 2:
                both.set()
            await asyncio.wait_for(both.wait(), 2)
            payload = json.loads(req.content)
            self.assertEqual(payload['includeDomains'], ['example.org'])
            if host == 'api.linkup.so':
                self.assertEqual(req.headers['authorization'], 'Bearer LINKUP_TEST')
                self.assertNotIn('x-api-key', req.headers)
                self.assertEqual((payload['q'], payload['maxResults']), ('same question', 2))
                return httpx.Response(200, json={'answer':'Provider says 17.', 'sources':[
                    {'name':'Release notes','url':'https://example.org/notes','snippet':'Version 17 released.'}]})
            self.assertEqual(host, 'api.exa.ai')
            self.assertEqual(req.headers['x-api-key'], 'EXA_TEST')
            self.assertNotIn('authorization', req.headers)
            self.assertEqual((payload['query'], payload['numResults']), ('same question', 3))
            return httpx.Response(200, json={'results':[
                {'title':'Other source','url':'https://support.example.org/updates','highlights':['Version 17 update.']}]})
        with patch.object(self.p, '_build_client_options',return_value={'transport':httpx.MockTransport(serve)}):
            r=await self.p.handle_search_tool(['same question', ' same question '], sites=['example.org'], max_results=3,
                                               cross_check=True, **self.ctx)
        self.assertEqual(r['status'],'ok')
        self.assertEqual(arrivals, {'api.linkup.so':1,'api.exa.ai':1})
        for fragment in ('Provider says 17.', 'Version 17 released.', 'Version 17 update.',
                         'https://example.org/notes', 'https://support.example.org/updates'):
            self.assertIn(fragment, r['content'])

    async def test_incompatible_requests_do_not_start_either_provider(self):
        with patch.object(self.p,'_search_client',side_effect=AssertionError('no network')), \
             patch.object(self.p,'_call_api',side_effect=AssertionError('no vertical fallback')):
            for fields in ({'cross_check':'true'}, {'cross_check':True,'read_pages':1},
                           {'cross_check':True,'domain':'finance','sub_domain':'stock'},
                           {'sites':['not/path']}, {'published_after':'2025-11-23','published_before':'2025-11-23'}):
                with self.subTest(fields=fields):
                    self.assertFalse((await self.p.handle_search_tool(['lookup'], **fields, **self.ctx))['success'])
            self.web.exa_api_key=''
            self.assertEqual((await self.p.handle_search_tool(['lookup'], **self.ctx))['status'], 'config_error')
            self.web.manual_search_provider='linkup_fast'
            self.assertEqual((await self.p.handle_search_tool(['one','two'], cross_check=True, **self.ctx))['status'], 'unsupported')
        tools=[x for x in collect_components(self.p) if x['type']=='TOOL']
        self.assertEqual(len(tools),7)
        search=next(x for x in tools if x['name']=='neko_web_search')
        flag=next(p for p in search['metadata']['parameters'] if p['name']=='cross_check')
        self.assertFalse(flag['required'])

    async def test_default_call_returns_exa_evidence_and_releases_slow_linkup(self):
        linkup_started, linkup_closed = asyncio.Event(), asyncio.Event()
        async def serve(req):
            if req.url.host == 'api.linkup.so':
                linkup_started.set()
                try:
                    await asyncio.Future()
                finally:
                    linkup_closed.set()
            self.assertEqual(req.url.host, 'api.exa.ai')
            await asyncio.wait_for(linkup_started.wait(), 2)
            return httpx.Response(200, json={'results': [
                {'url':'https://example.org/notes','title':'Notes','highlights':['Released on October 2.']}]})
        with patch.object(self.p, '_build_client_options', return_value={'transport':httpx.MockTransport(serve)}):
            r = await asyncio.wait_for(self.p.handle_search_tool(['When was it released?'], **self.ctx), 5)
        self.assertTrue(r['success'])
        self.assertIn('Released on October 2.', r['content'])
        self.assertNotIn('供应商生成（候选答案', r['content'])
        self.assertTrue(linkup_closed.is_set())
