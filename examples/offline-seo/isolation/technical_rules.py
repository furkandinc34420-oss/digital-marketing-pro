"""Pure offline rules. Classification NEVER authorizes an HTTP request."""
import ipaddress
import re
from urllib.parse import urlsplit, urljoin, quote

MAX_LINKS = 500
MAX_URL_CHARS = 2048
KINDS = ('internal', 'external', 'fragment', 'missing', 'empty', 'non_http', 'invalid', 'unsafe_or_unsupported', 'unresolved')
CODES = {'canonical_missing', 'canonical_multiple', 'canonical_location', 'canonical_invalid', 'canonical_external',
         'canonical_other_page', 'canonical_relative', 'robots_conflict', 'robots_noindex', 'h1_missing',
         'h1_multiple', 'h1_empty', 'link_invalid', 'link_empty', 'html_uncertain'}
UNRESERVED = frozenset('abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-._~')


class TechnicalLimitError(ValueError):
    pass


def valid_host(p):
    if not p.hostname or p.username is not None or p.password is not None or p.netloc.endswith(':'):
        return False
    p.port
    if ':' in p.hostname:
        ipaddress.IPv6Address(p.hostname)
        return True
    host = p.hostname.encode('idna').decode('ascii').rstrip('.')
    return len(host) <= 253 and all(re.fullmatch(r'[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?', x) for x in host.split('.'))


def classify_url(raw, source, base_observed=False):
    if raw is None:
        return {'kind': 'missing', 'resolved': None}
    if not isinstance(raw, str):
        raise ValueError('URL evidence must be text or null')
    if len(raw) > MAX_URL_CHARS:
        raise TechnicalLimitError('Technical URL length budget exceeded')
    if not raw.strip():
        return {'kind': 'empty', 'resolved': None}
    invalid = {'kind': 'invalid', 'resolved': None}
    if any(ord(ch) < 32 or ch.isspace() for ch in raw) or '\\' in raw or re.search(r'%(?![0-9A-Fa-f]{2})', raw):
        return invalid
    try:
        p = urlsplit(raw)
        if p.scheme in {'mailto', 'tel'}:
            return {'kind': 'non_http', 'resolved': None}
        if p.scheme and p.scheme not in {'http', 'https'}:
            return {'kind': 'unsafe_or_unsupported', 'resolved': None}
        # Validate the original authority BEFORE urljoin can reinterpret a bad absolute URL.
        if p.scheme or raw.startswith('//'):
            if not p.netloc or not valid_host(p):
                return invalid
        if base_observed and not p.scheme:
            return {'kind': 'unresolved', 'resolved': None}
        resolved = urljoin(source, raw)
        if len(resolved) > MAX_URL_CHARS:
            raise TechnicalLimitError('Resolved URL length budget exceeded')
        p = urlsplit(resolved)
        if p.scheme not in {'http', 'https'} or not valid_host(p):
            return invalid
        kind = 'fragment' if raw.startswith('#') else 'internal' if p.hostname == urlsplit(source).hostname else 'external'
        return {'kind': kind, 'resolved': resolved}
    except TechnicalLimitError:
        raise
    except (ValueError, UnicodeError):
        return invalid


def normalized_component(value):
    # IRI to URI, uppercase percent hex, decode ONLY unreserved ASCII bytes.
    value = quote(value, safe="%/:?@!$&'()*+,;=-._~")
    return re.sub(r'%([0-9A-Fa-f]{2})', lambda m: chr(int(m[1], 16)) if chr(int(m[1], 16)) in UNRESERVED else '%' + m[1].upper(), value)


def url_identity(value):
    p = urlsplit(value)
    host = p.hostname.encode('idna').decode('ascii').lower()
    return (p.scheme, host, p.port if p.port is not None else (443 if p.scheme == 'https' else 80),
            normalized_component(p.path or '/'), normalized_component(p.query))


def parse_robots(value):
    parts = value.split(',')
    directives = []
    i = 0
    while i < len(parts):
        part = parts[i].strip()
        m = re.match(r'^([A-Za-z][A-Za-z0-9_-]*)\s*:\s*(.*)$', part)
        if m:
            name, argument = m[1].lower(), m[2].strip()
            if name == 'unavailable_after':
                while i + 1 < len(parts):
                    nxt = parts[i + 1].strip()
                    if re.match(r'^[A-Za-z][A-Za-z0-9_-]*(?:\s*:|(?:\s+[A-Za-z][A-Za-z0-9_-]*)*\s*$)', nxt):
                        break
                    i += 1
                    argument += ', ' + parts[i].strip()
            directives.append(name + ': ' + argument)
        else:
            directives.extend(x.lower() for x in part.split())
        i += 1
    directives = sorted(set(directives))
    expanded = set(directives)
    if 'none' in expanded:
        expanded.update({'noindex', 'nofollow'})
    # 'all' means no restrictions, not an explicit index/follow instruction.
    return directives, sorted(expanded)


