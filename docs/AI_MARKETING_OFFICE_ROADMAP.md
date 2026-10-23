# AI Marketing Office — geliştirme ve güvenlik yol haritası

Bu dosya AI Marketing Office çalışmasının bağımsız Türkçe takip kaydıdır.
İnceleme tabanı: `feat/offline-seo-demo`, commit
`b246e31cad933e66b0f1d16acdeb420ff447d0ee` (23 Ekim 2026).
Yeni özellik, servis bağlantısı veya üretime çıkış onayı değildir.

[Orijinal Anthropic yol haritası](roadmap-multiagent-sessions-api.md) ayrı bir
sağlayıcı planıdır; buradaki aşamaların ön koşulu değildir. ChatGPT/Codex ile
ilerlenebilir; Claude aboneliği veya Anthropic API zorunluluğu yoktur. Gelecekteki
veri/model sağlayıcılarının ücretleri ayrıca değerlendirilmelidir.

## Durum ve takip kuralları

- **tamamlandı:** Yalnızca belirtilen dar iş ve kabul kriterleri tamamdır; ürünün
  üretime hazır olduğu anlamına gelmez.
- **prototip:** Kod ve regresyon testleri vardır; kurgusal veri ve belgelenmiş
  güven sınırları içinde çalışır. Profesyonel hizmete hazır sayılmaz.
- **planlandı:** Henüz uygulanmamıştır. Ön koşulu beklemesi normaldir.
- **engellendi:** Belirtilen güvenlik/işletim kapısı kapanmadan devreye alınamaz.
- **P0:** Dış erişim veya gerçek yan etki öncesi güvenlik engeli. **P1:** Güvenli
  ve faydalı ilk hizmet için gerekli. **P2:** Sonraki genişleme/ölçek iyileştirmesi.
  Aynı öncelikte aşağıdaki bağımlılık sırası esas alınır.

Her satır bir takip işidir. Durum değiştirilirken ilgili commit, test sonucu ve
kabul kanıtı bu dosyaya eklenmelidir; planlanan iş yalnızca belge yazılarak
tamamlandı sayılmaz. Daha önce geçen testler yeni sürüm için otomatik başarı
kanıtı değildir. Her güvenlik değişiminde ilgili regresyonlar, tam paket ve
Docker varsa iki müşterili entegrasyon yeniden değerlendirilir. Atlanan testler
gerekçesiyle kaydedilir; başarı sayılmaz.

**Son kayıtlı doğrulama:** Tam paket 995 toplam / 986 başarılı / 9 atlanan /
0 başarısız; son kayıt öncesi ilgili 176 test tekrar çalıştırılmış ve tamamı
geçmiştir (iki müşterili gerçek Docker dahil). Bu belge görevi sırasında test
paketi yeniden çalıştırılmadı; yalnızca belge ve bağlantı kontrolleri yapıldı.

**Kapsam sınırı:** Yalnızca iki kurgusal müşteri kullanılır. Gerçek müşteri,
trafik, sıralama, dönüşüm veya pazarlama başarısı doğrulanmış değildir.
`agents/` altındaki 24 dosya **ajan tanımıdır**; 24 çalışan, koordinatöre bağlı,
test edilmiş uzman anlamına gelmez. Şu an üç uzman analiz işlemi ve bunları
birleştiren `seo.audit` vardır. Koordinatör deterministik Python kodudur;
ücretli model çağıran 24 ajanlı bir ofis değildir.

## Önce: Aşama 5'in kalan işleri hangi sırayla yapılmalı?

1. **5.4 — Kalıcı görev–kaynak bağı:** İşçinin hangi müşteri/görev/yürütmeye ait
   olduğunun, gönderim öncesinden itibaren güvenilir kaydı olmadan supervisor
   güvenli biçimde kaynak seçemez.
2. **5.5 — Bağımsız supervisor:** Ana süreç SIGKILL ile öldüğünde de çalışmalı;
   yalnızca doğrulanmış sahiplikteki işçileri durdurmalı ve gerçek durmayı doğrulamalı.
3. **5.6 — Artık kaynak kurtarma ve operatör uyarıları:** Başarısız sonlandırma,
   daemon kesintisi ve belirsiz oluşturma sonuçları kaybolmamalı.
4. **5.7 ve 5.8 — Kalıcı güvenlik kaydı ve yedekleme/kurtarma:** Kurtarma sırasında
   eski yetki veya oturumun canlanmadığı kanıtlanmalı. Kayıt tasarımı 5.4 ile
   birlikte başlayabilir; test edilmiş kurtarma dış erişimden önce gereklidir.
