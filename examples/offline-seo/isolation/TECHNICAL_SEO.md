# Çevrimdışı Teknik SEO Uzman Ajanı

İşlem adı `seo.technical`. Mevcut `seo.analyze` ve `seo.content` işlemleri korunur.
Bu uzman ağ isteği yapmadan yalnızca iki kurgusal müşterinin örnek HTML'ini okur.
Gerçek WordPress, Google hesabı veya ücretli API bağlantısı yoktur.

```bash
PYTHONDONTWRITEBYTECODE=1 python -B examples/offline-seo/isolation/coordinator.py \
  --state-root /workspace/dmp-coordinator-state --customer demo-musteri \
  --task-id technical-001 --operation seo.technical
```

Durum dizininin üst dizini önceden var olmalı; dizin güvenilir yerel yöneticiye
ait ve özel olmalıdır. `diger-demo` için aynı görev kimliği ayrı kayda karşılık
gelir. Aynı müşteride başka işlem veya farklı kaynakla aynı kimlik çakışmadır.
Görev kayıtları ve CLI hata kodları [koordinatör kurallarıyla](COORDINATOR.md) aynıdır.

## Gözlenen kanıtlar

- Canonical etiketlerinin sayısı, head içindeki/dışındaki yerleşimi, ham hedefi,
  göreli/mutlak olması ve sayfa URL'sine göre çözümlenen HTTP(S) hedefi.
- Genel `robots` ve ayrı `googlebot`/`bingbot` meta direktifleri. `none`,
  noindex/nofollow olarak ayrıca gösterilir. `all` açık bir index/follow
  direktifine dönüştürülmez. Genel
  etiketlerdeki karşıt direktifler inceleme önerisi üretir. Bota özel kural
  genel kuralmış gibi sunulmaz; arama motorunun nihai davranışı hesaplanmaz.
- Ayrı H1 sayısı ve çıkarılan metinleri. Boş çıkarılan metin gizlilik nedeniyle
  de oluşabilir. Çok H1 tek başına sıralama cezası olarak sunulmaz.
- `a` etiketlerinin aynı host, farklı host, sayfa içi fragment, mailto/tel,
  eksik/boş, geçersiz veya desteklenmeyen protokol hedefleri olarak sayımı.
  “İç bağlantı” aynı hostname demektir; aynı port/origin garantisi değildir.

Canonical farklı bir sayfayı veya alan adını gösteriyorsa otomatik hatalı sayılmaz;
tercihin bilinçli olduğunun incelenmesi önerilir. Noindex'i kaldırma veya sayfayı
canlıya alma talimatı verilmez. Robots meta yokluğu da indekslenme kanıtı değildir.
Hedef URL'ler hiçbir zaman açılmaz. Örneğin `/hizmetler` için “iç bağlantı”
sınıflaması yapılması sayfanın var olduğunu veya HTTP 200 döndüğünü göstermez.
Bağlantı metni kalitesi ve rel=nofollow gibi ek özellikler ilk sürümde ölçülmez.

Her öneri müşteri/sayfa kimliği ve sayfadaki tek kanıt kaydına referans içerir;
insan incelemesi gerektirir ve uygulanmış olarak işaretlenmez. Ham dosya
özetleri raporun kökünde bulunur. İşçi siteye veya örnek girdilere yazmaz.

## Belirsizlik ve sınırlar

`base href` ilk sürümde uygulanmaz. Bu durumda göreli ve şema-göreli hedefler
`kind=unresolved`, `resolved=null` olarak gösterilir; kesin iç/dış bağlantı
sayımlarına katılmaz. Mutlak HTTP(S) adresleri kendi adresleri olarak değerlendirilebilir.
Canonical/robots yerleşimi yalnızca açık head veya body bağlamında biliniyor sayılır;
örtük head/body durumunda `in_head=null` ve `implicit_head_body_location` uyarısı
oluşur. Bu belirsizlikler `measurement_reliable=false` yapar; normal SEO önerileri
yerine yalnızca insan incelemesi uyarısı üretilir. Bozuk HTML aynı kurala tabidir.
Template/script/style/noscript içindeki teknik etiketler etkin sayılmaz.
HTML5 tarayıcı düzeltmesi, harici CSS/JS çalıştırma yoktur.

Mevcut güvenli dosya okuyucu, .invalid kaynak URL kontrolü ve kaynak özeti yeniden
kullanılır. Ayrıştırıcı 64 derinlik, 10.000 olay ve olaylarda 2 saniyelik süre
kontrolünü korur. Ayrıca sayfa başına en fazla 500 bağlantı, 500 canonical ve
500 robots etiketi; URL başına 2.048 karakter kabul edilir. Çıktıda en fazla
20 canonical ve önceliklendirilmiş 20 bağlantı örneği, 32 H1 gösterilir; tam sayılar ve
kısaltma işareti ayrıca verilir. Rapor 2 MiB sınırını aşarsa kontrollü hata olur.
Bu uygulama sınırları host kotası değildir; Docker'ın mevcut bellek/CPU/süre
sınırları ayrıca devam eder.

HTTP durum kodları, yönlendirmeler, gerçek robots.txt, sitemap, Google indeks
durumu, X-Robots-Tag HTTP başlığı ve hedeflerin erişilebilirliği ölçülmez.
Rapor `unmeasured` alanında bu sınırları açıkça taşır.

