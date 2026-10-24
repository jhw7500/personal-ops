"""Cooperating host config writers share one validation and replacement boundary."""
import hashlib
import json
from pathlib import Path
import tempfile
import threading
import unittest

from host_runtime_config import (
    HostRuntimeError, TARGET, TARGET_CONFIG, config_write_lock, save_config_cas,
)


class HostConfigCasTest(unittest.TestCase):
    def test_concurrent_writer_waits_then_rejects_changed_preimage(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'host-runtime.json'
            original = {'version': 1, 'enabled': False, 'targets': {TARGET: TARGET_CONFIG}}
            path.write_text(json.dumps(original))
            path.chmod(0o600)
            expected = hashlib.sha256(path.read_bytes()).hexdigest()
            incoming = dict(original, enabled=True)
            started, finished = threading.Event(), threading.Event()
            errors = []
            def write():
                started.set()
                try:
                    save_config_cas(path, incoming, expected_sha256=expected)
                except HostRuntimeError as exc:
                    errors.append(exc.code)
                finally:
                    finished.set()
            with config_write_lock(path):
                worker = threading.Thread(target=write)
                worker.start()
                self.assertTrue(started.wait(timeout=2))
                self.assertFalse(finished.wait(timeout=.05))
                path.write_text(json.dumps(original, indent=2))
                path.chmod(0o600)
            worker.join(timeout=2)
            self.assertFalse(worker.is_alive())
            self.assertEqual(errors, ['HOST_CONFIG_CHANGED'])
            self.assertFalse(json.loads(path.read_bytes())['enabled'])

    def test_missing_preimage_and_symlink_or_public_lock_fail_closed(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'host-runtime.json'
            incoming = {'version': 1, 'enabled': True, 'targets': {TARGET: TARGET_CONFIG}}
            save_config_cas(path, incoming, expected_sha256=None)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            with self.assertRaisesRegex(HostRuntimeError, 'HOST_CONFIG_CHANGED'):
                save_config_cas(path, incoming, expected_sha256=None)
            lock = path.with_name('.' + path.name + '.lock')
            lock.chmod(0o644)
            with self.assertRaisesRegex(HostRuntimeError, 'UNSAFE_STATE_FILE'):
                save_config_cas(path, incoming, expected_sha256='0'*64)


if __name__ == '__main__':
    unittest.main()
