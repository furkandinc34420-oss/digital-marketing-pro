# Kanıta dayalı öneri taslakları — çevrimdışı ilk sürüm

`evidence_recommendations.py`, `seo.analyze`, `seo.content`, `seo.technical` veya
`seo.audit` çıktısını yorumlayan küçük bir katmandır. Yeni uzman/görev, model API'si,
web isteği veya siteye yazma işlemi eklemez. Mevcut raporları ve SQLite kayıtlarını
değiştirmez; öneriler ilk sürümde saklanmadan türetilir.

## Güvenli kullanım ve kaynak bağı

Müşteri tarafına yönelik yol:

```python
drafts = service.recommendations(session, 'demo-musteri', 'audit-001')
```

Burada `service` mevcut `AccessService` nesnesidir. Aktif kimlik, üyelik ve rol
her çağrıda yeniden kontrol edilir; okuyucu rolü de kendi müşterisinin taslaklarını
alabilir. Kullanıcıdan rapor JSON'u, manifest veya kullanıcı/rol bilgisi alınmaz.
Görev `Coordinator.peek` ile yazmasız okunur. Ardından mevcut güvenli fixture
okuyucusuyla alınan ham kaynak baytları, aynı `input_digest` yardımcısı üzerinden
kayıtlı görev girdisiyle karşılaştırılır. Raporun müşteri, kaynak özetleri, işlem,
sayfa kimlikleri ve URL'leri bu kaynak görüntüsüyle doğrulanır. Değişmiş kaynakta
eski sonucu güncelmiş gibi yorumlamak yerine genel güvenli hata döner; yeni görevle
analiz veya ileride güvenilir bir kaynak arşivi gerekir.

`build(report, customer, expected_hashes)` ve `from_record(...)` düşük seviyeli,
güvenilir host yardımcılarıdır; doğrudan müşteri API'sine açılmamalıdır.
`expected_hashes` istek gövdesinden veya raporun kendi iddiasından alınarak kaynak
ispatı sayılmamalıdır. Kanonik görev özeti mevcut koordinatör kuralını kullanır;
öneri/rapor SHA-256'ları ise ayrı birer içerik bağıdır.

## Her taslakta bulunan alanlar

- Somut rapor gözlemi veya doğrulama bekleyen uyarı.
- Uzman adı, giriş raporundaki JSON yolları, `pages.json` sayfa kimliği ve ham dosya özetleri.
- Ölçülen mevcut değerler, işlem taslağı ve teknik gerekçe.
- İnceleme önceliği ve bu önceliğin gerekçesi.
- Gözlem kapsamına ait güven düzeyi, belirsizlikler ve kurala özgü riskler.
- Değişiklik öncesi değerler ve daha sonra ölçülecek başarı kriteri.
- Taslak durumu, insan onayı zorunluluğu ve uygulanmadığı bilgisi.

Güven düzeyi trafik kazanma olasılığı değildir. Bu katman HTML'i yeniden
ayrıştırmaz; doğrulanmış uzman raporunun ölçümlerini kullanır. Özet eşleşmesi,
rapor ölçümlerinin bağımsız doğruluk ispatı veya dijital imzası değildir.
Sektör bilgisi, trafik, sıralama, dönüşüm ve rakip verisi sağlanmadığından bilinmiyor
olarak işaretlenir; sektör uzmanlığı veya Google davranışı iddia edilmez.

İlk kurallar başlık/meta eşikleri, canonical gözlemi, noindex amacının incelenmesi,
H1 yokluğu, beyan edilen hedef kelime kullanımı ve HTML ölçüm belirsizliğini kapsar.
Tek uzman raporunda `source_complete=true`, yalnızca o uzman girdisinin tamamlandığını
ifade eder. `overall_audit_complete` ve `coverage` bütün denetimin kapsamını ayrıca
gösterir; diğer uzmanlar çalışmış gibi sunulmaz.
Diğer uzman bulguları kaybolmaz; desteklenmeyen kodlar düşük güvenle insan kanıt
incelemesine yönlendirilir. Eksik denetim eksik kalır. Uzmanlar çelişirse veya
ölçüm güvenilir değilse kesin düzeltme yerine doğrulama istenir.

