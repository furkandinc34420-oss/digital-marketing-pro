"""Versioned, bounded content report contract shared by worker and coordinator."""
import json
import math
import re
from urllib.parse import urlsplit

MAX_REPORT_BYTES = 2 * 1024 * 1024


def validate_content_result(report, customer):
    def require(ok):
        if not ok:
            raise ValueError('Invalid content result schema or identity')

    def text(value, limit):
        return isinstance(value, str) and len(value) <= limit

    def integer(value, maximum):
        return type(value) is int and 0 <= value <= maximum

    def url(value):
        try:
            p = urlsplit(value)
            return (p.scheme == 'https' and p.hostname and p.hostname.endswith('.invalid')
                    and p.username is None and p.password is None and p.port is None
                    and ':' not in p.netloc and not any(ch.isspace() for ch in value))
        except (ValueError, TypeError):
            return False

    require(isinstance(report, dict))
    require(set(report) == {'schema_version', 'customer_id', 'synthetic', 'source', 'mode', 'connection',
                            'operation', 'source_hashes', 'pages', 'proposals', 'method', 'unmeasured'})
    require(report.get('operation') == 'seo.content' and type(report.get('schema_version')) is int
            and report['schema_version'] == 1 and report.get('customer_id') == customer
            and report.get('synthetic') is True and report.get('mode') == 'proposal-only'
            and report.get('connection') == 'none' and report.get('source') == 'wordpress-fixture')
    hashes = report.get('source_hashes')
    require(isinstance(hashes, dict) and set(hashes) == {'profile.json', 'pages.json'})
    require(all(isinstance(v, str) and re.fullmatch('[a-f0-9]{64}', v) for v in hashes.values()))
    pages, proposals = report.get('pages'), report.get('proposals')
    require(isinstance(pages, list) and 1 <= len(pages) <= 100)
    require(isinstance(proposals, list) and len(proposals) <= 800)
    require(text(report.get('method'), 256) and isinstance(report.get('unmeasured'), list)
            and len(report['unmeasured']) <= 16 and all(text(s, 128) for s in report['unmeasured']))
    indexed = {}
    flags = ('keyword_in_title', 'keyword_in_h1', 'keyword_in_first_100_words')
    for page in pages:
        require(isinstance(page, dict) and set(page) == {'customer_id', 'page_id', 'url', 'evidence'})
        pid = page.get('page_id')
        require(integer(pid, 2**63 - 1) and pid > 0 and pid not in indexed
                and page.get('customer_id') == customer and text(page.get('url'), 4096) and url(page['url']))
        e = page.get('evidence')
        require(isinstance(e, dict) and set(e) == {'body_word_count', 'title', 'h1', 'display_truncated',
            'measurement_reliable', 'parse_warnings', 'primary_keyword', 'keyword_source',
            'keyword_occurrences', 'keyword_in_title', 'keyword_in_h1', 'keyword_in_first_100_words',
            'title_terms_missing_from_body', 'missing_title_term_count', 'title_body_overlap',
            'frequent_body_terms', 'noindex_observed'})
        require(isinstance(e, dict) and type(e.get('measurement_reliable')) is bool
                and type(e.get('display_truncated')) is bool and type(e.get('noindex_observed')) is bool)
        require(text(e.get('title'), 512) and isinstance(e.get('h1'), list)
                and len(e['h1']) <= 32 and all(text(s, 512) for s in e['h1']))
        keyword = e.get('primary_keyword')
        require(keyword is None or (text(keyword, 120) and bool(keyword.strip())))
        require(e.get('keyword_source') == ('fixture' if keyword is not None else 'not_set'))
        warnings = e.get('parse_warnings')
        require(isinstance(warnings, list) and len(warnings) <= 8 and all(text(s, 128) for s in warnings))
        require(e['measurement_reliable'] == (not warnings))
        for field in flags:
            require(field in e and (e[field] is None or type(e[field]) is bool))
        for field in ('body_word_count', 'keyword_occurrences', 'missing_title_term_count'):
            require(field in e and (e[field] is None or integer(e[field], 100000)))
        overlap = e.get('title_body_overlap')
        require('title_body_overlap' in e and (overlap is None or (type(overlap) in (int, float)
                and math.isfinite(overlap) and 0 <= overlap <= 1)))
        require(isinstance(e.get('title_terms_missing_from_body'), list)
                and len(e['title_terms_missing_from_body']) <= 32
                and all(text(s, 128) for s in e['title_terms_missing_from_body']))
        frequent = e.get('frequent_body_terms')
        require(isinstance(frequent, list) and len(frequent) <= 8)
        require(all(isinstance(pair, (list, tuple)) and len(pair) == 2 and text(pair[0], 128)
                    and integer(pair[1], 100000) for pair in frequent))
        if e['measurement_reliable']:
            require(integer(e['body_word_count'], 100000) and integer(e['missing_title_term_count'], 100000))
            if keyword is None:
                require(e['keyword_occurrences'] is None and all(e[field] is None for field in flags))
            else:
                require(integer(e['keyword_occurrences'], 100000) and all(type(e[field]) is bool for field in flags))
        if not e['measurement_reliable']:
            require(all(e[field] is None for field in (*flags, 'body_word_count', 'keyword_occurrences',
                                                       'missing_title_term_count', 'title_body_overlap')))
            require(not frequent and not e['title_terms_missing_from_body'])
        indexed[pid] = page
    allowed_codes = {'missing_title', 'missing_target', 'keyword_title', 'keyword_h1', 'keyword_intro',
                     'keyword_absent', 'title_content_gap', 'limited_content', 'html_uncertain'}
    for proposal in proposals:
        require(isinstance(proposal, dict) and set(proposal) == {'customer_id', 'page_id', 'url', 'code',
            'recommendation', 'evidence_page_id', 'source_hashes', 'status', 'human_approval_required', 'applied'})
        pid = proposal.get('page_id')
        require(type(pid) is int and pid in indexed and proposal.get('customer_id') == customer
                and proposal.get('url') == indexed[pid]['url'] and proposal.get('evidence_page_id') == pid
                and proposal.get('source_hashes') == hashes and 'evidence' not in proposal
                and proposal.get('human_approval_required') is True and proposal.get('applied') is False
                and proposal.get('status') == 'awaiting_human_review'
                and proposal.get('code') in allowed_codes and text(proposal.get('recommendation'), 2048))
        if not indexed[pid]['evidence']['measurement_reliable']:
            require(proposal['code'] == 'html_uncertain')
    size = 0
    for part in json.JSONEncoder(ensure_ascii=False, allow_nan=False).iterencode(report):
        size += len(part.encode('utf-8'))
        require(size <= MAX_REPORT_BYTES)
