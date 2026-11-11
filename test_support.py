"""Import the real SDK/plugin without installing a hyphenated package."""
import importlib
import io
import sys
import types
from pathlib import Path
from unittest.mock import AsyncMock

from PIL import Image

package = types.ModuleType('neko_web_test')
package.__path__ = [str(Path(__file__).parent)]
sys.modules.setdefault(package.__name__, package)
plugin = importlib.import_module('neko_web_test.plugin')


def host_context(scope='chat', platform='qq', **changes):
    """Host contract without an anchor/account, as in ordinary and Focus calls."""
    return dict(contract='neko.tools.context.v1', session_id=scope, stream_id=scope,
                platform=platform, account_id='', group_id='', user_id='test-user') | changes


class TestPlugin(plugin.NekoWebPlugin):
    @property
    def config(self):
        return self.test_config

    @property
    def ctx(self):
        return self.test_ctx

    def __init__(self):
        super().__init__()
        self.test_config = plugin.NekoWebPluginConfig()
        self.test_config.plugin.enabled = True
        self.test_ctx = types.SimpleNamespace(logger=types.SimpleNamespace(info=lambda *a: None),
                                             send=types.SimpleNamespace(image=AsyncMock(return_value=True)))


def image_bytes(size=(32, 32), color='blue', format='PNG'):
    output = io.BytesIO()
    with Image.new('RGB', size, color) as image:
        image.save(output, format=format)
    return output.getvalue()
