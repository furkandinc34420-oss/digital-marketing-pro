"""Bounded, deterministic Turkish SEO audit composition. No I/O or network."""
import hashlib
import json
import re

from content_schema import validate_content_result
from technical_schema import validate_technical_result

OPERATIONS = ('seo.analyze', 'seo.content', 'seo.technical')
MAX_AUDIT_BYTES = 3 * 1024 * 1024
MAX_EXPERT_BYTES = 2 * 1024 * 1024
ALLOWED_STATES = {'pending', 'running', 'succeeded', 'failed', 'interrupted'}


def bounded(value, limit=MAX_AUDIT_BYTES):
    size = 0
    for chunk in json.JSONEncoder(ensure_ascii=False, allow_nan=False).iterencode(value):
        size += len(chunk.encode('utf-8'))
        if size > limit:
            raise ValueError('Audit byte budget exceeded')
    return value


def validate_expert(operation, result, customer, hashes, catalog):
    if not isinstance(result, dict):
        raise ValueError('Invalid expert report')
    bounded(result, MAX_EXPERT_BYTES)
    if operation == 'seo.content':
        validate_content_result(result, customer)
    elif operation == 'seo.technical':
        validate_technical_result(result, customer)
    elif operation != 'seo.analyze':
        raise ValueError('Unknown audit expert')
    if (result.get('customer_id') != customer or result.get('synthetic') is not True
            or result.get('mode') != 'proposal-only' or result.get('connection') != 'none'
            or result.get('source_hashes') != hashes or result.get('operation', operation) != operation):
        raise ValueError('Expert scope, mode or source mismatch')
    if operation == 'seo.analyze' and (result.get('source') != 'wordpress-fixture'
            or result.get('source_sha256') != hashes['pages.json'] or 'offline-html-meta' not in result.get('checks', [])):
        raise ValueError('Invalid legacy SEO report identity')
    pages, proposals = result.get('pages'), result.get('proposals')
    if not isinstance(pages, list) or not isinstance(proposals, list) or len(proposals) > 1500:
        raise ValueError('Invalid expert collections')
    seen = set()
    metadata = {}
    for page in pages:
        if (not isinstance(page, dict) or type(page.get('page_id')) is not int or page['page_id'] in seen
                or catalog.get(str(page['page_id'])) != page.get('url')
                or page.get('customer_id', customer) != customer):
            raise ValueError('Expert page mismatch')
        seen.add(page['page_id'])
        if operation == 'seo.analyze':
            if (not isinstance(page.get('meta'), dict) or not isinstance(page.get('issues'), list)
                    or any(not isinstance(issue, dict) or not isinstance(issue.get('message'), str)
                           or len(issue['message']) > 4096 for issue in page['issues'])):
                raise ValueError('Invalid legacy SEO page')
            meta = page['meta']
            for field in ('title', 'description'):
                if (field not in meta or (meta[field] is not None and not isinstance(meta[field], str))
                        or meta.get(field + '_status') not in {'missing', 'too_short', 'too_long', 'good'}):
                    raise ValueError('Invalid legacy SEO metadata')
                value = meta[field]
                low, high = (30, 60) if field == 'title' else (120, 160)
                expected = ('missing' if not value else 'too_short' if len(value) < low
                            else 'too_long' if len(value) > high else 'good')
                if meta[field + '_status'] != expected or type(meta.get(field + '_length')) is not int or meta[field + '_length'] != len(value or ''):
                    raise ValueError('Contradictory legacy measurement')
            metadata[page['page_id']] = meta

    if {str(x) for x in seen} != set(catalog):
        raise ValueError('Expert page coverage mismatch')
    proposed_fields = set()
    for p in proposals:
        if (not isinstance(p, dict) or type(p.get('page_id')) is not int
                or catalog.get(str(p['page_id'])) != p.get('url') or p.get('customer_id') != customer
                or p.get('applied') is not False or p.get('human_approval_required') is not True):
            raise ValueError('Invalid expert proposal scope')
        if operation == 'seo.analyze' and (p.get('field') not in {'title', 'description'}
                or p.get('reason') not in {'missing', 'too_short', 'too_long'}
                or p.get('source_hashes') != hashes
                or not isinstance(p.get('reason'), str)):
            raise ValueError('Invalid legacy SEO proposal')
        if operation == 'seo.analyze':
            field = p['field']; meta = metadata[p['page_id']]
            key = (p['page_id'], field)
            if (key in proposed_fields or p.get('reason') != meta[field + '_status']
                    or 'expected_before' not in p or p['expected_before'] != meta[field]
                    or (p['expected_before'] is not None and not isinstance(p['expected_before'], str))
                    or 'proposed_value' not in p or (p['proposed_value'] is not None and
                        (not isinstance(p['proposed_value'], str) or not p['proposed_value'].strip()))
                    or p.get('source_sha256') != hashes['pages.json']
                    or p.get('status') != ('awaiting_human_review' if p['proposed_value'] else 'needs_authoring')):
                raise ValueError('Contradictory legacy proposal')
            proposed_fields.add(key)
    if operation == 'seo.analyze' and proposed_fields != {
            (pid, field) for pid, meta in metadata.items() for field in ('title', 'description')
            if meta[field + '_status'] != 'good'}:
        raise ValueError('Missing legacy proposal')



