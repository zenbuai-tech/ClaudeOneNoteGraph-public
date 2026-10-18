#!/usr/bin/env python3
"""OneNote Graph CLI for Claude Code. source_agent=claude-code; machine=mac-mini.

Only Python's standard library is required. Never print authentication material.
"""
import argparse
import base64
import ctypes
import hashlib
import hmac
import html
from html.parser import HTMLParser
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import math
import os
from pathlib import Path
import platform
import re
import secrets
import shlex
import stat
import sys
import time
from urllib import error, parse, request
import uuid

APP_NAME = "ClaudeOneNoteGraph"
APP_DIR = Path(__file__).resolve().parent
DEFAULT_CONFIG = APP_DIR / "config.local.json"
DEFAULTS = {"client_id": "", "tenant": "organizations", "root": "me",
            "notes_scope": "Notes.ReadWrite", "section_id": ""}
GRAPH = "https://graph.microsoft.com/v1.0/"
SERVICE = "claude-onenote-graph-v1"
LOGIN_DONE = "認証完了。認証情報をこのツール専用の保存先（statusのauth_storeで確認）に保存しました。"


class UserError(Exception):
    pass


class ApiError(UserError):
    def __init__(self, status, code, retry_after=None):
        self.status, self.code, self.retry_after = status, code, retry_after
        super().__init__("HTTP {} / {}".format(status, code))


class GraphError(UserError):
    def __init__(self, status, message):
        self.status = status
        super().__init__(message)


class NoRedirect(request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def http(method, url, headers=None, data=None):
    """No redirects: bearer tokens never follow a response to another host."""
    req = request.Request(url, data=data, headers=headers or {}, method=method)
    try:
        with request.build_opener(NoRedirect()).open(req, timeout=30) as response:
            body = response.read(16 * 1024 * 1024 + 1)
            if len(body) > 16 * 1024 * 1024:
                raise UserError("応答が16MBを超えました。対象ページを小さくしてください。")
            return body.decode("utf-8")
    except error.HTTPError as exc:
        code = "request_failed"
        try:
            obj = json.loads(exc.read(65536))
            item = obj.get("error", "request_failed")
            candidate = item.get("code", "request_failed") if isinstance(item, dict) else item
            if isinstance(candidate, str) and re.fullmatch(r"[A-Za-z0-9_.-]{1,80}", candidate):
                code = candidate
        except (ValueError, AttributeError):
            pass
        raw_retry = exc.headers.get("Retry-After", "")
        retry = int(raw_retry) if raw_retry.isdigit() else None
        raise ApiError(exc.code, code, retry) from None
    except (error.URLError, TimeoutError, OSError):
        raise UserError("通信に失敗しました。ネットワーク/TLSを確認してください。書き込みは自動再送していません。") from None


def check_config(cfg):
    try:
        uuid.UUID(cfg["client_id"])
    except (ValueError, KeyError, TypeError, AttributeError):
        raise UserError("Microsoft Entraのアプリケーション（クライアント）IDを設定してください。")
    tenant = cfg.get("tenant", "")
    if not isinstance(tenant, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.-]{0,253}", tenant):
        raise UserError("テナントはID、ドメイン、organizations、consumers、commonのいずれかです。")
    root = cfg.get("root", "me")
    if not isinstance(root, str) or not re.fullmatch(r"me|(?:sites|groups|users)/[A-Za-z0-9@.,_-]+", root):
        raise UserError("rootはme、sites/ID、groups/ID、users/IDの形式で指定してください。")
    scope = cfg.get("notes_scope", "Notes.ReadWrite")
    if scope not in ("Notes.ReadWrite", "Notes.ReadWrite.All"):
        raise UserError("notes_scopeが不正です。")
    section = cfg.get("section_id", "")
    if not isinstance(section, str) or len(section) > 1024:
        raise UserError("section_idが不正です。")
    store = cfg.get("auth_store", "keychain")
    if store not in ("keychain", "file"):
        raise UserError("auth_storeはkeychainかfileです。")
    cache = cfg.get("token_cache_dir", "")
    if (not isinstance(cache, str) or len(cache) > 1024 or any(ord(c) < 32 or ord(c) == 127 for c in cache)
            or (cache and not os.path.isabs(cache))):
        raise UserError("token_cache_dirは絶対パスで指定してください（~や変数は展開しません）。")
    machine = cfg.get("machine", "")
    if not isinstance(machine, str) or not re.fullmatch(r"[A-Za-z0-9._-]{0,64}", machine):
        raise UserError("machineは英数字・ピリオド・ハイフン・下線の64文字以内で指定してください。")
    return dict(cfg, root=root, notes_scope=scope, section_id=section, auth_store=store, token_cache_dir=cache)


def machine_name(cfg):
    """Value recorded as `machine` in created pages and mirrored notes."""
    if cfg.get("machine"):
        return cfg["machine"]
    return re.sub(r"[^A-Za-z0-9._-]", "-", platform.node().split(".")[0].lower())[:64] or "unknown"


def load_config(path):
    try:
        return check_config(json.loads(path.read_text(encoding="utf-8")))
    except FileNotFoundError:
        raise UserError("未設定です。READMEのアプリ登録後、configureを実行してください。") from None
    except (ValueError, TypeError):
        raise UserError("設定JSONを確認してください。秘密キーは設定しません。") from None


def save_config(path, cfg):
    if any(SECRET_NAME.search(key) for key in cfg if key not in ENV_NAMES):
        raise UserError("設定にトークン・パスワード・シークレットのような項目があるため保存しません。値は表示していません。")
    path.parent.mkdir(parents=True, exist_ok=True)
    # Public identifiers only. Atomic replacement prevents half-written configs.
    temp = path.with_name(path.name + ".tmp-" + uuid.uuid4().hex)
    try:
        with open(temp, "x", encoding="utf-8") as handle:
            os.chmod(temp, 0o600)
            json.dump(check_config(cfg), handle, ensure_ascii=False, indent=2)
            handle.write("\n")
        os.replace(temp, path)
    finally:
        if temp.exists():
            temp.unlink()


# Local public settings and file token cache. source_agent=claude-code; machine=mac-mini.
# Nothing here reads, exports or migrates Keychain items.
ENV_KEYS = {"ONENOTE_CLIENT_ID": "client_id", "ONENOTE_TENANT": "tenant", "ONENOTE_ROOT": "root",
            "ONENOTE_SECTION_ID": "section_id", "ONENOTE_NOTES_SCOPE": "notes_scope",
            "ONENOTE_AUTH_STORE": "auth_store", "ONENOTE_TOKEN_CACHE_DIR": "token_cache_dir",
            "ONENOTE_MACHINE": "machine"}
ENV_NAMES = {field: name for name, field in ENV_KEYS.items()}
SECRET_NAME = re.compile(r"token|secret|passw|pwd|credential|private|api_?key|assertion|cookie", re.IGNORECASE)
ENV_MAX, TOKEN_MAX = 16 * 1024, 64 * 1024
POSIX = os.name == "posix"
NOFOLLOW, NONBLOCK, BINARY = (getattr(os, name, 0) for name in ("O_NOFOLLOW", "O_NONBLOCK", "O_BINARY"))
CLOUD_FOLDERS = ("cloudstorage", "mobile documents", "dropbox", "google drive", "icloud drive")


def read_limited(fd, limit):
    """Read at most limit+1 bytes so callers can detect oversize input."""
    chunks, size = [], 0
    while size <= limit:
        chunk = os.read(fd, limit + 1 - size)
        if not chunk:
            break
        chunks.append(chunk)
        size += len(chunk)
    return b"".join(chunks)


def parse_env(text):
    """Strict public-only dotenv: KEY=value with optional matching quotes. No expansion or escapes."""
    values = {}
    for number, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        # Messages carry the line number only; values and unknown key names are never echoed.
        where = "--env-file {}行目: ".format(number)
        match = re.fullmatch(r"([A-Za-z_][A-Za-z0-9_]*)=(.*)", line)
        if not match:
            raise UserError(where + "KEY=value の形式ではありません。値は表示していません。")
        key, value = match.groups()
        if key not in ENV_KEYS:
            raise UserError(where + ("トークン・パスワード・シークレットは書けません。" if SECRET_NAME.search(key)
                                     else "未対応のキーです。") + "使えるのは " + ", ".join(ENV_KEYS) + " だけです。")
        if ENV_KEYS[key] in values:
            raise UserError(where + key + " が重複しています。")
        quote = value[:1] if value[:1] in ("'", '"') else ""
        if quote:
            if len(value) < 2 or value[-1] != quote or quote in value[1:-1]:
                raise UserError(where + "引用符が対応していません。値は表示していません。")
            value = value[1:-1]
        elif re.search(r"[\s'\"#]", value):
            raise UserError(where + "空白・引用符・#を含む値は引用符で囲んでください。値は表示していません。")
        if re.search(r"[$`\x00-\x1f\x7f]", value):
            raise UserError(where + "変数展開（$）・コマンド（`）・制御文字は使えません。値は表示していません。")
        values[ENV_KEYS[key]] = value
    return values


def load_env_file(path):
    try:
        fd = os.open(path, os.O_RDONLY | NONBLOCK | BINARY)
    except FileNotFoundError:
        raise UserError("--env-fileのファイルが見つかりません。") from None
    except OSError:
        raise UserError("--env-fileを開けません。パスと権限を確認してください。") from None
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise UserError("--env-fileには通常のファイルを指定してください。")
        data = read_limited(fd, ENV_MAX)
    finally:
        os.close(fd)
    if len(data) > ENV_MAX:
        raise UserError("--env-fileが16KBを超えています。公開設定だけを書いてください。")
    try:
        return parse_env(data.decode("utf-8-sig"))
    except UnicodeError:
        raise UserError("--env-fileはUTF-8で保存してください。値は表示していません。") from None


def effective_config(path, env_file=None, explicit=False):
    """(JSON or None, env values, merged). Precedence: --env-file > JSON > defaults.

    Without --env-file the JSON is required as before. An explicit --config must exist."""
    env = load_env_file(env_file) if env_file else {}
    stored = load_config(path) if path.exists() or explicit or not env else None
    return stored, env, check_config(dict(stored or DEFAULTS, **env))


def env_value(value):
    if re.fullmatch(r"[^\s'\"#$`\x00-\x1f\x7f]*", value):
        return value
    if not re.search(r"[\"$`\x00-\x1f\x7f]", value):
        return '"' + value + '"'
    raise UserError("この値は.envに安全に書けません（\"・$・`を含む）。別のパスを指定してください。")


def create_new(path, data):
    """Exclusive creation: an existing file (or symlink) is never opened, read or replaced."""
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | NOFOLLOW | BINARY, 0o600)
    except FileExistsError:
        raise UserError("出力先が既にあるため作成しません（上書きせず、内容も読んでいません）。別の--outputを指定してください。") from None
    except OSError:
        raise UserError("出力先を作成できません。フォルダの存在と書き込み権限を確認してください。") from None
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
    except BaseException:
        os.unlink(path)  # created exclusively above, so this is only our own partial file
        raise