## Güvenlik ve test

Mevcut `TrustedCaller` yerel güvenilir çağıran sınırı, müşteri başına SQLite,
girdi özeti, tekrar koruması ve Docker güvenlik kapısı korunmuştur. Yeni kod ve
şema imaja bağlantı takip etmeyen okuyucuyla eklenir. İşçi ağı kapalı, dosya sistemi
salt okunur, kullanıcı root değildir; host klasörü veya Docker soketi bağlanmaz.
Yeni ve kaydedilmiş sonuçlar işlem türü/şema/müşteri kimliği açısından doğrulanır.

Gerçek kullanıcı kimlik doğrulaması yoktur. Ana denetleyici Docker yetkisine ve
iki müşterinin kaynaklarına erişebilir; müşteriye açılmamalıdır. Konteynerler
host çekirdeğini paylaşır. Zorla kapanmada otomatik artık kaynak kurtarma yoktur.

```bash
PYTHONDONTWRITEBYTECODE=1 python -B -m unittest discover -s tests -p test_offline_technical_seo.py -v
```

Birim testleri canonical/robots/H1/bağlantı kanıtlarını, belirsiz HTML'yi, sınırları,
müşteri yetkilerini, değişmiş girdiyi, bozuk kayıtları ve güvenlik kapısını sınar.
Gerçek Docker testi iki müşteriyle üç SEO işlemini ve CLI tekrar isteğini çalıştırır.
Docker yoksa entegrasyon testi açık nedenle atlanır; başarı sayılmaz.

## Bağımsız inceleme sonrası düzeltmeler

URL kuralları `technical_rules.py` içindedir ve ağ erişim yetkisi vermez.
HTTP(S) şeması yazılmışsa geçerli bir authority/hostname zorunludur; `https://`,
`https:///hedef` ve `https:hedef` göreli adrese çevrilmez. Hatalı yüzde kodlamaları,
kullanıcı bilgileri, bozuk portlar ve geçersiz hostname biçimleri reddedilir.
Bu kontroller DNS çözmez, adresin kamusal olduğunu kanıtlamaz ve SSRF koruması
değildir. Gelecekteki HTTP istemcisi bağımsız ağ/yetki kontrolleri gerektirir.

Canonical karşılaştırmasında Unicode yol/sorgu metni UTF-8 URI biçimine getirilir;
yüzde kodlamalarının hex harfleri büyütülür ve yalnızca ayrılmamış ASCII karakterleri
çözülür. Örneğin `%7E` ile `~` aynı karşılaştırılır; `%2F` slash olarak açılmaz.
Sorgu sırası, protokol ve varsayılan olmayan port farkları korunur. Bu karşılaştırma
sunucunun iki URL'ye aynı yanıtı verdiğini kanıtlamaz; yönlendirme ölçülmez.

Robots ayrıştırması parametreleri değerleriyle saklar. `max-snippet: 50` bölünmez;
`unavailable_after` tarihinin içindeki virgül tarih değerinde kalır. Tarihin
geçerliliği veya arama motorunun uygulama zamanı hesaplanmaz. Genel robots,
googlebot ve bingbot grupları ayrı tutulur; her grupta açık index/noindex ve
follow/nofollow çatışmaları incelenir. Genel ve bota özel kurallardan nihai
Google indeks kararı türetilmez.

Rapor şeması artık sürüm 2'dir. Sınırlı ham canonical/robots/bağlantı listeleri ve
çıkarılan H1 metinleri `evidence.raw` içinde tutulur. Doğrulayıcı buradan bütün
sınıflandırmaları, robots gruplarını, çatışmaları, sayımları, örnek seçimini ve
belirsizlik bayraklarını yeniden hesaplar; çelişen kayıt reddedilir. Ham listeler
sayfa başına 500 elemanla, metin ve rapor bütçeleriyle sınırlıdır. Bu tutarlılık
kontrolü dijital imza değildir: güvenilir host üzerinde hem ham hem türetilmiş
veriyi tutarlı biçimde değiştiren yöneticilere karşı değişmezlik garantisi yoktur.
Kaynak özetleri gerçek dosya kimliğini temsil eder; sonuç imzası değildir.
Önceki teknik rapor sürüm 1 kayıtları sessizce kabul edilmez; açık yönetici taşıması
veya yeni görev kimliğiyle yeniden analiz gerekir.

Bağlantı örneklerinde önce geçersiz/desteklenmeyen, ardından boş/eksik ve çözülemeyen
hedefler gösterilir. Örneğin 21'inci bağlantı sorunluysa ilk örnekler arasına gelir.
Her örneğin `index` alanı ham bağlantı listesindeki sırasıdır. Önerilerin
`evidence_refs` alanı `links:20`, `robots:0`, `canonicals:1` gibi doğrudan ham kanıt
referansları taşır; doğrulayıcı bu referansların ilgili bulguyla uyuşmasını denetler.
Tam ham veriyi saklamak raporu büyütebilir; 2 MiB sınırı korunur ve aşımda kısmi
rapor başarılı kaydedilmez. Dosyalar müşteriye teslim edilecek genel günlük değildir.