5. **5.9 — Gerçek OIDC doğrulaması**, ardından **5.10 — MFA ve güvenli giriş
   yaşam döngüsü**. Çevrimdışı doğrulama tasarımı/test vektörleri 1–4 ile paralel
   hazırlanabilir; burada henüz gerçek sağlayıcıya bağlanılmaz.
6. **5.11 — Çok süreçli yetki/oturum iptali:** İkinci servis süreci açılmadan önce
   ortak depo ve iptal tasarımı doğrulanır; mevcut PID koruması o zamana kadar korunur.
7. **5.12 — Dış erişim güvenlik kapısı:** Gereken işler bağımsız inceleme ile
   doğrulanmadan müşteri API'si/paneli açılmaz.

**OIDC ile SIGKILL güvenliği farklı sorunları çözer.** OIDC kimin giriş yaptığını
doğrular; ana süreç öldüğünde Docker işçisini durdurmaz. Supervisor ise kullanıcı
kimliğini doğrulamaz. Mevcut çevrimdışı prototipte ilk somut öncelik 5.4, ardından
5.5'tir; bunlar gerçek sağlayıcı veya maliyet gerektirmeden sınanabilir. Gerçek
müşteriye dış erişim için OIDC/MFA kapıları da gereklidir. OIDC eklenmesi tek başına
yayına çıkış gerekçesi değildir; çok süreçli dağıtım için ayrıca 5.11 şarttır.

## Aşama 1–4 — Mevcut temel ve profesyonel hizmete kalan işler

| İş | Durum / öncelik / aşama | Ön koşullar | Doğrulanabilir kabul kriteri | Kod, test veya belge |
|---|---|---|---|---|
| 1.1 Kurgusal çevrimdışı SEO demosu | prototip / P1 / 1 | Güvenilir Linux/POSIX host ve örnek dosyalar | İki müşterinin kimlikleri ayrılır; ham kaynak özetleri korunur; yol kaçışı, symlink/hardlink ve limit aşımı reddedilir; analiz kaynak dosyalarını değiştirmez. | [Demo](../examples/offline-seo/README.md), [kod](../scripts/offline_seo.py), [test](../tests/test_offline_seo.py) |
| 1.2 Gerçek veri alma sözleşmesi | planlandı / P1 / 6–7 | 5.12, müşteri izni, 6.1 | Fixture açıkça fixture kalır; gerçek veri ayrı adaptörden sürüm/tarih/kaynakla gelir; güvenli URL/ağ erişim politikası ayrıca test edilir; hiçbir gerçek site izinsiz taranmaz. | Yeniden kullanım: [demo veri sınırları](../examples/offline-seo/README.md) |
| 2.1 Docker müşteri işçileri | prototip / P0 / 2 | Güvenilir yerel Docker daemon | Her işçi yalnızca kendi müşterisini okur; çapraz okuma/yazma ve gerçek Unix Docker soketi bağlantısı reddedilir; ağ, mount, UID, yetenek ve kaynak ayarları çalıştırmadan doğrulanır. | [Runner](../examples/offline-seo/isolation/runner.py), [izolasyon](../examples/offline-seo/isolation/README.md), [test](../tests/test_offline_isolation.py) |
| 2.2 Üretim host ve imaj güvenliği | planlandı / P0 / 5 | İşletim hedefi, 5.4–5.8 | Host/Docker yönetici erişimi müşteriden ayrılır; güncel ve sürümü sabitlenmiş çalışma zamanı, imaj kökeni ve tarama politikası doğrulanır; kaçış/host riski belgelenir. Konteyner VM veya aynı UID saldırganına tam koruma sayılmaz. | Mevcut sınırlar: [izolasyon](../examples/offline-seo/isolation/README.md) |
| 3.1 Baş Koordinatör ve müşteri başına SQLite | prototip / P1 / 3 | 1.1, 2.1, güvenilir çağıran | Aynı görev/aynı girdi tekrar işçi başlatmaz; değişen girdi çakışır; yanlış müşteri/şema reddedilir; kesinti başarı gösterilmez; görev/depo sınırları uygulanır. | [Koordinatör](../examples/offline-seo/isolation/coordinator.py), [test](../tests/test_offline_coordinator.py), [belge](../examples/offline-seo/isolation/COORDINATOR.md) |
| 3.2 Yeni uzman kabul süreci | planlandı / P2 / 6–9 | 3.1, 5.12, uzmana özel ölçüm kaynağı | Her yeni işlem sabit kayıt tablosuna, girdi/çıktı şemasına, kaynak bütçesine, iptal ve iki müşterili güvenlik testlerine sahip olur; ajan tanımı tek başına çalışan uzman sayılmaz. | [Mevcut uzman arayüzü](../examples/offline-seo/isolation/coordinator.py), [ajan tanımları](../agents/) |
| 4.1 İçerik ve teknik SEO uzmanları | prototip / P1 / 4 | 1.1–3.1 | `seo.analyze`, `seo.content`, `seo.technical` ayrı çalışır; H1/kelime, canonical, robots ve bağlantı bulguları kanıta bağlanır; bozuk HTML/belirsizlik ve çıktı limitleri test edilir; HTTP/indeks durumu uydurulmaz. | [İçerik](../examples/offline-seo/isolation/CONTENT_SEO.md), [teknik](../examples/offline-seo/isolation/TECHNICAL_SEO.md), [içerik testi](../tests/test_offline_content_seo.py), [teknik test](../tests/test_offline_technical_seo.py) |
| 4.2 Birleşik `seo.audit` | prototip / P1 / 4 | 4.1 | Üç uzman aynı kaynak görüntüsüne bağlanır; farklı sorunlar kaybolmaz; öneri/kanıt/uzman referansları korunur; bozuk veya başka göreve ait kayıt reddedilir; uzman hatası eksiksiz başarı sunulmaz. | [Rapor](../examples/offline-seo/isolation/audit_report.py), [belge](../examples/offline-seo/isolation/SEO_AUDIT.md), [test](../tests/test_offline_audit.py) |
| 4.3 Profesyonel SEO doğruluk kapsamı | planlandı / P1 / 6 | 6.1, sürümlü test kaynakları | Tarayıcı/ayrıştırıcı farkları, gerçek dil/URL örnekleri ve uzman kontrol listesiyle yanlış pozitif/negatifler ölçülür; kural eşikleri Google zorunluluğu sayılmaz; canlı HTTP/robots.txt/sitemap/indeks bulguları yalnızca ayrı doğrulanmış veriyle üretilir. | Başlangıç: [teknik kurallar](../examples/offline-seo/isolation/technical_rules.py), [test](../tests/test_offline_technical_seo.py) |

