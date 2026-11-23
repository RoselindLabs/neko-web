"""Collect one Linkup/Exa pair per distinct question on the caller's shared slots."""
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from time import monotonic_ns
import asyncio
import re

from httpx import TimeoutException

if __package__:
    from .providers import ProviderError
    from .public_http import validate_url
    from .retrieval import MAX_BATCH_RESULTS
else:
    from providers import ProviderError
    from public_http import validate_url
    from retrieval import MAX_BATCH_RESULTS


_BACKENDS = ('linkup_fast', 'exa')
_URL = re.compile(r'https?://\S+', re.IGNORECASE)


@dataclass
class ProviderOutcome:
    sources: list
    stats: dict
    answer: str
    status: str


def _empty(status: str) -> ProviderOutcome:
    return ProviderOutcome([], {'duplicates': 0, 'invalid': 0, 'omitted': 0}, '', status)


def _has_excerpt(outcome: ProviderOutcome) -> bool:
    # A candidate answer or a list of bare links alone cannot cut short the other provider.
    return outcome.status in ('ok', 'partial') and any(
        isinstance(s, dict) and isinstance(s.get('snippet'), str) and _URL.sub('', s['snippet']).strip()
        and not validate_url(s.get('url'))
        for s in outcome.sources
    )


class _Budget:
    """One cancellation timer per worker, even while an HTTP stream is unwinding."""

    def __init__(self, deadline: float):
        self.timer = asyncio.timeout_at(deadline)
        self.reason = 'timeout'

    def shorten(self, deadline: float, reason: str):
        if not self.timer.expired() and deadline < self.timer.when():
            self.timer.reschedule(deadline)
            self.reason = reason


async def collect_dual(
    queries: Sequence[str], count: int, search: Callable[[str, str, int, dict], Awaitable[ProviderOutcome]],
    semaphore: asyncio.Semaphore, timeout: float, *, cross_check: bool = False, grace: float = 1.0,
    total_timeout: float | None = None, observe: Callable[[dict], None] | None = None,
) -> list[dict[str, ProviderOutcome]]:
    """Search normalized queries without retries. Duplicate inputs share the same two outcomes.

    `search(backend, query, limit, timing)` runs under one acquired slot; sources are the
    normalized `search_sources` records. Per-request time starts at admission, whereas the
    shared batch deadline includes queuing. `observe` receives only integer durations in ms.
    """
    if not queries:
        return []
    limit = min(count, MAX_BATCH_RESULTS // len(queries))
    loop = asyncio.get_running_loop()
    deadline = loop.time() + (95 if total_timeout is None else total_timeout)

    async def request(backend: str, query: str, budget: _Budget) -> ProviderOutcome:
        queued = monotonic_ns()
        acquired = None
        timing = {}
        try:
            async with budget.timer:
                async with semaphore:
                    acquired = monotonic_ns()
                    now = loop.time()
                    budget.shorten(now + timeout, 'timeout')
                    if now >= budget.timer.when():
                        return _empty(budget.reason)
                    outcome = await search(backend, query, limit, timing)
                    if budget.timer.expired():
                        return _empty(budget.reason)
                    if outcome.status == 'ok' and not outcome.sources:
                        return ProviderOutcome([], outcome.stats, '', 'no_results')
                    return outcome
        except (TimeoutError, TimeoutException):
            return _empty(budget.reason if budget.timer.expired() else 'timeout')
        except ProviderError as exc:
            return _empty(exc.code)
        except Exception:
            return _empty('internal_error')
        finally:
            if observe is not None:
                end_ns = monotonic_ns()
                sample = {k: timing[k] for k in ('exa_search_ms', 'exa_queue_ms') if type(timing.get(k)) is int}
                sample['slot_wait_ms'] = ((end_ns if acquired is None else acquired) - queued) // 1_000_000
                if acquired is not None:
                    sample['query_ms'] = (end_ns - acquired) // 1_000_000
                observe(sample)

    async def pair(query: str) -> dict[str, ProviderOutcome]:
        budgets = {b: _Budget(deadline) for b in _BACKENDS}
        async with asyncio.TaskGroup() as group:
            tasks = {b: group.create_task(request(b, query, budget)) for b, budget in budgets.items()}
            if not cross_check:
                done, pending = await asyncio.wait(tasks.values(), return_when=asyncio.FIRST_COMPLETED)
                if pending and _has_excerpt(next(iter(done)).result()):
                    for b, task in tasks.items():
                        if task in pending:
                            budgets[b].shorten(loop.time() + grace, 'grace_elapsed')
        # Both workers have unwound (including HTTP and semaphore cleanup) by this point.
        return {b: t.result() for b, t in tasks.items()}

    async with asyncio.TaskGroup() as batch:
        pairs = {q: batch.create_task(pair(q)) for q in dict.fromkeys(queries)}
    return [pairs[q].result() for q in queries]