def token_account(cfg):
    # Same identity for both stores: personal and team targets of one user share a login.
    identity = cfg["tenant"] + ":" + cfg["client_id"] + ":" + cfg["notes_scope"]
    return hashlib.sha256(identity.encode()).hexdigest()


def default_token_dir():
    """Per-user application storage outside the repository."""
    try:
        home = Path.home()
    except (RuntimeError, KeyError):
        home = None
    if sys.platform == "win32":
        local = os.environ.get("LOCALAPPDATA", "")
        base = Path(local) if os.path.isabs(local) else home and home / "AppData" / "Local"
    elif sys.platform == "darwin":
        base = home and home / "Library" / "Application Support"
    else:
        base = home and home / ".local" / "state"
    if not base or not base.is_absolute():
        raise UserError("利用者フォルダを特定できません。--token-cache-dirで絶対パスを指定してください。")
    return base / APP_NAME / "token-cache"


def check_cache_location(directory):
    """Best effort: refuse Git checkouts, this tool's folder and recognisable cloud-sync folders."""
    real = Path(os.path.realpath(str(directory)))
    try:
        home = Path(os.path.realpath(str(Path.home())))
    except (RuntimeError, KeyError):
        home = None
    for place in (real, *real.parents):
        name = place.name.lower()
        if name.startswith("onedrive") or name in CLOUD_FOLDERS:
            raise UserError("認証キャッシュをOneDrive等の同期フォルダに置けません。PCごとのローカルフォルダを指定してください。")
        if place == home:
            break  # a dotfiles repository at the home folder itself is not treated as a checkout
        if place == APP_DIR or (place / ".git").exists():
            raise UserError("認証キャッシュをGitの作業フォルダやこのツールのフォルダに置けません。"
                            "既定の場所を使うか、--token-cache-dirでリポジトリの外を指定してください。")


def check_private(info, kind):
    if POSIX and (info.st_uid != os.getuid() or info.st_mode & 0o077):
        raise UserError("認証キャッシュの{0}の所有者が違うか、他の利用者がアクセスできる権限です。自動では変更していません。"
                        "自分だけの{0}にしてください（{1}）。".format(kind, "700" if kind == "フォルダ" else "600"))


def check_token(value):
    if (not isinstance(value, dict) or not set(value) <= {"access_token", "refresh_token", "expires_at"}
            or not isinstance(value.get("access_token"), str) or not value["access_token"]
            or not isinstance(value.get("refresh_token"), (str, type(None)))
            or type(value.get("expires_at")) not in (int, float) or not math.isfinite(value["expires_at"])):
        raise UserError("このツールの認証キャッシュが不正です。内容は表示していません。loginを再実行してください。")
    return value


