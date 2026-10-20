"""Fixture-only technical SEO observations; URLs are never fetched."""
import re
from urllib.parse import urlsplit

import offline_seo
from content_seo import PageText, ContentLimitError, bounded_report
from technical_rules import classify_url, url_identity, build_evidence, evidence_refs, TechnicalLimitError

MAX_LINKS = 500
MAX_URL_CHARS = 2048
UNMEASURED = ['HTTP status codes', 'Redirects', 'robots.txt', 'Sitemap', 'Google index status',
              'HTTP X-Robots-Tag', 'Rendered JavaScript/CSS', 'Target page availability']


class TechnicalPage(PageText):
    def __init__(self):
        super().__init__()
        self.canonicals, self.robots, self.links = [], [], []
        self.base_observed = False
        self.heading_indices = []

    def handle_starttag(self, tag, attrs):
        # Parent enforces depth/events/time, structure warnings and separate H1s.
        active = not any(t in {'script', 'style', 'template', 'noscript'} for t, _ in self.stack)
        tags = {t for t, _ in self.stack}
        in_head = True if 'head' in tags and 'body' not in tags else False if 'body' in tags and 'head' not in tags else None
        super().handle_starttag(tag, attrs)
        if not active:
            return
        if tag == 'h1':
            self.heading_indices.append(len(self.h1) - 1)
        a = dict(attrs)
        if tag == 'base' and 'href' in a:
            self.base_observed = True
            self.uncertain('base_url_requires_manual_review')
        if tag == 'link' and 'canonical' in (a.get('rel') or '').lower().split():
            self.canonicals.append({'href': a.get('href'), 'in_head': in_head})
        if tag == 'meta' and (a.get('name') or '').lower() in {'robots', 'googlebot', 'bingbot'}:
            value = a.get('content') or ''
            if len(value) > 2048:
                raise ContentLimitError('Robots directive length budget exceeded')
            self.robots.append({'agent': a['name'].lower(), 'content': value, 'in_head': in_head})
        if tag == 'a':
            self.links.append(a.get('href'))
        if max(len(self.canonicals), len(self.robots), len(self.links)) > MAX_LINKS:
            raise ContentLimitError('Technical element count budget exceeded')