## Aşama 5 — Güvenli kimlik, oturum ve yürütme

| İş | Durum / öncelik / aşama | Ön koşullar | Doğrulanabilir kabul kriteri | Kod, test veya belge |
|---|---|---|---|---|
| 5.1 Kalıcı kimlik ve müşteri yetki deposu | prototip / P0 / 5 | Güvenilir özel dizin, tek sahip süreç | Değişmez hesap ID, kimlik nesli ve açık üyelik korunur; admin üyelik olmadan müşteri verisi alamaz; bozuk indeks/kayıt reddedilir; kilit kaybı veya eski revizyonla yetki geri yazılamaz; SQL hatası güvenli kapatır. | [Depo](../examples/offline-seo/isolation/identity_store.py), [belge](../examples/offline-seo/isolation/IDENTITY_STORE.md), [test](../tests/test_offline_identity_store.py) |
| 5.2 Süreli oturum ve her istekte yetkilendirme | prototip / P0 / 5 | 5.1, güvenilir yerel fixture sağlayıcısı | Süre sonu/iptal/hesap kapanması/kimlik yeniden atama erişimi reddeder; okuma dosya değiştirmez; sonuç öncesi yetki yeniden kontrol edilir; restart/fork eski tokenı geçerli kılmaz; 1000 oturum sınırı ve saat hataları sınanır. | [Erişim](../examples/offline-seo/isolation/access_control.py), [oturum belgesi](../examples/offline-seo/isolation/SESSION_SECURITY.md), [erişim testi](../tests/test_offline_access.py), [oturum testi](../tests/test_offline_sessions.py) |
| 5.3 Süreç içi iptal ve SQLite kontrol maliyeti | prototip / P0 / 5 | 5.1–5.2, 2.1 | İptal ile Popen kısa kilitte sıralanır; gönderilmemiş create/import sahte stop_failed üretmez; gönderilmiş belirsiz sonuç unutulmaz; gerçek durma doğrulanır; müşteriler etkilenmez. Aynı kilitli kapsamda kontrol tekrarı azalırken sonraki istekte güncel yetki/depo kontrol edilir. | [Kontrol](../examples/offline-seo/isolation/execution_control.py), [iptal belgesi](../examples/offline-seo/isolation/CANCELLATION.md), [iptal testi](../tests/test_offline_cancellation.py), [başlatma testi](../tests/test_offline_dispatch.py) |
| 5.4 Kalıcı görev–Docker kaynak sahipliği | planlandı / P0 / 5, ilk iş | 3.1, 5.3; mevcut özeti ve kilitleri koruyan kayıt tasarımı | Müşteri, görev, benzersiz yürütme ve kaynak kimliği güvenilir kayıtta bağlanır; niyet gönderimden önce kalıcıdır; not_sent/sent_unconfirmed/identified ayrımı restart sonrası korunur; eksik/çelişkili sahiplikte başka kaynak durdurulmaz; create/import öncesi/sonrası öldürme testleri geçer. Docker ile SQLite arasında atomiklik varsayılmaz. | Başlangıç: [runner](../examples/offline-seo/isolation/runner.py), [koordinatör](../examples/offline-seo/isolation/coordinator.py), [yaşam döngüsü testleri](../tests/test_offline_dispatch.py) |
| 5.5 SIGKILL sonrası dış supervisor | planlandı / P0 / 5 | 5.4, 2.2; ana süreçten bağımsız yaşam döngüsü | Gerçek ana süreç SIGKILL testinde işçi bulunur; kayıt ve Docker sahipliği eşleşir; başka müşterinin/asıl görevin işçisi etkilenmez; kontrollü/zorunlu durma inspect ile doğrulanır; doğrulanamayan durum hata olur. Yeniden PID kullanımına güvenilmez; süre hedefi testte ölçülür. | Açık sınır: [iptal belgesi](../examples/offline-seo/isolation/CANCELLATION.md) |
| 5.6 Artık kaynak uzlaştırma ve operatör uyarıları | planlandı / P0 / 5 | 5.4–5.5, 5.7 kayıt sözleşmesi | Daemon kesintisi, lost reply ve stop/cleanup hatası kalıcı görünür; tekrar uzlaştırma idempotent olur; yalnızca doğrulanmış kaynaklar temizlenir, genel prune yoktur; çözülemeyen kaynaklar güvenilir operatöre bildirilir; görev yanlış başarılı gösterilmez. | Başlangıç: [mevcut temizleme](../examples/offline-seo/isolation/runner.py), [hata testleri](../tests/test_offline_dispatch.py) |
| 5.7 Kalıcı güvenlik ve yönetici denetim kayıtları | planlandı / P0 / 5 | 5.1, olay/veri saklama politikası | Kimlik/üyelik değişimi, erişim reddi, iptal ve kaynak hataları işlem kimliğiyle izlenir; sır/token/ham rapor yazılmaz; müşteri ayrımı, erişim izinleri, saklama/boyut sınırı ve silme politikası test edilir; kayıt arızasında kritik işlemin davranışı tanımlıdır. Mevcut bellek kuyruğu kalıcı kayıt sayılmaz. | Başlangıç: [erişim olayları](../examples/offline-seo/isolation/access_control.py), [yönetici hata kayıtları](../examples/offline-seo/isolation/coordinator.py) |
| 5.8 Yedekleme, güvenli kurtarma ve şema geçişleri | planlandı / P0 / 5 | 5.1, 5.4, 5.7; RPO/RTO ve anahtar sorumlusu | Tutarlı SQLite yedeği alınır; ayrı ortamda geri yükleme provası veri kaybı/süre hedefini ölçer; iptal edilmiş yetki ve oturum geri gelmez; eski/bozuk şema otomatik sıfırlanmaz; şifreleme, erişim ve anahtar kurtarma prosedürü sınanır. Kaynak kayıtları Docker gerçeğiyle uzlaştırılmadan görev başlatılmaz. | Mevcut sınır: [kimlik deposu](../examples/offline-seo/isolation/IDENTITY_STORE.md), [koordinatör](../examples/offline-seo/isolation/COORDINATOR.md) |
| 5.9 Gerçek OIDC sağlayıcı adaptörü | planlandı / P0 / 5 | 5.1–5.2; güvenilir sağlayıcı/istemci ayarı ve bağımsız inceleme | İzinli issuer/audience/azp, imza ve algoritma, süre, tek kullanımlık nonce/state, PKCE ve anahtar rotasyonu olumlu/olumsuz testlerden geçer; e-posta hesap kanıtı sayılmaz; dış token rolü yerel üyeliği aşamaz; sırlar loglanmaz. Fixture issue() gerçek girişe açılmaz. | Arayüz/tasarım: [oturum belgesi](../examples/offline-seo/isolation/SESSION_SECURITY.md), [IdentityProvider](../examples/offline-seo/isolation/access_control.py) |
| 5.10 MFA ve güvenli giriş/hesap kurtarma | planlandı / P0 / 5 | 5.9; sağlayıcı MFA politikası | En az yönetici ve ayrıcalıklı işlemler için MFA/yeniden doğrulama zorlanır; yok/düşük güvenli kimlik iddiası reddedilir; kurtarma MFA'yı aşamaz; güvenli çerez, TLS, CSRF, çıkış ve hız sınırı test edilir. Gerçek MFA henüz yoktur. | Tasarım girdisi: [oturum güven sınırı](../examples/offline-seo/isolation/SESSION_SECURITY.md) |
| 5.11 Çok süreçli ortak yetki ve iptal | planlandı / P0 / 5, ölçek öncesi | 5.4–5.8, ortak oturum/iptal deposu ve tutarlılık sözleşmesi | İki gerçek servis sürecinde iptal/üyelik kaldırma sonuç ve yeni kritik işlem erişimini kapatır; ağ/depo bölünmesinde güvenli kapanır; süresi dolmuş yetki önbelleği kullanılmaz; yeniden başlatma oturum diriltmez. Mevcut PID/fork reddi tasarım doğrulanmadan kaldırılmaz. | Mevcut PID sınırı: [erişim](../examples/offline-seo/isolation/access_control.py), [fork testleri](../tests/test_offline_sessions.py) |
| 5.12 Gerçek müşteri ve dış erişim kapısı | engellendi / P0 / 5 | 2.2, 5.4–5.10; çok süreç varsa 5.11 | Bağımsız güvenlik incelemesi ve tehdit modeli, gizli bilgi yönetimi, müşteri veri saklama/silme politikası, yedekten dönüş ve olay müdahale provası kabul edilir; başarısız kritik test yoktur; atlanan güvenlik testleri çözümlenir. Bu kapı yalnızca inceleme kanıtıyla açılır. | Sınırlar: [erişim belgesi](../examples/offline-seo/isolation/ACCESS_CONTROL.md), [iptal](../examples/offline-seo/isolation/CANCELLATION.md) |

