"""Validate types AND offline derivations, also when reading cached reports."""
import json
import re
from urllib.parse import urlsplit
from content_schema import MAX_REPORT_BYTES
from technical_rules import build_evidence, evidence_refs, classify_url, CODES


def validate_technical_result(report, customer):
    def require(ok):
        if not ok:
            raise ValueError('Invalid technical report schema, identity or derived evidence')

    def text(v, limit):
        return isinstance(v, str) and len(v) <= limit

    def integer(v, maximum):
        return type(v) is int and 0 <= v <= maximum

    require(isinstance(report, dict) and set(report) == {'schema_version', 'operation', 'customer_id', 'synthetic',
        'source', 'connection', 'mode', 'source_hashes', 'pages', 'proposals', 'unmeasured'})
    require(type(report['schema_version']) is int and report['schema_version'] == 2
            and report['operation'] == 'seo.technical' and report['customer_id'] == customer
            and report['synthetic'] is True and report['source'] == 'wordpress-fixture'
            and report['connection'] == 'none' and report['mode'] == 'proposal-only')
    hashes = report['source_hashes']
    require(isinstance(hashes, dict) and set(hashes) == {'profile.json', 'pages.json'}
            and all(isinstance(v, str) and re.fullmatch('[a-f0-9]{64}', v) for v in hashes.values()))
    require(isinstance(report['unmeasured'], list) and len(report['unmeasured']) <= 16
            and all(text(x, 128) for x in report['unmeasured'])
            and {'HTTP status codes', 'robots.txt', 'Sitemap', 'Google index status'} <= set(report['unmeasured']))
    require(isinstance(report['pages'], list) and 1 <= len(report['pages']) <= 100
            and isinstance(report['proposals'], list) and len(report['proposals']) <= 1500)
    pages = {}
    for page in report['pages']:
        require(isinstance(page, dict) and set(page) == {'customer_id', 'page_id', 'url', 'evidence'})
        pid, source = page['page_id'], page['url']
        require(integer(pid, 2**63 - 1) and pid > 0 and pid not in pages and page['customer_id'] == customer and text(source, 2048))
        try:
            parsed = urlsplit(source)
            require(parsed.scheme == 'https' and parsed.hostname and parsed.hostname.endswith('.invalid')
                    and parsed.username is None and parsed.password is None and parsed.port is None
                    and ':' not in parsed.netloc and not any(ch.isspace() for ch in source))
        except (ValueError, TypeError):
            raise ValueError('Invalid technical source URL') from None
        require(classify_url(source, source)['kind'] == 'internal')
        e = page['evidence']
        require(isinstance(e, dict) and isinstance(e.get('raw'), dict))
        raw = e['raw']
        require(set(raw) == {'canonicals', 'robots', 'links', 'headings', 'warnings', 'base_observed'}
                and type(raw['base_observed']) is bool)
        require(isinstance(raw['warnings'], list) and len(raw['warnings']) <= 8 and all(text(x, 128) for x in raw['warnings']))
        require(isinstance(raw['headings'], list) and len(raw['headings']) <= 10000
                and all(text(x, 100000) for x in raw['headings']) and sum(map(len, raw['headings'])) <= 100000)
        for name in ('canonicals', 'robots', 'links'):
            require(isinstance(raw[name], list) and len(raw[name]) <= 500)
        require(all(x is None or text(x, 2048) for x in raw['links']))
        for target in raw['canonicals']:
            require(isinstance(target, dict) and set(target) == {'href', 'in_head'}
                    and (target['href'] is None or text(target['href'], 2048))
                    and (target['in_head'] is None or type(target['in_head']) is bool))
        for rule in raw['robots']:
            require(isinstance(rule, dict) and set(rule) == {'agent', 'content', 'in_head'}
                    and rule['agent'] in ('robots', 'googlebot', 'bingbot') and text(rule['content'], 2048)
                    and (rule['in_head'] is None or type(rule['in_head']) is bool))
        # Recompute from all bounded raw elements, not just the first display samples.
        require(e == build_evidence(raw, source))
        # Dict equality alone would accept True as integer 1; enforce numeric types.
        require(all(type(e[k]) is bool for k in ('measurement_reliable', 'base_observed', 'evidence_truncated')))
        require(all(integer(e[k], 10000) for k in ('canonical_count', 'robots_count', 'h1_count', 'empty_h1_count'))
                and all(integer(x, 500) for x in e['link_counts'].values()))
        require(all(type(x['index']) is int for x in e['links']))
        require(all(type(x['relative']) is bool and (x['in_head'] is None or type(x['in_head']) is bool) for x in e['canonicals']))
        pages[pid] = page
    for p in report['proposals']:
        require(isinstance(p, dict) and set(p) == {'customer_id', 'page_id', 'url', 'evidence_page_id', 'code',
            'recommendation', 'evidence_refs', 'human_approval_required', 'applied', 'status'})
        pid = p['page_id']
        require(type(pid) is int and pid in pages and p['customer_id'] == customer
                and type(p['evidence_page_id']) is int and p['evidence_page_id'] == pid and p['url'] == pages[pid]['url']
                and p['code'] in CODES and text(p['recommendation'], 2048) and p['human_approval_required'] is True
                and p['applied'] is False and p['status'] == 'awaiting_human_review')
        e = pages[pid]['evidence']
        if not e['measurement_reliable']:
            require(p['code'] == 'html_uncertain')
        require(p['evidence_refs'] == evidence_refs(p['code'], e, p['url']))
        refs = p['evidence_refs']
        require(isinstance(refs, list) and 1 <= len(refs) <= 20)
        for ref in refs:
            require(text(ref, 64))
            if ref in {'headings', 'parse_warnings', 'canonical_count'}:
                continue
            match = re.fullmatch(r'(canonicals|robots|links):(\d+)', ref)
            require(match is not None)
            require(int(match[2]) < len(e['raw'][match[1]]))
    size = 0
    for part in json.JSONEncoder(ensure_ascii=False, allow_nan=False).iterencode(report):
        size += len(part.encode('utf-8'))
        require(size <= MAX_REPORT_BYTES)
