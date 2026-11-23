from unittest import IsolatedAsyncioTestCase, TestCase
from unittest.mock import patch
import re

from runtime import response_status
from search_preview import render_search_preview
from texts import MAX_CAPTURE_CHARS, TextRegistry


def hit(url, snippet, title='报告', published='', rank=1):
    return {'url': url, 'title': title, 'published': published, 'snippet': snippet,
            'source': 'example.com', 'provider_rank': rank}


def render(rows, store, label='Exa', scope='chat-a'):
    return render_search_preview(rows, label=label, registry=store, scope=scope)


def token_from(text):
    return re.search(r'neko_web_text\(text_id="([\w-]+)", offset=0\)', text)[1]


class PreviewTests(TestCase):
    def test_small_complete_selection_needs_no_snapshot_even_when_provider_filtered(self):
        store = TextRegistry()
        url = 'https://example.com/a?utm_source=original#section'
        text = render([('发布日期', [hit(url, '证据', published='2026-01-03')],
                        {'duplicates': 2, 'omitted': 3}, '')], store)
        self.assertEqual(text.splitlines()[0], 'Exa 搜索状态：ok')
        for value in [url, '报告', '证据', '日期：2026-01-03', '去重 2 条', '名额外未选 3 条', '不代表权威']:
            self.assertIn(value, text)
        self.assertFalse(store.entries)

    def test_preview_has_two_bounded_excerpts_but_snapshot_keeps_every_selected_hit(self):
        store = TextRegistry()
        urls = [f'https://example.com/{n}' for n in range(3)]
        snippets = [c * 230 + '\n尾注' + c for c in '甲乙丙']
        hits = [hit(u, s, rank=n) for n, (u, s) in enumerate(zip(urls, snippets), 1)]
        text = render([('问题', hits, {}, '')], store)
        self.assertEqual(response_status(text), 'ok')
        for n in [0, 1]:
            self.assertIn(urls[n], text)
            self.assertIn(snippets[n][:199], text)
        self.assertNotIn(urls[2], text)
        self.assertNotIn('尾注', text)
        self.assertIn('日期：未提供', text)
        self.assertIn('首屏 2 条', text)
        full = store.get('chat-a', token_from(text)).text
        for url, snippet in zip(urls, snippets):
            self.assertIn(url, full)
            self.assertIn(snippet, full)
        self.assertIn('供应商顺位：3', full)

    def test_shared_source_ids_follow_canonical_url_but_evidence_belongs_to_its_query(self):
        page = 'https://example.com/a?id=1'
        alias = page + '&utm_source=two#section'
        other = 'https://example.com/a?id=2'
        # With short excerpts the alias alone requires a snapshot. With a long common
        # prefix the two different tails must not be merged as an identical excerpt.
        for prefix in ['', '相同前缀' * 60]:
            with self.subTest(prefix_length=len(prefix)):
                store = TextRegistry()
                revised = prefix + '修订'
                text = render([('一', [hit(page, prefix + '初版')], {}, ''),
                               ('二', [hit(alias, revised), hit(other, '另一页')], {}, ''),
                               ('三', [hit(page, revised, title='新标题')], {}, ''),
                               ('四', [hit(page, revised, published='2026-01-03')], {}, ''),
                               ('五', [hit(page, revised, title='新标题')], {}, '')], store)
                self.assertEqual(text.splitlines().count(page), 1)
                self.assertNotIn(alias, text)
                self.assertEqual(text.count('[S1] 报告｜日期：未提供'), 1)
                self.assertIn('[S1]\n摘录：' + revised[:16], text.split('Q2 查询：')[1])
                self.assertIn('[S2] 报告｜日期：未提供\n' + other, text)
                self.assertIn('[S1] 标题：新标题\n摘录同 Q2 [S1]', text)
                self.assertIn('[S1] 日期：2026-01-03\n', text)
                self.assertEqual(text.count('新标题'), 1)
                self.assertIn('标题、日期同 Q3', text.split('Q5 查询：')[1])
                full = store.get('chat-a', token_from(text)).text
                q1, rest = full.split('Q2 查询：')
                q2 = rest.split('Q3 查询：')[0]
                self.assertIn(page, q1.splitlines())
                self.assertIn(prefix + '初版', q1)
                self.assertIn(alias, q2.splitlines())
                self.assertIn(revised, q2)
                self.assertEqual(full.count(revised), 1)

    def test_duplicate_reference_never_points_at_a_preview_entry_hidden_by_selection(self):
        store = TextRegistry()
        hits = [hit(f'https://example.com/{i}', f'证据{i}') for i in range(3)]
        text = render([('一', hits, {}, ''), ('二', hits[2:], {}, '')], store)
        self.assertIn('摘录：证据2', text.split('Q2 查询：')[1])
        self.assertNotIn('摘录同 Q1', text)
        self.assertIn('摘录同 Q1 [S3]', store.get('chat-a', token_from(text)).text)

    def test_long_links_never_turn_into_fake_links_or_squeeze_out_later_queries(self):
        rows = []
        urls = []
        for i in range(5):
            url = f'https://example.com/{i}/' + 'p' * 1800
            other = f'https://example.com/short/{i}'
            urls.extend([url, other])
            rows.append((f'问题{i}' + '问' * 800, [hit(url, '证' * 500, title=url[:300]),
                        hit(other, f'证据{i}')], {}, ''))
        store = TextRegistry()
        text = render(rows, store)
        self.assertLessEqual(len(text), 2800)
        full = store.get('chat-a', token_from(text)).text
        for i, row in enumerate(rows):
            self.assertIn(f'Q{i + 1} 查询：问题{i}', text)
            self.assertIn(row[0], full)
            self.assertIn(f'证据{i}', text)
        self.assertTrue(set(re.findall(r'https?://\S+', text)).issubset(urls))
        for url in urls:
            self.assertIn(url, full)

    def test_empty_successes_and_mixed_failures_survive_aggregation_without_raw_errors(self):
        empty = ('零', [], {}, '')
        failed = ('失败', [], {}, 'quota_or_rate_limit')
        unknown = ('未知', [], {}, 'HTTP 500 SECRET')
        self.assertEqual(response_status(render([empty, empty], None)), 'no_results')
        self.assertEqual(response_status(render([failed, unknown], None)), 'quota_or_rate_limit')
        text = render([empty, failed, unknown, ('部分', [hit('https://example.com', '证据')],
                       {'invalid': 1}, '')], None, 'Jina SERP (bing)')
        self.assertEqual(text.splitlines()[0], 'Jina SERP (bing) 搜索状态：partial')
        self.assertEqual(response_status(text), 'partial')
        for code in ['no_results', 'quota_or_rate_limit', 'provider_error', '无效 1 条']:
            self.assertIn(code, text)
        self.assertNotIn('SECRET', text)
        self.assertNotIn('neko_web_text', text)

    def test_failed_cache_discloses_loss_without_publishing_a_token(self):
        rows = [('问题', [hit('https://example.com', '证' * 210)], {}, '')]
        for registry, scope in [(None, 'chat'), (TextRegistry(), ''), (TextRegistry(max_bytes=10), 'chat')]:
            text = render(rows, registry, scope=scope)
            self.assertEqual(response_status(text), 'partial')
            self.assertIn('摘录：证', text)
            self.assertIn('未建立搜索摘录快照', text)
            self.assertNotIn('text_id', text)
        store = TextRegistry()
        text = render([('超限', [hit('https://example.com', '大' * MAX_CAPTURE_CHARS)], {}, '')], store)
        self.assertNotIn('text_id', text)
        self.assertFalse(store.entries)