def candidates(operation, result):
    for index, proposal in enumerate(result['proposals']):
        if operation == 'seo.analyze':
            code = proposal['field'] + '_' + proposal['reason']
            advice = {'title': 'Sayfa başlığının', 'description': 'Meta açıklamasının'}[proposal['field']] + ' eksiklik veya uzunluk uyarısını kaynak kanıtıyla inceleyin.'
        else:
            code = ALIASES.get(proposal['code'], proposal['code'])
            advice = proposal['recommendation']
        yield proposal, code, advice, {'operation': operation, 'proposal_index': index,
                                     'evidence_path': f"experts/{operation}/result/proposals/{index}"}
    if operation == 'seo.analyze':
        for pi, page in enumerate(result['pages']):
            for ii, issue in enumerate(page['issues']):
                known = {'Missing title tag': 'title_missing',
                         'Missing meta description': 'description_missing',
                         'Missing canonical tag': 'canonical_missing',
                         'Page has noindex directive (will be excluded from search)': 'robots_noindex',
                         'Missing viewport meta tag': 'viewport_missing'}
                # Exact producer messages, checked against the validated measurements.
                # Unknown messages always remain independent findings.
                for field, label, lo, hi in (('title', 'Title tag', 30, 60),
                                              ('description', 'Meta description', 120, 160)):
                    meta = page['meta']; status = meta[field + '_status']
                    if status in {'too_short', 'too_long'}:
                        word = 'short' if status == 'too_short' else 'long'
                        known[f"{label} too {word} ({meta[field + '_length']} chars, recommend {lo}-{hi})"] = field + '_' + status
                code = known.get(issue['message'], 'legacy_' + hashlib.sha256(issue['message'].encode()).hexdigest()[:12])
                yield page, code, 'Temel SEO uzmanının ek uyarısını ham kanıtıyla inceleyin.', {
                    'operation': operation, 'issue_index': ii,
                    'evidence_path': f"experts/{operation}/result/pages/{pi}/issues/{ii}"}


ALIASES = {'missing_title': 'title_missing'}
TITLES = {'title_too_short': 'Sayfa başlığı kısa', 'title_too_long': 'Sayfa başlığı uzun',
          'description_missing': 'Meta açıklaması eksik', 'description_too_short': 'Meta açıklaması kısa',
          'description_too_long': 'Meta açıklaması uzun', 'viewport_missing': 'Mobil viewport bilgisini inceleyin',
          'title_missing': 'Sayfa başlığını inceleyin', 'canonical_missing': 'Canonical ihtiyacını inceleyin',
          'robots_noindex': 'Noindex tercihinin amacını doğrulayın', 'html_uncertain': 'HTML belirsizliğini giderin'}