class Keychain:
    def __init__(self, cfg):
        self.account = token_account(cfg)

    def exists(self):
        lib, _ = self._native()
        service, account = SERVICE.encode(), self.account.encode()
        status = lib.SecKeychainFindGenericPassword(None, len(service), service,
            len(account), account, None, None, None)
        if status == -25300:
            return False
        self._check(status)
        return True

    def get(self):
        lib, _ = self._native()
        service, account = SERVICE.encode(), self.account.encode()
        length, data = ctypes.c_uint32(), ctypes.c_void_p()
        try:
            status = lib.SecKeychainFindGenericPassword(None, len(service), service,
                len(account), account, ctypes.byref(length), ctypes.byref(data), None)
            self._check(status)
            if not data.value or length.value > 1024 * 1024:
                raise UserError("このツールの認証データが不正です。")
            try:
                value = json.loads(ctypes.string_at(data, length.value).decode("utf-8"))
            except (ValueError, UnicodeError):
                raise UserError("このツールの認証を読み取れません。loginを再実行してください。") from None
            if not isinstance(value, dict):
                raise UserError("このツールの認証データが不正です。")
            return value
        finally:
            if data.value:
                lib.SecKeychainItemFreeContent(None, data)

    @staticmethod
    def _check(status):
        if status == -25300:
            raise UserError("ログインが必要です。loginを実行してください。")
        if status in (-25308, -25315, -25293, -128):
            raise UserError("Keychain access unavailable without user interaction (OSStatus {}). "
                "No password prompt was opened. Keep scheduled sync paused until access is restored.".format(status))
        if status != 0:
            raise UserError("Keychainエラー（OSStatus {}）。認証情報は表示していません。".format(status))

    @staticmethod
    def _native():
        # All operations run in THIS process with interaction disabled. A timeout
        # on a separate security CLI cannot prevent that CLI opening a prompt.
        # Never weaken ACLs, unlock the keychain, or export credentials to disk.
        if sys.platform != "darwin":
            raise UserError("auth_store=keychainはmacOS専用です。WindowsなどではREADMEの手順でauth_store=fileを明示してください"
                            "（WindowsではDPAPIで暗号化して保存します。自動では切り替えません）。")
        lib = ctypes.CDLL("/System/Library/Frameworks/Security.framework/Security")
        core = ctypes.CDLL("/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")
        lib.SecKeychainSetUserInteractionAllowed.argtypes = [ctypes.c_bool]
        lib.SecKeychainSetUserInteractionAllowed.restype = ctypes.c_int32
        interaction_status = lib.SecKeychainSetUserInteractionAllowed(False)
        if interaction_status != 0:
            raise UserError("Cannot disable Keychain prompts; no credential operation was attempted.")
        ptr, uint, char = ctypes.c_void_p, ctypes.c_uint32, ctypes.c_char_p
        lib.SecKeychainFindGenericPassword.argtypes = [ptr, uint, char, uint, char, ptr, ptr, ctypes.POINTER(ptr)]
        lib.SecKeychainFindGenericPassword.restype = ctypes.c_int32
        lib.SecKeychainAddGenericPassword.argtypes = [ptr, uint, char, uint, char, uint, ptr, ctypes.POINTER(ptr)]
        lib.SecKeychainAddGenericPassword.restype = ctypes.c_int32
        lib.SecKeychainItemModifyAttributesAndData.argtypes = [ptr, ptr, uint, ptr]
        lib.SecKeychainItemModifyAttributesAndData.restype = ctypes.c_int32
        lib.SecKeychainItemFreeContent.argtypes = [ptr, ptr]
        lib.SecKeychainItemFreeContent.restype = ctypes.c_int32
        lib.SecKeychainItemDelete.argtypes = [ptr]
        lib.SecKeychainItemDelete.restype = ctypes.c_int32
        core.CFRelease.argtypes = [ptr]
        core.CFRelease.restype = None
        return lib, core

    def put(self, value):
        # Tokens never enter process arguments, shell input, or temporary files.
        lib, core = self._native()
        service, account = SERVICE.encode(), self.account.encode()
        data = json.dumps(value, separators=(",", ":")).encode()
        item = ctypes.c_void_p()
        status = lib.SecKeychainFindGenericPassword(None, len(service), service,
            len(account), account, None, None, ctypes.byref(item))
        try:
            if status == -25300:  # errSecItemNotFound
                status = lib.SecKeychainAddGenericPassword(None, len(service), service,
                    len(account), account, len(data), data, ctypes.byref(item))
            elif status == 0:
                status = lib.SecKeychainItemModifyAttributesAndData(item, None, len(data), data)
            self._check(status)
        finally:
            if item.value:
                core.CFRelease(item)
        if self.get() != value:
            raise UserError("Keychain保存後の確認に失敗しました。loginを再実行してください。")

    def delete(self):
        lib, core = self._native()
        service, account = SERVICE.encode(), self.account.encode()
        item = ctypes.c_void_p()
        try:
            status = lib.SecKeychainFindGenericPassword(None, len(service), service,
                len(account), account, None, None, ctypes.byref(item))
            if status == -25300:
                return
            self._check(status)
            self._check(lib.SecKeychainItemDelete(item))
        finally:
            if item.value:
                core.CFRelease(item)


class DataBlob(ctypes.Structure):
    _fields_ = [("cbData", ctypes.c_uint32), ("pbData", ctypes.POINTER(ctypes.c_char))]


CRYPTPROTECT_UI_FORBIDDEN = 0x1  # never show a prompt


def dpapi(data, entropy, protect):
    """Encrypt/decrypt with Windows DPAPI for the current Windows user. Never shows UI.

    Credential Manager is not used: one entry holds only a few KB, less than the
    access and refresh tokens together. MSAL's own Windows token cache uses DPAPI."""
    crypt32, kernel32 = dpapi_native()
    source = ctypes.create_string_buffer(data, len(data))
    salt = ctypes.create_string_buffer(entropy, len(entropy))
    blob_in = DataBlob(len(data), ctypes.cast(source, ctypes.POINTER(ctypes.c_char)))
    blob_salt = DataBlob(len(entropy), ctypes.cast(salt, ctypes.POINTER(ctypes.c_char)))
    blob_out = DataBlob()
    call = crypt32.CryptProtectData if protect else crypt32.CryptUnprotectData
    ok = call(ctypes.pointer(blob_in), None, ctypes.pointer(blob_salt), None, None,
              CRYPTPROTECT_UI_FORBIDDEN, ctypes.pointer(blob_out))
    ctypes.memset(source, 0, len(data))
    if not ok:
        raise UserError("Windowsの暗号化（DPAPI）に失敗しました（エラー {}）。内容は表示していません。"
                        "別のWindowsユーザーやPCで作った認証は使えないため、loginを再実行してください。"
                        .format(ctypes.get_last_error()))
    try:
        return ctypes.string_at(blob_out.pbData, blob_out.cbData)
    finally:
        ctypes.memset(blob_out.pbData, 0, blob_out.cbData)  # wipe plaintext before freeing
        kernel32.LocalFree(blob_out.pbData)


def dpapi_native():
    if sys.platform != "win32":
        raise UserError("DPAPIによる暗号化はWindows専用です。")
    crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    ptr = ctypes.POINTER(DataBlob)
    for name, second in (("CryptProtectData", ctypes.c_wchar_p), ("CryptUnprotectData", ctypes.c_void_p)):
        fn = getattr(crypt32, name)
        fn.argtypes = [ptr, second, ptr, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint32, ptr]
        fn.restype = ctypes.c_int
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    kernel32.LocalFree.restype = ctypes.c_void_p
    return crypt32, kernel32


