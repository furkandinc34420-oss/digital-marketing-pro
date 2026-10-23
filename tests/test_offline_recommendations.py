"""Evidence-only drafts, source binding and authorized non-mutating access."""
import copy
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

REPO=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(REPO/'scripts'),str(REPO/'examples/offline-seo/isolation')]
import evidence_recommendations as rec
import audit_report as audit
import access_control as access
import coordinator as c
import offline_seo, content_seo, technical_seo
from runner import DockerLab,CUSTOMERS
A,B=CUSTOMERS


class TestEvidenceRecommendations(unittest.TestCase):
    def setUp(self):
        self.sources=DockerLab()._customer_snapshots()
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.app=c.Coordinator(Path(self.tmp.name)/'tasks')
        self.directory=access.AccessDirectory()
        self.provider=access.OfflineIdentityProvider(self.directory)
        self.service=access.AccessService(self.provider,self.directory,self.app)
        for tenant in CUSTOMERS:
            self.directory.set_tenant(access.Tenant(tenant))
            self.directory.set_user(access.User(tenant,access.Identity('offline-fixture',tenant)))
            self.directory.set_membership(access.Membership(tenant,tenant,access.Role.TENANT_ADMIN))
        self.tokens={t:self.provider.issue(t) for t in CUSTOMERS}
        class Scope:
            def __init__(self,tenant,snapshots):self.customer_id=tenant;self.snapshots=snapshots
            def read(self,name):
                raw=self.snapshots[self.customer_id][name]
                return json.loads(raw.decode('utf-8-sig')),hashlib.sha256(raw).hexdigest()
        self.adapters={}
        for op,module in zip(audit.OPERATIONS,(offline_seo,content_seo,technical_seo)):
            adapter=mock.Mock()
            adapter.execute_snapshot.side_effect=lambda t,s,module=module:{'result':module.analyze(Scope(t,s))}
            adapter.execute.side_effect=lambda tenant,adapter=adapter:adapter.execute_snapshot(tenant,DockerLab()._customer_snapshots())
            self.app._specialists[op]=adapter;self.adapters[op]=adapter

    def run_report(self,tenant=A,operation='seo.audit',task='report'):
        return self.app.run(c.TrustedCaller('fixture',frozenset(CUSTOMERS)),tenant,task,operation)

    def hashes(self,tenant=A):
        return {n:hashlib.sha256(raw).hexdigest() for n,raw in self.sources[tenant].items()}

    def test_all_four_inputs_for_two_customers(self):
        for tenant in CUSTOMERS:
            for operation in (*audit.OPERATIONS,'seo.audit'):
                record=self.run_report(tenant,operation,operation.replace('.','-'))
                output=rec.from_record(record,tenant,self.sources[tenant])
                self.assertEqual(output['customer_id'],tenant)
                self.assertEqual(output['source_hashes'],self.hashes(tenant))
                self.assertTrue(output['recommendations'])
                self.assertEqual(output['source_operation'],operation)
                for d in output['recommendations']:
                    self.assertEqual(d['customer_id'],tenant)
                    for key in ('issue','evidence','proposed_action','technical_rationale','priority','confidence','risks','baseline','success_criterion','human_approval_required'):
                        self.assertIn(key,d)
                    self.assertEqual(d['status'],'draft');self.assertFalse(d['applied'])
                    self.assertEqual(d['approval']['status'],'not_requested')
                    self.assertFalse(d['approval']['apply_supported'])
                    self.assertFalse(d['success_criterion']['measured'])

    def test_source_changes_and_task_digest_corruption_are_rejected(self):
        record=self.run_report()
        changed=copy.deepcopy(self.sources[A]);changed['pages.json']+=b'\n'
        with self.assertRaises(ValueError):rec.from_record(record,A,changed)
        bad=copy.deepcopy(record);bad['input_digest']='0'*64
        with self.assertRaises(ValueError):rec.from_record(bad,A,self.sources[A])

    def test_foreign_customer_and_manifest_are_rejected(self):
        record=self.run_report()
        for customer,hashes in ((B,self.hashes()),(A,self.hashes(B))):
            with self.assertRaises(ValueError):rec.build(record['result']['result'],customer,hashes)
        with self.assertRaises(ValueError):rec.from_record(record,A,self.sources[B])

    def test_evidence_pointers_resolve_and_metrics_are_not_mutable_aliases(self):
        report=self.run_report()['result']['result'];before=copy.deepcopy(report)
        output=rec.build(report,A,self.hashes())
        def resolve(pointer):
            value=report
            for key in pointer.strip('/').split('/'):
                value=value[int(key)] if isinstance(value,list) else value[key]
            return value
        for d in output['recommendations']:
            for claim in d['evidence']['claims']:
                self.assertEqual(resolve(claim['claim_path'])['page_id'],d['page_id']) if '/proposals/' in claim['claim_path'] else self.assertIn('message',resolve(claim['claim_path']))
            for observation in d['evidence']['observations']:
                self.assertEqual(resolve(observation['page_path'])['page_id'],d['page_id'])
        output['recommendations'][0]['evidence']['source_hashes']['pages.json']='changed'
        self.assertEqual(report,before)

    def test_no_performance_or_industry_claims_and_approval_binding_changes(self):
        record=self.run_report();one=rec.from_record(record,A,self.sources[A])
        self.assertIsNone(one['industry_context']['value'])
        self.assertIn('Trafik',one['unmeasured'])
        two=rec.from_record(record,A,self.sources[A]);self.assertEqual(one,two)
        changed=copy.deepcopy(record['result']['result']);changed['experts']['seo.analyze']['result']['customer_name']='Changed fixture name'
        # A complete report must be recomposed after changing nested evidence.
        changed=audit.compose(A,changed['source_hashes'],changed['page_catalog'],changed['experts'],'succeeded')
        other=rec.build(changed,A,self.hashes())
        self.assertNotEqual(one['report_sha256'],other['report_sha256'])
        self.assertNotEqual(one['recommendations'][0]['recommendation_id'],other['recommendations'][0]['recommendation_id'])

    def test_unknown_issue_and_unsupported_rule_are_manual_review(self):
        record=self.run_report(operation='seo.analyze')
        report=record['result']['result'];report['pages'][0]['issues'].append({'message':'Unsupported fictional claim'})
        output=rec.build(report,A,self.hashes())
        unknown=next(d for d in output['recommendations'] if d['code'].startswith('legacy_') and not d['evidence']['observations'])
        self.assertEqual(unknown['confidence']['level'],'low')
        self.assertIn('değişiklik önermeyin',unknown['proposed_action'])

    def test_partial_audit_is_not_presented_as_complete(self):
        self.adapters['seo.content'].execute_snapshot.side_effect=RuntimeError('PRIVATE')
        record=self.run_report();output=rec.from_record(record,A,self.sources[A])
        self.assertFalse(output['source_complete']);self.assertEqual(output['expert_states']['seo.content'],'failed')
        self.assertNotIn('PRIVATE',json.dumps(output))
        self.assertTrue(all(any('eksik' in text for text in d['confidence']['uncertainties']) for d in output['recommendations']))

    def test_conflicting_canonical_evidence_does_not_become_certain_fix(self):
        sources=copy.deepcopy(self.sources);data=json.loads(sources[A]['pages.json'])
        data['pages'][0]['html']='<html><head><title>Örnek</title><link rel="alternate canonical" href="'+data['pages'][0]['url']+'"></head><body><h1>Örnek</h1></body></html>'
        sources[A]['pages.json']=json.dumps(data).encode()
        with mock.patch.object(DockerLab,'_customer_snapshots',return_value=sources):record=self.run_report()
        output=rec.from_record(record,A,sources[A])
        d=next(d for d in output['recommendations'] if d['code']=='canonical_missing' and d['page_id']==1)
        self.assertEqual(d['confidence']['level'],'low');self.assertIn('değişiklik önermeyin',d['proposed_action'])

    def test_unreliable_html_never_yields_definitive_fix(self):
        sources=copy.deepcopy(self.sources);data=json.loads(sources[A]['pages.json'])
        data['pages'][0]['html']='<html><head><title>x</head><body><div><h1>bad</div></body>'
        sources[A]['pages.json']=json.dumps(data).encode()
        with mock.patch.object(DockerLab,'_customer_snapshots',return_value=sources):record=self.run_report()
        output=rec.from_record(record,A,sources[A])
        drafts=[d for d in output['recommendations'] if d['page_id']==1]
        self.assertTrue(drafts);self.assertTrue(all(d['confidence']['level']=='low' for d in drafts))

    def test_noindex_never_promises_google_result_or_orders_removal(self):
        sources=copy.deepcopy(self.sources);data=json.loads(sources[A]['pages.json'])
        data['pages'][0]['html']='<html><head><meta name="robots" content="noindex"></head><body><h1>Örnek</h1></body></html>'
        sources[A]['pages.json']=json.dumps(data).encode()
        with mock.patch.object(DockerLab,'_customer_snapshots',return_value=sources):record=self.run_report()
        d=next(d for d in rec.from_record(record,A,sources[A])['recommendations'] if d['code']=='robots_noindex' and d['page_id']==1)
        self.assertIn('değişiklik önermeyin',d['proposed_action'])
        self.assertIn('gerçek indeks durumu ölçülmedi',d['technical_rationale'])

    def test_output_count_and_byte_limits_fail_without_partial_success(self):
        report=self.run_report()['result']['result']
        with mock.patch.object(rec,'MAX_RECOMMENDATIONS',1):
            with self.assertRaises(ValueError):rec.build(report,A,self.hashes())
        with mock.patch.object(rec,'MAX_OUTPUT_BYTES',100):
            with self.assertRaises(ValueError):rec.build(report,A,self.hashes())

    def test_invalid_source_report_and_operation_are_rejected(self):
        record=self.run_report();report=record['result']['result']
        for change in (lambda r:r.update(summary_tr='forged'),lambda r:r.update(operation='unknown')):
            bad=copy.deepcopy(report);change(bad)
            with self.assertRaises(ValueError):rec.build(bad,A,self.hashes())
        bad=copy.deepcopy(record);bad['operation']='seo.content'
        with self.assertRaises(ValueError):rec.from_record(bad,A,self.sources[A])

    def test_authorized_reader_derivation_is_readonly_and_never_executes_again(self):
        self.run_report();self.directory.set_membership(access.Membership(A,A,access.Role.READER))
        root=self.app.store.root
        def snapshot():return {str(p):p.read_bytes() for p in root.rglob('*') if p.is_file()}
        before=snapshot();calls=sum(x.execute_snapshot.call_count for x in self.adapters.values())
        output=self.service.recommendations(self.tokens[A],A,'report')
        self.assertEqual(output['customer_id'],A);self.assertEqual(before,snapshot())
        self.assertEqual(calls,sum(x.execute_snapshot.call_count for x in self.adapters.values()))
        with self.assertRaises(access.AccessDenied):self.service.recommendations(self.tokens[A],B,'report')
        self.directory.remove_membership(A,A)
        with self.assertRaises(access.AccessDenied):self.service.recommendations(self.tokens[A],A,'report')

    def test_changed_current_sources_are_sanitized_in_facade(self):
        self.run_report();sources=copy.deepcopy(self.sources);sources[A]['pages.json']+=b'\n'
        with mock.patch.object(DockerLab,'_customer_snapshots',return_value=sources):
            with self.assertRaises(access.ServiceUnavailable) as caught:self.service.recommendations(self.tokens[A],A,'report')
        self.assertNotIn('pages.json',str(caught.exception))

    def test_forged_session_never_reads_report_or_sources(self):
        with mock.patch.object(self.app,'peek') as peek,mock.patch.object(DockerLab,'_customer_snapshots') as source:
            with self.assertRaises(access.AccessDenied):self.service.recommendations(object(),A,'report')
            peek.assert_not_called();source.assert_not_called()

    def test_corrupt_page_url_or_failed_expert_never_becomes_trusted_draft(self):
        record=self.run_report(operation='seo.analyze')
        bad=copy.deepcopy(record);bad['result']['result']['pages'][0]['url']='https://other.invalid/'
        with self.assertRaises(ValueError):rec.from_record(bad,A,self.sources[A])
        bad=copy.deepcopy(record);bad['status']='failed'
        with self.assertRaises(ValueError):rec.from_record(bad,A,self.sources[A])

    def test_false_content_claim_is_review_only(self):
        record=self.run_report(operation='seo.content');report=record['result']['result']
        original=report['proposals'][0];page=next(p for p in report['pages'] if p['page_id']==original['page_id'])
        page['evidence']['title']='Present title'
        original['code']='missing_title'
        output=rec.build(report,A,self.hashes())
        d=next(d for d in output['recommendations'] if d['code']=='title_missing')
        self.assertEqual(d['confidence']['level'],'low');self.assertIn('değişiklik önermeyin',d['proposed_action'])

    def test_forged_canonical_with_matching_sources_is_unverified(self):
        record=self.run_report(operation='seo.analyze')
        page=next(p for p in record['result']['result']['pages'] if p['meta'].get('canonical'))
        page['meta']['canonical']=None
        page['issues'].append({'message':'Missing canonical tag'})
        output=rec.from_record(record,A,self.sources[A])
        d=next(d for d in output['recommendations'] if d['page_id']==page['page_id'] and d['code']=='canonical_missing')
        self.assertEqual(d['confidence']['level'],'low')
        self.assertEqual(d['evidence']['measurement_verification'],'unverified')
        self.assertIn('değişiklik önermeyin',d['proposed_action'])

    def test_duplicate_budget_is_checked_before_validation(self):
        report=self.run_report(operation='seo.analyze')['result']['result']
        report['pages'][0]['issues']=[{'message':'Missing canonical tag'}]*10000
        with mock.patch.object(rec.audit,'validate_expert') as validate:
            with self.assertRaisesRegex(ValueError,'evidence limit'):rec.build(report,A,self.hashes())
            validate.assert_not_called()
        report['pages'][0]['issues']=[{'message':'Missing canonical tag'}]*1000
        self.assertTrue(rec.build(report,A,self.hashes())['recommendations'])

    def test_dictionary_order_does_not_change_ids(self):
        report=self.run_report()['result']['result']
        reordered=json.loads(json.dumps(report,sort_keys=True))
        reordered['experts']=dict(reversed(list(reordered['experts'].items())))
        self.assertEqual(rec.build(report,A,self.hashes()),rec.build(reordered,A,self.hashes()))

    def test_real_output_byte_boundary(self):
        # Exercise the actual byte counter, including non-ASCII UTF-8 bytes.
        limit=1024*1024
        below={'v':'a'*(limit-9)}
        self.assertEqual(len(json.dumps(below).encode()),limit)
        rec.audit.bounded(below,rec.MAX_OUTPUT_BYTES)
        with self.assertRaises(ValueError):rec.audit.bounded({'v':below['v']+'a'},rec.MAX_OUTPUT_BYTES)

        report=self.run_report(operation='seo.analyze')['result']['result']
        report['proposals']=[]
        template=copy.deepcopy(report['pages'][0])
        template['meta'].update(title='a'*35,title_length=35,title_status='good',description='a'*130,description_length=130,description_status='good',canonical=None)
        template['issues']=[{'message':'Missing canonical tag'},{'message':'unknown'}]
        for width in (100,4000):
            report['pages']=[]
            for pid in range(1,101):
                page=copy.deepcopy(template)
                page.update(page_id=pid,url='https://demo.invalid/'+str(pid)+'x'*width)
                report['pages'].append(page)
            if width==100:
                output=rec.build(report,A,self.hashes())
                self.assertLess(len(json.dumps(output,ensure_ascii=False).encode()),1024*1024)
            else:
                with self.assertRaisesRegex(ValueError,'byte budget'):rec.build(report,A,self.hashes())

    def test_revocation_during_derivation_is_effective_without_shared_lock(self):
        import threading
        self.run_report()
        entered=threading.Event();finished=threading.Event()
        original=rec.from_record
        def derive(*args):
            entered.set()
            self.assertTrue(finished.wait(5),'Policy lock held during derivation')
            return original(*args)
        def revoke():
            if entered.wait(5):self.directory.remove_membership(A,A)
            finished.set()
        worker=threading.Thread(target=revoke);worker.start()
        try:
            with mock.patch.object(rec,'from_record',side_effect=derive):
                with self.assertRaises(access.AccessDenied):self.service.recommendations(self.tokens[A],A,'report')
        finally:worker.join(6)
        self.assertFalse(worker.is_alive())

    def test_closed_account_and_reassigned_identity_reject_recommendations(self):
        self.run_report()
        self.directory.set_user(access.User(A,access.Identity('offline-fixture',A),active=False))
        with self.assertRaises(access.AccessDenied):self.service.recommendations(self.tokens[A],A,'report')
        self.directory.set_user(access.User(A,access.Identity('offline-fixture','moved-identity'),active=False))
        self.directory.set_user(access.User('new-user',access.Identity('offline-fixture',A)))
        self.directory.set_membership(access.Membership('new-user',A,access.Role.TENANT_ADMIN))
        with self.assertRaises(access.AccessDenied):self.service.recommendations(self.tokens[A],A,'report')

    @unittest.skipUnless(hasattr(__import__('os'),'fork'),'fork unavailable')
    def test_fork_cannot_read_recommendations(self):
        import os
        self.run_report()
        pid=os.fork()
        if pid==0:
            try:self.service.recommendations(self.tokens[A],A,'report')
            except access.AccessDenied:os._exit(0)
            except Exception:os._exit(2)
            os._exit(1)
        _,status=os.waitpid(pid,0)
        self.assertEqual(os.waitstatus_to_exitcode(status),0)

    def test_failed_read_is_nonmutating(self):
        self.run_report()
        def snapshot():return {str(p):p.read_bytes() for p in self.app.store.root.rglob('*') if p.is_file()}
        before=snapshot()
        changed=copy.deepcopy(self.sources);changed[A]['pages.json']+=b' '
        with mock.patch.object(DockerLab,'_customer_snapshots',return_value=changed):
            with self.assertRaises(access.ServiceUnavailable):self.service.recommendations(self.tokens[A],A,'report')
        self.assertEqual(before,snapshot())

    def test_expiry_during_recommendation_derivation_blocks_response(self):
        self.run_report()
        clock=mock.Mock(return_value=100.0)
        provider=access.OfflineIdentityProvider(self.directory,lifetime_seconds=10,clock=clock)
        service=access.AccessService(provider,self.directory,self.app)
        token=provider.issue(A)
        original=rec.from_record
        def derive(*args):
            result=original(*args)
            clock.return_value=110.0
            return result
        with mock.patch.object(rec,'from_record',side_effect=derive):
            with self.assertRaises(access.AccessDenied):service.recommendations(token,A,'report')

    def test_policy_changes_during_derivation_recheck_exact_read_permission(self):
        self.run_report()
        original=rec.from_record
        for change in ('close','rebind','reader'):
            with self.subTest(change=change):
                def derive(*args):
                    result=original(*args)
                    if change=='close':self.directory.set_user(access.User(A,access.Identity('offline-fixture',A),active=False))
                    elif change=='rebind':self.directory.set_user(access.User(A,access.Identity('offline-fixture','changed-subject')))
                    else:self.directory.set_membership(access.Membership(A,A,access.Role.READER))
                    return result
                try:
                    with mock.patch.object(rec,'from_record',side_effect=derive):
                        if change=='reader':
                            self.assertEqual(self.service.recommendations(self.tokens[A],A,'report')['customer_id'],A)
                        else:
                            with self.assertRaises(access.AccessDenied):self.service.recommendations(self.tokens[A],A,'report')
                finally:
                    self.directory.set_user(access.User(A,access.Identity('offline-fixture',A)))
                    self.directory.set_membership(access.Membership(A,A,access.Role.TENANT_ADMIN))
                    self.tokens[A]=self.provider.issue(A)

    def test_two_customer_real_docker_audit_to_authorized_drafts(self):
        from runner import IsolationUnavailable
        try:DockerLab()._docker('version','--format','{{.Server.Version}}')
        except (IsolationUnavailable,OSError,RuntimeError) as exc:self.skipTest(str(exc))
        self.app._specialists={'seo.analyze':c.SeoSpecialist(),'seo.content':c.ContentSeoSpecialist(),'seo.technical':c.TechnicalSeoSpecialist()}
        for tenant in CUSTOMERS:
            self.service.submit(self.tokens[tenant],tenant,'real-audit','seo.audit')
            output=self.service.recommendations(self.tokens[tenant],tenant,'real-audit')
            self.assertTrue(output['source_complete']);self.assertEqual(output['customer_id'],tenant)
            self.assertTrue(all(d['customer_id']==tenant for d in output['recommendations']))