5.3 için ölçülen fark, sağlıklı durum sorgusunda 12 → 6; 1000 oturum doluyken
yeni oturum reddinde 1003 → 4 tam sağlık/metadata kontrolüdür. Bu kontrol sayısıdır;
üretim gecikmesi, istek kapasitesi veya disk performansı garantisi değildir.
Tek süreçte işbirliği yapmayan Python adaptörü zorla durdurulamaz; güvenilmeyen
kod ana süreçte çalıştırılmamalıdır. Güvenilir CLI oturuma bağlı müşteri girişi değildir.

## Aşama 6 — Kanıta Dayalı Öneri Sistemi ve SEO/GEO uzmanlığı

Bu numaralama ileriye dönük ürün planıdır; öneri katmanının ilk prototipi zaten
önceki geliştirmelerde yazılmıştır. Yeniden yazılması planlanmıyor.

| İş | Durum / öncelik / aşama | Ön koşullar | Doğrulanabilir kabul kriteri | Kod, test veya belge |
|---|---|---|---|---|
| 6.0 Kaynak bağlı öneri taslakları | prototip / P1 / 6 | 4.1–4.2, 5.1–5.2 | Dört işlemden kaynak/görev/müşteri bağı korunarak taslak türetilir; öncelik, gerekçe, belirsizlik, risk, önce/sonra ölçütü ve insan onayı gereksinimi vardır; tümü uygulanmamış ve bağımsız doğrulanmamış düşük güvenli incelemedir; 3000 aday/kanıt, 200 öneri, 1 MiB sınırı ve kararlı ID test edilir. | [Kod](../examples/offline-seo/isolation/evidence_recommendations.py), [belge](../examples/offline-seo/isolation/EVIDENCE_RECOMMENDATIONS.md), [test](../tests/test_offline_recommendations.py) |
| 6.1 Bağımsız kaynak ve ölçüm doğrulaması | planlandı / P1 / 6, ilk kalite işi | 6.0, güvenilir sürümlü kaynak arşivi | Ölçüm kaynak HTML üzerinden bağımsız sınanır veya güvenilir üretim aşamasına bağlanmış sonuç bütünlüğü doğrulanır; canonical var/yok manipülasyonu reddedilir; kaynak hash'i tek başına doğruluk kanıtı sayılmaz; doğrulanamayan ölçüm düşük güvende kalır. | Yeniden kullanım: [from_record/build](../examples/offline-seo/isolation/evidence_recommendations.py), [manipülasyon testleri](../tests/test_offline_recommendations.py) |
| 6.2 Search Console ve Analytics veri okuma | planlandı / P1 / 6 | Gerçek bağlantıda 5.12, müşteri izni, salt okunur kapsam ve kota politikası | Önce çevrimdışı dışa aktarım örnekleri; sonra açık izinli adaptör. Mülk/site ve müşteri eşleşmesi, tarih aralığı, saat dilimi, gecikme/eksik veri ve kaynak izi doğrulanır; erişim iptali sınanır; trafik/sıralama/dönüşüm yoksa bilinmiyor yazılır. | Bağlanacak katman: [öneri sözleşmesi](../examples/offline-seo/isolation/EVIDENCE_RECOMMENDATIONS.md); adaptör henüz yok |
| 6.3 Sektör, arama niyeti ve rakip bağlamı | planlandı / P1 / 6 | 6.1, izinli ve tarihli veri, müşteri onaylı sektör profili | Sektör varsayımı kaynak/onayla ayrılır; rakip seçimi gerekçelendirilir; gözlem ve çıkarım etiketlenir; aynı HTML farklı sektör örneklerinde uygun belirsizlikle değerlendirilir; gerçek hacim/talep veya rakip verisi uydurulmaz. | Başlangıç: [içerik uzmanı](../examples/offline-seo/isolation/content_seo.py); sektör/rakip adaptörü henüz yok |
| 6.4 Doğrulanabilir GEO ölçümü | planlandı / P1 / 6 | 6.1, 6.3, sağlayıcı kullanım/ücret politikası; canlı bağlantıda 5.12 | Soru kümesi, model/sağlayıcı sürümü, tarih, dil/konum ve tekrar örneklemesi kaydedilir; ham yanıt ile gerçek kaynak/atıf saklanır; görünürlük metriğinin paydası ve oynaklığı açıklanır; örnek yanıt canlı ölçüm sayılmaz; görünürlük veya sıralama garantisi verilmez. | İleride 3.2 arayüzü; mevcut [ajan tanımları](../agents/) çalışan GEO entegrasyonu değildir |
| 6.5 Uzman kalitesi ve maliyet değerlendirmesi | planlandı / P1 / 6 | 6.1–6.4 için etiketli örnekler | Uzman insan değerlendirmesinde yanlış öneri, kanıt kapsamı ve belirsizlik hatası ölçülür; kabul eşikleri veri görülmeden belirlenir; deterministik kurallar önce kullanılır; model gerekirse sağlayıcıdan bağımsız ve bütçe sınırlı olur. | Temel: [SEO testleri](../tests/test_offline_technical_seo.py), [öneri testleri](../tests/test_offline_recommendations.py) |