def analyze(scope):
    base = offline_seo.analyze(scope)
    payload, digest = scope.read('pages.json')
    if digest != base['source_hashes']['pages.json']:
        raise offline_seo.SandboxError('Source changed during technical analysis')
    pages, proposals = [], []
    for page in payload['pages']:
        p = TechnicalPage()
        p.feed(page['html']); p.close()
        source = page['url']
        if classify_url(source, source)['kind'] != 'internal':
            raise offline_seo.SandboxError('Invalid technical source URL')
        raw = {'canonicals': p.canonicals, 'robots': p.robots, 'links': p.links,
               'headings': [' '.join(''.join(parts).split()) for parts in (p.h1[i] for i in p.heading_indices)],
               'warnings': p.warnings, 'base_observed': p.base_observed}
        try:
            evidence = build_evidence(raw, source)
        except TechnicalLimitError as exc:
            raise ContentLimitError(str(exc)) from exc
        canonical = [dict(item, **classify_url(item['href'], source, p.base_observed),
                          relative=bool(item['href'] and not re.match(r'^[A-Za-z][A-Za-z0-9+.-]*:', item['href']))) for item in p.canonicals]
        headings = raw['headings']
        global_rules = set(evidence['global_directives'])
        counts = evidence['link_counts']
        findings = []
        if not canonical:
            findings.append(('canonical_missing', 'HTML içinde canonical bulunamadı; sayfanın amacı için gerekli olup olmadığını inceleyin.'))
        elif len(canonical) > 1:
            findings.append(('canonical_multiple', f'{len(canonical)} canonical etiketi var; amaçlanan tek tercih edilen URL bilgisini gözden geçirin.'))
        for item in canonical:
            if item['in_head'] is False:
                findings.append(('canonical_location', 'Canonical head dışında gözlendi; HTML yerleşimini inceleyin.'))
            if item['kind'] not in {'internal', 'external', 'unresolved'}:
                findings.append(('canonical_invalid', 'Canonical boş, eksik veya uygun bir HTTP(S) hedefi değil; değeri inceleyin.'))
            elif item['kind'] == 'external':
                findings.append(('canonical_external', 'Canonical farklı bir alan adına yöneliyor; bunun bilinçli bir tercih olduğunu doğrulayın.'))
            elif item['resolved'] and url_identity(item['resolved']) != url_identity(source):
                findings.append(('canonical_other_page', 'Canonical başka bir URL belirtiyor; bu bir hata olmak zorunda değildir, tercih edilen sayfayı doğrulayın.'))
            if item['resolved'] and urlsplit(item['resolved']).fragment:
                findings.append(('canonical_invalid', 'Canonical fragment içeriyor; tercih edilen URL bilgisini fragment olmadan tanımlamayı değerlendirin.'))
            if item['relative']:
                findings.append(('canonical_relative', 'Göreli canonical gözlendi; hedefi doğrulayıp açık bir mutlak URL kullanmayı değerlendirin.'))
        if evidence['conflicting_agents']:
            findings.append(('robots_conflict', 'Şu meta gruplarında karşıt açık direktifler gözlendi: ' + ', '.join(evidence['conflicting_agents']) + '. Amaçlanan kuralları inceleyin; nihai indeks durumu ölçülmedi.'))
        if 'noindex' in global_rules:
            findings.append(('robots_noindex', 'Genel robots meta içinde noindex/none var. Bu yalnızca HTML gözlemidir; kaldırmadan önce sayfanın amacını doğrulayın.'))
        if not headings:
            findings.append(('h1_missing', 'H1 bulunamadı; sayfanın ana konusunu açıklayan başlık ihtiyacını inceleyin.'))
        elif len(headings) > 1:
            findings.append(('h1_multiple', f'{len(headings)} H1 gözlendi; başlık hiyerarşisini inceleyin. Bu tek başına sıralama cezası kanıtı değildir.'))
        if any(not h for h in headings):
            findings.append(('h1_empty', 'Çıkarılan H1 metni boş; gizlilik işaretlerini ve anlamlı başlık ihtiyacını inceleyin.'))
        if counts['invalid'] or counts['unsafe_or_unsupported']:
            findings.append(('link_invalid', 'Uygun HTTP(S) bağlantısı olarak değerlendirilemeyen hedefler var; örnekleri inceleyin. Hedeflere istek gönderilmedi.'))
        if counts['missing'] or counts['empty']:
            findings.append(('link_empty', 'Href alanı boş veya eksik a etiketleri var; bağlantı mı yoksa yer tutucu mu olduklarını inceleyin.'))
        if not evidence['measurement_reliable']:
            findings = [('html_uncertain', 'HTML veya base URL belirsiz; ölçümleri kesin kabul etmeden önce örneği inceleyin.')]
        pages.append({'customer_id': scope.customer_id, 'page_id': page['id'], 'url': source, 'evidence': evidence})
        # One finding per code and page; no duplicated evidence blobs.
        for code, advice in dict(findings).items():
            proposals.append({'customer_id': scope.customer_id, 'page_id': page['id'], 'url': source,
                              'evidence_page_id': page['id'], 'code': code, 'recommendation': advice,
                              'evidence_refs': evidence_refs(code, evidence, source),
                              'human_approval_required': True, 'applied': False, 'status': 'awaiting_human_review'})
    return bounded_report({'schema_version': 2, 'operation': 'seo.technical', 'customer_id': scope.customer_id,
                           'synthetic': True, 'source': 'wordpress-fixture', 'connection': 'none', 'mode': 'proposal-only',
                           'source_hashes': base['source_hashes'], 'pages': pages, 'proposals': proposals,
                           'unmeasured': list(UNMEASURED)})
