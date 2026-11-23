"""Merge one question's organized results without treating retrieval channels as sources."""
from itertools import zip_longest
import re

if __package__:
    from .retrieval import canonical_source
else:
    from retrieval import canonical_source


PROVIDERS = {'linkup_fast': 'Linkup', 'exa': 'Exa'}
_RETURNED = {'ok', 'partial'}
_URL = re.compile(r'https?://\S+', re.IGNORECASE)


def usable_excerpt(text):
    """A bare link is useful metadata, not an excerpt supporting a claim."""
    return bool(_URL.sub('', text).strip())


def merge_dual(query, outcomes, limit, *, cross_check=False):
    """Return the usual search row and, separately, its eligible Linkup candidate."""
    statuses = {p: outcomes[p].status for p in PROVIDERS}
    lists = [outcomes[p].sources if statuses[p] in _RETURNED else [] for p in PROVIDERS]
    counts = {k: sum(outcomes[p].stats.get(k, 0) for p in PROVIDERS)
              for k in ('duplicates', 'invalid', 'omitted')}

    # Interleave existing per-channel rankings. Attach ALL matches before applying the
    # page limit: even a late alias can supply a conflicting excerpt for a selected page.
    pages = {}
    for pair in zip_longest(*lists):
        for provider, source in zip(PROVIDERS, pair, strict=True):
            if source is not None:
                records = pages.setdefault(canonical_source(source['url']), {})
                counts['duplicates'] += bool(records)
                records[provider] = source
    chosen = list(pages.values())[:limit]
    counts['omitted'] += len(pages) - len(chosen)
    # Prefer the Linkup title/URL for the shared heading when present; the full
    # snapshot retains each channel's returned metadata and excerpt separately.
    sources = [next(records[p] for p in PROVIDERS if p in records) | {'by_provider': records}
               for records in chosen]
    evidence = [p for p in PROVIDERS if any(p in r and usable_excerpt(r[p]['snippet']) for r in chosen)]
    unselected = [p for p in PROVIDERS if p not in evidence and statuses[p] in _RETURNED
                  and any(usable_excerpt(s['snippet']) for s in outcomes[p].sources)]
    incomplete = cross_check and len(evidence) != len(PROVIDERS)
    failures = [s for s in statuses.values() if s not in _RETURNED | {'no_results', 'grace_elapsed'}]
    if len(failures) == len(PROVIDERS):
        error = failures[0]  # No successful retrieval to return as a partial success.
    elif incomplete or failures or 'partial' in statuses.values() or counts['invalid']:
        error = 'partial'
    else:
        error = ''  # ok / no_results, including an ordinary early finish after useful evidence.

    linkup = outcomes['linkup_fast']
    eligible = (linkup.status == 'ok' and 'linkup_fast' in evidence
                and not any(linkup.stats.get(k) for k in ('duplicates', 'invalid', 'omitted'))
                and all(usable_excerpt(s['snippet']) for s in linkup.sources)
                and sum('linkup_fast' in r for r in chosen) == len(linkup.sources))
    counts['dual'] = {'statuses': statuses, 'evidence': evidence, 'unselected': unselected, 'incomplete': incomplete,
                      'linkup_answer_eligible': eligible}
    return (query, sources, counts, error), linkup.answer.strip() if eligible else ''
