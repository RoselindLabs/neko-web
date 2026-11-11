"""Keep the tool discovery surface short; workflow details belong to results."""
import ast
from pathlib import Path
from unittest import TestCase


class ToolDescriptionTests(TestCase):
    def setUp(self):
        tree = ast.parse(Path(__file__).with_name('plugin.py').read_text(encoding='utf-8'))
        self.tools = {}
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Name) or node.func.id != 'Tool':
                continue
            name = ast.literal_eval(node.args[0])
            self.tools[name] = {kw.arg: kw.value for kw in node.keywords}

    def test_discovery_descriptions_stay_short(self):
        self.assertEqual(len(self.tools), 7)
        for name, tool in self.tools.items():
            description = ast.literal_eval(tool['description'])
            self.assertLessEqual(len(description), 45, name)
            self.assertNotIn('media_index', description, name)
            self.assertNotIn('cursor', description, name)

    def test_next_cursor_guidance_lives_in_parameter(self):
        parameters = self.tools['neko_web_images']['parameters']
        description = next(kw.value for kw in parameters.elts[1].keywords if kw.arg == 'description')
        self.assertIn('next_cursor', ast.literal_eval(description))

    def test_domains_not_required_for_ordinary_search(self):
        description = ast.literal_eval(self.tools['neko_web_domains']['description'])
        self.assertIn('普通网页搜索不需要', description)

    def test_main_purposes_use_model_visible_natural_keywords(self):
        self.assertIn('联网搜索', ast.literal_eval(self.tools['neko_web_search']['description']))
        self.assertIn('发送原图', ast.literal_eval(self.tools['neko_web_send_image']['description']))