def build_evidence(raw, source):
    warnings = list(raw['warnings'])
    if raw['base_observed'] and 'base_url_requires_manual_review' not in warnings:
        warnings.append('base_url_requires_manual_review')
    if any(x['in_head'] is None for x in raw['canonicals'] + raw['robots']):
        if 'implicit_head_body_location' not in warnings:
            warnings.append('implicit_head_body_location')
    canonical = []
    for item in raw['canonicals']:
        target = classify_url(item['href'], source, raw['base_observed'])
        canonical.append(dict(item, **target, relative=bool(item['href'] and not re.match(r'^[A-Za-z][A-Za-z0-9+.-]*:', item['href']))))
    robots = []
    groups = {agent: set() for agent in ('robots', 'googlebot', 'bingbot')}
    for item in raw['robots']:
        directives, expanded = parse_robots(item['content'])
        robots.append(dict(item, directives=directives, expanded=expanded))
        if item['in_head'] is True:
            groups[item['agent']].update(expanded)
    conflicts = [agent for agent, rules in groups.items() if {'index', 'noindex'} <= rules or {'follow', 'nofollow'} <= rules]
    links = [dict(index=i, href=href, **classify_url(href, source, raw['base_observed'])) for i, href in enumerate(raw['links'])]
    counts = {kind: sum(x['kind'] == kind for x in links) for kind in KINDS}
    priorities = {'invalid': 0, 'unsafe_or_unsupported': 0, 'missing': 1, 'empty': 1, 'unresolved': 2}
    samples = sorted(links, key=lambda x: (priorities.get(x['kind'], 3), x['index']))[:20]
    headings = raw['headings']
    return {'raw': raw, 'measurement_reliable': not warnings, 'parse_warnings': warnings,
            'base_observed': raw['base_observed'], 'canonicals': canonical[:20], 'canonical_count': len(canonical),
            'robots': robots, 'robots_count': len(robots), 'global_directives': sorted(groups['robots']),
            'per_agent_directives': {k: sorted(v) for k, v in groups.items()}, 'conflicting_agents': conflicts,
            'h1_count': len(headings), 'empty_h1_count': sum(not h for h in headings),
            'h1': [h[:512] for h in headings[:32]], 'link_counts': counts, 'links': samples,
            'evidence_truncated': len(canonical) > 20 or len(headings) > 32 or any(len(h) > 512 for h in headings) or len(links) > 20}


def evidence_refs(code, e, source):
    raw = e['raw']
    if not e['measurement_reliable']:
        return ['parse_warnings'] if code == 'html_uncertain' else []
    if code == 'canonical_missing':
        return ['canonical_count'] if not raw['canonicals'] else []
    if code == 'canonical_multiple':
        return ['canonicals:' + str(i) for i in range(min(20, len(raw['canonicals'])))] if len(raw['canonicals']) > 1 else []
    if code.startswith('canonical_'):
        refs = []
        for i, item in enumerate(raw['canonicals']):
            target = classify_url(item['href'], source, raw['base_observed'])
            href = item['href']
            conditions = {
                'canonical_location': item['in_head'] is False,
                'canonical_invalid': target['kind'] not in {'internal', 'external', 'unresolved'} or bool(target['resolved'] and urlsplit(target['resolved']).fragment),
                'canonical_external': target['kind'] == 'external',
                'canonical_other_page': target['kind'] == 'internal' and url_identity(target['resolved']) != url_identity(source),
                'canonical_relative': bool(href and not re.match(r'^[A-Za-z][A-Za-z0-9+.-]*:', href))}
            if conditions.get(code):
                refs.append('canonicals:' + str(i))
        return refs[:20]
    if code.startswith('robots_'):
        refs = []
        for i, rule in enumerate(raw['robots']):
            _, expanded = parse_robots(rule['content'])
            matches = (rule['agent'] in e['conflicting_agents'] if code == 'robots_conflict'
                       else rule['agent'] == 'robots' and 'noindex' in expanded)
            if rule['in_head'] is True and matches:
                refs.append('robots:' + str(i))
        return refs[:20]
    if code in {'link_invalid', 'link_empty'}:
        kinds = {'invalid', 'unsafe_or_unsupported'} if code == 'link_invalid' else {'missing', 'empty'}
        return ['links:' + str(i) for i, href in enumerate(raw['links'])
                if classify_url(href, source, raw['base_observed'])['kind'] in kinds][:20]
    valid = {'h1_missing': e['h1_count'] == 0, 'h1_multiple': e['h1_count'] > 1, 'h1_empty': e['empty_h1_count'] > 0}
    return ['headings'] if valid.get(code) else []