## Aşama 7 — İnsan onayı ve güvenli uygulama

| İş | Durum / öncelik / aşama | Ön koşullar | Doğrulanabilir kabul kriteri | Kod, test veya belge |
|---|---|---|---|---|
| 7.0 Çevrimdışı yan etki reddi | tamamlandı / P0 / 5–7, yalnızca ret kapısı | Güvenilir host | OfflineSideEffectGate her isteği reddeder; sahte onay/bütçe alanı izin oluşturmaz. Bu yalnızca reddetme davranışıdır; gerçek onay/uygulama sistemi değildir. | [Kapı](../examples/offline-seo/isolation/execution_control.py), [ret testi](../tests/test_offline_cancellation.py) |
| 7.1 Tek kullanımlık insan onayı ve uygulama kontrolü | planlandı / P0 / 7 | 5.12, 6.1; çok süreçte 5.11 | Onay müşteri, kaynak sürümü, tam değişiklik, işlem ve süreye bağlanır; aynı onay ikinci kez tüketilemez; hemen yan etki öncesi güncel yetki/iptal/limit kontrol edilir; değişmiş kaynak veya iptal reddedilir. Kontrol ile dış gönderim yarışı ve belirsiz sonuç açıkça yönetilir. | Arayüz: [CriticalOperation](../examples/offline-seo/isolation/execution_control.py), [öneri onay alanları](../examples/offline-seo/isolation/EVIDENCE_RECOMMENDATIONS.md) |
| 7.2 WordPress, sonra Shopify/özel site adaptörleri | engellendi / P1 / 7 | 7.1, güvenli sır deposu, test site/sandbox için ayrıca izin | Ortak oku/önizle/uygula/doğrula sözleşmesi; önce salt okunur ve prova. Gerçek değişiklik tam fark ve onayla yapılır; en az yetki, sürüm çakışması, tekrar gönderim ve geri dönüş sınanır; geri alınamayan işlem açıkça işaretlenir. | Girdi örneği: [WordPress benzeri fixture açıklaması](../examples/offline-seo/README.md); gerçek entegrasyon yok |
| 7.3 Google/Meta Ads bütçe ve işlem güvenliği | engellendi / P0 / 7 | 7.1, sağlayıcı izinleri, ortak bütçe/idempotency deposu | Müşteri/hesap eşleşmesi; para birimi ve dönem bazlı atomik bütçe rezervasyonu; eşzamanlı harcama üst sınırı; işlem anahtarı, mükerrer istek ve timeout uzlaştırması sandbox'ta sınanır. Belirsiz cevapta kör tekrar/rezervasyon iadesi yoktur; harcama geri alınabilir sayılmaz. | Tasarım başlangıcı: [yan etki sözleşmesi](../examples/offline-seo/isolation/execution_control.py); reklam uygulayıcı yok |

