"""Noninteractive Keychain regression tests; fake data only.
source_agent=claude-code; machine=mac-mini
"""
import ctypes
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import MagicMock, patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import onenote

CFG = onenote.check_config({'tenant': 'organizations', 'client_id': '11111111-1111-4111-8111-111111111111'})


class KeychainTests(unittest.TestCase):
    def setUp(self):
        self.lib, self.core = MagicMock(), MagicMock()
        self.lib.SecKeychainSetUserInteractionAllowed.return_value = 0
        self.lib.SecKeychainFindGenericPassword.return_value = -25308
        self.platform = patch('onenote.sys.platform', 'darwin')
        self.loader = patch('onenote.ctypes.CDLL', side_effect=lambda path: self.lib if '/Security.framework/' in path else self.core)
        self.process = patch('subprocess.run', side_effect=AssertionError('Must not launch security CLI'))
        for p in (self.platform, self.loader, self.process):
            p.start()
            self.addCleanup(p.stop)
        self.keychain = onenote.Keychain(CFG)

    def test_all_operations_disable_prompts_before_access_and_fail_closed(self):
        for operation in (self.keychain.get, self.keychain.exists, lambda: self.keychain.put({'fake': 'value'}), self.keychain.delete):
            self.lib.reset_mock()
            with self.assertRaisesRegex(onenote.UserError, 'without user interaction'):
                operation()
            self.assertEqual(self.lib.mock_calls[0][0], 'SecKeychainSetUserInteractionAllowed')
            self.lib.SecKeychainSetUserInteractionAllowed.assert_called_once_with(False)
            self.lib.SecKeychainAddGenericPassword.assert_not_called()
            self.lib.SecKeychainItemModifyAttributesAndData.assert_not_called()
            self.lib.SecKeychainItemDelete.assert_not_called()

    def test_cannot_disable_prompts_means_no_credential_lookup(self):
        self.lib.SecKeychainSetUserInteractionAllowed.return_value = -1
        with self.assertRaisesRegex(onenote.UserError, 'no credential operation'):
            self.keychain.get()
        self.lib.SecKeychainFindGenericPassword.assert_not_called()

    def provide(self, data):
        self.buffer = ctypes.create_string_buffer(data)
        def find(keychain, service_len, service, account_len, account, length, content, item):
            self.assertEqual(service, onenote.SERVICE.encode())
            self.assertEqual(account, self.keychain.account.encode())
            ctypes.cast(length, ctypes.POINTER(ctypes.c_uint32))[0] = len(data)
            ctypes.cast(content, ctypes.POINTER(ctypes.c_void_p))[0] = ctypes.cast(self.buffer, ctypes.c_void_p)
            return 0
        self.lib.SecKeychainFindGenericPassword.side_effect = find

    def test_read_long_utf8_value_and_free_native_memory(self):
        expected = {'fake': 'test-' * 4000, 'label': '\u65e5\u672c\u8a9e'}
        self.provide(json.dumps(expected, ensure_ascii=False).encode())
        self.assertEqual(self.keychain.get(), expected)
        self.lib.SecKeychainItemFreeContent.assert_called_once()

    def test_malformed_data_is_redacted_and_memory_freed(self):
        self.provide(b'FAKE-PRIVATE-NOT-JSON')
        with self.assertRaises(onenote.UserError) as caught:
            self.keychain.get()
        self.assertNotIn('FAKE-PRIVATE', str(caught.exception))
        self.lib.SecKeychainItemFreeContent.assert_called_once()

    def test_missing_item_exists_false_delete_noop_get_requires_login(self):
        self.lib.SecKeychainFindGenericPassword.return_value = -25300
        self.assertFalse(self.keychain.exists())
        self.keychain.delete()
        self.lib.SecKeychainItemDelete.assert_not_called()
        with self.assertRaisesRegex(onenote.UserError, 'login'):
            self.keychain.get()

    def test_update_and_readback_without_subprocess(self):
        expected = {'fake': 'updated'}
        data = json.dumps(expected).encode()
        self.buffer = ctypes.create_string_buffer(data)
        def find(keychain, sl, service, al, account, length, content, item):
            if item is not None:
                ctypes.cast(item, ctypes.POINTER(ctypes.c_void_p))[0] = 123
            else:
                ctypes.cast(length, ctypes.POINTER(ctypes.c_uint32))[0] = len(data)
                ctypes.cast(content, ctypes.POINTER(ctypes.c_void_p))[0] = ctypes.cast(self.buffer, ctypes.c_void_p)
            return 0
        self.lib.SecKeychainFindGenericPassword.side_effect = find
        self.lib.SecKeychainItemModifyAttributesAndData.return_value = 0
        self.keychain.put(expected)
        self.lib.SecKeychainItemModifyAttributesAndData.assert_called_once()
        self.core.CFRelease.assert_called_once()
        self.lib.SecKeychainItemFreeContent.assert_called_once()


if __name__ == '__main__':
    unittest.main()
