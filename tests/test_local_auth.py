"""Offline tests for --env-file, init-local and the explicit file token store.
Fake tokens and temporary folders only: no Microsoft, Keychain, real config or real token cache.
source_agent=claude-code; machine=mac-mini
"""
import io
import json
import os
from pathlib import Path
import stat
import sys
import tempfile
import unittest
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import onenote as app

BASE = {"client_id": "11111111-1111-4111-8111-111111111111", "tenant": "organizations"}
OTHER_CLIENT = "22222222-2222-4222-8222-222222222222"
SITE_ROOT = "sites/contoso.sharepoint.com,33333333-3333-4333-8333-333333333333,44444444-4444-4444-8444-444444444444"
TOKEN = {"access_token": "FAKE-ACCESS-NOT-REAL", "refresh_token": "FAKE-REFRESH-NOT-REAL", "expires_at": 4102444800.0}
posix_only = unittest.skipUnless(app.POSIX, "POSIX permissions and symlinks")


class LocalAuthCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        self.default_cache = self.dir / "default-cache"
        # Never read the real config.local.json, never write ~/Library, never touch Keychain.
        for p in (patch("onenote.DEFAULT_CONFIG", self.dir / "absent-default.json"),
                  patch("onenote.default_token_dir", lambda: self.default_cache),
                  patch("onenote.Keychain", side_effect=AssertionError("Keychain must not be used"))):
            p.start()
            self.addCleanup(p.stop)

    def file_cfg(self, cache=None, **values):
        return app.check_config(dict(BASE, auth_store="file", token_cache_dir=str(cache or self.dir / "cache"), **values))

    def write_env(self, text, name="local.env"):
        path = self.dir / name
        path.write_text(text, encoding="utf-8")
        return path

    def run_cli(self, *argv):
        output = io.StringIO()
        with patch("sys.argv", ["onenote.py", *argv]), patch("sys.stdout", output):
            app.main()
        return json.loads(output.getvalue()), output.getvalue()


class EnvFileTests(LocalAuthCase):
    def test_parses_public_values_comments_quotes_and_bom(self):
        cache = str(self.dir / "cache dir")
        path = self.dir / "a.env"
        path.write_bytes(("﻿# comment\n\nONENOTE_CLIENT_ID=" + BASE["client_id"] + "\r\n"
                          "  ONENOTE_TENANT='organizations'  \nONENOTE_SECTION_ID=1-abc!2-def\n"
                          'ONENOTE_TOKEN_CACHE_DIR="' + cache + '"\nONENOTE_AUTH_STORE=file\n').encode("utf-8"))
        self.assertEqual(app.load_env_file(path), {"client_id": BASE["client_id"], "tenant": "organizations",
                                                   "section_id": "1-abc!2-def", "token_cache_dir": cache,
                                                   "auth_store": "file"})

    def test_rejects_malformed_unknown_duplicate_and_secret_lines_without_echo(self):
        value = "FAKE-SECRET-VALUE"
        for line, reason in [("ONENOTE_CLIENT_SECRET=" + value, "シークレット"), ("ACCESS_TOKEN=" + value, "トークン"),
                             ("ONENOTE_PASSWORD=" + value, "パスワード"), ("MYSTERY_NAME_FAKE=" + value, "未対応"),
                             (value, "KEY=value"), ("export ONENOTE_TENANT=" + value, "KEY=value"),
                             ("ONENOTE_TENANT = " + value, "KEY=value"), ('ONENOTE_TENANT="' + value, "引用符"),
                             ("ONENOTE_TENANT='" + value + '"', "引用符"), ("ONENOTE_TENANT=" + value + " x", "空白"),
                             ("ONENOTE_TOKEN_CACHE_DIR=$HOME/" + value, "変数展開"),
                             ('ONENOTE_TOKEN_CACHE_DIR="/tmp/`' + value + '`"', "コマンド"),
                             ("ONENOTE_TENANT=organizations\nONENOTE_TENANT=" + value, "2行目: ONENOTE_TENANT が重複")]:
            with self.assertRaisesRegex(app.UserError, reason) as caught:
                app.parse_env(line)
            self.assertNotIn(value, str(caught.exception))
            self.assertNotIn("MYSTERY", str(caught.exception))

    def test_rejects_oversize_non_utf8_missing_and_non_regular_files(self):
        big = self.dir / "big.env"
        big.write_bytes(b"# " + b"x" * app.ENV_MAX)
        (self.dir / "utf16.env").write_bytes("ONENOTE_TENANT=x".encode("utf-16"))
        (self.dir / "folder.env").mkdir()
        for name, reason in [("big.env", "16KB"), ("utf16.env", "UTF-8"), ("folder.env", "通常のファイル"),
                             ("missing.env", "見つかりません")]:
            with self.assertRaisesRegex(app.UserError, reason):
                app.load_env_file(self.dir / name)

    def test_env_example_is_placeholder_only_and_selects_file(self):
        values = app.load_env_file(app.APP_DIR / ".env.example")
        self.assertEqual(values["auth_store"], "file")
        self.assertEqual(values["client_id"], "00000000-0000-0000-0000-000000000000")
        self.assertEqual(values["token_cache_dir"], "")
        self.assertNotIn("root", values)
        app.check_config(values)


