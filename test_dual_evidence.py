"""Offline evidence and output contracts, independent of the dual search scheduler."""
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import patch
import re

from dual_evidence import merge_dual
from retrieval import organize_results
from runtime import response_status
from search_preview import PREVIEW_CHARS, render_search_preview
from texts import TextRegistry


def outcome(*pages, status='ok', answer='', counts=None):
    sources, stats = organize_results([{'url': url, 'title': title, 'highlights': [text]}
                                       for url, title, text in pages], 10)
    return SimpleNamespace(sources=sources, stats=stats | (counts or {}), answer=answer, status=status)


def page(name, text):
    return 'https://example.org/' + name, '页 ' + name, text


def merge(left, right, limit=5, *, cross_check=False, query='问题'):
    return merge_dual(query, {'exa': right, 'linkup_fast': left}, limit, cross_check=cross_check)


def render(pairs, registry=None):
    rows = [r for r, _ in pairs]
    answers = {r[0]: a for r, a in pairs if a}
    return render_search_preview(rows, label='Linkup + Exa', registry=registry, scope='owner', answers=answers)


def full_text(preview, store):
    token = re.search(r'text_id="([\w-]+)"', preview)[1]
    return store.get('owner', token).text


class DualEvidenceTests(TestCase):
    def test_shared_page_retains_conflicting_excerpts_and_real_urls_from_both_channels(self):
        url = 'https://example.org/releases?v=3'
        alias = url + '&utm_source=exa#dates'
        prefix = '沿用上一公告。' * 120
        left, right = prefix + '生效日为周三。', prefix + '生效日为周五。'
        row, answer = merge(outcome((url, '公告', left), answer='候选周三'),
                            outcome((alias, '公告的另一标题', right)), 1, cross_check=True)
        self.assertEqual(len(row[1]), 1)
        store = TextRegistry()
        preview = render([(row, answer)], store)
        snapshot = full_text(preview, store)
        for view in [preview, snapshot]:
            self.assertEqual(view.count('[S1]'), 1)
            self.assertNotIn('[S2]', view)
            self.assertIn('入选 1 条', view)
            self.assertRegex(view, r'Linkup 摘录：[^\n]*周三。')
            self.assertRegex(view, r'Exa 摘录：[^\n]*周五。')
            self.assertIn('不是独立信源', view)
            self.assertIn('如有矛盾', view)
            self.assertIn('Linkup 生成', view)
            self.assertIn('候选周三', view)
        self.assertIn(url, preview.splitlines())
        self.assertNotIn(alias, preview)
        for original in (alias, '公告的另一标题', 'Linkup 摘录：' + left, 'Exa 摘录：' + right):
            self.assertIn(original, snapshot)

    def test_fair_page_budget_and_late_canonical_match_enriches_a_selected_page(self):
        lp = [page(f'l{i}', f'Linkup 内容{i}') for i in range(3)]
        ex = [page(f'e{i}', f'Exa 内容{i}') for i in range(4)] + [page('l0#section', 'Exa 补充')]
        row, answer = merge(outcome(*lp, answer='候选'), outcome(*ex), 5)
        self.assertEqual([s['url'] for s in row[1]], [p[0] for p in (lp[0], ex[0], lp[1], ex[1], lp[2])])
        self.assertEqual((row[2]['duplicates'], row[2]['omitted']), (1, 2))
        store = TextRegistry()
        preview = render([(row, answer)], store)
        for text in (preview, full_text(preview, store)):
            self.assertIn('Linkup 摘录：Linkup 内容0', text)
            self.assertIn('Exa 摘录：Exa 补充', text)
            self.assertIn('> 候选', text)  # Neither discarded Exa pages nor shared URLs disqualify it.

    def test_grace_is_normal_early_completion_but_timeout_error_and_empty_are_distinct(self):
        right = outcome(page('e', '只有乙方材料'), answer='不能使用 Exa 生成内容')
        for status, ordinary in [('grace_elapsed', 'ok'), ('no_results', 'ok'),
                                 ('timeout', 'partial'), ('quota_or_rate_limit', 'partial')]:
            for check in (False, True):
                with self.subTest(status=status, cross_check=check):
                    text = render([merge(outcome(status=status, answer='不能借用证据'), right, cross_check=check)])
                    self.assertEqual(response_status(text), 'partial' if check else ordinary)
                    self.assertIn('Linkup：' + status, text)
                    self.assertIn('Exa：ok', text)
                    self.assertIn('仅 Exa 提供入选摘录', text)
                    self.assertIn('Exa 摘录：只有乙方材料', text)
                    self.assertNotIn('候选答案', text)
                    self.assertNotIn('已核验', text)
                    if status == 'grace_elapsed':
                        self.assertIn('提前收束', text)
                        for wrong in ('timeout', '超时', '失败'):
                            self.assertNotIn(wrong, text)
        self.assertEqual(response_status(render([merge(outcome(status='no_results'), outcome(status='no_results'))])),
                         'no_results')
        text = render([merge(outcome(status='auth_error'), outcome(status='timeout'))])
        self.assertEqual(response_status(text), 'auth_error')
        self.assertIn('Exa：timeout', text)

    def test_cross_check_requires_selected_substantive_text_from_each_channel(self):
        candidate = outcome(page('one', '来源摘录'), answer='保留候选')
        for ex, limit, omission in [(outcome(page('one', '  ')), 1, '未提供'),
                                    (outcome(page('one', 'https://example.org/')), 1, '仅链接'),
                                    (outcome(page('different', '配额外材料')), 1, '摘录未入选')]:
            with self.subTest(omission=omission):
                text = render([merge(candidate, ex, limit, cross_check=True)])
                self.assertEqual(response_status(text), 'partial')
                for fact in ('交叉核对资料不足', '仅 Linkup 提供入选摘录', omission, '> 保留候选'):
                    self.assertIn(fact, text)

    def test_lower_ranked_real_excerpt_beats_an_empty_preview_hit(self):
        left = outcome(page('l', '甲证据'), answer='候选')
        right = outcome(page('empty', ''), page('e', '乙证据'))
        store = TextRegistry()
        preview = render([merge(left, right, 3, cross_check=True)], store)
        self.assertIn('Linkup 摘录：甲证据', preview)
        self.assertIn('Exa 摘录：乙证据', preview)
        self.assertNotIn(page('empty', '')[0], preview)
        self.assertIn(page('empty', '')[0], full_text(preview, store))

    def test_oversized_shared_page_snapshot_is_rejected_as_a_whole(self):
        left = outcome(page('same', '甲' * 900 + '甲尾'), answer='不留候选')
        right = outcome(page('same', '乙' * 900 + '乙尾'))
        store = TextRegistry()
        with patch('search_preview.MAX_CAPTURE_CHARS', 1700):
            text = render([merge(left, right)], store)
        self.assertIn('未建立搜索摘录快照', text)
        self.assertIn('Linkup 摘录：甲', text)
        self.assertIn('Exa 摘录：乙', text)
        self.assertNotIn('不留候选', text)
        self.assertFalse(store.entries)

    def test_candidate_is_question_local_and_needs_every_linkup_source_and_excerpt(self):
        right = outcome(page('a', 'Exa 同页证据'), answer='不属于 Linkup')
        base = page('a', 'Linkup 证据')
        cases = [outcome(base, answer='不可展示', counts={k: 1}) for k in ('invalid', 'duplicates', 'omitted')]
        cases.extend([outcome(base, answer='不可展示', status='partial'),
                      outcome(page('a', ''), answer='不可展示'),
                      outcome(base, page('b', '落选证据'), answer='不可展示')])
        pairs = [merge(outcome(base, answer='仅本题可见'), right, 1, query='有据')]
        for i, left in enumerate(cases):
            row, answer = merge(left, right, 1, query='缺失' + str(i))
            self.assertFalse(answer)
            self.assertTrue(row[1])  # Losing the answer must not discard the page.
            pairs.append((row, '不可展示'))  # Even if a caller passes an ineligible candidate.
        for pair in pairs[1:]:
            text = render([pairs[0], pair])
            self.assertEqual(text.count('候选答案'), 1)
            self.assertNotIn('不可展示', text)
            self.assertNotIn('仅本题可见', text.split('Q2 查询：')[1])

    def test_cache_failure_only_hides_candidates_whose_own_support_was_not_retained(self):
        pairs = [merge(outcome(page('a', '短证据'), answer='有据候选'), outcome(page('b', '乙' * 900)), query='有据'),
                 merge(outcome(page('c', '甲' * 900), answer='缺据候选'), outcome(page('c', 'Exa 短证据')), query='缺据')]
        text = render(pairs, TextRegistry(max_bytes=10))
        self.assertIn('未建立搜索摘录快照', text)
        self.assertNotIn('text_id', text)
        self.assertIn('有据候选', text)
        self.assertNotIn('缺据候选', text)

    def test_five_questions_keep_both_excerpt_budgets_and_atomic_scoped_snapshot(self):
        pairs = []
        for i in range(5):
            left, right = [], []
            for j in range(4):
                url = f'https://example.org/{i}/{j}/' + 'p' * 1800
                title = '报告' * 150
                if j < 2:
                    left.append((url, title, f'甲{i}{j}' + '甲' * 850 + f'甲尾{i}{j}'))
                right.append((url, title, f'乙{i}{j}' + '乙' * 850 + f'乙尾{i}{j}'))
            pairs.append(merge(outcome(*left, answer=f'答{i}' + '答' * 4500), outcome(*right), 4,
                               query=f'题{i}' + '问' * 400, cross_check=True))
        store = TextRegistry()
        text = render(pairs, store)
        self.assertLessEqual(len(text), PREVIEW_CHARS)
        token = re.search(r'text_id="([\w-]+)"', text)[1]
        self.assertIsNone(store.get('other', token))
        doc = store.get('owner', token)
        self.assertFalse(doc.capture_limited)
        for i, part in enumerate(text.split('\nQ')[1:]):
            for j in range(2):
                self.assertIn(f'Linkup 摘录：甲{i}{j}', part)
                self.assertIn(f'Exa 摘录：乙{i}{j}', part)
                self.assertIn(f'甲尾{i}{j}', doc.text)
            self.assertIn(f'答{i}', part)
            self.assertIn(f'乙尾{i}3', doc.text)
        self.assertEqual(doc.text.count('入选 4 条'), 5)
        self.assertEqual(text.count('首屏 2 条'), 5)
        self.assertEqual(doc.text.count('答案超过留存上限'), 5)