## Aşama 8 — Önce/sonra ölçüm ve müşteri sonuçları

| İş | Durum / öncelik / aşama | Ön koşullar | Doğrulanabilir kabul kriteri | Kod, test veya belge |
|---|---|---|---|---|
| 8.1 Değişiklik öncesi/sonrası ölçüm defteri | planlandı / P1 / 8; tasarımı 7'den önce | 6.1–6.2, uygulama için 7.1 | Önce kaynak/ölçüm sabitlenir; değişiklik ve zaman aralığı sonuca bağlanır; eksik veri ve mevsimsellik/başka değişiklikler gösterilir; gözlemsel artış nedensel SEO başarısı sayılmaz; sonuç measured=false iken başarı iddia edilmez. | Mevcut taslak alanları: [öneri katmanı](../examples/offline-seo/isolation/evidence_recommendations.py) |
| 8.2 Müşteri raporu ve PDF/zamanlama | planlandı / P1 / 8 | 4.2, 5.12, 8.1, güvenli çıktı/saklama politikası | Kaynak, tarih, kapsam, başarısız uzman ve belirsizlik görünürdür; HTML kaçışlama ve dosya/rapor müşteri yetkisi sınanır; PDF/zamanlanmış gönderim ayrı izinle açılır; yanlış müşteriye teslim testleri geçer. Bugünkü JSON raporu otomatik müşteri raporlama değildir. | Yeniden kullanım: [birleşik rapor](../examples/offline-seo/isolation/SEO_AUDIT.md) |
| 8.3 Sonuç değerlendirmesi ve öğrenme | planlandı / P1 / 8 | 8.1, müşteriyle önceden belirlenen başarı/hedef ölçütü | Başarı/başarısızlık ve bilinmeyen sonuç birlikte raporlanır; öneri sürümüyle etki ilişkisi izlenir; negatif sonuç saklanır; genellemeler örneklem ve belirsizlikle sunulur. Müşteri verisi izinsiz ortak modele/başka müşteriye taşınmaz. | Başlangıç: [öneri ölçüm sınırları](../examples/offline-seo/isolation/EVIDENCE_RECOMMENDATIONS.md) |

