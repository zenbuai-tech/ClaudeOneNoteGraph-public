"""Mirror behavior tests. source_agent=claude-code; machine=mac-mini."""
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import onenote
import sync

CFG = onenote.check_config({'tenant': 'organizations', 'client_id': '11111111-1111-4111-8111-111111111111', 'root': 'me', 'section_id': 'test-section'})


class FakeGraph:
    def __init__(self):
        self.cfg = CFG.copy()
        self.items = [{'id': 'page1', 'title': 'Example', 'lastModifiedDateTime': '2020-01-01T00:00:00Z', 'createdDateTime': '2020-01-01T00:00:00Z', 'links': {'oneNoteWebUrl': {'href': 'https://example.com/note'}}}]
        self.html = '<html><head><title>Hidden</title></head><body><h2>Meeting</h2><p>First statement</p></body></html>'
        self.calls, self.fail, self.truncated = [], False, False
    def section(self):
        return self.cfg['section_id']
    def collection(self, path, limit):
        if self.fail:
            raise onenote.UserError('API unavailable')
        return {'items': self.items, 'truncated': self.truncated}
    def call(self, method, path):
        assert method == 'GET'
        self.calls.append(path)
        return self.html


class SyncTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.vault = Path(self.tmp.name).resolve() / 'vault'
        self.graph = FakeGraph()
    def tearDown(self):
        self.tmp.cleanup()
    def path(self):
        return self.vault / sync.MIRROR / sync.filename('page1')
    def run_sync(self):
        return sync.sync(self.graph, self.vault)
    def test_create_update_and_idempotence(self):
        first = self.run_sync()
        self.assertEqual(first['status'], 'ok')
        self.assertEqual(len(first['created']), 1)
        data = self.path().read_text(encoding='utf-8')
        self.assertIn('## Meeting', data)
        self.assertIn('source_agent: "claude-code"', data)
        self.assertNotIn('Hidden', data)
        calls = len(self.graph.calls)
        second = self.run_sync()
        self.assertEqual(second['unchanged'], 1)
        self.assertEqual(len(self.graph.calls), calls)
        self.assertEqual(data, self.path().read_text(encoding='utf-8'))
        self.graph.items[0]['lastModifiedDateTime'] = '2020-01-02T00:00:00Z'
        self.graph.items[0]['title'] = 'Renamed'
        self.graph.html += '<p>Additional statement</p>'
        third = self.run_sync()
        self.assertEqual(len(third['updated']), 1)
        self.assertIn('Additional statement', self.path().read_text(encoding='utf-8'))
        self.assertEqual(len(list((self.vault / sync.MIRROR).glob('*.md'))), 1)
    def test_local_edits_are_never_overwritten(self):
        self.run_sync()
        self.path().write_text('Human edits', encoding='utf-8')
        report = self.run_sync()
        self.assertEqual(report['status'], 'needs_attention')
        self.assertEqual(len(report['conflicts']), 1)
        self.assertEqual(self.path().read_text(encoding='utf-8'), 'Human edits')
    def test_remote_absence_reports_without_deleting(self):
        self.run_sync()
        self.graph.items = []
        report = self.run_sync()
        self.assertEqual(report['not_seen'], ['page1'])
        self.assertTrue(self.path().exists())
    def test_failed_or_truncated_listing_never_marks_missing(self):
        self.run_sync()
        self.graph.fail = True
        result = self.run_sync()
        self.assertTrue(result['failed'])
        self.assertEqual(result['not_seen'], [])
        self.assertTrue(self.path().exists())
        self.graph.fail, self.graph.truncated = False, True
        self.assertTrue(self.run_sync()['failed'])
    def test_source_binding_rejects_different_section(self):
        self.run_sync()
        self.graph.cfg['section_id'] = 'different'
        with self.assertRaises(onenote.UserError):
            self.run_sync()
    def test_unmanaged_file_not_overwritten(self):
        self.path().parent.mkdir(parents=True)
        self.path().write_text('Existing content', encoding='utf-8')
        self.assertTrue(self.run_sync()['conflicts'])
        self.assertEqual(self.path().read_text(encoding='utf-8'), 'Existing content')
    def test_symlink_rejected(self):
        self.vault.mkdir()
        outside = Path(self.tmp.name) / 'outside'
        outside.mkdir()
        try:
            (self.vault / sync.MIRROR).symlink_to(outside)
        except OSError:  # Windows without Developer Mode cannot create symlinks
            self.skipTest('symlinks unavailable')
        with self.assertRaises(onenote.UserError):
            self.run_sync()
        self.assertEqual(list(outside.iterdir()), [])
    def test_outputs_are_preserved(self):
        self.run_sync()
        result = self.vault / 'AI_Outputs' / 'report.md'
        result.write_text('Human-approved result', encoding='utf-8')
        self.run_sync()
        self.assertEqual(result.read_text(encoding='utf-8'), 'Human-approved result')
    def test_budget_interrupt_resumes(self):
        original = self.graph.call
        self.graph.call = lambda *args: (_ for _ in ()).throw(sync.BudgetExceeded('budget'))
        self.assertTrue(self.run_sync()['deferred'])
        self.assertFalse(self.path().exists())
        self.graph.call = original
        self.assertEqual(len(self.run_sync()['created']), 1)
    def test_overlap_refetch_does_not_rewrite_identical_content(self):
        self.run_sync()
        statepath = self.vault / '.onenote-sync/state.json'
        state = json.loads(statepath.read_text(encoding='utf-8'))
        state['last_started_at'] = '2019-12-31T23:59:59+00:00'
        sync.save_json(statepath, state)
        before = self.path().read_bytes()
        result = self.run_sync()
        self.assertEqual(result['unchanged'], 1)
        self.assertEqual(before, self.path().read_bytes())
        self.assertEqual(len(self.graph.calls), 2)
    def test_conversion_preserves_semantics_and_blocks_active_links(self):
        converter = sync.Markdown()
        result = converter.convert('<h1>Title</h1><p><b>Strong</b> <a href="https://example.com/a">Source</a><a href="javascript:evil()">Unsafe</a></p><ul><li>One</li><li>Two</li></ul><table><tr><th>A</th><th>B</th></tr><tr><td>1</td><td>2</td></tr></table><script>evil()</script><img src="https://evil.example/x" alt="Diagram">')
        for expected in ['# Title', '**Strong**', '[Source](https://example.com/a)', '- One', '| A | B |', '| 1 | 2 |', 'Media not imported']:
            self.assertIn(expected, result)
        self.assertNotIn('javascript:', result)
        self.assertNotIn('evil()', result)
        self.assertTrue(converter.warnings)
    def test_resource_link_uses_downloaded_local_attachment(self):
        c = sync.Markdown(lambda url, label: 'attachments/test.png')
        result = c.convert('<img src="https://graph.microsoft.com/v1.0/me/onenote/resources/a/$value" alt="Chart">')
        self.assertIn('![Chart](attachments/test.png)', result)
        self.assertFalse(c.warnings)
    def test_budget_blocks_write_and_excess_requests(self):
        b = sync.Budget(1)
        with self.assertRaises(onenote.UserError):
            b.transport('POST', 'https://example.com')
        b.take()
        with self.assertRaises(sync.BudgetExceeded):
            b.take()
    @patch('onenote.sys.platform', 'darwin')
    @patch('onenote.Keychain.get', side_effect=onenote.UserError('Keychain access unavailable without user interaction'))
    def test_keychain_denial_stops_sync_without_network_or_note_changes(self, get):
        with patch('onenote.http') as transport:
            result = sync.sync(onenote.Graph(CFG, transport=transport), self.vault)
        self.assertEqual(result['status'], 'needs_attention')
        self.assertEqual(result['created'], [])
        self.assertEqual(result['updated'], [])
        transport.assert_not_called()


if __name__ == '__main__':
    unittest.main()
