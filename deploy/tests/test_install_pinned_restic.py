import hashlib
import importlib.util
from pathlib import Path
import tempfile
import unittest


class InstallResticTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        path = Path(__file__).resolve().parents[1] / 'scripts' / 'install-pinned-restic.py'
        cls.module = None
        if path.exists():
            spec = importlib.util.spec_from_file_location('install_restic', path)
            cls.module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(cls.module)

    def artifact(self, directory):
        data = b'\x7fELF\x02\x01' + b'\x00' * 12 + b'\x3e\x00' + b'fixture'
        source = directory / 'downloaded'
        source.write_bytes(data)
        return source, hashlib.sha256(data).hexdigest()

    def test_corrupt_artifact_is_never_executed_or_installed(self):
        self.assertIsNotNone(self.module, 'pinned installer not implemented')
        with tempfile.TemporaryDirectory() as root:
            base = Path(root)
            source, _ = self.artifact(base)
            with self.assertRaisesRegex(self.module.InstallError, 'digest'):
                self.module.install_verified(source, base / 'restic', '0' * 64,
                    version_probe=lambda _: self.fail('unverified code executed'))
            self.assertFalse((base / 'restic').exists())

    def test_existing_file_is_never_overwritten(self):
        self.assertIsNotNone(self.module, 'pinned installer not implemented')
        with tempfile.TemporaryDirectory() as root:
            base = Path(root)
            source, digest = self.artifact(base)
            target = base / 'restic'
            target.write_bytes(b'keep-existing')
            with self.assertRaisesRegex(self.module.InstallError, 'already_exists'):
                self.module.install_verified(source, target, digest,
                    version_probe=lambda _: self.fail('existing install touched'))
            self.assertEqual(target.read_bytes(), b'keep-existing')

    def test_wrong_version_removes_only_own_staging_file(self):
        self.assertIsNotNone(self.module, 'pinned installer not implemented')
        with tempfile.TemporaryDirectory() as root:
            base = Path(root)
            source, digest = self.artifact(base)
            with self.assertRaisesRegex(self.module.InstallError, 'version'):
                self.module.install_verified(source, base / 'restic', digest,
                    version_probe=lambda _: 'restic 0.18.0 compiled with go on linux/amd64')
            self.assertEqual([p.name for p in base.iterdir()], ['downloaded'])

    def test_publishes_the_verified_copy_after_version_probe(self):
        self.assertIsNotNone(self.module, 'pinned installer not implemented')
        with tempfile.TemporaryDirectory() as root:
            base = Path(root)
            source, digest = self.artifact(base)
            data = source.read_bytes()
            def probe(staged):
                self.assertNotEqual(staged, source)
                self.assertEqual(staged.read_bytes(), data)
                source.write_bytes(b'changed-after-copy')
                return 'restic 0.19.1 compiled with go1.26.4 on linux/amd64\n'
            value = self.module.install_verified(source, base / 'restic', digest, version_probe=probe)
            self.assertEqual(value['sha256'], digest)
            self.assertEqual((base / 'restic').read_bytes(), data)
            self.assertEqual({p.name for p in base.iterdir()}, {'downloaded', 'restic'})


if __name__ == '__main__':
    unittest.main()
