"""Windows code paths, exercised on any OS with fake crypt32/kernel32/msvcrt.
No real DPAPI, Credential Manager, Microsoft or network calls; fake data only.
source_agent=claude-code; machine=macbook-air
"""
import ctypes
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import onenote
import sync

CFG = onenote.check_config({'tenant': 'organizations', 'client_id': '11111111-1111-4111-8111-111111111111'})
TOKEN = {'access_token': 'FAKE-ACCESS-' + 'a' * 6000, 'refresh_token': 'FAKE-REFRESH-' + 'r' * 3000, 'expires_at': 4102444800.0}


class FakeDpapi:
    """Reversible stand-in for CryptProtectData that also enforces the entropy."""
    def __init__(self):
        self.buffers, self.freed, self.flags = [], [], []
        self.crypt32, self.kernel32 = MagicMock(), MagicMock()
        self.crypt32.CryptProtectData.side_effect = self.protect
        self.crypt32.CryptUnprotectData.side_effect = self.unprotect
        self.kernel32.LocalFree.side_effect = lambda p: self.freed.append(ctypes.cast(p, ctypes.c_void_p).value)

    @staticmethod
    def _read(blob_ptr):
        blob = blob_ptr.contents
        return ctypes.string_at(blob.pbData, blob.cbData)

    def _out(self, blob_ptr, data):
        buf = ctypes.create_string_buffer(data, len(data))
        self.buffers.append(buf)
        blob_ptr.contents.cbData = len(data)
        blob_ptr.contents.pbData = ctypes.cast(buf, ctypes.POINTER(ctypes.c_char))

    def protect(self, data_in, descr, entropy, reserved, prompt, flags, data_out):
        self.flags.append(flags)
        salt = self._read(entropy)
        self._out(data_out, b'DPAPI' + bytes([len(salt) % 256]) + salt + bytes(b ^ 0x5A for b in self._read(data_in)))
        return 1

    def unprotect(self, data_in, descr, entropy, reserved, prompt, flags, data_out):
        self.flags.append(flags)
        raw, salt = self._read(data_in), self._read(entropy)
        if not raw.startswith(b'DPAPI' + bytes([len(salt) % 256]) + salt):
            return 0
        self._out(data_out, bytes(b ^ 0x5A for b in raw[6 + len(salt):]))
        return 1

    def loader(self, name, use_last_error=False):
        return self.crypt32 if name == 'crypt32' else self.kernel32


class DpapiFileStoreTests(unittest.TestCase):
    """auth_store=file on Windows: the token file is DPAPI ciphertext, never plaintext."""
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.fake = FakeDpapi()
        patches = [patch('onenote.sys.platform', 'win32'),
                   patch('onenote.ctypes.WinDLL', side_effect=self.fake.loader, create=True),
                   patch('onenote.ctypes.get_last_error', return_value=13, create=True)]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        self.cache = Path(self.tmp.name) / 'cache'
        self.store = onenote.FileTokenStore(self.cfg())

    def cfg(self, **values):
        return onenote.check_config(dict(CFG, auth_store='file', token_cache_dir=str(self.cache), **values))

    def test_round_trip_large_token_without_plaintext_on_disk(self):
        self.assertTrue(self.store.encrypted)
        self.assertTrue(self.store.path.name.endswith('.dpapi'))
        self.assertFalse(self.store.exists())
        self.store.put(TOKEN)
        self.assertTrue(self.store.exists())
        self.assertEqual(onenote.FileTokenStore(self.cfg()).get(), TOKEN)
        on_disk = self.store.path.read_bytes()
        self.assertNotIn(b'FAKE-ACCESS', on_disk)
        self.assertNotIn(b'refresh_token', on_disk)
        self.assertEqual(sorted(p.name for p in self.cache.iterdir()), [self.store.path.name])

    def test_prompts_are_forbidden_and_native_memory_is_freed(self):
        self.store.put(TOKEN)
        self.assertTrue(self.fake.flags)
        self.assertTrue(all(f & onenote.CRYPTPROTECT_UI_FORBIDDEN for f in self.fake.flags))
        self.assertEqual(len(self.fake.freed), len(self.fake.flags))
        # Output buffers (including decrypted tokens) are wiped before LocalFree.
        self.assertTrue(all(set(buf.raw) <= {0} for buf in self.fake.buffers))

    def test_ciphertext_is_bound_to_tool_and_account(self):
        self.store.put(TOKEN)
        other = onenote.FileTokenStore(self.cfg(client_id='22222222-2222-4222-8222-222222222222'))
        other.path.write_bytes(self.store.path.read_bytes())
        other.path.chmod(0o600)  # the POSIX owner/mode check also runs when tests are not on Windows
        with self.assertRaises(onenote.UserError) as ctx:
            other.get()
        self.assertIn('DPAPI', str(ctx.exception))
        self.assertIn('13', str(ctx.exception))
        self.assertNotIn('FAKE', str(ctx.exception))

    def test_missing_and_delete(self):
        with self.assertRaises(onenote.UserError) as ctx:
            self.store.get()
        self.assertIn('login', str(ctx.exception))
        self.store.put(TOKEN)
        self.store.delete()
        self.assertFalse(self.store.exists())

    def test_selection_stays_explicit(self):
        self.assertIsInstance(onenote.token_store(self.cfg()), onenote.FileTokenStore)
        keychain = onenote.token_store(onenote.check_config(CFG))
        self.assertIsInstance(keychain, onenote.Keychain)
        with self.assertRaises(onenote.UserError) as ctx:
            keychain.exists()
        self.assertIn('auth_store=file', str(ctx.exception))
        self.assertIn('DPAPI', str(ctx.exception))

    def test_dpapi_refuses_other_platforms(self):
        with patch('onenote.sys.platform', 'darwin'):
            with self.assertRaises(onenote.UserError):
                onenote.dpapi(b'x', b'y', protect=True)
            self.assertFalse(onenote.FileTokenStore(self.cfg()).encrypted)