def compose(customer, hashes, catalog, experts, state):
    if state not in {'running', 'succeeded', 'failed', 'interrupted'} or set(experts) != set(OPERATIONS):
        raise ValueError('Invalid audit state')
    if (not isinstance(hashes, dict) or set(hashes) != {'profile.json', 'pages.json'}
            or any(not isinstance(v, str) or not re.fullmatch('[a-f0-9]{64}', v) for v in hashes.values())
            or not isinstance(catalog, dict) or not 1 <= len(catalog) <= 100
            or any(not re.fullmatch('[1-9][0-9]*', k) or not isinstance(v, str) or len(v) > 4096 for k, v in catalog.items())):
        raise ValueError('Invalid audit source manifest')
    statuses = [experts[op].get('status') for op in OPERATIONS if isinstance(experts[op], dict)]
    # Sequential execution: terminal prefix, at most one active/interrupted
    # step, then an unstarted suffix. Orchestration failure may stop anywhere.
    tail = False
    active = 0
    pending_seen = False
    for status in statuses:
        if status in {'pending', 'running', 'interrupted'}:
            tail = True
            if status == 'pending':
                pending_seen = True
            if status != 'pending':
                if pending_seen:
                    raise ValueError('Active step after unstarted step')
                active += 1
                if active > 1:
                    raise ValueError('Multiple active audit steps')
        elif tail:
            raise ValueError('Nonsequential audit states')
    if (state in {'failed', 'interrupted'} and 'running' in statuses
            or state == 'running' and 'interrupted' in statuses
            or state == 'succeeded' and statuses != ['succeeded'] * 3):
        raise ValueError('Contradictory parent and expert states')
    grouped = {}
    for operation in OPERATIONS:
        item = experts[operation]
        if not isinstance(item, dict) or set(item) != {'status', 'result', 'error'} or item['status'] not in ALLOWED_STATES:
            raise ValueError('Invalid audit expert state')
        if item['status'] != 'succeeded':
            expected_error = 'Analiz tamamlanamadı; yönetici incelemesi gerekiyor.' if item['status'] == 'failed' else None
            if item['error'] != expected_error:
                raise ValueError('Expert error and status mismatch')
            if item['result'] is not None or (item['error'] is not None and item['error'] != 'Analiz tamamlanamadı; yönetici incelemesi gerekiyor.'):
                raise ValueError('Invalid failed expert data')
            continue
        if item['error'] is not None:
            raise ValueError('Successful expert has an error')
        result = item['result']
        validate_expert(operation, result, customer, hashes, catalog)
        for proposal, code, advice, provenance in candidates(operation, result):
            key = (proposal['page_id'], proposal['url'], code)
            if key not in grouped:
                severity = ('high' if code in {'html_uncertain', 'canonical_invalid', 'canonical_multiple', 'robots_conflict'}
                            else 'medium' if code in {'title_missing', 'description_missing', 'h1_missing', 'robots_noindex'} else 'low')
                order = 0 if code == 'html_uncertain' else 1 if severity == 'high' or code == 'robots_noindex' else 2 if severity == 'medium' else 3
                grouped[key] = {'finding_id': hashlib.sha256(json.dumps(key).encode()).hexdigest()[:16],
                    'customer_id': customer, 'page_id': key[0], 'url': key[1], 'code': code,
                    'title_tr': TITLES.get(code, advice), 'severity': severity, 'fix_order': order,
                    'recommendations': [], 'sources': [], 'human_approval_required': True, 'applied': False}
            finding = grouped[key]
            if advice not in finding['recommendations']:
                finding['recommendations'].append(advice)
            finding['sources'].append(provenance)
    statuses = [experts[x]['status'] for x in OPERATIONS]
    complete = all(x == 'succeeded' for x in statuses) and state == 'succeeded'
    if state == 'succeeded' and not complete:
        raise ValueError('Incomplete audit cannot succeed')
    findings = sorted(grouped.values(), key=lambda f: (f['fix_order'], {'high': 0, 'medium': 1, 'low': 2}[f['severity']], f['page_id'], f['code']))
    label = {'running': 'Sürüyor', 'succeeded': 'Analizler tamamlandı', 'failed': 'Eksik: denetim tamamlanamadı', 'interrupted': 'Kesildi: sonuçlar eksik veya belirsiz'}[state]
    lines = [f'Birleşik SEO Denetimi — {customer}', f'Durum: {label}',
             f"Tamamlanan uzman: {statuses.count('succeeded')}/3", f'Birleştirilmiş bulgu: {len(findings)}',
             'Öncelikler inceleme sırasıdır; sıralama veya indeks garantisi değildir.']
    importance_tr = {'high': 'Yüksek', 'medium': 'Orta', 'low': 'Düşük'}
    lines.extend(f"{i}. [{importance_tr[f['severity']]}] {f['url']} — {f['title_tr']} (kanıt: {f['finding_id']})" for i, f in enumerate(findings, 1))
    return bounded({'schema_version': 1, 'operation': 'seo.audit', 'customer_id': customer,
        'synthetic': True, 'connection': 'none', 'mode': 'proposal-only', 'execution_state': state,
        'complete': complete, 'source_hashes': hashes, 'page_catalog': catalog,
        'experts': experts, 'findings': findings, 'summary_tr': '\n'.join(lines),
        'unmeasured': ['HTTP durumları', 'Gerçek robots.txt ve sitemap', 'Google indeks durumu', 'Canlı site performansı']})


def validate_audit(report, customer):
    if not isinstance(report, dict) or report.get('customer_id') != customer:
        raise ValueError('Audit customer mismatch')
    bounded(report)
    expected = compose(customer, report['source_hashes'], report['page_catalog'], report['experts'], report['execution_state'])
    if json.dumps(report, sort_keys=True, allow_nan=False) != json.dumps(expected, sort_keys=True, allow_nan=False):
        raise ValueError('Invalid audit derived report')
