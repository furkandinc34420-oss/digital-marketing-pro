"""Deterministic fixture-only content SEO observations, never ranking predictions."""
from collections import Counter
from html.parser import HTMLParser
import re
import json
import time

import offline_seo
from content_schema import MAX_REPORT_BYTES

MAX_KEYWORD_CHARS = 120
MAX_DEPTH = 64
MAX_EVENTS = 10000
MAX_PARSE_SECONDS = 2.0


class ContentLimitError(offline_seo.SandboxError):
    """Bounded content processing or output budget exceeded."""


def bounded_report(report):
    size = 0
    for part in json.JSONEncoder(ensure_ascii=False, allow_nan=False).iterencode(report):
        size += len(part.encode('utf-8'))
        if size > MAX_REPORT_BYTES:
            raise ContentLimitError('Content report exceeds byte budget')
    return report
STOP_WORDS = set('ve veya bir bu şu için ile de da the and of to in a an'.split())
VOID = set('area base br col embed hr img input link meta param source track wbr'.split())
BLOCK = set('p div section article header footer h1 h2 h3 h4 h5 h6 li ul ol br hr table tr td'.split())


def tokens(text):
    # Turkish case handling, then Unicode words. No stemming or semantic inference.
    return re.findall(r'[^\W_]+', text.replace('İ', 'i').replace('I', 'ı').casefold())


def occurrences(words, phrase):
    return sum(words[i:i + len(phrase)] == phrase for i in range(len(words) - len(phrase) + 1)) if phrase else 0


class PageText(HTMLParser):
    """Conservative extraction: ambiguous structure is never treated as reliable."""
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack = []
        self.body, self.title, self.h1 = [], [], []
        self.noindex = False
        self.warnings = []
        self.events = self.text_chars = 0
        self.deadline = time.monotonic() + MAX_PARSE_SECONDS

    def tick(self):
        self.events += 1
        if self.events > MAX_EVENTS or time.monotonic() > self.deadline:
            raise ContentLimitError('HTML event or processing time budget exceeded')

    def uncertain(self, reason):
        if reason not in self.warnings and len(self.warnings) < 8:
            self.warnings.append(reason)

    def handle_starttag(self, tag, attrs):
        self.tick()
        attributes = dict(attrs)
        if len(attributes) != len(attrs):
            self.uncertain('duplicate_attributes')
        if tag == 'meta' and (attributes.get('name') or '').lower() == 'robots':
            self.noindex |= 'noindex' in re.split(r'[\s,]+', (attributes.get('content') or '').lower())
        style = re.sub(r'\s+', '', (attributes.get('style') or '').lower())
        hidden = (tag in {'script', 'style', 'template', 'noscript'} or 'hidden' in attributes
                  or 'display:none' in style or 'visibility:hidden' in style)
        p_closers = BLOCK - {'br'} | set('address aside blockquote details dl fieldset figure figcaption form main menu nav pre search'.split())
        if tag in p_closers and any(t == 'p' for t, _ in self.stack):
            self.uncertain('implicit_paragraph_closure')
        if tag in {'p', 'li', 'h1'} and any(t == tag for t, _ in self.stack):
            self.uncertain('implicit_or_nested_block_closure')
        if tag == 'title' and self.title:
            self.uncertain('multiple_titles')
        if tag in BLOCK:
            self.body.append(' ')
            if any(t == 'h1' for t, _ in self.stack) and self.h1:
                self.h1[-1].append(' ')
        if tag not in VOID:
            if len(self.stack) >= MAX_DEPTH:
                raise ContentLimitError('HTML nesting depth exceeded')
            self.stack.append((tag, hidden))
        if tag == 'h1':
            self.h1.append([])

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in VOID:
            self.handle_endtag(tag)

    def handle_endtag(self, tag):
        self.tick()
        if not self.stack or self.stack[-1][0] != tag:
            self.uncertain('mismatched_or_unexpected_closing_tag')
        for index in range(len(self.stack) - 1, -1, -1):
            if self.stack[index][0] == tag:
                del self.stack[index:]
                break
        if tag in BLOCK:
            self.body.append(' ')
            if any(t == 'h1' for t, _ in self.stack) and self.h1:
                self.h1[-1].append(' ')

    def handle_data(self, data):
        self.tick()
        self.text_chars += len(data)
        if self.text_chars > offline_seo.MAX_HTML_CHARS:
            raise ContentLimitError('Extracted text budget exceeded')
        if any(hidden for _, hidden in self.stack):
            return
        tags = {tag for tag, _ in self.stack}
        if 'title' in tags:
            self.title.append(data)
        if 'h1' in tags:
            self.h1[-1].append(data)
        if not tags & {'head', 'title'}:
            self.body.append(data)

    def handle_comment(self, data):
        self.tick()

    def handle_decl(self, decl):
        self.tick()

    def handle_pi(self, data):
        self.tick()
        self.uncertain('processing_instruction')

    def unknown_decl(self, data):
        self.tick()
        self.uncertain('unknown_declaration')

    def close(self):
        super().close()
        self.tick()
        if self.stack or self.rawdata:
            self.uncertain('unclosed_or_incomplete_markup')