## Aşama 9 — Panel, hizmet paketleri ve kurumsal ölçek

| İş | Durum / öncelik / aşama | Ön koşullar | Doğrulanabilir kabul kriteri | Kod, test veya belge |
|---|---|---|---|---|
| 9.1 Müşteri paneli ve API | engellendi / P1 / 9 | 5.12, 8.2; çok süreç varsa 5.11 | Kimlik ile müşteri/rol ayrımı her uçta test edilir; bilinen yabancı görev ID'si bilgi vermez; salt okunur ekran kayıt değiştirmez; XSS/CSRF, indirme ve hata sızıntısı testleri geçer. Güvenilir CLI internete açılmaz. | Bağlanacak sınır: [AccessService](../examples/offline-seo/isolation/access_control.py) |
| 9.2 SEO/GEO/Ads ayrı hizmet ve yetenek paketleri | planlandı / P2 / 9 | 6.5, 8.2–8.3; Ads için 7.3 | Hizmet kapsamı, veri ihtiyacı, kota ve fiyat ayrı tanımlanır; satın alınmamış işlem sunucu tarafında reddedilir; paketin müşteri üyeliğini aşmadığı test edilir; SEO hazır diye GEO/Ads çalışıyor denmez. | Yeniden kullanım: [işlem kayıt tablosu](../examples/offline-seo/isolation/coordinator.py); paket/faturalama sistemi yok |
| 9.3 Kurumsal kapasite ve operasyon | planlandı / P2 / 9 | 5.11–5.12, yük ve kurtarma hedefleri | Kuyruk adaleti, müşteri başına kapasite, sınırlar, bölgesel veri politikası, olay müdahalesi ve geri yükleme yük altında sınanır; bir müşterinin aşırı işi diğerinin verisini/kapasitesini kontrolsüz etkilemez. Test kapsamı olmayan 24 uzman paralel açılmaz. | Mevcut sınırlar: [koordinatör](../examples/offline-seo/isolation/coordinator.py), [Docker](../examples/offline-seo/isolation/runner.py) |
| 9.4 Ajan/model maliyet yönetimi | planlandı / P1 / 9; ücretli modelden önce | İşlem maliyet defteri, 7.3 benzeri rezervasyon tasarımı | Model/token/araç/kaynak kullanımının müşteri ve görev maliyeti hesaplanır; tahmin ile gerçek ücret ayrılır; görev/gün/ay üst sınırları ve eşzamanlı rezervasyon sınanır; limitte durur; tekrarlı işe kör ücretli çağrı yapılmaz. Ücretsiz kota veya Claude aboneliği varsayılmaz. | Mevcut deterministik başlangıç: [koordinatör](../examples/offline-seo/isolation/coordinator.py); ücretli model yürütme yok |

