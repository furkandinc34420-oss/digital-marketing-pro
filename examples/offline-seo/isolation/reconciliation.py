"""Trusted-host offline reconciliation. Observe by default; no task success writes."""
import argparse
import hashlib
import json
import re
import subprocess
from pathlib import Path

from resource_store import ResourceStore, OwnershipError, FIELDS
from runner import DockerLab, CUSTOMERS
from supervisor import Supervisor

MESSAGES = {
    'active': ('info', 'Yürütme kilidi aktif; kaynağa müdahale edilmedi.'),
    'not_sent': ('info', 'Oluşturma komutu gönderilmemiş; kaynak varsayılmadı.'),
    'orphan_present': ('warning', 'Sahipliği doğrulanmış yetim kaynak mevcut; uygulama için operatör seçimi gerekli.'),
    'absent_observed': ('warning', 'Kayıtlı tam kimlik Docker listesinde yok; kayıt tamamlama için operatör seçimi gerekli.'),
    'pending_unresolved': ('error', 'Belirsiz oluşturma tek ve doğrulanabilir kaynağa bağlanamadı; insan incelemesi gerekli.'),
    'ownership_mismatch': ('error', 'Kimlik veya sahiplik kanıtı doğrulanamadı; müdahale reddedildi.'),
    'docker_unavailable': ('error', 'Docker durumu alınamadı; bağlantı veya komut sonucu belirsiz. Kaynak durumu değiştirilmedi.'),
    'missing_lease': ('error', 'Güvenilir yürütme kilidi doğrulanamadı; insan incelemesi gerekli.'),
    'review_required': ('warning', '5.5 insan incelemesi mandalı korunuyor; otomatik tekrar müdahale edilmedi.'),
    'cleanup_pending': ('warning', 'Temizleme niyeti kaydedildi; tamamlanma henüz doğrulanmadı.'),
    'cleanup_failed': ('error', 'Durdurma veya kaldırma doğrulanamadı; başarı kabul edilmedi. İnsan incelemesi gerekli.'),
    'removed': ('info', 'Kaynağın yokluğu doğrulandı ve kaynak kaydı tamamlandı; SEO görevi başarıya çevrilmedi.'),
    'image_blocked': ('warning', 'Aynı yürütmenin çözümlenmemiş konteyner kayıtları var; imaj kaldırılmadı.'),
    'busy': ('warning', 'Metadata veya yürütme kilidi meşgul; müdahale edilmedi, gözlem tekrar denenebilir.'),
    'removed_but_present': ('error', 'Kaldırılmış kaydın tam kimliği Docker üzerinde görünüyor; otomatik müdahale reddedildi.'),
}
ERRORS = (RuntimeError, OSError, ValueError, KeyError, TypeError, IndexError,
          AttributeError, subprocess.TimeoutExpired)