class PrecedenceTests(LocalAuthCase):
    def test_env_overrides_json_for_runtime_and_default_stays_keychain(self):
        config = self.dir / "config.json"
        app.save_config(config, dict(BASE, section_id="s-json"))
        before = config.read_text(encoding="utf-8")
        cache = self.dir / "cache dir"
        env = self.write_env("ONENOTE_CLIENT_ID=" + OTHER_CLIENT + "\nONENOTE_AUTH_STORE=file\n"
                             'ONENOTE_TOKEN_CACHE_DIR="' + str(cache) + '"\n')
        status, _ = self.run_cli("--config", str(config), "--env-file", str(env), "status")
        identity = app.check_config(dict(BASE, client_id=OTHER_CLIENT))
        self.assertEqual(status["auth_store"], "file")
        self.assertEqual(status["token_cache_file"], str(cache / ("onenote-token-" + app.token_account(identity) + ".json")))
        self.assertTrue(status["section_selected"])  # target still from the JSON
        self.assertFalse(status["cached_login_present"])
        self.assertEqual(config.read_text(encoding="utf-8"), before)

        class FakeKeychain:
            def __init__(self, cfg):
                pass

            def exists(self):
                return False
        with patch("onenote.Keychain", FakeKeychain):
            status, _ = self.run_cli("--config", str(config), "status")
        self.assertEqual(status["auth_store"], "keychain")
        self.assertNotIn("token_cache_file", status)

    def test_env_file_alone_is_sufficient_but_explicit_missing_config_is_not(self):
        env = self.write_env("ONENOTE_CLIENT_ID=" + BASE["client_id"] + "\nONENOTE_TENANT=organizations\n"
                             "ONENOTE_ROOT=" + SITE_ROOT + "\nONENOTE_SECTION_ID=s-env\nONENOTE_AUTH_STORE=file\n")
        status, _ = self.run_cli("--env-file", str(env), "status")
        self.assertEqual((status["auth_store"], status["root"], status["section_selected"]), ("file", SITE_ROOT, True))
        with self.assertRaisesRegex(app.UserError, "未設定"):
            self.run_cli("--config", str(self.dir / "typo.json"), "--env-file", str(env), "status")

    def test_saving_commands_write_only_json_and_never_shadowed_keys(self):
        config = self.dir / "config.json"
        app.save_config(config, dict(BASE, section_id="s-json"))
        env = self.write_env("ONENOTE_SECTION_ID=s-env\nONENOTE_AUTH_STORE=file\n"
                             "ONENOTE_TOKEN_CACHE_DIR=" + str(self.dir / "cache") + "\n")
        before = config.read_text(encoding="utf-8")
        with self.assertRaisesRegex(app.UserError, "ONENOTE_SECTION_ID"):
            self.run_cli("--config", str(config), "--env-file", str(env), "configure", "--section-id", "s-flag")
        with patch("onenote.Graph", side_effect=AssertionError("no request before refusing")):
            with self.assertRaisesRegex(app.UserError, "ONENOTE_SECTION_ID"):
                self.run_cli("--config", str(config), "--env-file", str(env), "link",
                             "https://contoso-my.sharepoint.com/personal/x/Documents/Notebooks/N", "--section", "s-1")
        self.assertEqual(config.read_text(encoding="utf-8"), before)
        result, _ = self.run_cli("--config", str(config), "--env-file", str(env), "configure", "--root", SITE_ROOT)
        self.assertEqual(result["auth_store"], "file")
        saved = json.loads(config.read_text(encoding="utf-8"))
        self.assertEqual((saved["root"], saved["section_id"], saved["auth_store"], saved["token_cache_dir"]),
                         (SITE_ROOT, "s-json", "keychain", ""))  # env values are not copied into the JSON
        config.write_text(json.dumps(dict(BASE, client_secret="FAKE-SECRET-VALUE")), encoding="utf-8")
        with self.assertRaisesRegex(app.UserError, "シークレット") as caught:
            self.run_cli("--config", str(config), "configure", "--section-id", "s-2")
        self.assertNotIn("FAKE-SECRET-VALUE", str(caught.exception))


