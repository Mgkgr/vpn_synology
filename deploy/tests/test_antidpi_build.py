import hashlib
import importlib.util
import io
from pathlib import Path
import tarfile
import tempfile
import unittest
from unittest.mock import patch


SCRIPT = Path(__file__).resolve().parents[1] / 'scripts' / 'build-antidpi-staging.py'


class AntidpiBuildTests(unittest.TestCase):
    def setUp(self):
        spec = importlib.util.spec_from_file_location('antidpi_build', SCRIPT)
        self.module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.module)

    def archive(self, name='runtime.py', content=b'test', link=False):
        buffer = io.BytesIO()
        with tarfile.open(fileobj=buffer, mode='w') as tar:
            item = tarfile.TarInfo(name)
            if link:
                item.type = tarfile.SYMTYPE
                item.linkname = '/etc/shadow'
            else:
                item.size = len(content)
            tar.addfile(item, None if link else io.BytesIO(content))
        return buffer.getvalue()

    def test_only_exact_allowlisted_regular_members_are_returned(self):
        blob = self.archive()
        result = self.module.verify_archive(blob, hashlib.sha256(blob).hexdigest(), {'runtime.py'})
        self.assertEqual(result, {'runtime.py': b'test'})

    def test_tampered_archive_never_reaches_build_inputs(self):
        with self.assertRaises(ValueError):
            self.module.verify_archive(self.archive(), '0' * 64, {'runtime.py'})

    def test_every_copied_runtime_file_is_in_the_build_allowlist(self):
        folder = SCRIPT.parents[1] / 'antidpi'
        ignored = (folder / '.dockerignore').read_text(encoding='utf-8').splitlines()
        for name in ('runtime.py', 'healthcheck.py', 'dns_pins.py'):
            self.assertIn(name, self.module.MEMBERS)
            self.assertIn('!' + name, ignored)

    def test_path_traversal_symlinks_and_missing_files_are_rejected(self):
        for name, link in (('../runtime.py', False), ('/runtime.py', False), ('runtime.py', True), ('other.py', False)):
            blob = self.archive(name, link=link)
            with self.subTest(name=name, link=link), self.assertRaises(ValueError):
                self.module.verify_archive(blob, hashlib.sha256(blob).hexdigest(), {'runtime.py'})

    def test_private_build_directory_contains_nonroot_readable_runtime_files(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch.object(self.module.os, 'open', wraps=self.module.os.open) as opener:
                self.module.materialize_context(root, {'runtime.py': b'test', '.artifacts/LICENSE': b'license'})
            self.assertEqual([call.args[2] for call in opener.call_args_list], [0o644, 0o644])
            self.assertEqual((root / 'runtime.py').read_bytes(), b'test')
            self.assertEqual((root / 'runtime.py').stat().st_mode & 0o444, 0o444)
            self.assertEqual((root / '.artifacts/LICENSE').read_bytes(), b'license')


if __name__ == '__main__':
    unittest.main()
