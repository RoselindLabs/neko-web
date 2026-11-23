"""Public tool workflow: a compact answer first, all selected evidence available offline."""
from unittest import IsolatedAsyncioTestCase
from unittest.mock import patch
import json
import re

import httpx

from test_support import TestPlugin, host_context


class SearchWorkflowTests(IsolatedAsyncioTestCase):
    async def test_batch_preview_and_keyword_expansion_stay_in_the_original_chat(self):
        plugin = TestPlugin()
        plugin.config.web.search_provider = 'exa'
        plugin.config.web.exa_api_key = 'fixture-key'
        await plugin.on_load()
        self.addAsyncCleanup(plugin.on_unload)
        calls = []

        def exa(request):
            self.assertEqual(str(request.url), 'https://api.exa.ai/search')
            payload = json.loads(request.content)
            query = payload['query']
            calls.append(query)
            self.assertEqual(payload['numResults'], 5)
            self.assertEqual(payload['type'], 'auto')
            marker = {'changes': 'version-6.2.1', 'upgrade': 'migrate-once'}[query]
            return httpx.Response(200, json={'results': [
                {'url': 'https://docs.example/release', 'title': 'Release notes',
                 'publishedDate': '2026-11-15', 'highlights': [marker + '。' + ('文字' * 350)]},
                *[{'url': f'https://s{i}.example/{query}', 'title': f'FAQ {i}',
                   'highlights': [f'{query}-details-{i}: ' + 'evidence ' * 80]} for i in range(2, 6)],
            ]})

        options = {'transport': httpx.MockTransport(exa), 'trust_env': False}
        with patch.object(plugin, '_build_client_options', return_value=options):
            reply = await plugin.handle_search_tool(['changes', 'upgrade'], _host_context=host_context('first'))
            self.assertTrue(reply['success'])
            preview = reply['content']
            self.assertLess(len(preview), 3000)
            self.assertEqual(preview.count('https://docs.example/release'), 1)
            self.assertIn('version-6.2.1', preview)
            self.assertIn('migrate-once', preview)
            self.assertNotIn('upgrade-details-5', preview)
            token = re.search(r'text_id\s*=\s*["\']?([a-zA-Z0-9_-]+)', preview).group(1)
            # Continue using exactly the public ID shown to the planner, not registry internals.
            foreign = await plugin.handle_text(token, keyword='upgrade-details-5', _host_context=host_context('second'))
            self.assertFalse(foreign['success'])
            full = await plugin.handle_text(token, keyword='upgrade-details-5', _host_context=host_context('first'))
            self.assertTrue(full['success'])
            self.assertIn('upgrade-details-5', full['content'])
            self.assertCountEqual(calls, ['changes', 'upgrade'])
