"""Offline contract/safety tests. No Microsoft or Keychain calls.
source_agent=claude-code; machine=mac-mini
"""
import json
import io
from pathlib import Path
import sys
import time
import unittest
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import onenote as app

CFG = app.check_config({"client_id": "11111111-1111-4111-8111-111111111111",
    "tenant": "organizations", "root": "me", "section_id": "section-A"})


class FakeAuth:
    def token(self):
        return "FAKE-NOT-A-REAL-TOKEN"


class MemoryVault:
    def __init__(self, value):
        self.value = value

    def get(self):
        return self.value

    def put(self, value):
        self.value = value


class ContractTests(unittest.TestCase):
    def test_cross_host_and_out_of_scope_pagination_rejected_before_request(self):
        calls = []
        def transport(method, url, headers, data):
            calls.append(url)
            return json.dumps({"value": [], "@odata.nextLink": "https://evil.example/leak"})
        graph = app.Graph(CFG, FakeAuth(), transport)
        with self.assertRaises(app.UserError):
            graph.collection("notebooks")
        self.assertEqual(len(calls), 1)
        for url in ["https://graph.microsoft.com.evil.example/v1.0/me/onenote/pages",
                    "https://graph.microsoft.com/v1.0/users/other/onenote/pages",
                    "https://graph.microsoft.com/v1.0/me/onenote/%2e%2e/mail",
                    "http://graph.microsoft.com/v1.0/me/onenote/pages"]:
            with self.assertRaises(app.UserError):
                graph.validate_url(url)

    def test_read_has_section_boundary_and_returns_text(self):
        replies = [json.dumps({"title": "模擬面談", "parentSection": {"id": "section-A"}}),
                   '<html><head><title>hidden</title></head><body><p>面談 &amp; 質問</p></body></html>']
        graph = app.Graph(CFG, FakeAuth(), lambda *args: replies.pop(0))
        result = graph.read("page-A")
        self.assertEqual(result["text"], "面談 & 質問")

    def test_read_and_append_reject_wrong_section_without_fetch_or_write(self):
        calls = []
        def transport(*args):
            calls.append(args)
            return json.dumps({"parentSection": {"id": "other"}})
        graph = app.Graph(CFG, FakeAuth(), transport)
        with self.assertRaises(app.UserError):
            graph.read("other-page")
        with self.assertRaises(app.UserError):
            graph.append("other-page", "test")
        self.assertEqual([x[0] for x in calls], ["GET", "GET"])

    def test_create_escapes_text_and_uses_pinned_section(self):
        calls = []
        def transport(*args):
            calls.append(args)
            return json.dumps({"id": "new-page", "title": "test"})
        graph = app.Graph(CFG, FakeAuth(), transport)
        graph.create("<Title>", '<script>alert("x")</script>\n日本語 & more')
        method, url, headers, body = calls[0]
        self.assertEqual(method, "POST")
        self.assertEqual(url, app.GRAPH + "me/onenote/sections/section-A/pages")
        self.assertNotIn("<script>", body.decode())
        self.assertIn("&lt;script&gt;", body.decode())
        self.assertIn("日本語 &amp; more", body.decode())

    def test_append_is_graph_patch_not_replacement(self):
        calls = []
        def transport(method, url, headers, data):
            calls.append((method, url, data))
            return json.dumps({"parentSection": {"id": "section-A"}}) if method == "GET" else ""
        app.Graph(CFG, FakeAuth(), transport).append("page-A", "追記")
        self.assertEqual(calls[1][0], "PATCH")
        self.assertEqual(json.loads(calls[1][2]), [{"target": "body", "action": "append", "content": "<p>追記</p>"}])

    def test_write_failure_not_retried(self):
        calls = []
        def transport(*args):
            calls.append(args)
            raise app.ApiError(503, "serviceUnavailable")
        with self.assertRaises(app.UserError):
            app.Graph(CFG, FakeAuth(), transport).create("test", "body")
        self.assertEqual(len(calls), 1)

    @patch("onenote.time.sleep")
    def test_read_retries_throttle_and_obeys_retry_after(self, sleep):
        calls = []
        def transport(*args):
            calls.append(args)
            if len(calls) == 1:
                raise app.ApiError(429, "throttled", 7)
            return json.dumps({"value": []})
        self.assertEqual(app.Graph(CFG, FakeAuth(), transport).collection("notebooks")["items"], [])
        sleep.assert_called_once_with(7)

    def test_valid_pagination_and_explicit_truncation(self):
        replies = [json.dumps({"value": [{"id": "1"}], "@odata.nextLink": app.GRAPH + "me/onenote/notebooks?$skip=1"}),
                   json.dumps({"value": [{"id": "2"}]})]
        graph = app.Graph(CFG, FakeAuth(), lambda *args: replies.pop(0))
        self.assertEqual(len(graph.collection("notebooks")["items"]), 2)
        graph = app.Graph(CFG, FakeAuth(), lambda *args: json.dumps({"value": [{"id": "1"}, {"id": "2"}]}))
        result = graph.collection("notebooks", limit=1)
        self.assertTrue(result["truncated"])

    def test_refresh_preserves_refresh_token_if_not_rotated(self):
        vault = MemoryVault({"access_token": "OLD", "refresh_token": "FAKE_REFRESH", "expires_at": 0})
        calls = []
        def transport(*args):
            calls.append(args)
            return json.dumps({"access_token": "NEW", "expires_in": 3600})
        auth = app.Auth(CFG, vault, transport)
        self.assertEqual(auth.token(), "NEW")
        self.assertEqual(vault.value["refresh_token"], "FAKE_REFRESH")
        self.assertIn(b"grant_type=refresh_token", calls[0][3])

    def test_valid_cached_token_does_not_contact_identity_service(self):
        vault = MemoryVault({"access_token": "FAKE", "expires_at": time.time() + 1000})
        auth = app.Auth(CFG, vault, lambda *args: self.fail("unexpected network request"))
        self.assertEqual(auth.token(), "FAKE")

    def test_invalid_identity_and_root_rejected(self):
        for change in [{"client_id": "bad"}, {"tenant": "evil/path"}, {"root": "me/../users"},
                       {"notes_scope": "Files.ReadWrite.All"}]:
            with self.assertRaises(app.UserError):
                app.check_config(dict(CFG, **change))

    def test_no_redirect_handler_never_forwards_bearer(self):
        self.assertIsNone(app.NoRedirect().redirect_request(None, None, 302, "", {}, "https://evil.example"))

    def test_pkce_loopback_checks_state_and_does_not_print_code_or_tokens(self):
        output = io.StringIO()
        vault = MemoryVault({})
        statuses = []
        state_data = {}
        class FakeServer:
            def __init__(server, bind, handler):
                self.assertEqual(bind, ("127.0.0.1", 8765))
                server.handler, server.count = handler, 0
            def handle_request(server):
                url = output.getvalue().splitlines()[0].split(": ", 1)[1]
                params = app.parse.parse_qs(app.parse.urlsplit(url).query)
                state_data.update(params)
                handler = object.__new__(server.handler)
                handler.headers = {"Host": "localhost:8765"}
                handler.wfile = io.BytesIO()
                handler.send_error = lambda code, *args: statuses.append(code)
                handler.send_response = lambda code: statuses.append(code)
                handler.send_header = lambda *args: None
                handler.end_headers = lambda: None
                if server.count == 0:
                    handler.path = "/callback?state=WRONG&code=DO_NOT_ACCEPT"
                elif server.count == 1:
                    handler.path = "/callback?" + app.parse.urlencode({"state": params["state"][0], "code": "MOCK_PRIVATE_CODE"})
                else:
                    handler.path = "/done"
                handler.do_GET()
                server.count += 1
            def server_close(server):
                pass
        def transport(method, url, headers, data):
            fields = app.parse.parse_qs(data.decode())
            self.assertEqual(fields["code"], ["MOCK_PRIVATE_CODE"])
            proof = app.base64.urlsafe_b64encode(app.hashlib.sha256(fields["code_verifier"][0].encode()).digest()).rstrip(b"=").decode()
            self.assertEqual(proof, state_data["code_challenge"][0])
            return json.dumps({"access_token": "MOCK_PRIVATE_TOKEN", "expires_in": 3600})
        with patch("onenote.HTTPServer", FakeServer), patch("sys.stdout", output):
            app.Auth(CFG, vault, transport).login_browser()
        self.assertEqual(statuses, [400, 303, 200])
        self.assertEqual(vault.value["access_token"], "MOCK_PRIVATE_TOKEN")
        self.assertNotIn("MOCK_PRIVATE", output.getvalue())


if __name__ == "__main__":
    unittest.main()
