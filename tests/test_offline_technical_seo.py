"""Offline technical evidence, strict schemas and real tenant-isolated dispatch."""
import copy
import hashlib
import json
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / 'scripts'))
sys.path.insert(0, str(REPO / 'examples/offline-seo/isolation'))
import offline_seo
import technical_seo as technical
import coordinator
from technical_schema import validate_technical_result
from runner import CUSTOMERS, DockerLab, IsolationUnavailable

A, B = CUSTOMERS


class TestOfflineTechnical(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / 'customers'
        shutil.copytree(REPO / 'examples/offline-seo/customers', self.root)
        self.scope = offline_seo.TenantScope(self.root, A)

    def page(self, html):
        path = self.root / A / 'pages.json'
        payload = json.loads(path.read_bytes())
        payload['pages'] = [dict(payload['pages'][0], html=html)]
        path.write_text(json.dumps(payload, ensure_ascii=False))

    def test_canonical_relative_external_missing_multiple_and_invalid(self):
        self.page('<html><head><link rel="CANONICAL alternate" href="/preferred/">'
                  '<link rel="canonical" href="https://partner.invalid/">'
                  '<link rel="canonical" href="https://demo.invalid:bad/">'
                  '<link rel="canonical" href="http://[bad"></head><body><h1>Başlık</h1></body></html>')
        report = technical.analyze(self.scope)
        e = report['pages'][0]['evidence']
        self.assertEqual(e['canonical_count'], 4)
        self.assertEqual(e['canonicals'][0]['resolved'], 'https://demo.invalid/preferred/')
        self.assertEqual(e['canonicals'][1]['kind'], 'external')
        self.assertEqual(e['canonicals'][2]['kind'], 'invalid')
        codes = {p['code'] for p in report['proposals']}
        self.assertTrue({'canonical_multiple', 'canonical_relative', 'canonical_external', 'canonical_invalid'} <= codes)
        validate_technical_result(report, A)

    def test_self_canonical_is_not_recommended_for_replacement(self):
        for target in ('https://demo.invalid/', 'https://demo.invalid', 'https://demo.invalid:443/'):
            self.page(f'<html><head><link rel="canonical" href="{target}"></head><body><h1>Başlık</h1></body></html>')
            report = technical.analyze(self.scope)
            self.assertFalse(any(p['code'].startswith('canonical_') for p in report['proposals']))
        self.page('<head><link rel="canonical" href="https://demo.invalid:0/"></head><h1>Başlık</h1>')
        self.assertIn('canonical_other_page', {p['code'] for p in technical.analyze(self.scope)['proposals']})
        self.page('<head><link rel="canonical" href="https://demo.invalid/#part"></head><h1>Başlık</h1>')
        self.assertIn('canonical_invalid', {p['code'] for p in technical.analyze(self.scope)['proposals']})

    def test_robots_none_conflict_and_bot_specific_rules_are_separate(self):
        self.page('<head><meta name="robots" content="NONE"><meta name="robots" content="index, follow">'
                  '<meta name="googlebot" content="noindex"></head><h1>Başlık</h1>')
        report = technical.analyze(self.scope)
        self.assertIn('noindex', report['pages'][0]['evidence']['global_directives'])
        self.assertIn('robots_conflict', {p['code'] for p in report['proposals']})
        self.page('<head><meta name="googlebot" content="noindex"></head><h1>Başlık</h1>')
        self.assertNotIn('noindex', technical.analyze(self.scope)['pages'][0]['evidence']['global_directives'])

    def test_h1_independent_inline_text_and_empty_heading(self):
        self.page('<h1>Tek<em>nik</em> SEO</h1><h1></h1>')
        report = technical.analyze(self.scope)
        e = report['pages'][0]['evidence']
        self.assertEqual(e['h1'], ['Teknik SEO', ''])
        self.assertEqual(e['empty_h1_count'], 1)
        self.assertTrue({'h1_multiple', 'h1_empty'} <= {p['code'] for p in report['proposals']})

    def test_link_classes_do_not_fetch_anything(self):
        self.page('<a href="/about">İç</a><a href="//partner.invalid/x">Dış</a>'
                  '<a href="#section">Bölüm</a><a href="mailto:demo@example.invalid">Mail</a>'
                  '<a href="javascript:alert(1)">Kod</a><a href="">Boş</a><a>Eksik</a>'
                  '<a href="https://demo.invalid:70000/">Hatalı</a>')
        with mock.patch('socket.create_connection', side_effect=AssertionError('network forbidden')):
            report = technical.analyze(self.scope)
        counts = report['pages'][0]['evidence']['link_counts']
        self.assertEqual(counts, {'internal': 1, 'external': 1, 'fragment': 1, 'non_http': 1,
                                 'unsafe_or_unsupported': 1, 'empty': 1, 'missing': 1, 'invalid': 1, 'unresolved': 0})
        self.assertTrue({'HTTP status codes', 'robots.txt', 'Sitemap', 'Google index status'} <= set(report['unmeasured']))

    def test_base_and_malformed_html_produce_only_uncertainty(self):
        for html in ('<head><base href="https://partner.invalid/"></head><a href="/x">X</a>', '<p hidden><div>Metin</div></p>'):
            self.page(html)
            report = technical.analyze(self.scope)
            self.assertFalse(report['pages'][0]['evidence']['measurement_reliable'])
            self.assertEqual({p['code'] for p in report['proposals']}, {'html_uncertain'})
            validate_technical_result(report, A)

    def test_limits_depth_link_count_and_long_url(self):
        for html in ('<div>' * 65 + '</div>' * 65, '<a href="/">X</a>' * 501,
                     '<a href="/' + 'a' * 2048 + '">X</a>'):
            self.page(html)
            with self.assertRaises(technical.ContentLimitError):
                technical.analyze(self.scope)

    def test_hashes_and_fixtures_unchanged_by_analysis(self):
        before = {str(p): p.read_bytes() for p in self.root.rglob('*.json')}
        report = technical.analyze(self.scope)
        self.assertEqual(before, {str(p): p.read_bytes() for p in self.root.rglob('*.json')})
        self.assertEqual(report['source_hashes']['pages.json'], hashlib.sha256(before[str(self.root / A / 'pages.json')]).hexdigest())
        self.assertTrue(all(p['human_approval_required'] and not p['applied'] for p in report['proposals']))

    def test_foreign_identity_and_symlink_rejected(self):
        path = self.root / A / 'pages.json'
        path.unlink(); path.symlink_to(self.root / B / 'pages.json')
        with self.assertRaises(offline_seo.SandboxError):
            technical.analyze(self.scope)
        path.unlink(); shutil.copyfile(self.root / B / 'pages.json', path)
        with self.assertRaises(offline_seo.SandboxError):
            technical.analyze(self.scope)

    def test_authorization_deduplication_and_changed_input_conflict(self):
        app = coordinator.Coordinator(Path(self.tmp.name) / 'state')
        caller = coordinator.TrustedCaller('local-test', frozenset({A}))
        with self.assertRaises(PermissionError):
            app.run(caller, B, 'task', 'seo.technical')
        with mock.patch.object(coordinator.TechnicalSeoSpecialist, 'execute_snapshot', return_value={'result': technical.analyze(self.scope)}) as execute:
            record = app.run(caller, A, 'task', 'seo.technical')
            self.assertEqual(record['status'], 'succeeded')
            self.assertEqual(app.run(caller, A, 'task', 'seo.technical'), record)
            snapshots = DockerLab()._customer_snapshots()
            payload = json.loads(snapshots[A]['pages.json']); payload['pages'][0]['html'] += '<h1>Yeni</h1>'
            snapshots[A]['pages.json'] = json.dumps(payload).encode()
            with mock.patch.object(DockerLab, '_customer_snapshots', return_value=snapshots):
                with self.assertRaises(coordinator.TaskConflict):
                    app.run(caller, A, 'task', 'seo.technical')
            execute.assert_called_once()

    def test_wrong_operation_schema_and_cached_result_rejected(self):
        report = technical.analyze(self.scope)
        for change in (lambda r: r.update(operation='seo.content'), lambda r: r.update(customer_id=B),
                       lambda r: r['pages'][0]['evidence'].update(h1_count='wrong'), lambda r: r.update(unmeasured=[])):
            bad = copy.deepcopy(report); change(bad)
            with self.assertRaises(ValueError):
                coordinator.Coordinator._validate_result({'result': bad}, A, 'seo.technical')
        app = coordinator.Coordinator(Path(self.tmp.name) / 'state')
        caller = coordinator.TrustedCaller('local-test', frozenset({A}))
        with mock.patch.object(coordinator.TechnicalSeoSpecialist, 'execute_snapshot', return_value={'result': report}):
            app.run(caller, A, 'task', 'seo.technical')
        report['operation'] = 'seo.content'
        with sqlite3.connect(Path(self.tmp.name) / 'state' / A / 'tasks.sqlite3') as db:
            db.execute('UPDATE tasks SET result=?', (json.dumps({'result': report}),))
        with self.assertRaises(ValueError):
            app.get(caller, A, 'task')

    def test_security_gate_prevents_start_for_new_action(self):
        lab = DockerLab(); lab.images[A] = 'sha256:' + 'a' * 64
        lab._create = mock.Mock(return_value='b' * 64); lab._cleanup = mock.Mock()
        lab._docker = mock.Mock(return_value='[{}]')
        with self.assertRaises(RuntimeError):
            lab.run(A, 'technical')
        self.assertFalse(any(call.args[0] == 'start' for call in lab._docker.call_args_list))

    def test_template_tags_are_not_active_canonical_robots_or_headings(self):
        self.page('<template><h1>Şablon</h1><link rel="canonical" href="/fake">'
                  '<meta name="robots" content="noindex"><a href="/fake">Link</a></template><h1>Asıl başlık</h1>')
        e = technical.analyze(self.scope)['pages'][0]['evidence']
        self.assertEqual(e['h1_count'], 1)
        self.assertEqual(e['h1'], ['Asıl başlık'])
        self.assertEqual(e['canonical_count'], 0)
        self.assertEqual(e['robots_count'], 0)
        self.assertEqual(sum(e['link_counts'].values()), 0)

    def test_body_canonical_location_and_tampered_resolution(self):
        self.page('<body><link rel="canonical" href="/preferred"><h1>Başlık</h1></body>')
        report = technical.analyze(self.scope)
        self.assertIn('canonical_location', {p['code'] for p in report['proposals']})
        report['pages'][0]['evidence']['canonicals'][0]['resolved'] = 'javascript:alert(1)'
        with self.assertRaises(ValueError):
            validate_technical_result(report, A)

    def test_report_limit_fails_without_partial_success(self):
        import content_seo
        with mock.patch.object(content_seo, 'MAX_REPORT_BYTES', 100):
            with self.assertRaises(technical.ContentLimitError):
                technical.analyze(self.scope)


class TestTechnicalDocker(unittest.TestCase):
    def test_two_customers_all_three_operations_and_cli(self):
        try:
            DockerLab()._docker('version', '--format', '{{.Server.Version}}')
        except (IsolationUnavailable, RuntimeError, OSError) as exc:
            self.skipTest(str(exc))
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / 'state'; app = coordinator.Coordinator(root)
            caller = coordinator.TrustedCaller('local-test', frozenset(CUSTOMERS))
            for customer in CUSTOMERS:
                for operation in ('seo.technical', 'seo.content', 'seo.analyze'):
                    record = app.run(caller, customer, operation.replace('.', '-'), operation)
                    self.assertEqual(record['status'], 'succeeded', record)
                    self.assertEqual(record['result']['result']['customer_id'], customer)
                    config = record['result']['config']
                    self.assertEqual(config['network'], 'none'); self.assertTrue(config['readonly'])
                    self.assertEqual(config['user'], '65534:65534'); self.assertFalse(config['mounts'])
                    self.assertFalse(config['privileged']); self.assertEqual(config['cap_drop'], ['ALL'])
                cli = subprocess.run([sys.executable, '-B', str(REPO / 'examples/offline-seo/isolation/coordinator.py'),
                                      '--state-root', str(root), '--customer', customer, '--task-id', 'seo-technical',
                                      '--operation', 'seo.technical'], capture_output=True, text=True, timeout=20)
                self.assertEqual(cli.returncode, 0, cli.stderr)
                self.assertEqual(json.loads(cli.stdout)['result']['result']['operation'], 'seo.technical')


class TestTechnicalReviewFixes(unittest.TestCase):
    setUp = TestOfflineTechnical.setUp
    page = TestOfflineTechnical.page

    def test_malformed_absolute_and_percent_urls_are_invalid(self):
        for url in ('https:///partner.invalid/x', 'https://', 'https:partner.invalid/x', 'http:/x',
                    'https://demo.invalid/%ZZ', '/x%', '//', 'https://-bad.invalid/', 'https://demo.invalid:'):
            self.assertEqual(technical.classify_url(url, 'https://demo.invalid/')['kind'], 'invalid', url)
        self.assertEqual(technical.classify_url('/x', 'https://demo.invalid/')['kind'], 'internal')

    def test_canonical_normalization_preserves_meaningful_differences(self):
        identity = technical.url_identity
        for a, b in (('/~user', '/%7Euser'), ('/a%2fb', '/a%2Fb'), ('/ürün', '/%C3%BCr%C3%BCn')):
            self.assertEqual(identity('https://demo.invalid' + a), identity('https://demo.invalid' + b))
        for target in ('http://demo.invalid/', 'https://partner.invalid/', 'https://demo.invalid:8443/'):
            self.assertNotEqual(identity('https://demo.invalid/'), identity(target))
        self.assertNotEqual(identity('https://demo.invalid/a/b'), identity('https://demo.invalid/a%2Fb'))
        self.assertNotEqual(identity('https://demo.invalid/?a=1&b=2'), identity('https://demo.invalid/?b=2&a=1'))

    def test_parameterized_robots_and_date_stay_whole(self):
        self.page('<head><meta name="robots" content="max-snippet: 50, unavailable_after: Fri, 03 Dec 2027 13:00:00 GMT, nofollow"></head><h1>Başlık</h1>')
        from technical_rules import parse_robots
        e = technical.analyze(self.scope)['pages'][0]['evidence']
        directives = e['robots'][0]['directives']
        self.assertIn('max-snippet: 50', directives)
        self.assertIn('unavailable_after: Fri, 03 Dec 2027 13:00:00 GMT', directives)
        self.assertIn('nofollow', directives)
        self.assertNotIn('50', directives)
        self.assertEqual(parse_robots('all, noindex')[1], ['all', 'noindex'])

    def test_all_is_not_conflict_and_bot_conflicts_are_detected(self):
        self.page('<head><meta name="robots" content="all, noindex">'
                  '<meta name="googlebot" content="index"><meta name="googlebot" content="noindex">'
                  '<meta name="bingbot" content="nofollow,follow"></head><h1>Başlık</h1>')
        report = technical.analyze(self.scope); e = report['pages'][0]['evidence']
        self.assertNotIn('index', e['global_directives'])
        self.assertEqual(e['conflicting_agents'], ['googlebot', 'bingbot'])
        self.assertIn('robots_conflict', {p['code'] for p in report['proposals']})
        validate_technical_result(report, A)

    def test_implicit_head_and_base_targets_are_machine_uncertain(self):
        self.page('<link rel="canonical" href="https://demo.invalid/"><h1>Başlık</h1>')
        report = technical.analyze(self.scope); e = report['pages'][0]['evidence']
        self.assertIsNone(e['canonicals'][0]['in_head'])
        self.assertFalse(e['measurement_reliable'])
        self.assertEqual({p['code'] for p in report['proposals']}, {'html_uncertain'})
        self.page('<head><base href="https://partner.invalid/"></head><a href="/x">X</a><a href="#part">B</a><a href="https://demo.invalid/x">A</a>')
        report = technical.analyze(self.scope); e = report['pages'][0]['evidence']
        self.assertEqual(e['link_counts']['unresolved'], 2)
        self.assertEqual(e['link_counts']['internal'], 1)
        self.assertTrue(all(x['resolved'] is None for x in e['links'] if x['kind'] == 'unresolved'))
        validate_technical_result(report, A)

    def test_problem_links_after_twentieth_are_prioritized_and_referenced(self):
        self.page('<h1>Başlık</h1>' + '<a href="/ok">X</a>' * 20 + '<a href="javascript:alert(1)">Sorun</a><a href="">Boş</a>')
        report = technical.analyze(self.scope); e = report['pages'][0]['evidence']
        self.assertEqual(e['links'][0]['index'], 20)
        self.assertEqual(e['links'][1]['index'], 21)
        proposal = next(p for p in report['proposals'] if p['code'] == 'link_invalid')
        self.assertEqual(proposal['evidence_refs'], ['links:20'])
        validate_technical_result(report, A)

    def test_inconsistent_derived_evidence_and_flags_are_rejected(self):
        self.page('<head><meta name="robots" content="noindex"></head><h1>Başlık</h1><a href="/x">X</a>')
        valid = technical.analyze(self.scope)
        changes = [lambda e: e.update(global_directives=[]), lambda e: e['link_counts'].update(internal=0, external=1),
                   lambda e: e['robots'][0].update(directives=['index'], expanded=['index']),
                   lambda e: e.update(base_observed=True), lambda e: e.update(measurement_reliable=False),
                   lambda e: e['links'][0].update(resolved='https://partner.invalid/'),
                   lambda e: e['raw'].update(base_observed=True)]
        for change in changes:
            bad = copy.deepcopy(valid); change(bad['pages'][0]['evidence'])
            with self.assertRaises(ValueError):
                validate_technical_result(bad, A)
        bad = copy.deepcopy(valid)
        bad['proposals'][0]['evidence_refs'] = ['links:999']
        with self.assertRaises(ValueError):
            validate_technical_result(bad, A)

    def test_all_raw_links_are_used_for_counts_not_just_samples(self):
        self.page('<h1>Başlık</h1>' + '<a href="/x">X</a>' * 25)
        valid = technical.analyze(self.scope)
        self.assertEqual(len(valid['pages'][0]['evidence']['links']), 20)
        self.assertEqual(len(valid['pages'][0]['evidence']['raw']['links']), 25)
        bad = copy.deepcopy(valid); bad['pages'][0]['evidence']['raw']['links'][24] = 'javascript:alert(1)'
        with self.assertRaises(ValueError):
            validate_technical_result(bad, A)

    def test_inconsistent_cached_evidence_is_rejected_without_retry(self):
        self.page('<head><meta name="robots" content="noindex"></head><h1>Başlık</h1>')
        report = technical.analyze(self.scope)
        app = coordinator.Coordinator(Path(self.tmp.name) / 'state')
        caller = coordinator.TrustedCaller('local-test', frozenset({A}))
        with mock.patch.object(coordinator.TechnicalSeoSpecialist, 'execute_snapshot', return_value={'result': report}) as execute:
            app.run(caller, A, 'cached', 'seo.technical')
            bad = copy.deepcopy(report); bad['pages'][0]['evidence']['global_directives'] = []
            with sqlite3.connect(Path(self.tmp.name) / 'state' / A / 'tasks.sqlite3') as db:
                db.execute('UPDATE tasks SET result=?', (json.dumps({'result': bad}),))
            with self.assertRaises(ValueError):
                app.run(caller, A, 'cached', 'seo.technical')
            execute.assert_called_once()
