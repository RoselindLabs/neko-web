import asyncio
import base64
import io
import json
from unittest import IsolatedAsyncioTestCase, TestCase
from unittest.mock import AsyncMock, patch

import httpx
from PIL import Image

from test_support import TestPlugin, plugin, image_bytes, host_context
from images import prepare_image, ImageFetchError, image_urls_in_document
from preview import PreviewPager
from originals import OriginalRegistry
from page import page_text
from retrieval import retrieve
from providers import ProviderError, extract_firecrawl
from runtime import Deadline
from types import SimpleNamespace


class ImageQualityTests(TestCase):
    def test_fake_png_is_rejected(self):
        with self.assertRaises(ImageFetchError):
            prepare_image(b'\x89PNG\r\n\x1a\nnot-an-image', 'https://example.com/a.png')

    def test_pixel_limit_enforced(self):
        with patch('images.MAX_PIXELS', 100):
            with self.assertRaises(ImageFetchError):
                prepare_image(image_bytes(), 'https://example.com/a.png')

    def test_original_bytes_preserved_preview_small(self):
        data = image_bytes((2400, 1600))
        original = prepare_image(data, 'https://example.com/a.png', preview=False)
        preview = prepare_image(data, 'https://example.com/a.png')
        self.assertEqual(original.data, data)
        self.assertEqual(original.original_sha256, preview.original_sha256)
        self.assertEqual((preview.width, preview.height), (2400, 1600))
        self.assertLessEqual(len(preview.data), 256 * 1024)
        with Image.open(io.BytesIO(preview.data)) as image:
            self.assertLessEqual(max(image.size), 1280)

    def test_explicit_original_url_beats_thumbnail(self):
        html = '<a href="/original.png?signature=x"><img src="/small.jpg"></a><img data-original="/full.png" srcset="/medium.png 2000w">'
        urls = image_urls_in_document(html, 'https://example.com/page')
        self.assertEqual(urls, ['https://example.com/original.png?signature=x', 'https://example.com/full.png'])

    def test_article_survives_long_navigation(self):
        html = ('<nav>' + 'MENU ' * 2000 + '</nav><main><article>REAL_BODY</article></main><footer>footer</footer>').encode()
        _, text = page_text(html, 'text/html', 1000)
        self.assertEqual(text, 'REAL_BODY')

    def test_registry_scope_capacity_expiry_and_claim(self):
        now = [0]
        registry = OriginalRegistry(clock=lambda: now[0])
        image = prepare_image(image_bytes(), 'https://example.com/a.png')
        token = registry.add('a', image)
        with self.assertRaises(ValueError):
            registry.claim('b', token)
        registry.claim('a', token)
        with self.assertRaises(ValueError):
            registry.claim('a', token)
        registry.items[token].state = 'sent'
        now[0] = 601
        with self.assertRaises(ValueError):
            registry.claim('a', token)