class FileTokenStore:
    """Per-user token file, used only when auth_store=file is configured.

    Windows: contents encrypted with DPAPI for the current Windows user (bound to this
    tool and account), so the file is useless to other users, other PCs and copies.
    POSIX: plaintext; directory 0700 / file 0600, owner checked; unsafe existing entries
    are reported, never chmod-ed."""

    def __init__(self, cfg):
        self.dir = Path(cfg["token_cache_dir"]) if cfg["token_cache_dir"] else default_token_dir()
        self.encrypted = sys.platform == "win32"
        self.path = self.dir / ("onenote-token-" + token_account(cfg) + (".dpapi" if self.encrypted else ".json"))
        self.entropy = (SERVICE + ":" + token_account(cfg)).encode()

    def check(self, create=False):
        """Validate the directory; True if it exists. Never reads the token file."""
        check_cache_location(self.dir)
        try:
            info = os.lstat(self.dir)
        except FileNotFoundError:
            if not create:
                return False
            missing, place = [], self.dir
            while not os.path.lexists(place):
                missing.append(place)
                place = place.parent
            for place in reversed(missing):
                os.mkdir(place, 0o700)
            if POSIX:
                os.chmod(self.dir, 0o700)  # only the directory created just now
            info = os.lstat(self.dir)
        if not stat.S_ISDIR(info.st_mode) or getattr(info, "st_reparse_tag", 0):
            raise UserError("認証キャッシュのフォルダがシンボリックリンクか通常のフォルダではないため使いません。変更していません。")
        check_private(info, "フォルダ")
        return True

    @staticmethod
    def _check_file(info, size=True):
        if not stat.S_ISREG(info.st_mode) or getattr(info, "st_reparse_tag", 0):
            raise UserError("認証キャッシュがシンボリックリンクか通常のファイルではないため使いません。変更していません。")
        check_private(info, "ファイル")
        if size and info.st_size > TOKEN_MAX:
            raise UserError("認証キャッシュが64KBを超えています。内容は読んでいません。loginを再実行してください。")

    @staticmethod
    def _os_error(exc):
        return UserError("認証キャッシュを読み書きできません（{}）。内容は表示していません。".format(type(exc).__name__))

    def exists(self):
        try:
            if not self.check():
                return False
            self._check_file(os.lstat(self.path), size=False)
            return True
        except FileNotFoundError:
            return False
        except OSError as exc:
            raise self._os_error(exc) from None

    def get(self):
        missing = UserError("ログインが必要です。loginを実行してください。")
        try:
            if not self.check():
                raise missing
            self._check_file(os.lstat(self.path))
            fd = os.open(self.path, os.O_RDONLY | NOFOLLOW | NONBLOCK | BINARY)
            try:
                self._check_file(os.fstat(fd))  # the opened object, not just the name checked above
                data = read_limited(fd, TOKEN_MAX)
            finally:
                os.close(fd)
        except FileNotFoundError:
            raise missing from None
        except OSError as exc:
            raise self._os_error(exc) from None
        if len(data) > TOKEN_MAX:
            raise UserError("認証キャッシュが64KBを超えています。内容は読んでいません。loginを再実行してください。")
        if self.encrypted:
            data = dpapi(data, self.entropy, protect=False)
        try:
            value = json.loads(data.decode("utf-8"))
        except (ValueError, UnicodeError):
            value = None
        return check_token(value)

    def put(self, value):
        data = json.dumps(check_token(value), separators=(",", ":")).encode()
        if self.encrypted:
            data = dpapi(data, self.entropy, protect=True)
        temp = self.dir / ("." + self.path.name + ".tmp-" + uuid.uuid4().hex)
        try:
            self.check(create=True)
            try:
                self._check_file(os.lstat(self.path), size=False)  # never replace a link or a foreign file
            except FileNotFoundError:
                pass
            fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | NOFOLLOW | BINARY, 0o600)
            try:
                with os.fdopen(fd, "wb") as handle:
                    if POSIX:
                        os.fchmod(handle.fileno(), 0o600)
                    handle.write(data)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temp, self.path)
            finally:
                if os.path.lexists(temp):
                    os.unlink(temp)
        except OSError as exc:
            raise self._os_error(exc) from None
        if self.get() != value:
            raise UserError("認証キャッシュの保存後の確認に失敗しました。loginを再実行してください。")

    def delete(self):
        """Remove only this identity's own regular file."""
        try:
            if not self.check():
                return
            info = os.lstat(self.path)
            if not stat.S_ISREG(info.st_mode) or getattr(info, "st_reparse_tag", 0):
                raise UserError("認証キャッシュがシンボリックリンクか通常のファイルではないため削除しません。")
            os.unlink(self.path)
        except FileNotFoundError:
            return
        except OSError as exc:
            raise self._os_error(exc) from None


def token_store(cfg):
    """Explicit selection only. A Keychain failure never falls back to the plaintext file."""
    return FileTokenStore(cfg) if cfg.get("auth_store") == "file" else Keychain(cfg)


