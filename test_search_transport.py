"""Real HTTPX pool/stream lifetimes and SDK diagnostics, with no provider traffic."""
import asyncio
import json
import logging
from unittest import IsolatedAsyncioTestCase
from unittest.mock import patch

import httpx

from test_support import TestPlugin, host_context, plugin


class SearchTransportTests(IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.p = TestPlugin()
        self.p.config.web.search_provider = 'exa'
        self.p.config.web.exa_api_key = 'KEY_PRIVATE'
        await self.p.on_load()
        self.p.test_ctx.logger = logging.getLogger('test.neko_web.search')

    async def asyncTearDown(self):
        await self.p.on_unload()

    async def test_five_exa_queries_reuse_two_http11_sockets_and_log_only_integer_durations(self):
        sockets, clients, bodies = [], [], []
        both_connected = asyncio.Event()

        async def serve(reader, writer):
            # HTTP/1.1 keep-alive fixture: socket identity, not just constructor call counting.
            sockets.append(writer)
            if len(sockets) == 2:
                both_connected.set()
            try:
                while await reader.readline():
                    headers = {}
                    while (line := await reader.readline()) not in (b'', b'\r\n'):
                        k, v = line.decode('ascii').split(':', 1)
                        headers[k.lower()] = v.strip()
                    body = json.loads(await reader.readexactly(int(headers['content-length'])))
                    bodies.append((headers['x-api-key'], body))
                    await asyncio.wait_for(both_connected.wait(), 3)
                    result = {'results': [{'url': 'https://secret-source.example/path', 'title': 'TITLE_PRIVATE',
                                           'highlights': ['BODY_PRIVATE']}], 'account': 'ACCOUNT_PRIVATE'}
                    queue = b''
                    if body['query'].endswith('0'):
                        result['searchTime'] = 124.6
                        queue = b'x-exa-queue-ms: 7.8\r\n'
                    if body['query'].endswith('1'):
                        result['searchTime'] = {'diagnostic': 'TIMING_PRIVATE'}
                        queue = b'x-exa-queue-ms: HEADER_PRIVATE\r\n'
                    data = json.dumps(result).encode()
                    writer.write(b'HTTP/1.1 200 OK\r\n' + queue + f'Content-Length: {len(data)}\r\n\r\n'.encode() + data)
                    await writer.drain()
            finally:
                writer.close()
                await writer.wait_closed()

        server = await asyncio.start_server(serve, '127.0.0.1', 0)
        port = server.sockets[0].getsockname()[1]

        async def loopback(request):
            self.assertEqual(str(request.url), 'https://api.exa.ai/search')
            request.url = request.url.copy_with(scheme='http', host='127.0.0.1', port=port)

        real_client = httpx.AsyncClient
        def make_client(**options):
            self.assertFalse(options['trust_env'])
            self.assertNotIn('proxy', options)
            client = real_client(event_hooks={'request': [loopback]}, **options)
            clients.append(client)
            return client

        queries = [f'QUERY_PRIVATE{i}' for i in range(5)]
        async with server:
            with patch.object(plugin.httpx, 'AsyncClient', side_effect=make_client), self.assertLogs(self.p.ctx.logger) as logs:
                result = await self.p.handle_search_tool(queries, max_results=10, _host_context=host_context('SCOPE_PRIVATE'))
        self.assertEqual(result['status'], 'ok')
        self.assertEqual(len(clients), 1)
        self.assertTrue(clients[0].is_closed)
        self.assertEqual(len(sockets), 2)
        self.assertCountEqual([b['query'] for _, b in bodies], queries)
        self.assertEqual({(key, b['numResults']) for key, b in bodies}, {('KEY_PRIVATE', 4)})

        messages = [r.getMessage() for r in logs.records]
        self.assertEqual(len(messages), 6)
        for message in messages[:-1]:
            self.assertRegex(message, r'^neko_web\.search slot_wait_ms=\d+ query_ms=\d+(?: exa_search_ms=124 exa_queue_ms=7)?$')
        self.assertEqual(sum('exa_' in m for m in messages), 1)
        self.assertRegex(messages[-1], r'^neko_web\.search batch_ms=\d+$')
        self.assertNotIn('slot_wait_ms', result['content'])

    async def test_jina_pool_and_snapshot_renew_each_call_with_only_the_selected_proxy(self):
        web = self.p.config.web
        web.manual_search_provider = 'jina_serp'
        web.proxy_mode, web.proxy_url = 'custom', 'http://other-proxy.example:8080'
        web.jina_proxy_url, web.jina_api_key = 'http://first.example:8080', 'key-one'
        pools, sent = [], []
        real_client = httpx.AsyncClient
        def make_client(**options):
            def reply(req):
                self.assertEqual(req.url.host, 'serp.jina.ai')
                q, engine = req.url.params['q'], req.url.params['engine']
                sent.append((q, engine, req.headers['authorization']))
                # Change the live config during the first wave. Queued requests still use the snapshot.
                web.jina_search_engine, web.jina_api_key = 'bing', 'key-two'
                web.jina_proxy_url = 'http://second.example:8080'
                return httpx.Response(200, json={'meta': {'query': q, 'engine': engine}, 'results': []})
            client = real_client(transport=httpx.MockTransport(reply), trust_env=False)
            pools.append((options, client))
            return client

        with patch.object(plugin.httpx, 'AsyncClient', side_effect=make_client), \
                patch.object(plugin, 'render_search_preview', wraps=plugin.render_search_preview) as render:
            for scope, questions in [('first', ['a', 'b', 'c']), ('second', ['d'])]:
                result = await self.p.handle_search_tool(questions, _host_context=host_context(scope))
                self.assertEqual(result['status'], 'no_results')
                self.assertEqual(render.call_args.kwargs['scope'], scope)
        self.assertEqual(render.call_count, 2)
        self.assertEqual([o['proxy'] for o, _ in pools], ['http://first.example:8080', 'http://second.example:8080'])
        self.assertTrue(all(not o['trust_env'] and c.is_closed for o, c in pools))
        self.assertCountEqual(sent, [(q, 'google', 'Bearer key-one') for q in 'abc'] + [('d', 'bing', 'Bearer key-two')])

    async def test_anysearch_batch_keeps_unstructured_text_and_uses_one_pool(self):
        self.p.config.web.search_provider = 'anysearch'
        raw = 'Exa 搜索状态：ok\n1. Unstructured text\nhttps://example.com/one\nExcerpt'
        calls = []
        def reply(request):
            self.assertEqual(str(request.url), plugin.ANYSEARCH_ENDPOINT)
            self.assertEqual(request.headers['X-Anysearch-Client'], 'neko-web/1.13.0')
            calls.append(json.loads(request.content)['params']['arguments'])
            return httpx.Response(200, json={'result': {'content': [{'type': 'text', 'text': raw}]}})
        client = httpx.AsyncClient(transport=httpx.MockTransport(reply))
        with patch.object(plugin.httpx, 'AsyncClient', return_value=client) as factory, \
                patch.object(plugin, 'render_search_preview') as render:
            result = await self.p.handle_search_tool(['one', 'two'], _host_context=host_context())
        self.assertEqual(result['content'].count(raw), 2)
        self.assertCountEqual(calls, [dict(query=q, max_results=5) for q in ['one', 'two']])
        factory.assert_called_once()
        self.assertTrue(client.is_closed)
        render.assert_not_called()

    async def test_structured_batch_preserves_success_across_invalid_response_and_http_error(self):
        calls = []
        def reply(request):
            q = json.loads(request.content)['query']
            calls.append(q)
            if q == 'bad':
                return httpx.Response(200, json={'results': [{'url': 'http://127.0.0.1/'}]})
            if q == 'rate':
                return httpx.Response(429, text='never expose this error')
            return httpx.Response(200, json={'results': [{'url': 'https://example.com/kept', 'highlights': ['evidence']}]})
        with patch.object(self.p, '_build_client_options', return_value={'transport': httpx.MockTransport(reply)}):
            result = await self.p.handle_search_tool(['good', 'bad', 'rate'], _host_context=host_context())
        self.assertCountEqual(calls, ['good', 'bad', 'rate'])
        self.assertEqual(result['status'], 'partial')
        for item in ('evidence', 'invalid_response', 'quota_or_rate_limit'):
            self.assertIn(item, result['content'])
        self.assertNotIn('never expose', result['content'])

    async def test_config_reload_drains_inflight_batch_before_closing_pool(self):
        entered = asyncio.Event()
        active = 0
        cleanup = []
        async def reply(req):
            nonlocal active
            active += 1
            if active == 2:
                entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                cleanup.append(client.is_closed)

        client = httpx.AsyncClient(transport=httpx.MockTransport(reply), trust_env=False)
        with patch.object(plugin.httpx, 'AsyncClient', return_value=client) as factory, \
                patch.object(plugin, 'render_search_preview') as render, self.assertLogs(self.p.ctx.logger) as logs:
            work = asyncio.create_task(self.p.handle_search_tool(['a', 'b', 'c'], _host_context=host_context()))
            await asyncio.wait_for(entered.wait(), 3)
            await self.p.on_config_update(plugin.CONFIG_RELOAD_SCOPE_SELF, {}, '')
        self.assertTrue(work.cancelled())
        self.assertEqual(cleanup, [False, False])
        self.assertTrue(client.is_closed)
        factory.assert_called_once()
        render.assert_not_called()
        # The third query was still queued: its wait was measured, its RTT was not.
        self.assertEqual(sum('slot_wait_ms' in r.getMessage() for r in logs.records), 3)
        self.assertEqual(sum('query_ms' in r.getMessage() for r in logs.records), 2)
