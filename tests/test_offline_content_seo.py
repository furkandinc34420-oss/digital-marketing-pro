"""Literal content observations and the new operation through real isolation."""
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / 'scripts'))
sys.path.insert(0, str(REPO / 'examples/offline-seo/isolation'))
import content_seo as content
import offline_seo
import coordinator
from runner import CUSTOMERS, DockerLab, IsolationUnavailable

A, B = CUSTOMERS


class TestOfflineContentSeo(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / 'customers'
        shutil.copytree(REPO / 'examples/offline-seo/customers', self.root)
        self.scope = offline_seo.TenantScope(self.root, A)

    def page(self, html, keyword='kurgusal SEO'):
        p = self.root / A / 'pages.json'
        data = json.loads(p.read_bytes())
        data['pages'] = [dict(data['pages'][0], html=html, primary_keyword=keyword)]
        p.write_text(json.dumps(data, ensure_ascii=False))

    def test_phrase_evidence_and_concrete_proposals(self):
        self.page('<title>Kurgusal SEO</title><h1>Başka konu</h1><p>Burada açıklama var.</p>')
        report = content.analyze(self.scope)
        e = report['pages'][0]['evidence']
        self.assertTrue(e['keyword_in_title'])
        self.assertFalse(e['keyword_in_h1'])
        self.assertEqual(e['keyword_occurrences'], 0)
        self.assertIn('keyword_absent', [p['code'] for p in report['proposals']])
        self.assertTrue(all(p['human_approval_required'] and not p['applied'] for p in report['proposals']))

    def test_script_style_hidden_and_metadata_not_counted(self):
        self.page('<head><title>Hedef</title><meta name="description" content="kurgusal SEO"></head>'
                  '<script>kurgusal SEO</script><style>kurgusal SEO</style><template>kurgusal SEO</template>'
                  '<div hidden>kurgusal SEO</div><p aria-hidden="true">kurgusal SEO</p><p>Görünür metin</p>')
        e = content.analyze(self.scope)['pages'][0]['evidence']
        self.assertEqual(e['keyword_occurrences'], 1)
        self.assertEqual(e['body_word_count'], 4)

    def test_turkish_case_entities_and_word_boundaries(self):
        self.page('<title>İÇERİK SEO</title><h1>İçerik SEO</h1><p>İÇERİK&nbsp;SEO içerik seosu</p>', 'içerik seo')
        e = content.analyze(self.scope)['pages'][0]['evidence']
        self.assertTrue(e['keyword_in_title'])
        self.assertEqual(e['keyword_occurrences'], 2)  # H1 and paragraph, not seosu.

    def test_no_keyword_is_not_invented(self):
        self.page('<title>Örnek</title><p>Konulu metin</p>')
        p = self.root / A / 'pages.json'
        data = json.loads(p.read_bytes()); data['pages'][0].pop('primary_keyword')
        p.write_text(json.dumps(data))
        report = content.analyze(self.scope)
        self.assertIsNone(report['pages'][0]['evidence']['keyword_occurrences'])
        self.assertIn('missing_target', [p['code'] for p in report['proposals']])

    def test_keyword_limits_fail_closed(self):
        for keyword in ('', 'a' * 121, ['keyword'], ' '.join(['x'] * 13)):
            self.page('<title>Örnek</title><p>Metin</p>', keyword)
            with self.assertRaises(offline_seo.SandboxError):
                content.analyze(self.scope)

    def test_input_files_unchanged_and_hashes_reproducible(self):
        before = {str(p): p.read_bytes() for p in self.root.rglob('*.json')}
        first = content.analyze(self.scope)
        self.assertEqual(first, content.analyze(self.scope))
        self.assertEqual(before, {str(p): p.read_bytes() for p in self.root.rglob('*.json')})
        self.assertEqual(first['source_hashes']['pages.json'], hashlib.sha256(before[str(self.root / A / 'pages.json')]).hexdigest())

    def test_foreign_fixture_identity_and_symlink_rejected(self):
        pagefile = self.root / A / 'pages.json'
        pagefile.unlink(); pagefile.symlink_to(self.root / B / 'pages.json')
        with self.assertRaises(offline_seo.SandboxError):
            content.analyze(self.scope)
        pagefile.unlink(); shutil.copyfile(self.root / B / 'pages.json', pagefile)
        with self.assertRaises(offline_seo.SandboxError):
            content.analyze(self.scope)

    def test_noindex_is_reported_without_network_requests(self):
        self.page('<head><meta name="robots" content="follow, noindex"></head><p>Metin</p>')
        with mock.patch('socket.create_connection', side_effect=AssertionError('network forbidden')):
            report = content.analyze(self.scope)
        self.assertTrue(report['pages'][0]['evidence']['noindex_observed'])
        self.assertIn('Rankings', report['unmeasured'])

    def test_new_operation_authorization_and_task_conflict(self):
        app = coordinator.Coordinator(Path(self.tmp.name) / 'state')
        caller = coordinator.TrustedCaller('test', frozenset({A}))
        with self.assertRaises(PermissionError):
            app.run(caller, B, 'content', 'seo.content')
        with mock.patch.object(coordinator.ContentSeoSpecialist, 'execute_snapshot',
                               return_value={'result': content.analyze(self.scope)}) as run:
            one = app.run(caller, A, 'content', 'seo.content')
            self.assertEqual(app.run(caller, A, 'content', 'seo.content'), one)
            run.assert_called_once()
        with self.assertRaises(coordinator.TaskConflict):
            app.run(caller, A, 'content', 'seo.analyze')

    def test_worker_policy_rejects_unsafe_content_container(self):
        # Existing policy must run BEFORE start for the added action as well.
        lab = DockerLab()
        lab.images[A] = 'sha256:' + 'a' * 64
        lab._create = mock.Mock(return_value='b' * 64)
        lab._cleanup = mock.Mock()
        lab._docker = mock.Mock(return_value='[{}]')
        with self.assertRaises(RuntimeError):
            lab.run(A, 'content')
        self.assertFalse(any(call.args[0] == 'start' for call in lab._docker.call_args_list))


class TestOfflineContentDocker(unittest.TestCase):
    def test_two_customers_content_and_original_seo_with_same_security_policy(self):
        try:
            DockerLab()._docker('version', '--format', '{{.Server.Version}}')
        except (IsolationUnavailable, RuntimeError, OSError) as exc:
            self.skipTest(str(exc))
        with tempfile.TemporaryDirectory() as tmp:
            app = coordinator.Coordinator(Path(tmp) / 'state')
            caller = coordinator.TrustedCaller('local-test', frozenset(CUSTOMERS))
            for customer in CUSTOMERS:
                record = app.run(caller, customer, 'content', 'seo.content')
                self.assertEqual(record['status'], 'succeeded', record)
                result = record['result']
                self.assertEqual(result['result']['operation'], 'seo.content')
                self.assertEqual(result['result']['customer_id'], customer)
                self.assertEqual(result['config']['network'], 'none')
                self.assertTrue(result['config']['readonly'])
                self.assertEqual(result['config']['user'], '65534:65534')
                self.assertFalse(result['config']['mounts'])
                self.assertNotIn('OTHER_CUSTOMER_SENTINEL' if customer == A else 'kurgusal SEO testi', json.dumps(result, ensure_ascii=False))
                self.assertEqual(app.run(caller, customer, 'content', 'seo.content'), record)
                cli = subprocess.run([sys.executable, '-B', str(REPO / 'examples/offline-seo/isolation/coordinator.py'),
                                      '--state-root', str(Path(tmp) / 'state'), '--customer', customer,
                                      '--task-id', 'content', '--operation', 'seo.content'],
                                     capture_output=True, text=True, timeout=20)
                self.assertEqual(cli.returncode, 0, cli.stderr)
                self.assertEqual(json.loads(cli.stdout)['result']['result']['operation'], 'seo.content')
                original = app.run(caller, customer, 'technical', 'seo.analyze')
                self.assertEqual(original['status'], 'succeeded', original)


class TestContentHardening(unittest.TestCase):
    setUp = TestOfflineContentSeo.setUp
    page = TestOfflineContentSeo.page

    def test_separate_h1_cannot_form_a_phrase(self):
        self.page('<h1>içerik</h1><h1>SEO</h1>', 'içerik seo')
        e = content.analyze(self.scope)['pages'][0]['evidence']
        self.assertEqual(e['h1'], ['içerik', 'SEO'])
        self.assertFalse(e['keyword_in_h1'])

    def test_nested_inline_formatting_preserves_heading_words(self):
        self.page('<h1>içe<em>ri<strong>k</strong></em> SEO</h1>', 'içerik seo')
        e = content.analyze(self.scope)['pages'][0]['evidence']
        self.assertEqual(e['h1'], ['içerik SEO'])
        self.assertTrue(e['keyword_in_h1'])
        self.page('<h1>içerik<br>SEO</h1>', 'içerik seo')
        self.assertTrue(content.analyze(self.scope)['pages'][0]['evidence']['keyword_in_h1'])

    def test_aria_hidden_remains_visible_but_visual_hidden_is_excluded(self):
        self.page('<p aria-hidden="true">içerik seo</p><p hidden>gizli</p>'
                  '<div style="display: none">gizli</div><span style="visibility:hidden">gizli</span>', 'içerik seo')
        e = content.analyze(self.scope)['pages'][0]['evidence']
        self.assertEqual(e['body_word_count'], 2)
        self.assertEqual(e['keyword_occurrences'], 1)
        self.assertTrue(e['measurement_reliable'])

    def test_malformed_html_reports_uncertainty_not_seo_absence(self):
        for html in ('<p hidden>gizli<p>görünür', '<p hidden><div>görünür</div></p>', '<div><h1>başlık</div>', '<h1>kapanmamış'):
            self.page(html)
            report = content.analyze(self.scope)
            e = report['pages'][0]['evidence']
            self.assertFalse(e['measurement_reliable'])
            self.assertIsNone(e['body_word_count'])
            self.assertIsNone(e['keyword_occurrences'])
            self.assertTrue(e['parse_warnings'])
            self.assertEqual({p['code'] for p in report['proposals']}, {'html_uncertain'})
            coordinator.validate_content_result(report, A)

    def test_depth_limit_rejects_bounded_but_deep_html(self):
        self.page('<div>' * 65 + 'metin' + '</div>' * 65)
        with self.assertRaises(content.ContentLimitError):
            content.analyze(self.scope)

    def test_event_limit_includes_comments_and_void_elements(self):
        for html in ('<br>' * 10001, '<!--x-->' * 10001):
            self.page(html)
            with self.assertRaises(content.ContentLimitError):
                content.analyze(self.scope)

    def test_parse_time_limit_is_controlled(self):
        self.page('<p>metin</p>')
        with mock.patch.object(content, 'MAX_PARSE_SECONDS', -1):
            with self.assertRaises(content.ContentLimitError):
                content.analyze(self.scope)

    def test_large_evidence_is_clipped_and_not_repeated(self):
        self.page('<title>' + 'x' * 80000 + '</title><p>Metin</p>')
        report = content.analyze(self.scope)
        self.assertTrue(report['pages'][0]['evidence']['display_truncated'])
        self.assertLess(len(json.dumps(report, ensure_ascii=False).encode()), 20000)
        for p in report['proposals']:
            self.assertNotIn('evidence', p)
            self.assertEqual(p['evidence_page_id'], report['pages'][0]['page_id'])
        coordinator.validate_content_result(report, A)

    def test_report_byte_limit_raises_controlled_error(self):
        with self.assertRaises(content.ContentLimitError):
            content.bounded_report({'payload': 'x' * (content.MAX_REPORT_BYTES + 1)})
        with mock.patch.object(content, 'MAX_REPORT_BYTES', 100):
            with self.assertRaises(content.ContentLimitError):
                content.analyze(self.scope)

    def configured_app(self):
        app = coordinator.Coordinator(Path(self.tmp.name) / 'state')
        caller = coordinator.TrustedCaller('test', frozenset({A}))
        return app, caller

    def test_content_same_id_changed_source_conflicts(self):
        app, caller = self.configured_app()
        report = content.analyze(self.scope)
        with mock.patch.object(coordinator.ContentSeoSpecialist, 'execute_snapshot', return_value={'result': report}) as execute:
            app.run(caller, A, 'task', 'seo.content')
            snapshots = DockerLab()._customer_snapshots()
            changed = json.loads(snapshots[A]['pages.json'])
            changed['pages'][0]['html'] += '<p>Farklı kurgusal içerik</p>'
            snapshots[A]['pages.json'] = json.dumps(changed, ensure_ascii=False).encode('utf-8')
            with mock.patch.object(DockerLab, '_customer_snapshots', return_value=snapshots):
                with self.assertRaises(coordinator.TaskConflict):
                    app.run(caller, A, 'task', 'seo.content')
            execute.assert_called_once()

    def test_wrong_operation_or_schema_cannot_be_saved_as_success(self):
        import copy
        valid = content.analyze(self.scope)
        mutations = [lambda r: r.update(operation='seo.analyze'), lambda r: r.pop('pages'),
                     lambda r: r['pages'][0].update(customer_id=B),
                     lambda r: r['pages'][0]['evidence'].update(body_word_count='wrong'),
                     lambda r: r['proposals'][0].update(applied=True),
                     lambda r: r['proposals'][0].update(evidence_page_id=999),
                     lambda r: r.update(schema_version=999),
                     lambda r: r.update(unexpected_payload='foreign'),
                     lambda r: r['pages'][0]['evidence'].pop('parse_warnings')]
        app, caller = self.configured_app()
        for index, mutate in enumerate(mutations):
            report = copy.deepcopy(valid); mutate(report)
            with mock.patch.object(coordinator.ContentSeoSpecialist, 'execute_snapshot', return_value={'result': report}):
                record = app.run(caller, A, 'bad-' + str(index), 'seo.content')
            self.assertEqual(record['status'], 'failed')
            self.assertIsNone(record['result'])

    def test_corrupted_cached_content_result_is_rejected(self):
        import sqlite3
        app, caller = self.configured_app()
        with mock.patch.object(coordinator.ContentSeoSpecialist, 'execute_snapshot', return_value={'result': content.analyze(self.scope)}):
            record = app.run(caller, A, 'cached', 'seo.content')
        dbpath = Path(self.tmp.name) / 'state' / A / 'tasks.sqlite3'
        invalid = record['result']; invalid['result']['operation'] = 'seo.analyze'
        for encoded in (json.dumps(invalid), '{}', 'null', '{bad-json'):
            with sqlite3.connect(dbpath) as db:
                db.execute('UPDATE tasks SET result=? WHERE task_id=?', (encoded, 'cached'))
            with self.assertRaises((ValueError, KeyError, TypeError)):
                app.get(caller, A, 'cached')

    def test_output_limit_failure_is_persisted_without_partial_report(self):
        app, caller = self.configured_app()
        with mock.patch.object(coordinator.ContentSeoSpecialist, 'execute_snapshot', side_effect=content.ContentLimitError('Content report exceeds byte budget')):
            record = app.run(caller, A, 'limited', 'seo.content')
        self.assertEqual(record['status'], 'failed')
        self.assertIsNone(record['result'])
        self.assertIn('byte budget', app.admin_diagnostics(A, 'limited')['message'])
