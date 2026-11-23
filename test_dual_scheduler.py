"""Dual-provider admission, evidence races and cancellation without any network traffic."""
from contextlib import contextmanager
from unittest import IsolatedAsyncioTestCase
from unittest.mock import patch
import asyncio

from httpx import ReadTimeout

from dual_search import ProviderOutcome, collect_dual
from providers import ProviderError
from runtime import WorkManager


BACKENDS = ('linkup_fast', 'exa')


def hit(snippet='an excerpt', *, url='https://example.org/article', status='ok'):
    return ProviderOutcome([{'url': url, 'snippet': snippet}], {}, '', status)


@contextmanager
def advancing_clock():
    loop = asyncio.get_running_loop()
    now = loop.time
    jump = 0

    def advance(seconds):
        nonlocal jump
        jump += seconds

    with patch.object(loop, 'time', lambda: now() + jump), patch.object(loop, 'slow_callback_duration', float('inf')):
        yield advance


class DualSchedulerTests(IsolatedAsyncioTestCase):
    async def test_either_provider_can_win_once_both_requests_are_in_flight(self):
        for winner in BACKENDS:
            with self.subTest(winner=winner):
                started = {b: asyncio.Event() for b in BACKENDS}
                drained = []
                samples = []
                evidence = hit(status='partial')

                async def search(backend, query, limit, timing):
                    timing['private'] = query
                    if backend == 'exa':
                        timing.update(exa_search_ms=7, exa_queue_ms='unexpected')
                    started[backend].set()
                    if backend == winner:
                        await started[next(b for b in BACKENDS if b != winner)].wait()
                        return evidence
                    try:
                        await asyncio.Event().wait()
                    finally:
                        await asyncio.sleep(0)
                        drained.append(backend)

                row, = await asyncio.wait_for(collect_dual(['secret query'], 5, search, asyncio.Semaphore(2),
                                                          10, grace=.01, observe=samples.append), 2)
                loser, = drained
                self.assertEqual(list(row), list(BACKENDS))
                self.assertIs(row[winner], evidence)
                self.assertEqual(row[loser].status, 'grace_elapsed')
                self.assertEqual(row[loser].sources, [])
                self.assertEqual(len(samples), 2)
                self.assertTrue(all(s.keys() <= {'slot_wait_ms', 'query_ms', 'exa_search_ms'} for s in samples))
                self.assertTrue(all(type(s[k]) is int for s in samples for k in ('query_ms', 'slot_wait_ms')))
                self.assertEqual([s['exa_search_ms'] for s in samples if 'exa_search_ms' in s], [7])

    async def test_no_evidence_or_error_cannot_shorten_a_pending_peer(self):
        first = {
            'empty': ProviderOutcome([], {}, 'unsupported answer', 'ok'),
            'no snippet': hit('  '), 'link only': hit('https://example.org/article'),
            'invalid URL': hit(url='http://127.0.0.1/'),
            'key': ProviderError('fixture', 'config_error'), 'bug': RuntimeError('private diagnostic'),
            'http': ReadTimeout('private timeout detail'),
        }
        release = asyncio.Event()

        async def search(backend, query, limit, timing):
            if backend == 'exa':
                await release.wait()
                return hit(query)
            value = first[query]
            if isinstance(value, Exception):
                raise value
            return value

        # Wait for the first side of *every* pair, then leave its zero-length grace behind.
        samples = []
        def observed(sample):
            samples.append(sample)
            if len(samples) == len(first):
                asyncio.get_running_loop().call_later(.03, release.set)

        rows = await asyncio.wait_for(collect_dual(list(first), 5, search, asyncio.Semaphore(2 * len(first)),
                                                  10, grace=0, observe=observed), 2)
        self.assertEqual([r['linkup_fast'].status for r in rows],
                         ['no_results', 'ok', 'ok', 'ok', 'config_error', 'internal_error', 'timeout'])
        self.assertEqual([r['exa'].sources[0]['snippet'] for r in rows], list(first))
        self.assertEqual(rows[0]['linkup_fast'].answer, '')
        self.assertNotIn('private', repr(rows))

    async def test_completed_peer_ends_grace_early_and_cross_check_ignores_grace(self):
        for cross_check in (False, True):
            with self.subTest(cross_check=cross_check):
                release = asyncio.Event()

                async def search(backend, query, limit, timing):
                    if backend == 'exa':
                        await release.wait()
                    else:
                        asyncio.get_running_loop().call_later(.03, release.set)
                    return hit(backend)

                row, = await asyncio.wait_for(collect_dual(['first'], 1, search, asyncio.Semaphore(2), 10,
                                                          cross_check=cross_check, grace=0 if cross_check else 8), 2)
                self.assertEqual([r.sources[0]['snippet'] for r in row.values()], list(BACKENDS))

    async def test_shared_slot_and_pair_order_deduplicate_without_expanding_the_batch_budget(self):
        slots = asyncio.BoundedSemaphore(2)
        calls = []
        active = peak = 0
        queries = ['one', 'two', 'one', 'three', 'four']

        async def search(backend, query, limit, timing):
            nonlocal active, peak
            calls.append((backend, query, limit))
            active += 1
            peak = max(peak, active)
            try:
                await asyncio.sleep(0)
                return hit(query)
            finally:
                active -= 1

        async with slots:  # An independent search already owns one global slot.
            rows = await asyncio.wait_for(collect_dual(queries, 10, search, slots, 10), 2)
        self.assertEqual(peak, 1)
        self.assertEqual(calls, [(b, q, 4) for q in dict.fromkeys(queries) for b in BACKENDS])
        self.assertEqual([r['exa'].sources[0]['snippet'] for r in rows], queries)

    async def test_request_timeout_is_independent_of_batch_timeout(self):
        async def search(backend, query, limit, timing):
            if backend == 'exa':
                await asyncio.Event().wait()
            return hit()

        row, = await asyncio.wait_for(collect_dual(['q'], 1, search, asyncio.Semaphore(2), .03,
                                                  cross_check=True, total_timeout=10), 2)
        self.assertEqual([r.status for r in row.values()], ['ok', 'timeout'])

    async def test_batch_cutoff_keeps_a_half_finished_pair_and_expires_queued_requests(self):
        started, drained, samples = [], [], []
        occupied = asyncio.Event()

        async def search(backend, query, limit, timing):
            started.append((backend, query))
            if (backend, query) == ('linkup_fast', 'one'):
                return hit('retained')
            if len(started) == 3:
                occupied.set()
            try:
                await asyncio.Event().wait()
            finally:
                drained.append((backend, query))

        # Advance the batch clock only once both slots are occupied and one side is complete.
        with advancing_clock() as advance:
            batch = asyncio.create_task(collect_dual(['one', 'two', 'three'], 1, search, asyncio.Semaphore(2),
                                                     120, grace=60, total_timeout=30, observe=samples.append))
            await asyncio.wait_for(occupied.wait(), 2)
            advance(31)
            rows = await asyncio.wait_for(batch, 2)
        self.assertEqual(started, [('linkup_fast', 'one'), ('exa', 'one'), ('linkup_fast', 'two')])
        self.assertCountEqual(drained, started[1:])
        self.assertEqual(rows[0]['linkup_fast'].sources, hit('retained').sources)
        self.assertEqual([r.status for row in rows for r in row.values()], ['ok'] + ['timeout'] * 5)
        self.assertEqual(sum('query_ms' in s for s in samples), 3)
        self.assertEqual(sum('slot_wait_ms' in s for s in samples), 6)

    async def test_grace_cancellation_does_not_interrupt_http_cleanup_twice(self):
        cleaning, release, closed = asyncio.Event(), asyncio.Event(), asyncio.Event()

        async def search(backend, query, limit, timing):
            if backend == 'exa':
                return hit()
            try:
                await asyncio.Event().wait()
            finally:
                cleaning.set()
                await release.wait()
                closed.set()

        with advancing_clock() as advance:
            batch = asyncio.create_task(collect_dual(['one'], 1, search, asyncio.Semaphore(2), 10, grace=0))
            await asyncio.wait_for(cleaning.wait(), 2)
            advance(96)  # Cross both the request timeout and the default batch deadline during cleanup.
            asyncio.get_running_loop().call_later(.01, release.set)
            row, = await asyncio.wait_for(batch, 2)
        self.assertTrue(closed.is_set())
        self.assertEqual(row['linkup_fast'].status, 'grace_elapsed')

    async def test_reload_drains_active_and_queued_children_before_client_close(self):
        work = WorkManager()
        active, draining, drained = [], [], []
        started, cleaning, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
        slots = asyncio.BoundedSemaphore(2)

        async def search(backend, query, limit, timing):
            if (backend, query) == ('linkup_fast', 'one'):
                return hit('ready before reload')
            active.append((backend, query))
            if len(active) == 2:
                started.set()
            try:
                await asyncio.Event().wait()
            finally:
                draining.append((backend, query))
                if len(draining) == 2:
                    cleaning.set()
                await release.wait()
                drained.append((backend, query))

        async def owner():
            async with work.request():
                return await collect_dual(['one', 'two', 'three'], 2, search, slots, 10, cross_check=True)

        owner_task = asyncio.create_task(owner())
        await asyncio.wait_for(started.wait(), 2)
        close = asyncio.create_task(work.close())
        try:
            await asyncio.wait_for(cleaning.wait(), 2)
            self.assertFalse(close.done())
        finally:
            release.set()
        await asyncio.wait_for(close, 2)
        with self.assertRaises(asyncio.CancelledError):
            await owner_task
        self.assertEqual(active, [('exa', 'one'), ('linkup_fast', 'two')])
        self.assertCountEqual(drained, active)
        async with asyncio.timeout(1), slots, slots:
            self.assertTrue(slots.locked())