class BackendTests(LocalAuthCase):
    def test_selection_is_explicit_and_file_store_never_loads_keychain(self):
        with patch("onenote.Keychain", lambda cfg: "keychain-backend"):
            self.assertEqual(app.Auth(app.check_config(BASE)).vault, "keychain-backend")
        with patch("onenote.ctypes.CDLL", side_effect=AssertionError("Security.framework must not load")):
            store = app.Auth(self.file_cfg()).vault
            self.assertIsInstance(store, app.FileTokenStore)
            store.put(TOKEN)
            self.assertTrue(store.exists())
            self.assertEqual(store.get(), TOKEN)
            store.delete()
            self.assertFalse(store.exists())

    def test_keychain_failure_never_falls_back_to_file(self):
        class FailingKeychain:
            def __init__(self, cfg):
                pass

            def get(self):
                raise app.UserError("Keychain access unavailable without user interaction (OSStatus -25293).")
        with patch("onenote.Keychain", FailingKeychain):
            with self.assertRaisesRegex(app.UserError, "-25293"):
                app.Auth(app.check_config(BASE), transport=lambda *a: self.fail("no network")).token()
        self.assertFalse(self.default_cache.exists())
        self.assertFalse((self.dir / "cache").exists())
        with patch("onenote.sys.platform", "win32"), patch("onenote.ctypes.CDLL", side_effect=AssertionError):
            with self.assertRaisesRegex(app.UserError, "auth_store=file") as caught:
                KEYCHAIN(app.check_config(BASE)).exists()
        self.assertIn("自動では切り替えません", str(caught.exception))

    def test_identity_shared_by_personal_and_team_but_split_by_scope_tenant_client(self):
        path = app.FileTokenStore(self.file_cfg(root="me", section_id="s-personal")).path
        self.assertEqual(app.FileTokenStore(self.file_cfg(root=SITE_ROOT, section_id="s-team")).path, path)
        self.assertEqual(path.name, "onenote-token-" + KEYCHAIN(app.check_config(BASE)).account
                         + (".dpapi" if sys.platform == "win32" else ".json"))
        others = {app.FileTokenStore(self.file_cfg(**change)).path for change in
                  ({"notes_scope": "Notes.ReadWrite.All"}, {"tenant": "contoso.onmicrosoft.com"},
                   {"client_id": OTHER_CLIENT})}
        self.assertEqual(len(others), 3)
        self.assertNotIn(path, others)

    def test_default_token_dirs_per_platform(self):
        home = Path("/fake/home")
        cases = [("darwin", {}, home / "Library/Application Support/ClaudeOneNoteGraph/token-cache"),
                 ("win32", {"LOCALAPPDATA": "/fake/local"}, Path("/fake/local/ClaudeOneNoteGraph/token-cache")),
                 ("win32", {"LOCALAPPDATA": ""}, home / "AppData/Local/ClaudeOneNoteGraph/token-cache"),
                 ("win32", {"LOCALAPPDATA": "relative"}, home / "AppData/Local/ClaudeOneNoteGraph/token-cache"),
                 ("linux", {}, home / ".local/state/ClaudeOneNoteGraph/token-cache")]
        for platform, env, expected in cases:
            with patch("onenote.sys.platform", platform), patch.dict(os.environ, env), \
                    patch.object(app.Path, "home", return_value=home):
                self.assertEqual(DEFAULT_TOKEN_DIR(), expected, platform)
        with patch("onenote.sys.platform", "win32"), patch.dict(os.environ, {"LOCALAPPDATA": ""}), \
                patch.object(app.Path, "home", side_effect=RuntimeError):
            with self.assertRaisesRegex(app.UserError, "--token-cache-dir"):
                DEFAULT_TOKEN_DIR()


