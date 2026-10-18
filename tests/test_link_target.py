"""Offline tests for link targets. No Microsoft, Keychain, or link-host calls.
source_agent=claude-code; machine=mac-mini
"""
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import onenote as app

BASE = {"client_id": "11111111-1111-4111-8111-111111111111", "tenant": "organizations"}
SITE = "contoso.sharepoint.com,22222222-2222-4222-8222-222222222222,33333333-3333-4333-8333-333333333333"
USER = "users/44444444-4444-4444-8444-444444444444"
PERSONAL_LINK = "https://contoso-my.sharepoint.com/personal/taro_contoso_com/Documents/Notebooks/Personal%20Notes"
TEAM_LINK = "onenote:https://contoso.sharepoint.com/sites/sample-team/SiteAssets/Sample%20Team%20Notebook"
LIBRARY_LINK = ("https://contoso.sharepoint.com/sites/sample-team/Shared%20Documents/Forms/AllItems.aspx"
                "?id=%2Fsites%2Fsample%2Dteam%2FShared%20Documents&viewid=55555555%2D5555%2D4555%2D8555%2D555555555555")
SECTIONS = "/onenote/notebooks/1-nb/sections?$select=id,displayName&$expand=parentNotebook($select=id)"


class FakeAuth:
    def token(self):
        return "FAKE-NOT-A-REAL-TOKEN"


class Transport:
    def __init__(self, routes):
        self.routes, self.calls = routes, []

    def __call__(self, method, url, headers, data):
        self.calls.append((method, url, json.loads(data) if data else None))
        if (method, url) not in self.routes:
            raise AssertionError("unexpected request: " + method + " " + url)
        reply = self.routes[(method, url)]
        if isinstance(reply, Exception):
            raise reply
        return json.dumps(reply)


def notebook(location, nb_id="1-nb", name="模擬ノート"):
    self_url = app.GRAPH + location + "/onenote/notebooks/" + nb_id
    return {"id": nb_id, "name": name, "self": self_url, "sectionsUrl": self_url + "/sections"}


def sections(*items, parent="1-nb"):
    return {"value": [{"id": i, "displayName": n, "parentNotebook": {"id": parent}} for i, n in items]}


def routes(root, resolved, listed):
    return {("POST", app.GRAPH + root + "/onenote/notebooks/GetNotebookFromWebUrl"): resolved,
            ("GET", app.GRAPH + root + SECTIONS): listed}


class LinkTargetTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def config(self, name, **values):
        path = self.dir / name
        app.save_config(path, dict(BASE, **values))
        return path, app.load_config(path)

    def run_link(self, path, cfg, transport, link, root=None, **kwargs):
        graph = app.Graph(app.check_config(dict(cfg, root=root or cfg["root"])), FakeAuth(), transport)
        return app.link_target(path, cfg, graph, link, **kwargs)

    def assert_graph_only(self, transport, root):
        for method, url, _ in transport.calls:
            self.assertTrue(url.startswith(app.GRAPH + root + "/onenote/"), url)
            self.assertIn(method, ("GET", "POST"))
            self.assertNotIn("sharepoint.com/", url)

    def test_personal_link_lists_sections_under_me_without_saving(self):
        path, cfg = self.config("personal.json", root="me", section_id="section-A")
        before = path.read_text(encoding="utf-8")
        transport = Transport(routes("me", notebook(USER), sections(("s-1", "日報"), ("s-2", "会議"))))
        result = self.run_link(path, cfg, transport, PERSONAL_LINK)
        self.assertEqual(result["notebook"], {"id": "1-nb", "name": "模擬ノート"})
        self.assertEqual([s["id"] for s in result["sections"]], ["s-1", "s-2"])
        self.assertFalse(result["saved"])
        self.assertEqual(path.read_text(encoding="utf-8"), before)
        self.assertEqual(transport.calls[0][2], {"webUrl": PERSONAL_LINK})
        self.assert_graph_only(transport, "me")

    def test_team_link_saves_separate_config_and_keeps_personal(self):
        personal, _ = self.config("personal.json", root="me", section_id="section-A")
        personal_before = personal.read_text(encoding="utf-8")
        team, team_cfg = self.config("team.json")
        root = "sites/" + SITE
        transport = Transport(routes(root, notebook(root), sections(("t-1", "共有メモ"), ("t-2", "検証用"))))
        result = self.run_link(team, team_cfg, transport, TEAM_LINK, root=root, section="検証用")
        self.assertTrue(result["saved"])
        self.assertEqual(result["section"], {"id": "t-2", "displayName": "検証用"})
        saved = app.load_config(team)
        self.assertEqual((saved["root"], saved["section_id"]), (root, "t-2"))
        self.assertEqual(personal.read_text(encoding="utf-8"), personal_before)
        self.assertEqual(app.Graph(saved, FakeAuth()).base, app.GRAPH + root + "/onenote/")
        self.assertEqual(app.Graph(app.load_config(personal), FakeAuth()).base, app.GRAPH + "me/onenote/")
        self.assert_graph_only(transport, root)

    def test_existing_target_not_overwritten_without_replace(self):
        path, cfg = self.config("personal.json", root="me", section_id="section-A")
        before = path.read_text(encoding="utf-8")
        transport = Transport(routes("me", notebook(USER), sections(("s-2", "会議"))))
        with self.assertRaisesRegex(app.UserError, "--replace"):
            self.run_link(path, cfg, transport, PERSONAL_LINK, section="s-2")
        self.assertEqual(path.read_text(encoding="utf-8"), before)
        self.assertTrue(self.run_link(path, cfg, transport, PERSONAL_LINK, section="s-2", replace=True)["saved"])
        self.assertEqual(app.load_config(path)["section_id"], "s-2")

    def test_multiple_candidates_need_explicit_section(self):
        path, cfg = self.config("personal.json", root="me")
        transport = Transport(routes("me", notebook(USER), sections(("s-1", "メモ"), ("s-2", "メモ"))))
        self.assertIn("--section", self.run_link(path, cfg, transport, PERSONAL_LINK)["next"])
        with self.assertRaisesRegex(app.UserError, "セクションID"):
            self.run_link(path, cfg, transport, PERSONAL_LINK, section="メモ")
        with self.assertRaisesRegex(app.UserError, "ありません"):
            self.run_link(path, cfg, transport, PERSONAL_LINK, section="other")
        self.assertEqual(app.load_config(path)["section_id"], "")
        self.assertTrue(self.run_link(path, cfg, transport, PERSONAL_LINK, section="s-2")["saved"])
        self.assertEqual(app.load_config(path)["section_id"], "s-2")

    def test_truncated_listing_accepts_only_listed_section_id(self):
        path, cfg = self.config("personal.json", root="me")
        before = path.read_text(encoding="utf-8")
        listed = sections(*[("s-%d" % i, "日報" if i in (1, 201) else "メモ%d" % i) for i in range(1, 202)])
        transport = Transport(routes("me", notebook(USER), listed))
        result = self.run_link(path, cfg, transport, PERSONAL_LINK)
        self.assertTrue(result["truncated"])
        self.assertEqual(len(result["sections"]), 200)
        self.assertIn("名前では指定できません", result["next"])
        for section in ("日報", "メモ2", "s-201"):
            with self.assertRaisesRegex(app.UserError, "セクションのIDで指定"):
                self.run_link(path, cfg, transport, PERSONAL_LINK, section=section)
        self.assertEqual(path.read_text(encoding="utf-8"), before)
        result = self.run_link(path, cfg, transport, PERSONAL_LINK, section="s-1")
        self.assertEqual(result["section"], {"id": "s-1", "displayName": "日報"})
        self.assertEqual(app.load_config(path)["section_id"], "s-1")

    def test_invalid_links_rejected_before_any_request(self):
        path, cfg = self.config("personal.json", root="me")
        transport = Transport({})
        host, shared, part = "会社アカウント", "共有リンク", "セクション/ページ"
        for link, reason in [("", host), ("http://contoso.sharepoint.com/sites/a/Notebook", host),
                             ("https://evil.example/sites/a/Notebook", host),
                             ("https://contoso.sharepoint.com.evil.example/sites/a/Notebook", host),
                             ("https://user:pw@contoso.sharepoint.com/sites/a/Notebook", host),
                             ("https://contoso.sharepoint.com:443/sites/a/Notebook", host),
                             ("https://1drv.ms/o/s!SECRET-SHARE", host),
                             ("https://onedrive.live.com/edit.aspx?resid=SECRET-SHARE", host),
                             ("onenote:file:///Users/someone/Notebook", host),
                             ("https://contoso.sharepoint.com/sites/a/Note\nbook", host), ("x" * 3000, host),
                             ("https://contoso.sharepoint.com/:o:/s/a/SECRET-SHARE?e=SECRET-SHARE", shared),
                             ("onenote:https://contoso.sharepoint.com/sites/a/SiteAssets/Notebook/Section.one#Page&page-id={x}", part),
                             ("https://contoso.sharepoint.com/sites/a/SiteAssets/Notebook?wd=target%28Section.one%7C%29", part)]:
            with self.assertRaisesRegex(app.UserError, reason) as caught:
                self.run_link(path, cfg, transport, link, root="sites/" + SITE)
            self.assertNotIn("SECRET", str(caught.exception))
        self.assertEqual(transport.calls, [])

    def test_link_and_root_kind_must_agree(self):
        path, cfg = self.config("config.json", root="me")
        transport = Transport({})
        with self.assertRaisesRegex(app.UserError, "--site-id"):
            self.run_link(path, cfg, transport, TEAM_LINK)
        with self.assertRaisesRegex(app.UserError, "個人用"):
            self.run_link(path, cfg, transport, PERSONAL_LINK, root="sites/" + SITE)
        with self.assertRaisesRegex(app.UserError, "ホスト名"):
            self.run_link(path, cfg, transport, TEAM_LINK, root="sites/fabrikam.sharepoint.com,a,b")
        self.assertEqual(transport.calls, [])

    def test_out_of_scope_or_noncanonical_reference_rejected(self):
        root = "sites/" + SITE
        path, cfg = self.config("team.json", root=root)
        good = notebook(root)
        shape, place = "想定外の形式", "一致しない"
        cases = [(dict(good, self="https://graph.microsoft.com.evil.example/v1.0/" + root + "/onenote/notebooks/1-nb"), shape),
                 (dict(good, self=good["self"].replace("/v1.0/", "/beta/")), shape),
                 (dict(good, self=good["self"].replace("https://", "http://")), shape),
                 (dict(good, self=good["self"] + "?x=1"), shape),
                 (dict(good, self=good["self"].replace("/onenote/notebooks/", "/onenote/sections/")), shape),
                 (dict(good, self=good["self"].replace("/onenote/", "/onenote/%2e%2e/")), shape),
                 (dict(good, sectionsUrl="https://evil.example/sections"), shape),
                 ({k: v for k, v in good.items() if k != "sectionsUrl"}, shape),
                 (dict(good, id="1-other"), shape),
                 (notebook("sites/other.sharepoint.com,a,b"), place),
                 (notebook(USER), place), (["not", "an", "object"], "確認できません")]
        for reply, reason in cases:
            transport = Transport(routes(root, reply, sections(("t-1", "x"))))
            with self.assertRaisesRegex(app.UserError, reason):
                self.run_link(path, cfg, transport, TEAM_LINK, section="t-1")
            self.assertEqual([c[0] for c in transport.calls], ["POST"])
        self.assertEqual(app.load_config(path)["section_id"], "")

    def test_me_root_does_not_follow_site_location(self):
        path, cfg = self.config("personal.json", root="me")
        transport = Transport(routes("me", notebook("sites/" + SITE), sections(("t-1", "x"))))
        with self.assertRaisesRegex(app.UserError, "一致しない"):
            self.run_link(path, cfg, transport, PERSONAL_LINK)
        self.assertEqual(len(transport.calls), 1)

    def test_section_must_belong_to_resolved_notebook(self):
        path, cfg = self.config("personal.json", root="me")
        transport = Transport(routes("me", notebook(USER), sections(("s-9", "x"), parent="1-other")))
        with self.assertRaisesRegex(app.UserError, "所属"):
            self.run_link(path, cfg, transport, PERSONAL_LINK, section="s-9")
        self.assertEqual(app.load_config(path)["section_id"], "")

    def test_permission_denied_explains_without_expanding_scope(self):
        root = "sites/" + SITE
        path, cfg = self.config("team.json")
        before = path.read_text(encoding="utf-8")
        transport = Transport({("POST", app.GRAPH + root + "/onenote/notebooks/GetNotebookFromWebUrl"):
                               app.ApiError(403, "Forbidden")})
        with self.assertRaises(app.UserError) as caught:
            self.run_link(path, cfg, transport, TEAM_LINK, root=root, section="t-1")
        message = str(caught.exception)
        self.assertIn("Notes.ReadWrite.All", message)
        self.assertIn("自動で変更していません", message)
        self.assertNotIn("FAKE-NOT-A-REAL-TOKEN", message)
        self.assertEqual(len(transport.calls), 1)
        self.assertEqual(path.read_text(encoding="utf-8"), before)

    def test_unresolvable_notebook_link_gives_guidance(self):
        root = "sites/" + SITE
        path, cfg = self.config("team.json", root=root)
        transport = Transport({("POST", app.GRAPH + root + "/onenote/notebooks/GetNotebookFromWebUrl"):
                               app.ApiError(404, "20102")})
        with self.assertRaisesRegex(app.UserError, "onenote:付き可"):
            self.run_link(path, cfg, transport, "https://contoso.sharepoint.com/sites/sample-team/Shared%20Documents")

    def test_library_url_is_not_a_notebook_and_lists_site_candidates(self):
        path, cfg = self.config("team.json")
        transport = Transport({})
        with self.assertRaisesRegex(app.UserError, "ライブラリ一覧"):
            self.run_link(path, cfg, transport, LIBRARY_LINK)
        root = "sites/" + SITE
        with self.assertRaisesRegex(app.UserError, "ライブラリ一覧"):
            self.run_link(path, cfg, transport, LIBRARY_LINK, root=root, section="t-1")
        self.assertEqual(transport.calls, [])
        href = "https://contoso.sharepoint.com/sites/sample-team/"
        transport = Transport({("GET", app.GRAPH + root + "/onenote/notebooks?$select=id,displayName,links"): {"value": [
            {"id": "1-assets", "displayName": "チームノート", "links": {"oneNoteWebUrl": {"href": href + "SiteAssets/Team%20Notebook"}}},
            {"id": "1-docs", "displayName": "共有ノート", "links": {"oneNoteWebUrl": {"href": href + "Shared%20Documents/Shared%20Notebook"}}},
            {"id": "1-bad", "displayName": "外部", "links": {"oneNoteWebUrl": {"href": "https://evil.example/x"}}}]}})
        result = self.run_link(path, cfg, transport, LIBRARY_LINK, root=root)
        self.assertEqual(result["kind"], "library")
        self.assertFalse(result["saved"])
        self.assertEqual([(n["id"], n["in_link_scope"]) for n in result["notebooks"]],
                         [("1-docs", True), ("1-assets", False), ("1-bad", False)])
        self.assertIsNone(result["notebooks"][2]["webUrl"])
        self.assertEqual(app.load_config(path)["section_id"], "")
        self.assert_graph_only(transport, root)

    def test_link_kinds(self):
        self.assertEqual(app.link_info(LIBRARY_LINK)["scope"], "/sites/sample-team/Shared Documents")
        for link, kind in [(TEAM_LINK, "notebook"), (PERSONAL_LINK, "notebook"),
                           ("https://contoso.sharepoint.com/sites/sample-team", "site"),
                           ("https://contoso.sharepoint.com/sites/sample-team/SitePages/Home.aspx", "site"),
                           ("https://contoso.sharepoint.com/sites/sample-team/_layouts/15/viewlsts.aspx", "site"),
                           ("https://contoso.sharepoint.com/sites/sample-team/_layouts/15/Doc.aspx?sourcedoc=%7Bx%7D", "notebook"),
                           ("https://contoso-my.sharepoint.com/personal/taro_contoso_com/_layouts/15/onedrive.aspx", "library")]:
            self.assertEqual(app.link_info(link)["kind"], kind, link)

    def test_cli_link_uses_site_id_and_separate_config(self):
        team, _ = self.config("team.json")
        root = "sites/" + SITE
        transport = Transport(routes(root, notebook(root), sections(("t-1", "検証用"))))
        real_graph, output = app.Graph, io.StringIO()
        argv = ["onenote.py", "--config", str(team), "link", TEAM_LINK, "--site-id", SITE, "--section", "検証用"]
        with patch("sys.argv", argv), patch("sys.stdout", output), patch("onenote.Auth", lambda cfg: FakeAuth()), \
                patch("onenote.Graph", lambda cfg, auth: real_graph(cfg, auth, transport)):
            app.main()
        self.assertTrue(json.loads(output.getvalue())["saved"])
        self.assertEqual(app.load_config(team)["root"], root)
        self.assertNotIn("FAKE-NOT-A-REAL-TOKEN", output.getvalue())
        self.assert_graph_only(transport, root)


if __name__ == "__main__":
    unittest.main()