Başlık/açıklama karakter eşikleri Google zorunluluğu değildir. Kelimenin geçmesi
arama talebi veya doğru niyet kanıtı değildir. Canonical yokluğu otomatik hata
sayılmaz; noindex kaldırılması otomatik önerilmez. H1 ve diğer göstergeler de
sıralama garantisi değildir.

## İnsan onayı ve ölçüm

Tüm çıktılar `status=draft`, `human_approval_required=true`, `applied=false` ve
`approval.status=not_requested` taşır. Onay/uygulama metodu yoktur. Her önerinin
kimliği müşteri, rapor içeriği, kaynaklar ve tam taslakla bağlıdır; girdiler değişirse
kimlik de değişir. Bu hash imzalı onay tokenı değildir. JSON'u değiştirerek onay
verilmiş gösterilmesi uygulama yetkisi yaratmaz.

Gelecekteki uygulayıcı, güncel kullanıcı yetkisini ve özgün kaynakları yeniden
doğrulamalı; kesin değişiklik farkını insanın incelemesine sunmalı; onayı belirli
kaynak/değişiklik sürümüne bağlamalıdır. Bu aşamada bu süreç uygulanmaz.

Başarı alanı `measured=false` kalır. Yeni kaynakla tekrar analiz ve insan incelemesi
gerekir. Trafik/sıralama/dönüşüm etkisi için ileride gerçek öncesi/sonrası ölçümler,
uygun zaman aralığı ve değişkenlerin değerlendirilmesi gerekir; burada üretilmez.

## Sınırlar ve testler

En fazla 200 öneri ve toplam 1 MiB JSON çıktı kabul edilir. Çıktı bütçesi öneriler
oluşturulurken izlenir; aşımda sessiz kesme veya kısmi başarı yerine hata üretilir.
Uzmanların mevcut 2 MiB ve denetimin 3 MiB giriş sınırları yeniden kullanılır.
Yeni bağımlılık veya dış hesap yoktur. Mevcut PID, müşteri yetkilendirme, SQLite
okuma ve Docker sınırları korunur. Sonuç metinleri güvenilmeyen başlık/kelime
barındırabilir; gelecekteki panel bunları HTML olarak çalıştırmadan kaçışlamalıdır.

Testler: `python -B -m unittest discover -s tests -p test_offline_recommendations.py`.
İki müşteri, dört girdi türü, kaynak değişimi, çelişki/belirsizlik, bozuk rapor,
yazmasız erişim, çıktı sınırları ve gerçek Docker denetiminden türetme kapsanır.
Henüz sektör bağlamı, canlı veri, içerik yazımı, onay kaydı, uygulama, PDF/panel,
kalıcı öneri deposu veya gerçek performans ölçümü yoktur.

## Bağımsız inceleme sonrası güvenlik kuralları

Kaynak özeti yalnızca görev girdisinin baytlarını bağlar; raporun o girdiden doğru
üretildiğini ispatlamaz. Bu sürümde üreticiye bağlı güvenilir sonuç doğrulaması
ve bağımsız HTML ölçümü bulunmadığından **bütün öneriler düşük güvenli insan
incelemesidir**. `measurement_verification=unverified` bunu makine tarafından
okunabilir biçimde belirtir. `report_measurements_consistent` yalnızca rapor içi
uyumu gösterir, kaynak doğrulaması değildir. Canonical ölçümü sonradan değiştirilse
de kesin düzeltme önerisi üretilmez. Rapor hash'i kimlik bağıdır, güven kanıtı değildir.

İşlem öncesinde toplam 3000 aday/kanıt sınırı uygulanır. Tekrar kontrolü küme
üzerinden yapılır; 200 öneri, 1 MiB çıktı ve mevcut girdi boyutları ayrıca korunur.
Uzmanlar sabit sırada, kanıtlar ve gözlemler kararlı sırada işlenir. JSON nesne
anahtar sırası kimliği değiştirmez. Gerçek rapor içeriği veya kaynak değişirse
rapor ve öneri kimlikleri değişir; liste sırası içerik değişikliği sayılır.

Hesaplama ortak yetki kilidinin dışında yapılır. Sonuç dönmeden önce aynı oturum,
hesap ve güncel müşteri üyeliği tekrar kontrol edilir. Hesaplama sırasında iptal
edilen yetkiyle sonuç alınamaz. İlk yetkili SQLite okuması kilit altında kalır.