class FileStoreTests(LocalAuthCase):
    def entries(self, folder):
        return sorted(os.listdir(folder))

    def test_private_lifecycle_with_atomic_replace(self):
        cache = self.dir / "nested" / "cache"
        store = app.FileTokenStore(self.file_cfg(cache))
        self.assertFalse(store.exists())
        store.delete()  # nothing to delete, nothing created
        self.assertFalse(cache.exists())
        store.put(TOKEN)
        if app.POSIX:
            self.assertEqual(stat.S_IMODE(os.stat(cache).st_mode), 0o700)
            self.assertEqual(stat.S_IMODE(os.stat(cache.parent).st_mode), 0o700)
            self.assertEqual(stat.S_IMODE(os.stat(store.path).st_mode), 0o600)
        first = os.stat(store.path).st_ino
        rotated = dict(TOKEN, refresh_token="FAKE-REFRESH-2")
        store.put(rotated)
        self.assertNotEqual(os.stat(store.path).st_ino, first)  # replaced, not rewritten in place
        self.assertEqual(app.FileTokenStore(self.file_cfg(cache)).get(), rotated)
        self.assertEqual(self.entries(cache), [store.path.name])  # no temporary files left
        store.delete()
        self.assertEqual(self.entries(cache), [])
        with self.assertRaisesRegex(app.UserError, "login"):
            store.get()

    def test_malformed_oversize_or_wrong_shape_cache_fails_without_contents(self):
        store = app.FileTokenStore(self.file_cfg())
        store.put(TOKEN)
        cases = [b"FAKE-PRIVATE-NOT-JSON", b"\xffFAKE-PRIVATE", json.dumps(["FAKE-PRIVATE"]).encode(),
                 json.dumps(dict(TOKEN, access_token="")).encode(), json.dumps(dict(TOKEN, expires_at=True)).encode(),
                 json.dumps(dict(TOKEN, id_token="FAKE-PRIVATE")).encode(),
                 b'{"access_token":"FAKE-PRIVATE","expires_at":Infinity}',
                 json.dumps(dict(TOKEN, access_token="FAKE-PRIVATE" * 6000)).encode()]
        for data in cases:
            store.path.write_bytes(data)
            with self.assertRaises(app.UserError) as caught:
                store.get()
            self.assertNotIn("FAKE-PRIVATE", str(caught.exception))
        store.delete()
        with self.assertRaises(app.UserError):
            store.put({"access_token": "FAKE-PRIVATE", "expires_at": "soon"})
        self.assertFalse(store.path.exists())

    @posix_only
    def test_symlinks_and_non_regular_entries_are_refused_untouched(self):
        cache = self.dir / "cache"
        store = app.FileTokenStore(self.file_cfg(cache))
        store.check(create=True)
        target = self.dir / "elsewhere.json"
        target.write_text(json.dumps(dict(TOKEN, access_token="FAKE-PRIVATE-TARGET")), encoding="utf-8")
        os.chmod(target, 0o600)
        os.symlink(target, store.path)
        for operation in (store.exists, store.get, lambda: store.put(TOKEN), store.delete):
            with self.assertRaisesRegex(app.UserError, "シンボリックリンク") as caught:
                operation()
            self.assertNotIn("FAKE-PRIVATE", str(caught.exception))
        self.assertTrue(store.path.is_symlink())
        self.assertIn("FAKE-PRIVATE-TARGET", target.read_text(encoding="utf-8"))
        store.path.unlink()
        os.mkfifo(store.path)  # would block a naive reader
        with self.assertRaisesRegex(app.UserError, "通常のファイル"):
            store.get()
        real = self.dir / "real-cache"
        real.mkdir(mode=0o700)
        os.symlink(real, self.dir / "linked-cache")
        with self.assertRaisesRegex(app.UserError, "シンボリックリンク"):
            app.FileTokenStore(self.file_cfg(self.dir / "linked-cache")).put(TOKEN)
        self.assertEqual(self.entries(real), [])

    @posix_only
    def test_unsafe_posix_modes_are_refused_and_not_chmodded(self):
        shared = self.dir / "shared"
        shared.mkdir()
        os.chmod(shared, 0o755)
        with self.assertRaisesRegex(app.UserError, "700"):
            app.FileTokenStore(self.file_cfg(shared)).put(TOKEN)
        self.assertEqual(stat.S_IMODE(os.stat(shared).st_mode), 0o755)
        self.assertEqual(self.entries(shared), [])
        store = app.FileTokenStore(self.file_cfg())
        store.put(TOKEN)
        os.chmod(store.path, 0o644)
        for operation in (store.exists, store.get, lambda: store.put(TOKEN)):
            with self.assertRaisesRegex(app.UserError, "600"):
                operation()
        self.assertEqual(stat.S_IMODE(os.stat(store.path).st_mode), 0o644)

    def test_cache_location_refuses_checkouts_tool_folder_cloud_and_relative_paths(self):
        repo = self.dir / "some-repo"
        (repo / ".git").mkdir(parents=True)
        tool_cache = app.APP_DIR / "token-cache"
        self.assertFalse(tool_cache.exists())
        for folder, reason in [(repo / "sub" / "cache", "Git"), (tool_cache, "Git"),
                               (self.dir / "OneDrive - Contoso" / "cache", "OneDrive")]:
            with self.assertRaisesRegex(app.UserError, reason):
                app.FileTokenStore(self.file_cfg(folder)).put(TOKEN)
            self.assertFalse(folder.exists())
        with self.assertRaisesRegex(app.UserError, "絶対パス"):
            app.check_config(dict(BASE, auth_store="file", token_cache_dir="relative/cache"))
        config = self.dir / "config.json"
        with self.assertRaisesRegex(app.UserError, "Git"):
            self.run_cli("--config", str(config), "configure", "--client-id", BASE["client_id"], "--tenant",
                         "organizations", "--auth-store", "file", "--token-cache-dir", str(repo / "cache"))
        self.assertFalse(config.exists())


