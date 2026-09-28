import importlib.util
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch


class BackupRunnerTests(unittest.TestCase):
    def setUp(self):
        path = Path(__file__).resolve().parents[1] / 'scripts/run-reviewed-backup.py'
        spec = importlib.util.spec_from_file_location('reviewed_backup_runner', path)
        self.m = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.m)

    def test_existing_nonempty_directory_with_open_permissions_is_not_rewritten(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / 'private'
            path.mkdir()
            (path / 'existing').write_text('preserve', encoding='utf-8')
            with patch.object(self.m.os, 'chmod') as chmod:
                with self.assertRaises(ValueError):
                    self.m.prepare_private_directory(path)
                chmod.assert_not_called()

    def test_only_empty_root_owned_directory_may_have_inherited_acl_removed(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / 'private'
            path.mkdir()
            with patch.object(self.m.os, 'chmod', wraps=self.m.os.chmod) as chmod:
                if self.m.os.name == 'posix' and self.m.os.geteuid() != 0:
                    with self.assertRaises(ValueError):
                        self.m.prepare_private_directory(path)
                    chmod.assert_not_called()
                    return
                # Windows cannot enforce POSIX 0700; the guard must not pretend otherwise.
                if self.m.os.name == 'nt':
                    with self.assertRaises(ValueError):
                        self.m.prepare_private_directory(path)
                else:
                    self.m.prepare_private_directory(path)
                chmod.assert_called_once_with(str(path), 0o700)


if __name__ == '__main__':
    unittest.main()
