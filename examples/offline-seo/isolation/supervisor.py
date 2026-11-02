"""Independent trusted-host orphan stopper. No tenant endpoint, network or prune."""
import argparse
import json
import re
import subprocess
import time
from pathlib import Path

from resource_store import ResourceStore, OwnershipError
from runner import DockerLab, CUSTOMERS


class Supervisor:
    def __init__(self, root):
        self.root = Path(root)

    @staticmethod
    def _running(config):
        state = config.get('State')
        if (not isinstance(state,dict) or type(state.get('Running')) is not bool
                or type(state.get('Restarting')) is not bool):
            raise OwnershipError('Uncertain Docker state')
        if state['Restarting']: raise OwnershipError('Restarting resource needs review')
        return state['Running']

    def _stop(self, store, row):
        lab = DockerLab(ownership_root=self.root)
        lab.resource_store = store
        lab.resource_task = row['task']; lab.resource_execution = row['execution']
        lab._resource_customer = row['customer']
        # Discovery is bounded by the native output limit. Labels are a search
        # hint only; every full ID and all six ownership fields must verify.
        if row['state'] == 'sent_unconfirmed':
            ids = lab._docker('ps','-a','--no-trunc','--filter',
                              'label=dmp-owner-attempt='+row['attempt'],'--format','{{.ID}}').splitlines()
            if len(ids)!=1: raise OwnershipError('Unresolved Docker creation')
            resource = ids[0]
        else: resource = row['resource']
        if not isinstance(resource,str) or not re.fullmatch('[a-f0-9]{64}',resource):
            raise OwnershipError('Invalid full Docker identity')
        config = json.loads(lab._docker('inspect',resource))[0]
        owned = store.verify(row,resource,config)
        label = config.get('Config',{}).get('Labels',{}).get('dmp-offline-isolation')
        if not isinstance(label,str) or not re.fullmatch('[a-f0-9]{32}',label):
            raise OwnershipError('Missing lab ownership marker')
        lab.label='dmp-offline-isolation='+label
        lab._bindings[row['attempt']] = owned
        store.supervisor_result(row,'stopping')  # Must commit BEFORE any mutation.
        if self._running(config):
            try: lab._docker('stop','--time','2',resource,timeout=5)
            except (RuntimeError,OSError,subprocess.TimeoutExpired): pass  # Inspect reality after a lost reply.
            config=json.loads(lab._docker('inspect',resource))[0]
            store.verify(row,resource,config)
            if self._running(config):
                try: lab._docker('kill',resource,timeout=5)
                except (RuntimeError,OSError,subprocess.TimeoutExpired): pass
                config=json.loads(lab._docker('inspect',resource))[0]
                store.verify(row,resource,config)
                if self._running(config): raise OwnershipError('Stop not verified')
        # Remove the verified, stopped ID: an already submitted delayed start
        # cannot resurrect it after the coordinator has died. No image removal.
        lab._docker('rm','--force',resource,timeout=5)
        remaining=lab._docker('ps','-a','--no-trunc','--filter','id='+resource,'--format','{{.ID}}').splitlines()
        if remaining: raise OwnershipError('Removal not verified')
        store.transition(owned,'identified','removed',resource,supervisor_outcome='removed')

    @staticmethod
    def _review(store,row):
        # Fixed code only, no stderr/proof/path. Never retry an ambiguous commit
        # through a fresh connection while the current operation is poisoned.
        if store.poisoned:
            raise OwnershipError('Supervisor storage unavailable; review required') from None
        store.supervisor_result(row,'review')

    def scan(self, customer):
        if customer not in CUSTOMERS: raise OwnershipError('Unknown resource customer')
        store=ResourceStore(self.root)
        rows=store.records(customer)
        leases={l['execution']:l for l in store.leases(customer)}
        outcomes={}
        previous=store.supervisor_outcomes(customer)
        for row in rows:
            if row['kind']!='container' or row['state'] in ('not_sent','removed'): continue
            if previous.get(row['attempt'])=='review':
                outcomes[row['attempt']]='review'
                continue  # Ambiguous cases require explicit trusted operator review.
            lease=leases.get(row['execution'])
            try:
                if lease is None or lease['task']!=row['task']:
                    raise OwnershipError('No verifiable execution lease')
                with store.orphan_claim(customer,lease) as orphan:
                    if not orphan:
                        outcomes[row['attempt']]='active'
                        continue
                    try:
                        # Re-read after claim: another supervisor may have finished.
                        latest=next(r for r in store.records(customer) if r['attempt']==row['attempt'])
                        if latest['state']=='removed': continue
                        if store.supervisor_outcomes(customer).get(row['attempt'])=='review':
                            outcomes[row['attempt']]='review'
                            continue
                        self._stop(store,latest)
                        outcomes[row['attempt']]='removed'
                    except (RuntimeError,OSError,ValueError,KeyError,TypeError,IndexError,AttributeError,subprocess.TimeoutExpired):
                        # Persist the review latch BEFORE releasing the execution
                        # claim; a competing supervisor must not retry in this gap.
                        self._review(store,row)
                        outcomes[row['attempt']]='review'
            except BlockingIOError:
                # Tenant metadata flock contention is not broken ownership.
                # No terminal latch: safely retry observation on a later scan.
                outcomes[row['attempt']]='busy'
            except (RuntimeError,OSError,ValueError,KeyError,TypeError,IndexError,AttributeError,subprocess.TimeoutExpired):
                self._review(store,row)
                outcomes[row['attempt']]='review'
        return outcomes


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--ownership-root',type=Path,required=True)
    parser.add_argument('--customer',choices=CUSTOMERS,required=True)
    parser.add_argument('--once',action='store_true')
    args=parser.parse_args()
    while True:
        try:
            outcomes=Supervisor(args.ownership_root).scan(args.customer)
        except (RuntimeError,OSError,ValueError):
            print('Supervisor deposu doğrulanamadı; güvenilir yönetici incelemesi gerekli.',flush=True)
            if args.once: return 2
            time.sleep(1)
            continue  # Retry observation, never act without a healthy store.
        if args.once:
            print(json.dumps(outcomes),flush=True)
            return 2 if any(v in {'review','busy'} for v in outcomes.values()) else 0
        time.sleep(1)  # Monotonic polling; age is never evidence of owner death.


if __name__=='__main__':
    raise SystemExit(main())