class AuthFlowTests(LocalAuthCase):
    def test_refresh_rotation_is_persisted_in_file_store(self):
        cfg = self.file_cfg()
        app.FileTokenStore(cfg).put({"access_token": "FAKE-OLD", "refresh_token": "FAKE-R1", "expires_at": 0})
        calls = []

        def transport(method, url, headers, data):
            calls.append(data)
            return json.dumps({"access_token": "FAKE-NEW", "refresh_token": "FAKE-R2", "expires_in": 3600})
        self.assertEqual(app.Auth(cfg, transport=transport).token(), "FAKE-NEW")
        self.assertIn(b"refresh_token=FAKE-R1", calls[0])
        self.assertEqual(app.FileTokenStore(cfg).get()["refresh_token"], "FAKE-R2")
        self.assertEqual(app.Auth(cfg, transport=lambda *a: self.fail("cached token expected")).token(), "FAKE-NEW")

    def test_login_with_file_store_is_backend_neutral_and_prints_no_tokens(self):
        cfg = self.file_cfg()
        replies = [{"verification_uri": "https://microsoft.com/devicelogin", "user_code": "ABCD-EFGH",
                    "device_code": "FAKE-DEVICE-PRIVATE", "interval": 5, "expires_in": 900},
                   {"access_token": "FAKE-ACCESS-PRIVATE", "refresh_token": "FAKE-REFRESH-PRIVATE", "expires_in": 3600}]
        output = io.StringIO()
        with patch("onenote.time.sleep"), patch("sys.stdout", output):
            app.Auth(cfg, transport=lambda *a: json.dumps(replies.pop(0))).login()
        self.assertIn(app.LOGIN_DONE, output.getvalue())
        self.assertNotIn("Keychain", output.getvalue())
        self.assertNotIn("PRIVATE", output.getvalue())
        self.assertEqual(app.FileTokenStore(cfg).get()["refresh_token"], "FAKE-REFRESH-PRIVATE")

    def test_status_redacts_and_logout_removes_only_selected_store(self):
        cache = self.dir / "cache"
        config = self.dir / "config.json"
        app.save_config(config, self.file_cfg(cache))
        mine = app.FileTokenStore(self.file_cfg(cache))
        other = app.FileTokenStore(self.file_cfg(cache, notes_scope="Notes.ReadWrite.All"))
        mine.put(TOKEN)
        other.put(TOKEN)
        status, raw = self.run_cli("--config", str(config), "status")
        self.assertEqual((status["auth_store"], status["token_cache_file"], status["cached_login_present"]),
                         ("file", str(mine.path), True))
        self.assertNotIn("FAKE-", raw)
        result, raw = self.run_cli("--config", str(config), "logout")
        self.assertEqual(result["auth_store"], "file")
        self.assertNotIn("FAKE-", raw)
        self.assertFalse(mine.path.exists())
        self.assertEqual(other.get(), TOKEN)
        deleted = []

        class FakeKeychain:
            def __init__(self, cfg):
                pass

            def delete(self):
                deleted.append(True)
        keychain_config = self.dir / "keychain.json"
        app.save_config(keychain_config, dict(BASE, notes_scope="Notes.ReadWrite.All"))
        with patch("onenote.Keychain", FakeKeychain):
            self.run_cli("--config", str(keychain_config), "logout")
        self.assertEqual(deleted, [True])
        self.assertEqual(other.get(), TOKEN)  # the file cache of the same identity is left alone


