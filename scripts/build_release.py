"""Allowlisted plugin ZIP and isolated import of its extracted runtime."""
import hashlib
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import types
import zipfile

ROOT = Path(__file__).resolve().parents[1]
RUNTIME = (
    'plugin.py', 'providers.py', 'runtime.py', 'retrieval.py', 'public_http.py',
    'page.py', 'images.py', 'preview.py', 'originals.py', 'texts.py',
    'automation_auth.py', 'automation_jobs.py', 'automation_retrieval.py', 'weather.py',
)
FILES = (*RUNTIME, '_manifest.json', 'README.md', 'CHANGELOG.md', 'LICENSE', 'config.example.toml')


def build(output):
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(output, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
        for name in FILES:
            path = ROOT/name
            if not path.is_file() or path.is_symlink():
                raise ValueError('Missing or unsafe release member: ' + name)
            info = zipfile.ZipInfo(name, date_time=(2026, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o100644 << 16
            archive.writestr(info, path.read_bytes().replace(b'\r\n', b'\n'))
    verify(output)
    return hashlib.sha256(output.read_bytes()).hexdigest()


def verify(output):
    with tempfile.TemporaryDirectory(prefix='neko-web-release-') as directory:
        root = Path(directory)
        with zipfile.ZipFile(output) as archive:
            if sorted(archive.namelist()) != sorted(FILES):
                raise ValueError('Unexpected release members')
            archive.extractall(root)
        manifest = json.loads((root/'_manifest.json').read_text(encoding='utf-8'))
        if manifest['id'] != 'neko.web':
            raise ValueError('Wrong plugin ID')
        for name in RUNTIME:
            compile((root/name).read_bytes(), name, 'exec')
        name = '_neko_web_release_smoke'
        if name in sys.modules:
            raise RuntimeError('Release smoke namespace already in use')
        package = types.ModuleType(name)
        package.__path__ = [str(root)]
        sys.modules[name] = package
        try:
            spec = importlib.util.spec_from_file_location(name+'.plugin', root/'plugin.py')
            module = importlib.util.module_from_spec(spec)
            sys.modules[spec.name] = module
            spec.loader.exec_module(module)
            if Path(module.__file__).resolve().parent != root.resolve():
                raise RuntimeError('Imported source instead of extracted package')
            if type(module.create_plugin()).__name__ != 'NekoWebPlugin':
                raise RuntimeError('Plugin discovery factory failed')
        finally:
            for key in list(sys.modules):
                if key == name or key.startswith(name+'.'):
                    sys.modules.pop(key, None)


if __name__ == '__main__':
    version = json.loads((ROOT/'_manifest.json').read_text(encoding='utf-8'))['version']
    path = ROOT/'dist'/f'neko-web-{version}.zip'
    print(path.name, 'SHA256', build(path), 'EXTRACTED_SDK_IMPORT_OK')
