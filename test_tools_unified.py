"""Merged model tools, real SDK discovery and synthetic provider/download work."""
import asyncio
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock, Mock, patch

from maibot_sdk.components import collect_components

from test_support import TestPlugin, plugin, image_bytes
from neko_web_test.page import PageRead, read_public_page


class UnifiedToolsTests(IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.p = TestPlugin()
        self.p.config.web.search_provider = 'exa'
        await self.p.on_load()
        self.p._provider_search = AsyncMock(return_value='synthetic results')
        self.p._call_api = AsyncMock(return_value='synthetic vertical')

    async def asyncTearDown(self):
        await self.p.on_unload()

    async def test_real_sdk_exports_seven_tools_and_only_new_shapes(self):
        tools = {c['name']: c['metadata'] for c in collect_components(self.p) if c['type'] == 'TOOL'}
        self.assertEqual(set(tools), {'neko_web_search', 'neko_web_domains', 'neko_web_read',
                                     'neko_web_images', 'neko_web_text', 'neko_web_send_image', 'neko_web_weather'})
        search = {p['name']: p for p in tools['neko_web_search']['parameters']}
        self.assertTrue(search['queries']['required'])
        self.assertNotIn('query', search)
        self.assertEqual({p['name'] for p in tools['neko_web_read']['parameters']},
                         {'url', 'mode', 'include_images'})
        self.assertEqual({p['name'] for p in tools['neko_web_images']['parameters']}, {'urls', 'cursor'})
        self.assertFalse(self.p.config.automation.enabled)

    async def test_one_or_many_searches_preserve_filters_dedup_and_explicit_extraction(self):
        result = await self.p.handle_search_tool(['one', ' one '], sites=['example.com'],
                                               read_pages=2, stream_id='chat')
        self.assertTrue(result['success'])
        self.assertEqual(self.p._provider_search.call_args.args, ('one', 5, {'includeDomains': ['example.com']}, 2))
        self.assertEqual(self.p._provider_search.call_args.kwargs['scope'], 'chat')
        self.p._provider_search.reset_mock()
        result = await self.p.handle_search_tool(['one', 'two', 'one'])
        self.assertTrue(result['success'])
        self.assertEqual(self.p._provider_search.await_count, 2)
        self.assertIn('查询：two', result['content'])
        self.p._call_api.assert_not_called()

    async def test_invalid_or_unsupported_batch_never_spends_provider_requests(self):
        for queries, fields in [([], {}), ('one', {}), (['x'] * 6, {}), ([''], {}),
                               (['x' * 1001], {}), (['one'], {'read_pages': True}),
                               (['one'], {'query': 'ambiguous'}),
                               (['one', 'two'], {'read_pages': 1}),
                               (['one', 'two'], {'domain': 'finance'})]:
            result = await self.p.handle_search_tool(queries, **fields)
            self.assertFalse(result['success'], (queries, fields))
        self.p._provider_search.assert_not_called()
        self.p._call_api.assert_not_called()

    async def test_default_page_reads_only_text_and_keeps_offline_continuation(self):
        page = PageRead('https://example.com', 'Synthetic', 'short', [], [],
                        ['https://example.com/image.png'], captured_text='x' * 12000)
        async def run(action, failure):
            return await action(None, plugin.Deadline.after(55))
        self.p._run_public = run
        with patch.object(plugin, 'read_public_page', AsyncMock(return_value=page)) as reader, \
             patch.object(self.p._pager, 'create', Mock()) as create, \
             patch.object(self.p._pager, 'batch', AsyncMock()) as batch:
            result = await self.p.handle_read_tool(page.final_url, stream_id='chat')
        self.assertFalse(reader.call_args.kwargs['include_images'])
        create.assert_not_called(); batch.assert_not_called()
        self.assertEqual(result['content_items'], [])
        self.assertTrue(result['text_has_more'])
        self.assertFalse(result['has_more'])
        more = await self.p.handle_text(result['text_id'], offset=result['next_offset'], stream_id='chat')
        self.assertEqual(more['start'], 8000)
        self.assertFalse((await self.p.handle_text(result['text_id'], stream_id='other'))['success'])
        self.p.ctx.send.image.assert_not_called()

    async def test_read_modes_and_explicit_preview_never_auto_switch_or_send(self):
        self.p.handle_read = AsyncMock(return_value={'success': True})
        self.p.handle_extract = AsyncMock(return_value={'success': True})
        for fields in ({'mode': 'auto'}, {'include_images': 'true'}, {'mode': 'article', 'include_images': True}):
            self.assertFalse((await self.p.handle_read_tool('https://example.com', **fields))['success'])
        self.p.handle_read.assert_not_called(); self.p.handle_extract.assert_not_called()
        await self.p.handle_read_tool('https://example.com', mode='article', stream_id='chat')
        self.p.handle_extract.assert_awaited_once_with('https://example.com', stream_id='chat')
        await self.p.handle_read_tool('https://example.com', include_images=True, stream_id='chat')
        self.p.handle_read.assert_awaited_once_with('https://example.com', include_images=True, stream_id='chat')
        self.p.ctx.send.image.assert_not_called()

    async def test_images_url_or_cursor_routes_exclusively_and_keeps_chat_scope(self):
        self.p.handle_fetch_images = AsyncMock(return_value={'success': True})
        self.p.handle_images_next = AsyncMock(return_value={'success': True})
        for fields in ({}, {'urls': [], 'cursor': 'token'}, {'urls': ['https://example.com'], 'cursor': 'token'}, {'cursor': 1}):
            self.assertFalse((await self.p.handle_images_tool(**fields))['success'])
        self.p.handle_fetch_images.assert_not_called(); self.p.handle_images_next.assert_not_called()
        await self.p.handle_images_tool(urls=['https://example.com'], stream_id='chat')
        await self.p.handle_images_tool(cursor='token', stream_id='chat')
        self.p.handle_fetch_images.assert_awaited_once_with(['https://example.com'], stream_id='chat')
        self.p.handle_images_next.assert_awaited_once_with('token', stream_id='chat')

    async def test_text_only_download_skips_image_parser_decode_and_candidate_requests(self):
        from neko_web_test import page as page_module
        for body, mime in [(b'<title>T</title><article>Body</article><img src="/photo.png">', 'text/html'),
                           (image_bytes(), 'image/png')]:
            with patch.object(page_module, 'download_public', AsyncMock(return_value=(body, mime, 'https://example.com'))) as download, \
                 patch.object(page_module, 'image_urls_in_document', Mock(side_effect=AssertionError('no image parsing'))), \
                 patch.object(page_module, 'decode_image', AsyncMock(side_effect=AssertionError('no image decoding'))):
                result = await read_public_page(None, 'https://example.com', include_images=False, capture_text=True)
            download.assert_awaited_once()
            self.assertEqual(result.images, [])
            self.assertEqual(result.candidates, [])

    async def test_merged_tool_keeps_existing_lifecycle_tracking(self):
        entered = asyncio.Event()
        async def slow(*args, **kwargs):
            entered.set()
            await asyncio.Event().wait()
        async def run(action, failure):
            return await action(None, plugin.Deadline.after(55))
        self.p._run_public = run
        with patch.object(plugin, 'read_public_page', side_effect=slow):
            work = asyncio.create_task(self.p.handle_read_tool('https://example.com', stream_id='chat'))
            await entered.wait()
            self.assertIn(work, self.p._work.tasks)
            await self.p.on_config_update(plugin.CONFIG_RELOAD_SCOPE_SELF, {}, '')
            self.assertTrue(work.cancelled())
