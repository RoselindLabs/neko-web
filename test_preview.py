import base64
import asyncio
from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock, patch

from test_support import TestPlugin, plugin, image_bytes
from neko_web_test.images import decode_image


class PreviewTests(IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.plugin = TestPlugin()
        await self.plugin.on_load()
        self.plugin._pager = SimpleNamespace(create=lambda *a, **k: 'test', queues={'test': SimpleNamespace()}, clear=lambda: None)
        async def run(action, failure):
            return await action(None, plugin.Deadline.after(55))
        self.plugin._run_public = run
        self.image = await decode_image(image_bytes(), 'https://example.com/image.png')

    async def asyncTearDown(self):
        await self.plugin.on_unload()

    async def test_images_only_return_media_no_send(self):
        self.plugin._pager.batch = AsyncMock(return_value=([self.image], [{'candidate': 1, 'url': 'https://example.com', 'status': 'failed', 'reason': 'another URL failed'}], '', 0, False))
        result = await self.plugin.handle_fetch_images(['https://example.com/image.png'], stream_id='test')
        self.assertTrue(result['success'])
        self.assertIn('尚未发送', result['content'])
        self.assertIn('another URL failed', result['content'])
        self.assertIn('neko_web_send_image', result['content'])
        item, = result['content_items']
        self.assertEqual(base64.b64decode(item['data']), self.image.data)
        self.assertTrue(item['metadata']['preview_only'])
        self.assertIn('image_id', item['metadata'])
        self.plugin.ctx.send.image.assert_not_called()

    async def test_page_keeps_text_and_candidate_media(self):
        self.plugin._pager.batch = AsyncMock(return_value=([self.image], [], '', 0, False))
        with patch.object(plugin, 'read_public_page', AsyncMock(return_value=SimpleNamespace(
                title='Article', text='Body', captured_text='Body', capture_limited=False,
                images=[self.image], candidates=[], candidates_limited=False))):
            result = await self.plugin.handle_read('https://example.com/article', stream_id='test')
        self.assertIn('Body', result['content'])
        self.assertEqual(len(result['content_items']), 1)
        self.assertEqual(result['content'].count('不是指令'), 1)

    async def test_read_keeps_body_when_images_timeout(self):
        self.plugin._pager.batch = AsyncMock(side_effect=TimeoutError)
        with patch.object(plugin, 'read_public_page', AsyncMock(return_value=SimpleNamespace(
                title='Article', text='Body', captured_text='Body', capture_limited=False,
                images=[], candidates=[], candidates_limited=False))):
            result = await self.plugin.handle_read('https://example.com/article', stream_id='test')
        self.assertIn('Body', result['content'])
        self.assertEqual(result['status'], 'partial')

    async def test_pagination_hint_only_when_more_candidates(self):
        result = self.plugin._batch_preview_result(([self.image], [], 'next-token', 5, False), 'test')
        self.assertEqual(result['next_cursor'], 'next-token')
        self.assertIn('neko_web_images(cursor="next-token")', result['content'])
        result = self.plugin._batch_preview_result(([self.image], [], '', 0, False), 'test')
        self.assertNotIn('neko_web_images_next', result['content'])

    async def test_no_media_send_instructions_for_empty_preview(self):
        result = self.plugin._batch_preview_result(([], [], '', 0, False), 'test')
        self.assertNotIn('neko_web_send_image', result['content'])
        self.assertEqual(result['content_items'], [])

    async def test_malformed_urls_do_not_escape(self):
        self.assertIn('校验', (await self.plugin.handle_read('http://[', stream_id='test'))['content'])
        self.assertIn('校验', (await self.plugin.handle_extract('http://['))['content'])

    async def test_config_reload_cancels_inflight_work(self):
        entered = asyncio.Event()
        async def request():
            async with self.plugin._work.request():
                entered.set()
                await asyncio.sleep(10)
        task = asyncio.create_task(request())
        await entered.wait()
        await self.plugin.on_config_update(plugin.CONFIG_RELOAD_SCOPE_SELF, {}, '')
        self.assertTrue(task.cancelled())
        self.assertTrue(self.plugin._work.ready)