def analyze(scope):
    # Reuse all fixture identity, URL, duplicate-ID and HTML limits and raw hashes.
    base = offline_seo.analyze(scope)
    payload, source_hash = scope.read('pages.json')
    if source_hash != base['source_hashes']['pages.json']:
        raise offline_seo.SandboxError('Source changed during content analysis')
    reports, proposals = [], []
    for page in payload['pages']:
        keyword = page.get('primary_keyword')
        if keyword is not None and (not isinstance(keyword, str) or not keyword.strip()
                or len(keyword) > MAX_KEYWORD_CHARS or not 1 <= len(tokens(keyword)) <= 12):
            raise offline_seo.SandboxError('Invalid bounded primary keyword')
        parsed = PageText()
        parsed.feed(page['html'])
        parsed.close()
        body = ' '.join(''.join(parsed.body).split())
        title = ' '.join(''.join(parsed.title).split())
        headings = [' '.join(''.join(parts).split()) for parts in parsed.h1]
        words, title_words = tokens(body), tokens(title)
        phrase = tokens(keyword) if keyword else []
        count = occurrences(words, phrase)
        title_terms = set(title_words) - STOP_WORDS
        absent = sorted(title_terms - set(words))
        evidence = {'body_word_count': len(words), 'title': title[:512], 'h1': [h[:512] for h in headings[:32]],
                    'display_truncated': len(title) > 512 or len(headings) > 32 or any(len(h) > 512 for h in headings),
                    'measurement_reliable': not parsed.warnings, 'parse_warnings': parsed.warnings,
                    'primary_keyword': keyword, 'keyword_source': 'fixture' if keyword else 'not_set',
                    'keyword_occurrences': count if phrase else None,
                    'keyword_in_title': bool(occurrences(title_words, phrase)) if phrase else None,
                    'keyword_in_h1': any(occurrences(tokens(h), phrase) for h in headings) if phrase else None,
                    'keyword_in_first_100_words': bool(occurrences(words[:100], phrase)) if phrase else None,
                    'title_terms_missing_from_body': [term[:128] for term in absent[:32]],
                    'missing_title_term_count': len(absent),
                    'title_body_overlap': round(len(title_terms & set(words)) / len(title_terms), 3) if title_terms else None,
                    'frequent_body_terms': [(term[:128], count) for term, count in Counter(w for w in words if w not in STOP_WORDS).most_common(8)],
                    'noindex_observed': parsed.noindex}
        if parsed.warnings:
            # Preserve observations as uncertain, not absence assertions or SEO advice.
            for field in ('body_word_count', 'keyword_occurrences', 'keyword_in_title', 'keyword_in_h1',
                          'keyword_in_first_100_words', 'title_body_overlap'):
                evidence[field] = None
            evidence['title_terms_missing_from_body'] = []
            evidence['missing_title_term_count'] = None
            evidence['frequent_body_terms'] = []
        opportunities = []
        if not title:
            opportunities.append(('missing_title', 'Başlık bulunamadı; sayfanın konusunu açıkça anlatan bir başlık hazırlayın.'))
        if not keyword:
            opportunities.append(('missing_target', 'Önce bu sayfanın amacına uygun bir hedef anahtar kelime belirleyin.'))
        else:
            for field, code, advice in (
                ('keyword_in_title', 'keyword_title', f'Konuya uygunsa başlıkta “{keyword}” ifadesini doğal biçimde kullanın.'),
                ('keyword_in_h1', 'keyword_h1', f'Ana başlığı “{keyword}” konusunu açıkça anlatacak şekilde gözden geçirin.'),
                ('keyword_in_first_100_words', 'keyword_intro', f'Giriş bölümünde “{keyword}” konusunu okuyucuya açıklayın.')):
                if not evidence[field]:
                    opportunities.append((code, advice))
            if count == 0:
                opportunities.append(('keyword_absent', f'Gövde metninde “{keyword}” bulunamadı; ilgiliyse bu konuyu açıklayan bir bölüm ekleyin.'))
        if absent:
            opportunities.append(('title_content_gap', 'Başlıktaki şu terimler gövdede yok: ' + ', '.join(term[:128] for term in absent[:12]) + '. İçeriği tamamlayın veya başlığı mevcut içerikle uyumlu hale getirin.'))
        if len(words) < 50:
            opportunities.append(('limited_content', f'Gövde {len(words)} kelime içeriyor. Okuyucunun sorusunu yanıtlayan açıklama ve somut örnek gerekip gerekmediğini inceleyin; sabit bir ideal uzunluk yoktur.'))
        if parsed.warnings:
            opportunities = [('html_uncertain', 'HTML yapısı belirsiz; içerik ölçümlerine dayanarak değişiklik önermeden önce örneği inceleyin.')]
        reports.append({'customer_id': scope.customer_id, 'page_id': page['id'], 'url': page['url'], 'evidence': evidence})
        for code, advice in opportunities:
            proposals.append({'customer_id': scope.customer_id, 'page_id': page['id'], 'url': page['url'],
                              'code': code, 'recommendation': advice, 'evidence_page_id': page['id'],
                              'source_hashes': base['source_hashes'], 'status': 'awaiting_human_review',
                              'human_approval_required': True, 'applied': False})
    return bounded_report({'schema_version': 1, 'customer_id': scope.customer_id, 'synthetic': True, 'source': 'wordpress-fixture',
            'mode': 'proposal-only', 'connection': 'none', 'operation': 'seo.content',
            'source_hashes': base['source_hashes'], 'pages': reports, 'proposals': proposals,
            'method': 'Literal Unicode token and phrase comparison; advisory heuristics only',
            'unmeasured': ['Search volume', 'Keyword difficulty', 'Rankings', 'Semantic relevance', 'Rendered CSS/JavaScript']})
