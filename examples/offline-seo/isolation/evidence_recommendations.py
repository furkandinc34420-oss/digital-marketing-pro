"""Pure, bounded recommendation drafts from validated offline SEO reports.

No HTML execution, network, file writes, approval or application capability.
Expected hashes MUST come from a trusted source/task binding, not a request body.
"""
import copy
import hashlib
import json

import audit_report as audit

MAX_OUTPUT_BYTES = 1024 * 1024
MAX_RECOMMENDATIONS = 200
MAX_CANDIDATES = 3000
MAX_EVIDENCE = 3000
UNKNOWN = ['Sektör ve arama niyeti doğrulanmadı.', 'Trafik, sıralama, dönüşüm ve rakip ölçümü yok.',
           'Ölçümler bu katmanda HTML’den yeniden üretilmedi; doğrulanmış uzman raporu kullanıldı.',
           'Çevrimdışı HTML gözlemi tarayıcı veya Google indeks sonucu değildir.']
RULES = {
    'title_missing': ('HTML raporunda sayfa başlığı eksik', 'Sayfa içeriğini doğru özetleyen bir başlık taslağı hazırlayın.',
                      'Başlık sayfanın konusunu açıklamalıdır.', 'Başlığın mevcut ve içerikle uyumlu olduğunu tekrar analiz ve insan incelemesiyle doğrulayın.'),
    'canonical_missing': ('HTML raporunda canonical gözlenmedi', 'İçerik kopyalarını ve tercih edilen URL amacını inceleyin; gerekliyse canonical taslağı hazırlayın.',
                          'Canonical ihtiyacı URL ve içerik stratejisine bağlıdır; eksiklik tek başına hata kanıtı değildir.', 'Tercih edilen hedef insan tarafından onaylansın; yeni HTML analizinde amaçlanan canonical gözlensin.'),
    'robots_noindex': ('Raporda genel noindex direktifi gözlendi', 'Sayfanın indekslenme amacını insan doğrulasın; noindex kaldırmayı otomatik önermeyin.',
                       'Noindex bilinçli bir tercih olabilir; gerçek indeks durumu ölçülmedi.', 'İndeksleme tercihi belgelensin; yeni analizde amaçlanan direktifler gözlensin. Google sonucu ayrıca ölçülmelidir.'),
    'h1_missing': ('HTML raporunda H1 gözlenmedi', 'Sayfanın konusunu açıklayan H1 taslağını erişilebilirlik ve içerik açısından değerlendirin.',
                   'Başlık yapısı içeriğin anlaşılmasına yardım eder; tek H1 bir sıralama garantisi değildir.', 'Yeni analizde anlamlı H1 gözlensin ve insan tarafından içerikle uyumu doğrulansın.'),
    'html_uncertain': ('HTML ölçümünün güvenilirliği belirsiz', 'Önce ayrıştırma uyarılarını ve kaynak HTML yapısını insan incelemesiyle doğrulayın.',
                       'Belirsiz ölçümden kesin SEO düzeltmesi çıkarılamaz.', 'Aynı analiz tekrarlandığında belirsizlik giderilsin veya kalan sınırlama açıkça belgelensin.'),
}


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                      allow_nan=False, separators=(',', ':')).encode()).hexdigest()


