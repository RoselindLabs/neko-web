"""Host identity isolation and real SDK send normalization; all IO is synthetic."""
import base64
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock, patch

from maibot_sdk.components import collect_components
from maibot_sdk.context import PluginContext

from test_support import TestPlugin, host_context, image_bytes, plugin
from neko_web_test.images import prepare_image
from neko_web_test.page import PageRead


class TrustedContextTests(IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.p = TestPlugin()
        await self.p.on_load()
        self.raw = image_bytes()
        self.image = prepare_image(self.raw, 'https://example.com/photo.png', preview=False)
        self.forged = dict(stream_id='foreign', session_id='foreign', chat_id='foreign',
                           platform='forged', account_id='forged')

    async def asyncTearDown(self):
        await self.p.on_unload()

    def test_exactly_five_tools_opt_in_without_exposing_identity_parameters(self):
        tools = {c['name']: c['metadata'] for c in collect_components(self.p) if c['type'] == 'TOOL'}
        scoped = {'neko_web_' + name for name in ('search', 'read', 'text', 'images', 'send_image')}
        self.assertEqual({name for name, meta in tools.items()
                          if meta.get('trusted_context') == 'neko.tools.context.v1'}, scoped)
        for meta in tools.values():
            self.assertFalse({'_host_context', 'stream_id', 'account_id', 'platform', 'session_id'} &
                             {p['name'] for p in meta['parameters']})

    async def test_missing_malformed_or_conflicting_context_rejected_before_work(self):
        contexts = [None, {}, host_context(contract='other')]
        for key in ('session_id', 'stream_id'):
            for value in ('', None, [], ' bad ', 'bad\n', 'x' * 513):
                contexts.append(host_context(**{key: value}))
            missing = host_context()
            del missing[key]
            contexts.append(missing)
        contexts.append(host_context(stream_id='different'))
        calls = [(self.p.handle_search_tool, {'queries': ['one']}),
                 (self.p.handle_search_tool, {'queries': ['one', 'two']}),
                 (self.p.handle_read_tool, {'url': self.image.source}),
                 (self.p.handle_read_tool, {'url': self.image.source, 'mode': 'article'}),
                 (self.p.handle_text, {'text_id': 'known'}),
                 (self.p.handle_images_tool, {'urls': [self.image.source]}),
                 (self.p.handle_images_tool, {'cursor': 'known'}),
                 (self.p.handle_send_image, {'image_id': 'known'})]
        with patch.object(self.p, '_provider_search', AsyncMock()) as search, \
             patch.object(self.p, '_run_public', AsyncMock()) as public, \
             patch.object(self.p, '_call_api', AsyncMock()) as api, \
             patch.object(self.p._texts, 'get') as get, \
             patch.object(self.p._originals, 'claim') as claim, \
             patch.object(plugin, 'extract_firecrawl_document', AsyncMock()) as extract:
            for context in contexts:
                for call, args in calls:
                    with self.subTest(context=context, tool=call.__name__, args=args):
                        # Ordinary fields cannot fill or repair the trusted envelope.
                        kwargs = dict(self.forged)
                        if context is not None:
                            kwargs['_host_context'] = context
                        result = await call(**args, **kwargs)
                        self.assertFalse(result['success'])
                        self.assertEqual(result['status'], 'invalid_context')
            for mock in (search, public, api, get, claim, extract, self.p.ctx.send.image):
                mock.assert_not_called()

    async def test_platform_and_account_are_not_cache_authority(self):
        self.p._provider_search = AsyncMock(return_value='synthetic results')
        for optional in ({}, {'platform': '', 'account_id': ''},
                         {'platform': None, 'account_id': None}):
            trusted = {'contract': 'neko.tools.context.v1', 'session_id': 'chat',
                       'stream_id': 'chat', **optional}
            result = await self.p.handle_search_tool(['one'], _host_context=trusted, **self.forged)
            self.assertTrue(result['success'])
            self.assertEqual(self.p._provider_search.call_args.kwargs['scope'], 'chat')
        self.assertEqual(self.p._provider_search.await_count, 3)

    async def test_search_and_read_cache_only_trusted_scope_despite_extra_stream_id(self):
        doc = plugin.capture_document('https://example.com/article', 'title', 'trusted body', 'firecrawl')
        self.p.config.web.search_provider = 'exa'
        self.p.config.web.extract_provider = 'firecrawl'
        async def retrieve(*args, **kwargs):
            return kwargs['render_document'](doc, 1000)
        async def public(action, failure):
            return await action(None, plugin.Deadline.after(55))
        self.p._run_public = public
        page = PageRead(doc.url, doc.title, doc.text, [], [], captured_text=doc.text)
        with patch.object(plugin, 'retrieve', side_effect=retrieve), \
             patch.object(plugin, 'read_public_page', AsyncMock(return_value=page)), \
             patch.object(plugin, 'extract_firecrawl_document', AsyncMock(return_value=doc)):
            for platform in ('qq', 'telegram'):
                trusted = host_context(platform, platform)
                result = await self.p.handle_search_tool(['one'], read_pages=1,
                                                        _host_context=trusted, **self.forged)
                self.assertTrue(result['success'])
                for mode in ('page', 'article'):
                    result = await self.p.handle_read_tool(doc.url, mode=mode,
                                                          _host_context=trusted, **self.forged)
                    token = result['text_id']
                    self.assertIsNone(self.p._texts.get('foreign', token))
                    self.assertIsNotNone(self.p._texts.get(platform, token))
                    text = await self.p.handle_text(token, _host_context=trusted, **self.forged)
                    self.assertIn(doc.text, text['content'])
                    denied = await self.p.handle_text(token, _host_context=host_context('foreign'),
                                                       stream_id=platform)
                    self.assertFalse(denied['success'])
        self.assertEqual({entry.scope for entry in self.p._texts.entries.values()}, {'qq', 'telegram'})

    async def test_images_and_cursor_use_trusted_scope_not_extra_stream_id(self):
        trusted = host_context('telegram-chat', 'telegram')
        async def public(action, failure):
            return await action(None, plugin.Deadline.after(55))
        self.p._run_public = public
        with patch.object(self.p._pager, 'batch', AsyncMock(return_value=([self.image], [], '', 0, False))) as batch:
            first = await self.p.handle_images_tool(urls=[self.image.source], _host_context=trusted, **self.forged)
            scope, cursor = batch.call_args.args[1:3]
            self.assertEqual(scope, 'telegram-chat')
            self.assertIn(cursor, self.p._pager.queues)
            token = first['content_items'][0]['metadata']['image_id']
            with self.assertRaises(ValueError):
                self.p._originals.claim('foreign', token)
            await self.p.handle_images_tool(cursor=cursor, _host_context=trusted, **self.forged)
            self.assertEqual(batch.call_args.args[1:3], ('telegram-chat', cursor))

    async def sdk_send(self, receipt, platform):
        calls = []
        async def rpc(method, plugin_id, payload, **kwargs):
            self.assertEqual((method, plugin_id, payload['capability']), ('cap.call', 'neko.web', 'send.image'))
            calls.append(payload['args'])
            if isinstance(receipt, BaseException):
                raise receipt
            return receipt
        self.p.test_ctx = PluginContext('neko.web', rpc_call=rpc)
        scope = platform + '-chat'
        token = self.p._originals.add(scope, self.image)
        with patch.object(plugin, 'load_image', AsyncMock(return_value=self.image)) as load:
            result = await self.p.handle_send_image(token, _host_context=host_context(scope, platform), **self.forged)
            again = await self.p.handle_send_image(token, _host_context=host_context(scope, platform), **self.forged)
        self.assertFalse(again['success'])
        load.assert_awaited_once()
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]['stream_id'], scope)
        self.assertEqual(base64.b64decode(calls[0]['image_base64']), self.raw)
        return result, self.p._originals.items[token].state

    async def test_real_sdk_failures_stay_unknown_and_never_resend(self):
        for receipt in (False, None, {'success': False}, {}, TimeoutError(), RuntimeError()):
            with self.subTest(receipt=receipt):
                result, state = await self.sdk_send(receipt, 'telegram')
                self.assertFalse(result['success'])
                self.assertEqual(result['status'], 'delivery_unconfirmed')
                self.assertEqual(state, 'unknown')

    async def test_real_sdk_success_copy_distinguishes_original_submission_from_delivery(self):
        for platform in ('qq', 'telegram'):
            for receipt in (True, {'success': True}):
                with self.subTest(platform=platform, receipt=receipt):
                    result, state = await self.sdk_send(receipt, platform)
                    self.assertTrue(result['success'])
                    self.assertEqual(state, 'sent')
                    self.assertIn('插件向宿主提交下载到的原始字节', result['content'])
                    self.assertIn('可能转换或压缩', result['content'])
                    self.assertIn('不代表用户收到原始文件', result['content'])
                    self.assertIn('QQ/TG', result['content'])