## İlk somut geliştirme görevi: 5.4'ün küçük çevrimdışı sürümü

**Amaç:** Mevcut koordinatör/runner'ı yeniden yazmadan, yalnızca güvenilir hostun
erişebildiği kalıcı yürütme–kaynak kaydı için küçük bir şema ve yaşam döngüsü eklemek.
Bu görev supervisor, OIDC, API, gerçek site veya reklam entegrasyonu eklemez.

Önce kayıt sahipliği ve crash noktalarının tasarımı incelenmeli. Mevcut görev
`input_digest` kuralları, müşteri bazlı SQLite ayrımı ve Docker politikası korunmalı;
işçiye SQLite deposu/soket verilmemeli. Gerekirse ayrı host deposu tercihi, mevcut
görev kaydıyla tutarlılığı açıklayan gerekçeyle değerlendirilmelidir.

Kabul testleri:

1. İki müşteride aynı görev ID'si ayrı kalır; yürütme kimliği ve kaynak sahibi
   tek anlamlıdır. Başka müşteri kaydı/kaynağı bağlama girişimi reddedilir.
2. Create/import niyeti komut gönderilmeden kalıcıdır; gönderilmemiş, gönderilmiş
   fakat cevabı belirsiz ve kimliği doğrulanmış durumlar ayrı kaydedilir.
3. Komut öncesi/sonrası, kimlik cevabı öncesi/sonrası ve SQLite commit kaybı
   noktalarında ayrı süreç kesilir; restart sonrası belirsizlik kaybolmaz,
   yanlış başarı veya otomatik tekrar oluşmaz.
4. Değiştirilmiş/sahipliği doğrulanamayan kaynak durdurulmaz veya temizlenmez;
   operatöre güvenli inceleme gereksinimi üretilir. Bu ilk işte otomatik supervisor
   henüz olmadığı açık kalır. Testler kendi kaynaklarını güvenle temizler.
5. Mevcut dispatch/iptal, oturum/kimlik, dört SEO işlemi ve öneri regresyonları
   geçer; iki müşterili gerçek Docker kontrol edilir. Kayıtlara token, kaynak HTML
   veya gerçek müşteri verisi yazılmaz. README'nin yerel BOM farkı korunur.

Bu kabul kanıtlarından sonra 5.5'e geçilir. Üretime çıkış ve gerçek sağlayıcı
bağlantısı ayrıca yetkilendirilmiş iş olarak ele alınır.