def _observation(operation, page, code):
    """Only measured facts; None means this expert cannot substantiate the rule."""
    e = page.get('evidence', {})
    reliable = e.get('measurement_reliable', True)
    metrics = {}; present = None
    if operation == 'seo.analyze':
        reliable = not any(i['message'] in {'HTML parsing failed; meta checks incomplete',
                            'Empty response body; could not parse meta tags'} for i in page['issues'])
        e = page['meta']
    if code.startswith(('title_', 'description_')) and code.split('_',1)[1] in {'missing','too_short','too_long'} and operation == 'seo.analyze':
        field, condition = code.split('_', 1)
        value = e[field]
        metrics = {field: value[:512] if value else value, field + '_length': e[field + '_length'],
                   field + '_status': e[field + '_status'], 'display_truncated': bool(value and len(value) > 512)}
        present = e[field + '_status'] == condition
    elif code == 'title_missing' and operation == 'seo.content':
        metrics = {'title': e['title'], 'display_truncated': e['display_truncated']}
        present = not e['title']
    elif code == 'canonical_missing':
        if operation == 'seo.technical':
            metrics = {'canonical_count': e['canonical_count']}; present = e['canonical_count'] == 0
        elif operation == 'seo.analyze':
            value = e.get('canonical')
            if value is None or isinstance(value, str):
                metrics = {'canonical': value[:512] if value else value}; present = not value
    elif code == 'robots_noindex' and operation == 'seo.technical':
        metrics = {'global_directives': e['global_directives'], 'per_agent_directives': e['per_agent_directives']}
        present = 'noindex' in e['global_directives']
    elif code == 'h1_missing' and operation == 'seo.technical':
        metrics = {'h1_count': e['h1_count']}; present = e['h1_count'] == 0
    elif code in {'keyword_title', 'keyword_h1', 'keyword_intro', 'keyword_absent'} and operation == 'seo.content':
        field = {'keyword_title':'keyword_in_title', 'keyword_h1':'keyword_in_h1',
                 'keyword_intro':'keyword_in_first_100_words', 'keyword_absent':'keyword_occurrences'}[code]
        metrics = {'primary_keyword': e['primary_keyword'], 'keyword_source': e['keyword_source'], field: e[field]}
        present = None if e['primary_keyword'] is None or e[field] is None else e[field] == (0 if code == 'keyword_absent' else False)
    elif code == 'html_uncertain' and operation != 'seo.analyze':
        metrics = {'parse_warnings': e['parse_warnings'], 'measurement_reliable': reliable}; present = not reliable
    return {'metrics': metrics, 'supports_issue': present, 'measurement_reliable': reliable}


def _rule(code):
    if code in RULES: return RULES[code]
    if code.startswith(('title_', 'description_')) and code.endswith(('too_short','too_long','missing')):
        return ('Meta alanı aracın karakter eşiği dışında', 'Meta metnini sayfa amacı ve içerikle karşılaştırarak taslak hazırlayın.',
                '30–60 başlık ve 120–160 açıklama eşikleri bu aracın sezgisidir; Google kuralı değildir.',
                'Yeni değer ve karakter sayısı tekrar ölçülsün; anlam ve doğruluk insan tarafından onaylansın.')
    if code in {'keyword_title','keyword_h1','keyword_intro','keyword_absent'}:
        return ('Beyan edilmiş hedef kelime için kullanım uyarısı', 'Önce beyan edilen kelimenin sayfa niyetine uygunluğunu doğrulayın; gerekliyse doğal metin taslağı hazırlayın.',
                'Eşleşme sözcükseldir; arama hacmi, talep veya anlamsal uygunluk ölçülmedi.',
                'İlgili kullanım işareti tekrar ölçülsün; doğal anlatım ve niyet uyumu insan tarafından doğrulansın. Zorunlu kelime eklemek başarı sayılmaz.')
    return ('Uzman uyarısı doğrulama gerektiriyor', 'Değişiklik taslağından önce ilgili kanıtı insan incelemesiyle doğrulayın.',
            'Bu ilk sürüm bu sorun kodu için bağımsız doğrulama kuralı içermiyor.',
            'Uyarı kaynak veriyle doğrulansın veya gerekçeli biçimde geçersiz olarak belgelensin.')


def _priority_reason(code, verified):
    if code == 'html_uncertain': return 'Diğer ölçümlere güvenmeden önce ayrıştırma belirsizliği giderilmeli.'
    if not verified: return 'Sorun kesinleşmedi; bu yalnızca kanıt inceleme sırasıdır, değişiklik önceliği değildir.'
    if code == 'title_missing': return 'Başlık gözlenmedi; sayfa konusunun tanımını incelemek önceliklidir. Trafik etkisi ölçülmedi.'
    if code == 'h1_missing': return 'Başlık yapısı eksik gözlendi; okunabilirlik ve içerik düzeni incelenmeli. Sıralama etkisi bilinmiyor.'
    if code == 'robots_noindex': return 'İndeksleme tercihi ile sayfanın amacı çelişebilir; önce amacı doğrulayın. Google durumu ölçülmedi.'
    if code == 'canonical_missing': return 'Canonical her sayfada zorunlu değildir; kopyalar ve tercih edilen hedef bilinmediği için düşük öncelik.'
    if code.startswith(('title_', 'description_')): return 'Karakter eşikleri sezgiseldir; düşük inceleme önceliği trafik etkisi tahmini değildir.'
    if code.startswith('keyword_'): return 'Hedef kelimenin arama talebi ve niyeti doğrulanmadı; düşük inceleme önceliği.'
    return 'Doğrulama kuralı sınırlı; bu sıra ticari etki veya trafik tahmini değildir.'