class InitLocalTests(LocalAuthCase):
    def no_token_access(self):
        for name in ("exists", "get", "put", "delete"):
            p = patch.object(app.FileTokenStore, name, side_effect=AssertionError("init-local must not touch tokens"))
            p.start()
            self.addCleanup(p.stop)

    def test_creates_new_env_from_json_ids_and_runs_with_json_target(self):
        config = self.dir / "config.json"
        app.save_config(config, dict(BASE, root=SITE_ROOT, section_id="s-team", notes_scope="Notes.ReadWrite.All"))
        out, cache = self.dir / "local.env", self.dir / "token cache"
        with patch.object(app.FileTokenStore, "get", side_effect=AssertionError("no token read")), \
                patch.object(app.FileTokenStore, "put", side_effect=AssertionError("no token write")):
            result, raw = self.run_cli("--config", str(config), "init-local", "--output", str(out),
                                       "--token-cache-dir", str(cache))
        self.assertEqual(set(result), {"created", "auth_store", "next"})
        self.assertEqual((result["created"], result["auth_store"]), (str(out), "file"))
        self.assertIn("--env-file", result["next"])
        self.assertTrue(result["next"].endswith(" login"))
        self.assertNotIn(BASE["client_id"], raw)
        if app.POSIX:
            self.assertEqual(stat.S_IMODE(os.stat(out).st_mode), 0o600)
        self.assertEqual(app.load_env_file(out), {"client_id": BASE["client_id"], "tenant": "organizations",
                                                  "notes_scope": "Notes.ReadWrite.All", "auth_store": "file",
                                                  "token_cache_dir": str(cache)})
        self.assertFalse(cache.exists())
        status, _ = self.run_cli("--config", str(config), "--env-file", str(out), "status")
        self.assertEqual((status["auth_store"], status["root"], status["section_selected"]), ("file", SITE_ROOT, True))
        self.assertTrue(status["token_cache_file"].startswith(str(cache)))

    def test_never_overwrites_and_refuses_missing_or_invalid_input(self):
        self.no_token_access()
        existing = self.write_env("EXISTING-FAKE-CONTENT\n", "existing.env")
        with self.assertRaisesRegex(app.UserError, "上書きせず") as caught:
            self.run_cli("init-local", "--output", str(existing), "--client-id", BASE["client_id"], "--tenant", "x")
        self.assertNotIn("EXISTING", str(caught.exception))
        self.assertEqual(existing.read_text(encoding="utf-8"), "EXISTING-FAKE-CONTENT\n")
        if app.POSIX:
            os.symlink(self.dir / "nowhere.env", self.dir / "dangling.env")
            with self.assertRaisesRegex(app.UserError, "上書きせず"):
                self.run_cli("init-local", "--output", str(self.dir / "dangling.env"),
                             "--client-id", BASE["client_id"], "--tenant", "x")
            self.assertFalse((self.dir / "nowhere.env").exists())
        out = self.dir / "new.env"
        for argv, reason in [((), "--client-id"), (("--client-id", "not-a-uuid", "--tenant", "x"), "アプリケーション"),
                             (("--client-id", BASE["client_id"], "--tenant", "bad/tenant"), "テナント")]:
            with self.assertRaisesRegex(app.UserError, reason):
                self.run_cli("init-local", "--output", str(out), *argv)
            self.assertFalse(out.exists())
        with self.assertRaisesRegex(app.UserError, "--output"):
            self.run_cli("--env-file", str(existing), "init-local", "--output", str(out))
        result, _ = self.run_cli("init-local", "--output", str(out), "--client-id", BASE["client_id"],
                                 "--tenant", "organizations")
        self.assertEqual(app.load_env_file(out)["auth_store"], result["auth_store"])


KEYCHAIN, DEFAULT_TOKEN_DIR = app.Keychain, app.default_token_dir  # originals, before setUp patches


if __name__ == "__main__":
    unittest.main()