class Auth:
    def __init__(self, cfg, vault=None, transport=http):
        self.cfg, self.transport = cfg, transport
        self.vault = vault if vault is not None else token_store(cfg)
        self.base = "https://login.microsoftonline.com/" + cfg["tenant"] + "/oauth2/v2.0/"
        self.scope = "https://graph.microsoft.com/" + cfg["notes_scope"] + " offline_access"

    def post(self, endpoint, fields):
        data = parse.urlencode(dict(fields, client_id=self.cfg["client_id"])).encode()
        return json.loads(self.transport("POST", self.base + endpoint,
            {"Content-Type": "application/x-www-form-urlencoded"}, data))

    def remember(self, result, old=None):
        if not isinstance(result.get("access_token"), str) or not result["access_token"]:
            raise UserError("認証応答にアクセストークンがありません。")
        saved = {"access_token": result["access_token"],
                 "refresh_token": result.get("refresh_token", (old or {}).get("refresh_token")),
                 "expires_at": time.time() + int(result.get("expires_in", 3600))}
        self.vault.put(saved)
        return saved["access_token"]

    def login(self):
        flow = self.post("devicecode", {"scope": self.scope})
        # Only the user-facing short code is displayed, never device_code/tokens.
        print("MicrosoftログインURL: " + flow["verification_uri"], flush=True)
        print("確認コード: " + flow["user_code"], flush=True)
        print("自分のアカウント・アプリ名・OneNote権限を確認して認証してください。", flush=True)
        interval = max(5, int(flow.get("interval", 5)))
        end = time.monotonic() + int(flow["expires_in"])
        while time.monotonic() < end:
            time.sleep(interval)
            try:
                result = self.post("token", {"grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                                             "device_code": flow["device_code"]})
                self.remember(result)
                print(LOGIN_DONE)
                return
            except ApiError as exc:
                if exc.code == "authorization_pending":
                    continue
                if exc.code == "slow_down":
                    interval += 5
                    continue
                raise UserError("認証を完了できませんでした: " + exc.code) from None
        raise UserError("ログインの有効時間が切れました。loginを再実行してください。")

    def login_browser(self):
        """Authorization code + S256 PKCE; loopback only, no callback logging."""
        verifier = secrets.token_urlsafe(64)
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
        state = secrets.token_urlsafe(32)
        redirect_uri = "http://localhost:8765/callback"
        received = {}

        class Callback(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                parts = parse.urlsplit(self.path)
                if self.headers.get("Host") != "localhost:8765":
                    self.send_error(400)
                    return
                if parts.path == "/callback":
                    params = parse.parse_qs(parts.query)
                    actual_state = params.get("state", [""])[0]
                    if not hmac.compare_digest(actual_state, state):
                        self.send_error(400, "Invalid state")
                        return
                    if params.get("error"):
                        received["error"] = "Microsoftで認証が拒否または中断されました。"
                    elif params.get("code"):
                        received["code"] = params["code"][0]
                    else:
                        self.send_error(400)
                        return
                    # Remove code from address bar before any UI observation.
                    self.send_response(303)
                    self.send_header("Location", "/done")
                    self.send_header("Cache-Control", "no-store")
                    self.send_header("Referrer-Policy", "no-referrer")
                    self.end_headers()
                elif parts.path == "/done":
                    self.send_response(200)
                    self.send_header("Content-Type", "text/plain; charset=utf-8")
                    self.send_header("Cache-Control", "no-store")
                    self.end_headers()
                    self.wfile.write("認証応答を受け取りました。ターミナルの接続結果を確認してください。このタブは閉じられます。".encode())
                    received["done"] = True
                else:
                    self.send_error(404)

        try:
            server = HTTPServer(("127.0.0.1", 8765), Callback)
        except OSError:
            raise UserError("認証用ポート8765を使用できません。他のログイン処理が動いていないか確認してください。") from None
        server.timeout = 1
        fields = {"client_id": self.cfg["client_id"], "response_type": "code",
                  "redirect_uri": redirect_uri, "response_mode": "query", "scope": self.scope,
                  "state": state, "code_challenge": challenge, "code_challenge_method": "S256",
                  "prompt": "select_account"}
        print("MicrosoftログインURL: " + self.base + "authorize?" + parse.urlencode(fields), flush=True)
        print("ブラウザでアプリ名・利用者・OneNote権限を確認してください。", flush=True)
        end = time.monotonic() + 900
        try:
            while not received.get("done") and time.monotonic() < end:
                server.handle_request()
                # Callback can arrive without following redirect; allow a short grace.
                if ("code" in received or "error" in received) and "callback_at" not in received:
                    received["callback_at"] = time.monotonic()
                if received.get("callback_at") and time.monotonic() - received["callback_at"] > 3:
                    break
        finally:
            server.server_close()
        if received.get("error"):
            raise UserError(received["error"])
        if not received.get("code"):
            raise UserError("ログイン待ちが終了しました。loginを再実行してください。")
        result = self.post("token", {"grant_type": "authorization_code", "code": received["code"],
                                   "redirect_uri": redirect_uri, "code_verifier": verifier, "scope": self.scope})
        self.remember(result)
        print(LOGIN_DONE)

    def token(self):
        cached = self.vault.get()
        if cached.get("expires_at", 0) > time.time() + 120:
            return cached["access_token"]
        if not cached.get("refresh_token"):
            raise UserError("再ログインが必要です。loginを実行してください。")
        try:
            result = self.post("token", {"grant_type": "refresh_token",
                "refresh_token": cached["refresh_token"], "scope": self.scope})
        except ApiError as exc:
            raise UserError("認証更新に失敗しました（" + exc.code + "）。loginを再実行してください。") from None
        return self.remember(result, cached)


def segment(value):
    if not value or value in (".", ".."):
        raise UserError("対象IDが空または不正です。")
    return parse.quote(value, safe="")


class PlainText(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts, self.hidden = [], 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style", "head"):
            self.hidden += 1
        if not self.hidden and tag in ("p", "div", "br", "li", "tr", "h1", "h2", "h3"):
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in ("script", "style", "head"):
            self.hidden = max(0, self.hidden - 1)
        if not self.hidden and tag in ("p", "div", "li", "tr"):
            self.parts.append("\n")

    def handle_data(self, data):
        if not self.hidden:
            self.parts.append(data)


def plaintext(content):
    parser = PlainText()
    parser.feed(content)
    return "\n".join(line.strip() for line in "".join(parser.parts).splitlines() if line.strip())


def paragraphs(text):
    if not text.strip():
        raise UserError("本文は空にできません。")
    if len(text.encode("utf-8")) > 200000:
        raise UserError("初期版は本文200KBまでです。")
    return "".join("<p>" + html.escape(line) + "</p>" for line in text.splitlines())


class Graph:
    def __init__(self, cfg, auth=None, transport=http):
        self.cfg, self.auth, self.transport = cfg, auth or Auth(cfg), transport
        self.base = GRAPH + cfg["root"] + "/onenote/"

    def validate_url(self, url):
        p = parse.urlsplit(url)
        base = parse.urlsplit(self.base)
        decoded = parse.unquote(p.path)
        if (p.scheme != "https" or p.netloc != "graph.microsoft.com" or p.fragment
                or not p.path.startswith(base.path) or "\\" in decoded
                or any(x in (".", "..") for x in decoded.split("/"))):
            raise UserError("指定OneNote以外へのリクエストを拒否しました。")

    def call(self, method, path, payload=None, html_body=False):
        url = path if path.startswith("https://") else self.base + path
        self.validate_url(url)
        headers = {"Authorization": "Bearer " + self.auth.token(), "Accept": "application/json"}
        data = None
        if payload is not None:
            headers["Content-Type"] = "text/html; charset=utf-8" if html_body else "application/json"
            data = payload.encode("utf-8") if html_body else json.dumps(payload).encode("utf-8")
        for attempt in range(3):
            try:
                return self.transport(method, url, headers, data)
            except ApiError as exc:
                if method == "GET" and attempt < 2 and exc.status in (429, 502, 503, 504):
                    if exc.retry_after is not None and exc.retry_after > 30:
                        raise UserError("APIが待機を要求しています。{}秒後に再実行してください。".format(exc.retry_after)) from None
                    time.sleep(exc.retry_after if exc.retry_after is not None else 2 ** attempt)
                    continue
                hint = ""
                if exc.status == 401:
                    hint = " loginで再認証してください。"
                elif exc.status == 403:
                    hint = " 利用者のノート権限・アプリ同意・組織ポリシーを確認してください。"
                elif exc.status == 404:
                    hint = " 対象IDとrootを確認してください。"
                if method != "GET":
                    hint += " 重複防止のため自動再送していません。"
                raise GraphError(exc.status, str(exc) + hint) from None

    def collection(self, path, limit=200):
        items, seen = [], set()
        while path and len(items) < limit:
            if path in seen:
                raise UserError("ページングが循環したため停止しました。")
            seen.add(path)
            obj = json.loads(self.call("GET", path))
            items.extend(obj.get("value", []))
            path = obj.get("@odata.nextLink")
        return {"items": items[:limit], "truncated": len(items) > limit or bool(path)}

    def section(self):
        if not self.cfg["section_id"]:
            raise UserError("先にsectionsで保存先を選び、configure --section-idで固定してください。")
        return self.cfg["section_id"]

    def assert_page_section(self, page_id):
        obj = json.loads(self.call("GET", "pages/" + segment(page_id) + "?$expand=parentSection"))
        if obj.get("parentSection", {}).get("id") != self.section():
            raise UserError("このページは設定されたセクションに属していません。")
        return obj

    def read(self, page_id):
        meta = self.assert_page_section(page_id)
        raw = self.call("GET", "pages/" + segment(page_id) + "/content?includeIDs=true")
        return {"id": page_id, "title": meta.get("title"), "links": meta.get("links"),
                "lastModifiedDateTime": meta.get("lastModifiedDateTime"), "html": raw, "text": plaintext(raw)}

    def create(self, title, text):
        if not title.strip() or len(title) > 200:
            raise UserError("タイトルは1〜200文字で指定してください。")
        content = "<!DOCTYPE html><html><head><title>" + html.escape(title) + "</title></head><body>" + paragraphs(text) + "</body></html>"
        result = json.loads(self.call("POST", "sections/" + segment(self.section()) + "/pages", content, True))
        return {k: result.get(k) for k in ("id", "title", "links", "createdDateTime")}

    def append(self, page_id, text):
        self.assert_page_section(page_id)
        body = [{"target": "body", "action": "append", "content": paragraphs(text)}]
        self.call("PATCH", "pages/" + segment(page_id) + "/content", body)
        return {"id": page_id, "appended": True}

    def resolve_notebook(self, link):
        """GetNotebookFromWebUrl. self/sectionsUrl are checked, never requested."""
        obj = json.loads(self.call("POST", "notebooks/GetNotebookFromWebUrl", {"webUrl": link}))
        if not isinstance(obj, dict):
            raise UserError("ノートブックの解決結果を確認できません。")
        notebook_id, name, self_url = obj.get("id"), obj.get("displayName") or obj.get("name"), obj.get("self")
        match = re.fullmatch(r"https://graph\.microsoft\.com/v1\.0/(me|(?:sites|groups|users)/[A-Za-z0-9@.,_-]+)"
                             r"/onenote/notebooks/([^/?#]+)", self_url if isinstance(self_url, str) else "")
        if (not isinstance(notebook_id, str) or not match or parse.unquote(match.group(2)) != notebook_id
                or obj.get("sectionsUrl") != self_url + "/sections"):
            raise UserError("Graphが返したノートブックの参照先が想定外の形式のため停止しました。")
        found, root = match.group(1).lower(), self.cfg["root"].lower()
        if found != root and not (root == "me" and found.startswith("users/")):
            raise UserError("解決したノートブックの場所が指定rootと一致しないため停止しました。サイトID/rootを確認してください。")
        # Later requests stay under this root; me -> users/ID is confirmed by listing below.
        return {"id": notebook_id, "name": name if isinstance(name, str) else ""}

    def notebook_sections(self, notebook_id):
        listing = self.collection("notebooks/" + segment(notebook_id) +
            "/sections?$select=id,displayName&$expand=parentNotebook($select=id)")
        items = []
        for item in listing["items"]:
            if (not isinstance(item, dict) or not isinstance(item.get("id"), str)
                    or (item.get("parentNotebook") or {}).get("id") != notebook_id):
                raise UserError("セクションの所属ノートブックを確認できないため停止しました。")
            items.append({"id": item["id"], "displayName": item.get("displayName")})
        return {"items": items, "truncated": listing["truncated"]}


# Link targets. source_agent=claude-code; machine=mac-mini.
# The pasted link is classified locally and sent to Graph only as data; it is never fetched.
def within(path, scope):
    path, scope = path.lower(), scope.lower().rstrip("/")
    return path == scope or path.startswith(scope + "/")


def link_info(value):
    text = value.strip()
    inner = text[8:] if text[:8].lower() == "onenote:" else text
    try:
        p = parse.urlsplit(inner)
        host = p.hostname or ""
    except ValueError:
        p, host = None, ""
    if (p is None or len(text) > 2048 or any(ord(c) < 32 or ord(c) == 127 for c in text)
            or p.scheme.lower() != "https" or p.netloc.lower() != host
            or not re.fullmatch(r"[a-z0-9-]+\.sharepoint\.com", host)):
        raise UserError("会社アカウントのOneNoteリンク（https://…sharepoint.com/…、onenote:付き可）を指定してください。"
                        "短縮URL・個人用Microsoftアカウント・他のホストは対象外です。")
    path = parse.unquote(p.path)
    lower = path.lower().rstrip("/")
    if re.match(r"/:[a-z]:/", lower):
        raise UserError("共有リンク（/:o:/ 形式など）は解決しません。ノートブックの場所のURL（onenote:付き可）を指定してください。")
    if re.search(r"\.one(?:/|$)", lower) or "wd=target" in p.query.lower() or "page-id=" in p.fragment.lower():
        raise UserError("セクション/ページへのリンクです。ノートブック自体のリンクを指定してください。")
    if ("/forms/" in lower and lower.endswith(".aspx")) or lower.endswith("/_layouts/15/onedrive.aspx"):
        kind = "library"
    elif re.search(r"/_layouts/15/(?:doc|doc2|wopiframe|wopiframe2)\.aspx$", lower):
        kind = "notebook"
    elif "/_layouts/" in lower or re.fullmatch(r"(?:/(?:sites|teams|personal)/[^/]+)?(?:/sitepages(?:/.*)?)?", lower):
        kind = "site"
    else:
        kind = "notebook"
    scope = re.split(r"/(?:forms|_layouts|sitepages)(?:/|$)", path, 1, re.IGNORECASE)[0].rstrip("/")
    folder = parse.parse_qs(p.query).get("id", [""])[0]
    if kind == "library" and folder.startswith("/") and within(folder, scope):
        scope = folder.rstrip("/")
    return {"value": text, "kind": kind, "host": host, "path": path, "scope": scope}


def check_link_root(info, root):
    personal = info["host"].endswith("-my.sharepoint.com")
    if personal and not (root == "me" or root.startswith("users/")):
        raise UserError("個人用（OneDrive）のリンクです。rootがmeの設定（チーム用とは別の--config）で実行してください。")
    if not personal and not root.startswith(("sites/", "groups/")):
        raise UserError("SharePointチームサイトのリンクです。公式Microsoft 365コネクタ等でサイトIDを確認し、"
                        "--site-idで指定してください。このツールはSites権限を追加しません。")
    parts = root[6:].split(",") if root.startswith("sites/") else []
    if len(parts) == 3 and parts[0].lower() != info["host"]:
        raise UserError("サイトIDのホスト名がリンクと一致しません。")


def notebook_candidates(graph, info):
    listing = graph.collection("notebooks?$select=id,displayName,links")
    items = []
    for item in listing["items"]:
        href = ((item.get("links") or {}).get("oneNoteWebUrl") or {}).get("href")
        try:
            found = link_info(href) if isinstance(href, str) else None
        except UserError:
            found = None
        items.append({"id": item.get("id"), "displayName": item.get("displayName"),
                      "webUrl": href if found else None,
                      "in_link_scope": bool(found) and found["host"] == info["host"] and within(found["path"], info["scope"])})
    items.sort(key=lambda x: not x["in_link_scope"])
    return {"items": items, "truncated": listing["truncated"]}


def link_error(exc, cfg):
    if exc.status == 403:
        hint = (" ノートの共有・閲覧権限を確認してください。" if cfg["notes_scope"] == "Notes.ReadWrite.All" else
                " チーム/共有ノートにはNotes.ReadWrite.Allが必要な場合があります。必要性と組織の許可を確認してから、"
                "このconfigでconfigure --notes-scope Notes.ReadWrite.Allとloginを実行してください。")
        return UserError(str(exc) + hint + " 権限は自動で変更していません。")
    if exc.status in (400, 404):
        return UserError(str(exc) + " リンクとroot/サイトIDを確認してください。ノートブックはその場所のURL（onenote:付き可）で指定します。")
    return exc


def link_target(path, cfg, graph, link, section=None, replace=False):
    """Check a link under graph.cfg["root"]. Only an explicit section writes the local config."""
    info = link_info(link)
    root = graph.cfg["root"]
    if info["kind"] != "notebook" and (section or not root.startswith(("sites/", "groups/"))):
        if info["host"].endswith("-my.sharepoint.com"):
            raise UserError("これはノートブックではなく個人用OneDriveの一覧URLです。ノートブックの場所のURLを指定するか、notebooksで一覧を確認してください。")
        raise UserError("これはノートブックではなく、SharePointの{}URLです。ノートブックの場所のURL（onenote:付き可）を指定するか、"
                        "--site-idを付けて--sectionなしで実行するとサイト内のノートブック候補を表示します。".format(
                            "ライブラリ一覧の" if info["kind"] == "library" else "サイトの"))
    check_link_root(info, root)
    try:
        if info["kind"] != "notebook":
            listing = notebook_candidates(graph, info)
            return {"kind": info["kind"], "root": root, "config_path": str(path), "saved": False,
                    "notebooks": listing["items"], "truncated": listing["truncated"],
                    "next": "候補のwebUrlをlinkに渡してセクションを確認してください。in_link_scopeは渡したリンクの範囲内かを示します。"}
        notebook = graph.resolve_notebook(info["value"])
        listing = graph.notebook_sections(notebook["id"])
    except GraphError as exc:
        raise link_error(exc, graph.cfg) from None
    result = {"kind": "notebook", "root": root, "config_path": str(path), "notebook": notebook,
              "sections": listing["items"], "truncated": listing["truncated"], "saved": False}
    if not section:
        result["next"] = "保存するには、同じ--configで--sectionに" + (
            "表示されたセクションIDを付けて再実行してください。一覧が途中で切れているため、名前では指定できません。"
            if listing["truncated"] else "セクションIDか名前を付けて再実行してください。")
        return result
    chosen = [s for s in listing["items"] if s["id"] == section]
    if not chosen and listing["truncated"]:
        # A unique name among the first 200 may still repeat in the unlisted part.
        raise UserError("セクション一覧が200件で打ち切られているため、名前では指定できません（同名が他にないか確認できません）。"
                        "一覧に表示されたセクションのIDで指定してください。")
    chosen = chosen or [s for s in listing["items"] if s["displayName"] == section]
    if len(chosen) != 1:
        raise UserError("同名のセクションが複数あります。セクションIDで指定してください。" if chosen else
                        "指定したセクションがこのノートブックの一覧にありません。")
    if (cfg["root"], cfg["section_id"]) != (root, chosen[0]["id"]):
        if cfg["section_id"] and not replace:
            raise UserError("この設定には別の接続先が保存済みのため変更しません。別の--configを使うか、確認のうえ--replaceを付けてください。")
        save_config(path, dict(cfg, root=root, section_id=chosen[0]["id"]))
    result.update(saved=True, section=chosen[0])
    return result


def emit(value):
    print(json.dumps(value, ensure_ascii=False, indent=2), flush=True)


def body_arg(args):
    # Content comes from stdin, not arbitrary filesystem reads.
    return sys.stdin.read(200001) if args.stdin else args.text


def login_command(env_file):
    script, env_file = str(APP_DIR / "onenote.py"), os.path.abspath(str(env_file))
    if sys.platform == "win32":
        return 'python "{}" --env-file "{}" login'.format(script, env_file)
    return "python3 {} --env-file {} login".format(shlex.quote(script), shlex.quote(env_file))


def init_local(config_path, output, client_id=None, tenant=None, cache_dir=None):
    """Create a NEW public .env selecting auth_store=file. Tokens and existing files are never read."""
    if config_path.exists():
        base = load_config(config_path)
    elif client_id is None or tenant is None:
        raise UserError("既存の設定がありません。--client-idと--tenantを指定するか、.env.exampleをコピーして記入してください。")
    else:
        base = dict(DEFAULTS, token_cache_dir="")
    values = {"client_id": base["client_id"] if client_id is None else client_id,
              "tenant": base["tenant"] if tenant is None else tenant, "notes_scope": base["notes_scope"],
              "auth_store": "file", "token_cache_dir": base["token_cache_dir"] if cache_dir is None
              else os.path.abspath(cache_dir) if cache_dir else ""}
    cfg = check_config(dict(DEFAULTS, **values))
    store = FileTokenStore(cfg)
    store.check()
    if Path(os.path.realpath(os.path.abspath(str(output)))).parent == Path(os.path.realpath(str(store.dir))):
        raise UserError(".envは認証キャッシュとは別のフォルダに作成してください。")
    lines = ["# ClaudeOneNoteGraph の公開設定（init-localで作成）。トークン・パスワード・シークレットは書かない。",
             "# 使い方: python3 onenote.py --env-file このファイル <command>。変数展開はしません。"]
    for name, key in ENV_KEYS.items():
        if key == "token_cache_dir":
            lines.append("# 空欄なら利用者ごとの既定の場所（リポジトリの外）。指定する場合は絶対パス。")
        if key in values:
            lines.append(name + "=" + env_value(cfg[key]))
    lines += ["# 接続先（root・セクション）は --config のJSONから読みます。JSONなしで使う場合だけ次の2行を有効にします。",
              "# ONENOTE_ROOT=me", "# ONENOTE_SECTION_ID="]
    text = "\n".join(lines) + "\n"
    if parse_env(text) != {key: cfg[key] for key in values}:
        raise UserError("作成する.envの確認に失敗したため作成しません。")
    create_new(output, text.encode("utf-8"))
    return {"created": os.path.abspath(str(output)), "auth_store": "file", "next": login_command(output)}


def main():
    parser = argparse.ArgumentParser(description="Claude Code用OneNote読み書きCLI（秘密キー不要）")
    parser.add_argument("--config", type=Path, help="公開設定JSON（既定: このフォルダのconfig.local.json）")
    parser.add_argument("--env-file", type=Path, help="指定した時だけ読む公開設定（ONENOTE_*）。JSONより優先")
    commands = parser.add_subparsers(dest="command", required=True)
    setup = commands.add_parser("configure")
    setup.add_argument("--client-id")
    setup.add_argument("--tenant")
    setup.add_argument("--root")
    setup.add_argument("--section-id")
    setup.add_argument("--notes-scope", choices=["Notes.ReadWrite", "Notes.ReadWrite.All"])
    setup.add_argument("--auth-store", choices=["keychain", "file"], help="fileは明示した時だけ使うファイル保存（WindowsはDPAPIで暗号化、macOSは平文）")
    setup.add_argument("--token-cache-dir", help="auth_store=fileの保存先（空で既定の場所）")
    setup.add_argument("--machine", help="作成ページ・同期メモに記録するパソコン名（例: win-pc）")
    init = commands.add_parser("init-local", help="新しい.env（公開設定・auth_store=file）を作成。上書きしない")
    init.add_argument("--output", type=Path, required=True, help="作成する.envのパス（既存なら作成しない）")
    init.add_argument("--client-id", help="既存の設定がない場合に指定")
    init.add_argument("--tenant", help="既存の設定がない場合に指定")
    init.add_argument("--token-cache-dir", help="省略時は利用者ごとの既定の場所")
    login = commands.add_parser("login")
    login.add_argument("--device-code", action="store_true", help="組織ポリシーで許可された場合のみ")
    for name in ("status", "logout", "notebooks", "sections", "pages", "verify"):
        commands.add_parser(name)
    read = commands.add_parser("read")
    read.add_argument("page_id")
    read.add_argument("--format", choices=["json", "text", "html"], default="json")
    create = commands.add_parser("create")
    create.add_argument("--title", required=True)
    append = commands.add_parser("append")
    append.add_argument("page_id")
    for cmd in (create, append):
        group = cmd.add_mutually_exclusive_group(required=True)
        group.add_argument("--text")
        group.add_argument("--stdin", action="store_true")
    link = commands.add_parser("link", help="リンクから接続先を確認。--section指定時だけ設定へ保存")
    link.add_argument("url", help="ノートブックの場所のURL（onenote:付き可）")
    target = link.add_mutually_exclusive_group()
    target.add_argument("--site-id", help="公式Microsoft 365コネクタ等で確認したSharePointサイトID")
    target.add_argument("--root")
    link.add_argument("--section", help="保存するセクションのIDまたは名前")
    link.add_argument("--replace", action="store_true", help="保存済みの別の接続先を置き換える")
    args = parser.parse_args()
    config_path = args.config or DEFAULT_CONFIG
    if args.command == "init-local":
        if args.env_file:
            raise UserError("init-localは--env-fileを読みません。作成するファイルは--outputで指定してください。")
        emit(init_local(config_path, args.output, args.client_id, args.tenant, args.token_cache_dir))
        return
    if args.command == "configure":
        # Saves only the --config JSON; --env-file is never rewritten or copied into it.
        env = load_env_file(args.env_file) if args.env_file else {}
        stored = load_config(config_path) if config_path.exists() else dict(DEFAULTS)
        if args.token_cache_dir:
            args.token_cache_dir = os.path.abspath(args.token_cache_dir)
        flags = {key: getattr(args, key) for key in ENV_NAMES if getattr(args, key) is not None}
        shadowed = [ENV_NAMES[key] for key in flags if key in env and env[key] != flags[key]]
        if shadowed:
            raise UserError("--env-fileで{}を指定しているため保存しません（実行時は--env-fileが優先されます）。"
                            "--env-fileのファイルを編集してください。".format(", ".join(shadowed)))
        try:
            saved = check_config(dict(stored, **flags))
            cfg = check_config(dict(saved, **env))
        except UserError as exc:
            raise UserError(str(exc) + (" configureは--configのJSONだけに保存し、--env-fileの値はコピーしません。"
                                        if env else "")) from None
        for item in (saved, cfg):
            if item["auth_store"] == "file":
                FileTokenStore(item).check()
        save_config(config_path, saved)
        result = {"configured": True, "config_path": str(config_path), "section_selected": bool(cfg["section_id"]),
                  "auth_store": cfg["auth_store"]}
        if cfg["auth_store"] == "file":
            result["note"] = "auth_store=fileは新しいloginが必要です。Keychainの認証は移行しません。"
        emit(result)
        return
    stored, env, cfg = effective_config(config_path, args.env_file, args.config is not None)
    auth = Auth(cfg)
    if args.command == "login":
        auth.login() if args.device_code else auth.login_browser()
    elif args.command == "status":
        # Presence and location only; token contents are never read here.
        status = {"configured": True, "auth_store": cfg["auth_store"]}
        if isinstance(auth.vault, FileTokenStore):
            status["token_cache_file"] = str(auth.vault.path)
            status["token_cache_encryption"] = "DPAPI" if auth.vault.encrypted else "none"
        status.update(cached_login_present=auth.vault.exists(), machine=machine_name(cfg), root=cfg["root"],
                      section_selected=bool(cfg["section_id"]), notes_scope=cfg["notes_scope"],
                      env_file=str(args.env_file) if args.env_file else None,
                      note="保存済み認証の有無のみ（内容は表示しません）。接続・権限・読み書きの成功はverifyで検証。")
        emit(status)
    elif args.command == "logout":
        auth.vault.delete()
        emit({"local_logout": True, "auth_store": cfg["auth_store"],
              "note": "選択した保存先のこのツールの認証だけを削除しました。Microsoft側のアプリ同意は解除していません。"})
    elif args.command == "link":
        if args.section:
            fixed = [ENV_NAMES[key] for key in ("root", "section_id") if key in env]
            if fixed:
                raise UserError("--env-fileで{}を指定しているため、linkでは保存しません（実行時は--env-fileが優先されます）。"
                                "--sectionなしで一覧を確認し、--env-fileのファイルを編集するか別の--configを使ってください。".format(", ".join(fixed)))
            if stored is None:
                raise UserError("保存先の--configのJSONがありません。先にconfigure --client-id --tenantで作成してください"
                                "（--env-fileの値はJSONへコピーしません）。")
        root = "sites/" + args.site_id if args.site_id is not None else args.root or cfg["root"]
        graph = Graph(check_config(dict(cfg, root=root)), auth)
        emit(link_target(config_path, stored or cfg, graph, args.url, args.section, args.replace))
    else:
        graph = Graph(cfg, auth)
        if args.command == "notebooks":
            emit(graph.collection("notebooks?$select=id,displayName"))
        elif args.command == "sections":
            emit(graph.collection("sections?$select=id,displayName&$expand=parentNotebook($select=id,displayName)"))
        elif args.command == "pages":
            emit(graph.collection("sections/" + segment(graph.section()) + "/pages?$select=id,title,lastModifiedDateTime"))
        elif args.command == "read":
            result = graph.read(args.page_id)
            emit(result) if args.format == "json" else print(result[args.format])
        elif args.command == "create":
            emit(graph.create(args.title, body_arg(args)))
        elif args.command == "append":
            emit(graph.append(args.page_id, body_arg(args)))
        elif args.command == "verify":
            marker = "Claude-OneNote-" + uuid.uuid4().hex
            result = graph.create("Claude接続テスト " + time.strftime("%Y-%m-%d %H:%M"),
                "架空の接続検証です。\nsource_agent: claude-code\nmachine: " + machine_name(cfg) + "\n" + marker)
            emit({"step": "created", **result})
            page_id = result["id"]
            first = graph.read(page_id)
            if marker not in first["text"]:
                raise UserError("作成は成功しましたが、本文の再取得確認が未完了です。作成IDで再確認してください。")
            suffix = marker + "-append"
            graph.append(page_id, "追記の検証です。\n" + suffix)
            emit({"step": "appended", "id": page_id})
            second = graph.read(page_id)
            if suffix not in second["text"]:
                raise UserError("追記は送信済みですが、再取得確認が未完了です。作成IDで再確認してください。")
            emit({"verified": True, "page_id": page_id, "checks": ["create", "read", "append", "read-back"],
                  "note": "テストページは検証証拠として残しています。"})


def utf8_stdio():
    # Windows pipes and consoles default to the ANSI code page (cp932), which
    # garbles or rejects Japanese page text and emoji. Claude Code reads UTF-8.
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        if stream is not None and hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")


if __name__ == "__main__":
    try:
        utf8_stdio()
        main()
    except (UserError, ValueError, KeyError, TypeError, AttributeError) as exc:
        # Unexpected payload errors are deliberately redacted.
        message = str(exc) if isinstance(exc, UserError) else "応答形式を確認できませんでした。認証情報は表示していません。"
        print("エラー: " + message, file=sys.stderr)
        sys.exit(1)
    except OSError as exc:
        print("エラー: ファイル操作に失敗しました（{}）。内容は表示していません。".format(type(exc).__name__), file=sys.stderr)
        sys.exit(1)
    except KeyboardInterrupt:
        print("中断しました。書き込み中だった場合は対象ページを確認してください。", file=sys.stderr)
        sys.exit(130)