def _risks(code):
    if code.startswith('canonical_'): return ['Yanlış hedef, farklı sayfaların sinyallerini birleştirebilir.', 'İçerik kopyaları ve tercih edilen URL henüz doğrulanmadı.']
    if code.startswith('robots_'): return ['Bilinçli noindex tercihinin kaldırılması istenmeyen sayfaların görünürlüğünü etkileyebilir.', 'Direktif gözlemi gerçek indeks durumu değildir.']
    if code.startswith('keyword_'): return ['Kelime doldurma doğal anlatımı bozabilir.', 'Beyan edilen hedef kelime yanlış niyeti temsil edebilir.']
    if code.startswith(('title_', 'description_')): return ['Sırf karakter eşiğini tutturmak anlamı ve doğruluğu bozabilir.', 'Yeni metin sayfa içeriğini yanlış temsil edebilir.']
    if code.startswith('h1_'): return ['Başlık değişikliği erişilebilirlik ve içerik yapısını bozabilir.']
    return ['Belirsiz veya desteklenmeyen kanıtla değişiklik yapmak yanlış sorunu çözmeye çalışabilir.']


def build(report, customer, expected_hashes):
    """Consume one of four reports; never accept a mixed tenant/source bundle."""
    audit.bounded(report)
    # Bound collection work before expert validation/composition or copying.
    bundles = ([item.get('result') for item in report.get('experts', {}).values()
                if isinstance(item, dict) and item.get('status') == 'succeeded']
               if isinstance(report, dict) and report.get('operation') == 'seo.audit'
               else [report])
    count = 0
    for bundle in bundles:
        if not isinstance(bundle, dict): raise ValueError('Invalid expert bundle')
        count += len(bundle.get('proposals', []))
        for page in bundle.get('pages', []):
            count += len(page.get('issues', []))
        if count > min(MAX_CANDIDATES, MAX_EVIDENCE):
            raise ValueError('Recommendation evidence limit exceeded')
    report = copy.deepcopy(report)
    if not isinstance(report, dict) or report.get('customer_id') != customer or report.get('source_hashes') != expected_hashes:
        raise ValueError('Recommendation source/customer mismatch')
    operation = report.get('operation', 'seo.analyze')
    if operation == 'seo.audit':
        audit.validate_audit(report, customer)
        catalog = report['page_catalog']; complete = report['complete']
        experts = {op:report['experts'][op]['result'] for op in audit.OPERATIONS if report['experts'][op]['status'] == 'succeeded'}
        states = {op:item['status'] for op,item in report['experts'].items()}
    else:
        if operation not in audit.OPERATIONS: raise ValueError('Unsupported recommendation input')
        catalog = {str(p['page_id']):p['url'] for p in report['pages']}
        audit.validate_expert(operation, report, customer, expected_hashes, catalog)
        experts = {operation:report}; states = {operation:'succeeded'}; complete = True
    # Private copy prevents output dictionaries from aliasing caller-owned reports.
    indexed = {op:{p['page_id']:p for p in r['pages']} for op,r in experts.items()}
    grouped = {}
    seen_claims = set()
    for op,r in experts.items():
        for proposal,code,_,source in audit.candidates(op,r):
            key = (proposal['page_id'],proposal['url'],code)
            if key not in grouped:
                if len(grouped) >= MAX_RECOMMENDATIONS: raise ValueError('Recommendation count limit exceeded')
                grouped[key] = []
            prefix = f'/experts/{op}/result' if operation == 'seo.audit' else ''
            path = (f"{prefix}/proposals/{source['proposal_index']}" if 'proposal_index' in source
                    else f"{prefix}/pages/{next(i for i,p in enumerate(r['pages']) if p['page_id']==key[0])}/issues/{source['issue_index']}")
            evidence = {'operation':op, 'claim_path':path}
            claim_key = (key, op, path)
            if claim_key not in seen_claims:
                seen_claims.add(claim_key)
                grouped[key].append(evidence)
    report_hash = fingerprint(report)
    drafts = []
    remaining = MAX_OUTPUT_BYTES - 4096  # Reserve bounded envelope metadata.
    for (pid,url,code),claims in grouped.items():
        evidence = []; uncertainties = list(UNKNOWN)
        for op,r in experts.items():
            page = indexed[op][pid]; measured = _observation(op,page,code)
            if measured['metrics']:
                pi = next(i for i,p in enumerate(r['pages']) if p['page_id']==pid)
                prefix = f'/experts/{op}/result' if operation == 'seo.audit' else ''
                evidence.append({'operation':op,'page_path':f'{prefix}/pages/{pi}', **measured})
        supports = [e['supports_issue'] for e in evidence if e['supports_issue'] is not None]
        page_uncertain = any(not p[pid].get('evidence',{}).get('measurement_reliable',True) for p in indexed.values())
        # No independent source measurement or producer-authenticated result is
        # available here. Structural consistency is NOT proof of observation.
        report_consistent = bool(supports) and all(supports) and not page_uncertain and all(e['measurement_reliable'] for e in evidence)
        verified = False
        uncertainties.append('Kaynak baytları eşleşse de rapor ölçümleri kaynak HTML üzerinden bağımsız doğrulanmadı; üreticiye bağlı güvenilir sonuç bütünlüğü yok.')
        evidence.sort(key=lambda e: (e['operation'], e['page_path']))
        claims = sorted(claims, key=lambda e: (e['operation'], e['claim_path']))
        if len(experts) < 3: uncertainties.append('Yalnızca mevcut başarılı uzmanlar kapsanır; diğer SEO boyutları doğrulanmadı.')
        for op,pages in indexed.items():
            for warning in pages[pid].get('evidence',{}).get('parse_warnings',[]):
                message = op + ': ' + warning
                if message not in uncertainties: uncertainties.append(message)
        if not complete: uncertainties.append('Birleşik denetim eksik; diğer uzmanlar farklı kanıt sağlayabilir.')
        if not verified: uncertainties.append('Kanıt eksik, çelişkili veya ayrıştırma belirsiz; sorun kesinleştirilmedi.')
        problem,action,reason,success = _rule(code)
        if not verified:
            problem = 'Doğrulama bekleyen uyarı: ' + code
            action = 'Önce kaynak kanıtı ve uzmanlar arasındaki farkı insan incelemesiyle doğrulayın; değişiklik önermeyin.'
            success = 'Kaynak HTML ile sorun doğrulansın veya uyarı gerekçeli biçimde elensin.'
        priority = 'high' if code == 'html_uncertain' else 'medium' if code in {'title_missing','h1_missing','robots_noindex'} else 'low'
        draft = {'customer_id':customer,'page_id':pid,'url':url,'code':code,'status':'draft',
            'issue':problem,'evidence':{'claims':claims,'observations':evidence,'source_hashes':copy.deepcopy(expected_hashes),
                        'source_locator':{'file':'pages.json','page_id':pid},
                        'measurement_verification':'unverified', 'report_measurements_consistent':report_consistent},
            'proposed_action':action,'technical_rationale':reason,
            'priority':{'level':priority,'reason':_priority_reason(code,verified)},
            'confidence':{'level':'medium' if verified else 'low','scope':'offline_observation_only','uncertainties':uncertainties},
            'risks':_risks(code),
            'baseline':copy.deepcopy(evidence),'success_criterion':{'description':success,'measured':False,
                'method':'Aynı araçlarla yeni kaynak görüntüsünü yeniden analiz ve insan incelemesi.'},
            'human_approval_required':True,'applied':False,
            'approval':{'status':'not_requested','apply_supported':False,
                        'requirements':['Güncel yetki ve kaynak özeti doğrulansın.','Kesin değişiklik farkı insan tarafından incelensin.']}}
        # Binding covers tenant, source, report, rule, evidence and exact draft.
        audit.bounded(draft, remaining - 256)
        draft['recommendation_id'] = fingerprint({'report_sha256':report_hash,'draft':draft})
        remaining -= len(json.dumps(draft,ensure_ascii=False,allow_nan=False).encode('utf-8')) + 2
        drafts.append(draft)
    drafts.sort(key=lambda d:({'high':0,'medium':1,'low':2}[d['priority']['level']],d['page_id'],d['code']))
    output = {'schema_version':1,'customer_id':customer,'operation':'seo.recommendations',
              'mode':'draft-only','connection':'none','synthetic':True,'source_operation':operation,
              'source_hashes':copy.deepcopy(expected_hashes),'report_sha256':report_hash,
              'evidence_scope':'validated_expert_report_not_independent_html_reanalysis',
              'source_complete':complete,'overall_audit_complete':operation == 'seo.audit' and complete,
              'coverage':{'included_experts':sorted(experts),'not_included':[op for op in audit.OPERATIONS if op not in experts]},
              'expert_states':states,'industry_context':{'status':'not_provided','value':None},
              'unmeasured':['Trafik','Sıralama','Dönüşüm','Rakip verisi','Google indeks durumu'],
              'recommendations':drafts,'human_approval_required':True,'applied':False}
    return audit.bounded(output, MAX_OUTPUT_BYTES)