class SnapshotToolTests(IsolatedAsyncioTestCase):
    async def test_real_text_tool_follows_snapshot_by_offset_or_keyword_without_fetching(self):
        from test_support import TestPlugin, host_context, plugin
        from neko_web_test.search_preview import render_search_preview as packaged_render

        app = TestPlugin()
        await app.on_load()
        self.addAsyncCleanup(app.on_unload)
        now = [0]
        app._texts = plugin.TextRegistry(clock=lambda: now[0], ttl=10)
        query = '测试'
        source = hit('https://example.com', '摘' * 600 + '定位词')
        with patch.object(plugin.httpx, 'AsyncClient', side_effect=AssertionError('must not fetch')):
            text = packaged_render([(query, [source], {}, '')], label='Jina SERP (google)',
                                   registry=app._texts, scope='chat-a')
            token = token_from(text)
            ctx = {'_host_context': host_context('chat-a')}
            first = await app.handle_text(token, offset=0, limit=100, **ctx)
            self.assertIn('搜索摘录快照状态：ok', first['content'])
            self.assertNotIn('原文状态', first['content'])
            self.assertEqual(first['next_offset'], 100)
            second = await app.handle_text(token, offset=first['next_offset'], keyword='定位词', **ctx)
            self.assertIn('定位词', second['content'])
            self.assertIn('搜索摘录快照末尾', second['content'])
            missing = await app.handle_text(token, keyword='查无此词', **ctx)
            self.assertEqual(missing['status'], 'not_found')
            self.assertIn('本快照', missing['content'])
            for scope, clock in [('other-chat', 0), ('chat-a', 10)]:
                now[0] = clock
                result = await app.handle_text(token, _host_context=host_context(scope))
                self.assertEqual(result['status'], 'unavailable')
                self.assertNotIn('定位词', result['content'])