class Reconciler:
    def __init__(self, root):
        self.root = Path(root)

    @staticmethod
    def _ids(lab, row, pending=False):
        if row['kind'] == 'container':
            args = ['ps', '-a', '--no-trunc']
            args += ['--filter', 'label=dmp-owner-attempt='+row['attempt']] if pending else ['--filter', 'id='+row['resource']]
            args += ['--format', '{{.ID}}']
            pattern = '[a-f0-9]{64}'
        else:
            args = ['image', 'ls', '--no-trunc', '--quiet']
            if pending: args += ['--filter', 'label=dmp-owner-attempt='+row['attempt']]
            pattern = 'sha256:[a-f0-9]{64}'
        ids = lab._docker(*args).splitlines()
        if any(not re.fullmatch(pattern, rid) for rid in ids) or row['kind']=='container' and len(ids)!=len(set(ids)):
            raise OwnershipError('Invalid Docker identity listing')
        if row['kind']=='container' and not pending and any(rid!=row['resource'] for rid in ids):
            raise OwnershipError('Unexpected Docker identity listing')
        return sorted(set(ids))

    @staticmethod
    def _lab(store, row):
        lab = DockerLab(ownership_root=store.root)
        lab.resource_store = store
        lab.resource_task = row['task']
        lab.resource_execution = row['execution']
        lab._resource_customer = row['customer']
        return lab

    def _one(self, store, row, apply, retry_reviewed):
        lab = self._lab(store, row)
        if row['state']=='not_sent': return 'not_sent'
        ids = self._ids(lab, row, pending=row['state']=='sent_unconfirmed')
        if row['state']=='removed':
            return 'removed_but_present' if row['resource'] in ids else 'removed'
        previous = store.supervisor_outcomes(row['customer']).get(row['attempt'])
        if row['state']=='sent_unconfirmed':
            if len(ids)!=1: return 'pending_unresolved'
            resource = ids[0]
        else:
            resource = row['resource']
            if resource not in ids:
                if not apply: return 'absent_observed'
                if previous=='review' and not retry_reviewed: return 'review_required'
                store.observation(row, 'cleanup_pending')
                store.reconcile_absent(row)
                return 'removed'
        config = json.loads(lab._docker('inspect', resource))[0]
        owned = store.verify(row, resource, config)
        label = config.get('Config',{}).get('Labels',{}).get('dmp-offline-isolation')
        if not isinstance(label,str) or not re.fullmatch('[a-f0-9]{32}',label):
            raise OwnershipError('Missing lab marker')
        lab.label='dmp-offline-isolation='+label
        lab._bindings[row['attempt']]=owned
        if previous=='review' and not retry_reviewed: return 'review_required'
        if not apply: return 'orphan_present'
        if row['kind']=='image':
            peers=store.records(row['customer'])
            if any(p['execution']==row['execution'] and p['kind']=='container'
                   and p['state'] not in ('not_sent','removed') for p in peers):
                return 'image_blocked'
        store.observation(owned, 'cleanup_pending')  # Durable intent against current verified snapshot.
        try:
            if row['kind']=='container':
                Supervisor(store.root)._stop(store, owned) # Existing native proof + stop protocol.
            else:
                lab._docker('image','rm',resource,timeout=5) # No --force, preserves other references.
                if resource in self._ids(lab, owned):
                    raise OwnershipError('Image removal not verified')
                store.transition(owned,'identified','removed',resource)
        except ERRORS:
            if store.poisoned: raise
            if row['kind']=='container': store.supervisor_result(row,'review')
            return 'cleanup_failed'
        return 'removed'

    def scan(self, customer, *, apply=False, retry_reviewed=False, limit=100, offset=0):
        if type(apply) is not bool or type(retry_reviewed) is not bool:
            raise ValueError('Operator choices must be explicit booleans')
        if customer not in CUSTOMERS or retry_reviewed and not apply:
            raise ValueError('Invalid trusted operator selection')
        if type(limit) is not int or not 1<=limit<=100 or type(offset) is not int or not 0<=offset<=10000:
            raise ValueError('Invalid reconciliation batch')
        store = ResourceStore(self.root)
        all_rows = store.records(customer)
        rows = sorted(all_rows,key=lambda r:(r['kind']!='container',r['attempt']))[offset:offset+limit]
        leases = {r['execution']:r for r in store.leases(customer)}
        health = DockerLab(ownership_root=self.root)
        try: health._docker('version','--format','{{.Server.Version}}')
        except ERRORS:
            for row in rows: store.observation(row,'docker_unavailable')
            return self._scan_report(customer,len(all_rows),len(rows),offset,limit,health_check_failed=True)
        busy_count=0
        # Containers must be resolved before their execution's images.
        for snapshot in sorted(rows,key=lambda r:(r['kind']!='container',r['attempt'])):
            row=snapshot
            if row['state']=='not_sent':
                store.observation(row,'not_sent');continue
            lease=leases.get(row['execution'])
            if lease is None or lease['task']!=row['task']:
                store.observation(row,'missing_lease');continue
            try:
                with store.orphan_claim(customer,lease) as orphan:
                    if not orphan:
                        # An owner OR a competing supervisor can hold this flock.
                        # Do not infer healthy ownership or close existing alerts.
                        busy_count+=1
                        store.observation(row,'busy',preserve_existing=True)
                        continue
                    current=next(r for r in store.records(customer) if r['attempt']==row['attempt'])
                    if any(current[k]!=row[k] for k in FIELDS):
                        raise OwnershipError('Changed resource binding')
                    row=current
                    try: code=self._one(store,row,apply,retry_reviewed)
                    except OwnershipError:
                        if store.poisoned: raise
                        code='ownership_mismatch'
                    except ERRORS:
                        if store.poisoned: raise
                        code='docker_unavailable'
                    latest=next(r for r in store.records(customer) if r['attempt']==row['attempt'])
                    store.observation(latest,code)  # CAS against the post-operation snapshot under claim.
            except BlockingIOError:
                busy_count+=1
                store.observation(row,'busy',preserve_existing=True)
            except (OSError,OwnershipError):
                if store.poisoned: raise
                store.observation(row,'missing_lease')
        return self._scan_report(customer,len(all_rows),len(rows),offset,limit,busy_count)

    def _scan_report(self,customer,total,scanned,offset,limit,busy_count=0,health_check_failed=False):
        result=self.report(customer)
        result.update(scanned=scanned,scan_total=total,busy_count=busy_count,health_check_failed=health_check_failed,scan_has_more=offset+limit<total,
                      next_offset=offset+limit if offset+limit<total else None)
        return result

    def report(self, customer, *, limit=100, offset=0):
        """Versioned bounded operator-event projection; NOT the 5.7 audit history."""
        if type(limit) is not int or not 1<=limit<=500 or type(offset) is not int or offset<0:
            raise ValueError('Invalid operator report page')
        store=ResourceStore(self.root)
        sources,conditions=store.operator_snapshot(customer)
        rows={r['attempt']:r for r in sources}
        items=[]
        for condition in conditions:
            row=rows[condition['attempt']];code=condition['code']
            severity,message=MESSAGES[code]
            alert_id=hashlib.sha256((customer+'\0'+row['attempt']).encode()).hexdigest()
            items.append(dict(schema_version=1,component='offline-reconciliation',
                customer_id=customer,task_id=row['task'],execution_id=row['execution'],
                attempt_id=row['attempt'],resource_kind=row['kind'],code=code,
                severity=severity,message=message,revision=condition['revision'],alert_id=alert_id,
                event_id=hashlib.sha256((alert_id+'\0'+str(condition['revision'])).encode()).hexdigest()))
        order={'error':0,'warning':1,'info':2}
        items.sort(key=lambda item:(order[item['severity']],item['attempt_id']))
        return dict(schema_version=1,customer_id=customer,total=len(items),
                    unresolved=sum(i['severity']!='info' for i in items),
                    has_more=offset+limit<len(items),items=items[offset:offset+limit])


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--ownership-root',type=Path,required=True)
    parser.add_argument('--customer',choices=CUSTOMERS,required=True)
    parser.add_argument('--apply',action='store_true')
    parser.add_argument('--retry-reviewed',action='store_true')
    parser.add_argument('--json',action='store_true')
    parser.add_argument('--limit',type=int,default=100)
    parser.add_argument('--offset',type=int,default=0)
    args=parser.parse_args()
    if args.retry_reviewed and not args.apply: parser.error('--retry-reviewed requires --apply')
    try: result=Reconciler(args.ownership_root).scan(args.customer,apply=args.apply,retry_reviewed=args.retry_reviewed,limit=args.limit,offset=args.offset)
    except ERRORS:
        print('Depo veya kayıt görüntüsü doğrulanamadı; kalıcı uyarı yazılamamış olabilir. Güvenilir operatör incelemesi gerekli.')
        return 2
    if args.json: print(json.dumps(result,ensure_ascii=False))
    else:
        for item in result['items']:
            print(f"[{item['severity']}] {item['task_id']} / {item['resource_kind']}: {item['message']}")
        if result['health_check_failed']: print('Docker sağlık kontrolü başarısız; kaynak kaydı yoksa kalıcı kaynak uyarısı oluşturulamaz.')
        if result['busy_count']: print('Meşgul kilitler var; mevcut uyarılar kapatılmadı, daha sonra tekrar gözlemleyin.')
        if result['has_more']: print('Daha fazla uyarı var; report() sayfalaması ile tamamını inceleyin.')
        if result['scan_has_more']: print('Uzlaştırma kısmi; sonraki --offset: '+str(result['next_offset']))
    return 2 if result['unresolved'] or result['scan_has_more'] or result['busy_count'] or result['health_check_failed'] else 0


if __name__=='__main__': raise SystemExit(main())