class PlatformSelectionTests(unittest.TestCase):
    def test_machine_name_from_config_or_host(self):
        self.assertEqual(onenote.machine_name(dict(CFG, machine='win-laptop')), 'win-laptop')
        with patch('onenote.platform.node', return_value='DESKTOP-AB12 3.example'):
            self.assertEqual(onenote.machine_name(CFG), 'desktop-ab12-3')
        with patch('onenote.platform.node', return_value=''):
            self.assertEqual(onenote.machine_name(CFG), 'unknown')
        with self.assertRaises(onenote.UserError):
            onenote.check_config(dict(CFG, machine='bad name/..'))

    def test_utf8_stdio_reconfigures_streams(self):
        streams = [io.TextIOWrapper(io.BytesIO(), encoding='cp932') for _ in range(3)]
        with patch.object(sys, 'stdin', streams[0]), patch.object(sys, 'stdout', streams[1]), patch.object(sys, 'stderr', streams[2]):
            onenote.utf8_stdio()
            print('日本語と絵文字🙂', end='')
            sys.stdout.flush()
        self.assertTrue(all(s.encoding == 'utf-8' for s in streams))
        self.assertEqual(streams[1].buffer.getvalue().decode('utf-8'), '日本語と絵文字🙂')


class FakeMsvcrt:
    LK_NBLCK, LK_UNLCK = 2, 0

    def __init__(self):
        self.held, self.calls = False, []

    def locking(self, fd, mode, nbytes):
        self.calls.append(mode)
        if mode == self.LK_NBLCK:
            if self.held:
                raise PermissionError(13, 'locked')
            self.held = True
        elif mode == self.LK_UNLCK:
            self.held = False


class WindowsLockTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.vault = Path(self.tmp.name).resolve() / 'vault'
        self.msvcrt = FakeMsvcrt()
        for p in (patch('sync.fcntl', None), patch('sync.msvcrt', self.msvcrt, create=True)):
            p.start()
            self.addCleanup(p.stop)

    def tearDown(self):
        self.tmp.cleanup()

    def test_second_run_is_refused_and_lock_is_released(self):
        with sync.locked(self.vault):
            with self.assertRaises(onenote.UserError) as ctx:
                with sync.locked(self.vault):
                    pass
            self.assertIn('already running', str(ctx.exception))
        self.assertFalse(self.msvcrt.held)
        with sync.locked(self.vault):
            self.assertTrue(self.msvcrt.held)
        self.assertEqual(self.msvcrt.calls[-1], FakeMsvcrt.LK_UNLCK)

    def test_lock_released_when_sync_body_fails(self):
        with self.assertRaises(RuntimeError):
            with sync.locked(self.vault):
                raise RuntimeError('boom')
        self.assertFalse(self.msvcrt.held)


if __name__ == '__main__':
    unittest.main()