def from_record(record, customer, sources):
    """Trusted facade bridge. Current fixtures must match the durable task input.

    Historical sources are not guessed; changed input requires a fresh analysis
    or a future trusted archive containing the original snapshot.
    """
    from coordinator import input_digest
    from offline_seo import MAX_FILE_BYTES
    if set(sources) != {'profile.json','pages.json'} or any(type(raw) is not bytes or len(raw) > MAX_FILE_BYTES for raw in sources.values()):
        raise ValueError('Invalid recommendation source snapshot')
    if record.get('operation') not in (*audit.OPERATIONS,'seo.audit') or record.get('status') not in {'succeeded','failed','interrupted'}:
        raise ValueError('No usable recommendation source task')
    if input_digest(record['operation'],sources) != record.get('input_digest'):
        raise ValueError('Recommendation task input changed')
    expected = {name:hashlib.sha256(raw).hexdigest() for name,raw in sources.items()}
    result = record.get('result')
    if not isinstance(result,dict): raise ValueError('No source report')
    if record['operation'] != result['result'].get('operation','seo.analyze'):
        raise ValueError('Recommendation task/report operation mismatch')
    report = result['result']
    if record['operation'] != 'seo.audit' and record['status'] != 'succeeded':
        raise ValueError('Independent expert did not complete successfully')
    if record['operation'] == 'seo.audit' and report.get('execution_state') != record['status']:
        raise ValueError('Audit task/report state mismatch')
    profile = json.loads(sources['profile.json'].decode('utf-8-sig'))
    payload = json.loads(sources['pages.json'].decode('utf-8-sig'))
    if any(d.get('customer_id') != customer or d.get('synthetic') is not True for d in (profile,payload)):
        raise ValueError('Foreign source snapshot')
    pages = payload.get('pages')
    if not isinstance(pages,list) or not 1 <= len(pages) <= 100:
        raise ValueError('Invalid source page catalog')
    catalog = {}
    for page in pages:
        if page.get('customer_id') != customer or type(page.get('id')) is not int or page['id'] <= 0 or str(page['id']) in catalog:
            raise ValueError('Invalid source page ownership')
        catalog[str(page['id'])] = page['url']
    if record['operation'] == 'seo.audit':
        if report.get('page_catalog') != catalog: raise ValueError('Foreign audit page catalog')
    else:
        audit.validate_expert(record['operation'],report,customer,expected,catalog)
    return build(report,customer,expected)