class AuditAsyncTests(IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.p = TestPlugin()
        await self.p.on_load()

    async def asyncTearDown(self):
        await self.p.on_unload()

    async def test_preview_payload_fits_real_rpc_budget(self):
        image = prepare_image(image_bytes((2400, 1600)), 'https://example.com/a.png')
        result = self.p._image_preview_result([image] * 4, [], 'chat')
        self.assertLess(len(json.dumps(result).encode()), 2 * 1024 * 1024)

    async def test_send_original_bytes_and_no_duplicate(self):
        raw = image_bytes((1600, 1200))
        preview = prepare_image(raw, 'https://example.com/a.png')
        original = prepare_image(raw, preview.source, preview=False)
        token = self.p._originals.add('chat', preview)
        with patch.object(plugin, 'load_image', AsyncMock(return_value=original)):
            result = await self.p.handle_send_image(token, _host_context=host_context())
            again = await self.p.handle_send_image(token, _host_context=host_context())
        self.assertEqual(result['status'], 'sent')
        self.assertFalse(again['success'])
        self.p.ctx.send.image.assert_awaited_once()
        self.assertEqual(base64.b64decode(self.p.ctx.send.image.call_args.args[0]), raw)

    async def test_changed_original_is_not_sent(self):
        preview = prepare_image(image_bytes(color='red'), 'https://example.com/a.png')
        different = prepare_image(image_bytes(color='blue'), preview.source, preview=False)
        token = self.p._originals.add('chat', preview)
        with patch.object(plugin, 'load_image', AsyncMock(return_value=different)):
            result = await self.p.handle_send_image(token, _host_context=host_context())
        self.assertEqual(result['status'], 'source_changed')
        self.p.ctx.send.image.assert_not_called()

    async def test_unconfirmed_send_is_not_retried(self):
        raw = image_bytes()
        preview = prepare_image(raw, 'https://example.com/a.png')
        token = self.p._originals.add('chat', preview)
        self.p.ctx.send.image.return_value = False
        with patch.object(plugin, 'load_image', AsyncMock(return_value=prepare_image(raw, preview.source, preview=False))):
            result = await self.p.handle_send_image(token, _host_context=host_context())
            await self.p.handle_send_image(token, _host_context=host_context())
        self.assertEqual(result['status'], 'delivery_unconfirmed')
        self.p.ctx.send.image.assert_awaited_once()

    async def test_original_download_failure_offers_link_not_preview(self):
        image = prepare_image(image_bytes(), 'https://example.com/a.png')
        token = self.p._originals.add('chat', image)
        with patch.object(plugin, 'load_image', AsyncMock(side_effect=plugin.ImageFetchError('文件超过大小限制'))):
            result = await self.p.handle_send_image(token, _host_context=host_context())
        self.assertEqual(result['status'], 'original_unavailable')
        self.assertIn(image.source, result['content'])
        self.p.ctx.send.image.assert_not_called()

    async def test_anysearch_error_is_safe_and_mcp_failure_recognized(self):
        for payload in [{'error': {'message': 'FIXTURE_SECRET'}},
                        {'result': {'isError': True, 'content': [{'type': 'text', 'text': 'FIXTURE_SECRET'}]}}]:
            with self.assertRaises(plugin.ProviderError) as caught:
                self.p._extract_text(payload)
            self.assertNotIn('FIXTURE_SECRET', str(caught.exception))

    async def test_anysearch_extract_blocks_private_before_post(self):
        self.p.config.web.extract_provider = 'anysearch'
        with patch.object(plugin, 'post_json', AsyncMock()) as post:
            result = await self.p._call_api('extract', {'url': 'http://127.0.0.1/private'})
        self.assertIn('blocked_target', result)
        post.assert_not_called()

    async def test_anysearch_response_bounded_and_invalid_config_rejected(self):
        result = self.p._extract_text({'result': {'content': [{'type': 'text', 'text': 'x' * 30000}]}})
        self.assertLessEqual(len(result), 16030)
        with self.assertRaises(ValueError):
            plugin.WebConfig(timeout_seconds=-1)
        with self.assertRaises(ValueError):
            plugin.WebConfig(max_images=100)

    async def test_scraped_404_cannot_be_successful_body(self):
        with patch('providers.validate_remote_target', AsyncMock()):
            async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, json={'success': True, 'data': {'markdown': '# Not found', 'metadata': {'statusCode': 404}}}))) as client:
                with self.assertRaises(ProviderError) as caught:
                    await extract_firecrawl(client, 'test', 'https://example.com', 1000, 5)
        self.assertEqual(caught.exception.code, 'page_error')

    async def test_long_search_sources_cannot_crowd_out_paid_body(self):
        config = SimpleNamespace(exa_api_key='test', firecrawl_api_key='test', extract_provider='firecrawl', timeout_seconds=5)
        raw = [{'url': 'https://example.com/p?id=' + 'x' * 1800 + str(i), 'title': 't' * 300, 'highlights': ['s' * 1000]} for i in range(10)]
        with patch('retrieval.search_exa_results', AsyncMock(return_value=raw)), \
             patch('retrieval.extract_firecrawl', AsyncMock(return_value='BODY_MARKER' + 'b' * 4000)) as extract:
            result = await retrieve(None, config, 'query', 10, {}, 2)
        self.assertEqual(extract.await_count, 2)
        self.assertEqual(result.count('BODY_MARKER'), 2)
        self.assertIn('partial', result)
        self.assertLessEqual(len(result), 16000)

    async def test_all_invalid_search_results_not_no_results(self):
        config = SimpleNamespace(exa_api_key='test', timeout_seconds=5)
        with patch('retrieval.search_exa_results', AsyncMock(return_value=[{}, {'url': 'http://127.0.0.1'}])):
            with self.assertRaises(ProviderError) as caught:
                await retrieve(None, config, 'query', 5, {})
        self.assertEqual(caught.exception.code, 'invalid_response')

    async def test_almost_expired_deadline_preserves_search_results(self):
        config = SimpleNamespace(exa_api_key='test', firecrawl_api_key='test', extract_provider='firecrawl', timeout_seconds=5)
        with patch('retrieval.search_exa_results', AsyncMock(return_value=[{'url': 'https://example.com', 'highlights': ['evidence']}])), \
             patch('retrieval.extract_firecrawl', AsyncMock(return_value='unexpected request')) as extract:
            result = await retrieve(None, config, 'query', 5, {}, 1, deadline=Deadline.after(2.01))
        self.assertIn('evidence', result)
        extract.assert_not_awaited()

    async def test_preview_inflight_tasks_count_against_capacity(self):
        pager = PreviewPager()
        entered = asyncio.Event()
        release = asyncio.Event()
        async def load(*args):
            entered.set()
            await release.wait()
            return prepare_image(image_bytes(), args[1])
        tasks = []
        try:
            with patch('preview.load_image', side_effect=load):
                for i in range(4):
                    entered.clear()
                    token = pager.create('chat', [f'https://example.com/{i}'])
                    tasks.append(asyncio.create_task(pager.batch(None, 'chat', token, plugin.make_policy())))
                    await entered.wait()
                with self.assertRaises(ImageFetchError):
                    pager.create('chat', ['https://example.com/fifth'])
                release.set()
                await asyncio.gather(*tasks)
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
        self.assertEqual(pager.active, {})

    async def test_cancel_during_send_blocks_unverified_repeat(self):
        raw = image_bytes()
        preview = prepare_image(raw, 'https://example.com/a.png')
        token = self.p._originals.add('chat', preview)
        entered = asyncio.Event()
        async def send(*args, **kwargs):
            entered.set()
            await asyncio.sleep(10)
        self.p.ctx.send.image.side_effect = send
        with patch.object(plugin, 'load_image', AsyncMock(return_value=prepare_image(raw, preview.source, preview=False))):
            task = asyncio.create_task(self.p.handle_send_image(token, _host_context=host_context()))
            await entered.wait()
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertEqual(self.p._originals.items[token].state, 'unknown')
        self.assertFalse((await self.p.handle_send_image(token, _host_context=host_context()))['success'])

    async def test_wrong_chat_cannot_send_original(self):
        preview = prepare_image(image_bytes(), 'https://example.com/a.png')
        token = self.p._originals.add('chat', preview)
        with patch.object(plugin, 'load_image', AsyncMock()) as load:
            result = await self.p.handle_send_image(token, _host_context=host_context('other'))
        self.assertFalse(result['success'])
        load.assert_not_called()

    async def test_batch_errors_have_unsuccessful_tool_status(self):
        self.p._provider_search = AsyncMock(return_value='Exa 搜索状态：auth_error；密钥错误。')
        result = await self.p.handle_batch_search(['query'], _host_context=host_context())
        self.assertFalse(result['success'])
        self.assertEqual(result['status'], 'auth_error')
