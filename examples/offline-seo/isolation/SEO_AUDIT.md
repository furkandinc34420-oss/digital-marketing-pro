# Birleşik SEO Denetim Raporu

`seo.audit`, mevcut üç uzmanı Baş Koordinatörde sırayla yönetir:
`seo.analyze`, `seo.content`, `seo.technical`. Üç işlem bağımsız görev olarak da
kullanılmaya devam eder. Yeni paket, model API'si veya web bağlantısı yoktur.

```bash
PYTHONDONTWRITEBYTECODE=1 python -B examples/offline-seo/isolation/coordinator.py \
  --state-root /workspace/dmp-coordinator-state --customer demo-musteri \
  --task-id audit-001 --operation seo.audit
```

Durum dizininin üst dizini önceden bulunmalı ve güvenilir yerel yönetici kontrolünde
olmalıdır. `diger-demo` için aynı kimlik farklı müşteri kaydını ifade eder.

## Akış ve güvenlik

Tek bir ana görev, müşteri kilidini bütün denetim boyunca tutar. Uzmanlar aynı,
değiştirilemeyen ham kaynak görüntüsüyle çağrılır. Her uzman mevcut bağımsız Docker
başlatıcısından geçer; izolasyon ayarları ve temizleme doğrulaması değişmez.
Koordinatör HTML analizi için korumasız geri dönüş yapmaz. Rapor birleştirme ve
JSON doğrulama güvenilir host üzerindedir; bu süreç iki müşteriye erişebilir.

Alt adımlar ayrı SQLite görevleri değildir; ana kaydın `experts` bölümünde kendi
durum ve sonuçları vardır. Birleşik görev 1.000 görev kotasında tek görev sayılır.
Her uzman başlangıcında ve sonrasında SQLite'a ara kayıt yazılır. Müşteri kilidi
nedeniyle aktif görev sırasında eşzamanlı durum isteği beklemek yerine kilit
hatası alabilir. Panel için canlı ilerleme yayını henüz yoktur.

Başarısız uzman sonucu birleştirmeye alınmaz; diğer uzmanlar devam eder. Ana görev
`failed`, rapor `complete=false` olur. Başarılı uzmanların sonuçları korunur.
Ham hatalar normal rapora eklenmez; sınırlı yönetici ayrıntıları mevcut
`admin_errors` yolundan okunur. CLI başarısız birleşik görevde 1, kesilmiş görevde
3 döndürür. Eksik uzman verisinden sağlıklı veya eksiksiz denetim sonucu çıkarılmaz.

Süreç kesilirse son ara kayıt korunur. Sonraki erişim `running` ana görevi
`interrupted` yapar; yarıda kalan uzman da kesilmiş görünür. Henüz başlanmamış
uzman `pending` kalır. Görev ve rapor durumları uyumlu hale getirilip kaydedilir.
Aynı kimlik tekrar gönderildiğinde işler yeniden başlatılmaz. Kesilmiş gerçek
Docker kaynaklarının otomatik kurtarılması hâlâ yoktur; yönetici incelemesi gerekir.
SIGKILL testi kontrollü sahte uzman kullanır; gerçek Docker kaynağı bırakmaz.

## Rapor ve öncelikler

Rapor şema sürümü 1'dir. Yapılandırılmış `findings`, uzmanların tam sonuçlarını
tek kez tutan `experts`, kaynak özetleri ve Türkçe `summary_tr` içerir.
Panel veya PDF üreticisi bu veriyi okuyabilir; bu sürüm PDF veya panel geliştirmez.
Metinler güvenilmeyen sayfa içeriği barındırabilir; gelecekteki HTML arayüzü bunları
HTML olarak çalıştırmadan uygun biçimde kaçışlayarak göstermelidir.

Birleştirme anahtarı müşteri kapsamındaki sayfa kimliği, URL ve açık sorun kodudur.
Bilinen eşdeğer title/canonical/noindex bulguları ortak koda bağlanır. Bulanık metin
benzerliğiyle farklı sorunlar silinmez. Farklı öneriler ve bütün uzman/kanıt
referansları saklanır. Eski uzmanın ek uyarıları da kaynak sayfa/issue yoluyla
rapora alınır. Tam ham uzman sonuçları kanıt kaybını önler.

Önem ve düzeltme sırası editöryel inceleme önceliğidir; Google sıralama etkisi veya
canlı indeks durumu kanıtı değildir. HTML belirsizliği önce; canonical/robots
çatışmaları sonraki; içerik ve meta iyileştirmeleri daha sonra gösterilir.
Noindex kaldırma talimatı olarak yorumlanmamalıdır. Uzmanlar arasındaki farklı
görüşler saklanır; bu sürüm otomatik olarak hangisinin doğru olduğunu seçmez.
Eski uzmanın ham raporunda bulunan kesin ifadeler de canlı ölçüm sayılmaz.

`complete=true` yalnızca üç uzman doğrulanmış sonuçla bittiğinde ve ana görev
başarıyla kaydedildiğinde kullanılabilir. Bu, sitenin SEO açısından sorunsuz
olduğu anlamına gelmez. HTTP durumları, robots.txt/sitemap ve Google indeks
durumu birleşik raporda da ölçülmemiş olarak işaretlenir.

## Doğrulama, sınırlar ve kalan riskler

Her uzman sonucu müşteri, işlem, kaynak özeti ve tam sayfa kapsamı açısından
kontrol edilir. İçerik/teknik şemaları yeniden kullanılır; eski meta uzmanı için
birleşik akışta ek yapı kontrolleri uygulanır. Kayıtlı birleşik rapor okunurken
bulgular, öncelik sırası ve Türkçe özet uzman sonuçlarından yeniden doğrulanır.
Bozuk kayıt otomatik güvenilir sayılmaz. Bu, dijital imza veya aynı işletim sistemi
kullanıcısı altında kötü niyetli koda karşı güvenlik sınırı değildir.

- Her uzman raporu birleşik akışta en fazla 2 MiB; birleşik rapor en fazla 3 MiB.
- Ara kayıtlarda hata/kesinti bilgileri için 64 KiB boşluk bırakılır.
- Sığmayan uzman sonucu başarısız sayılır; sessiz kesilmez veya başarı gibi sunulmaz.
- Mevcut 64 MiB müşteri SQLite sınırı, 1.000 görev sınırı ve Docker kaynak sınırları korunur.
- Üç uzman sıralı çalıştığı için toplam süre tek uzmandan uzundur. Her Docker işlemi
  kendi mevcut sınırlarına tabidir; dağıtık kuyruk veya genel görev zamanlayıcısı yoktur.

Gerçek kullanıcı kimlik doğrulaması, dış erişim servisi, canlı site yazma ve
otomatik düzeltme yoktur. Aynı kimlikte kaynak/işlem değişirse çakışma oluşur;
yeni bilinçli analiz için yeni görev kimliği kullanılır. Çalışma zamanı kod sürümü
kaynak özetine dahil değildir. Otomatik devam veya yeniden deneme yoktur.

## Bağımsız inceleme sonrası bütünlük kuralları

Birleşik görevin girdi özeti, sıralı dosya adları ve ham dosya baytlarının SHA-256
özetlerinden tek `input_digest` yardımcısıyla hesaplanır. Kayıt okunurken raporun
manifesti aynı yardımcıdan geçirilip SQLite girdisiyle karşılaştırılır. Kaynak
JSON yeniden serileştirilmez. Bağımsız üç işlemin eski ham bayt özeti değişmez.
Bu düzeltmeden önceki deneysel `seo.audit` kayıtları yeni kuralla uyuşmazsa
reddedilir; otomatik benimseme veya yeniden çalıştırma yapılmaz. Yeni görev kimliği
kullanılmalıdır. Manifest imza değildir; güvenilir host yöneticisine karşı korumaz.

Eski meta uzmanında değer, uzunluk, ölçüm durumu ve öneri nedeni birlikte
kontrol edilir. Öneri alan türleri, kaynak ve insan onayı durumu doğrulanır.
Aynı uyarının yalnızca üreticinin açık ve ölçümle eşleşen mesajı ortak sorun
koduna çevrilir. Bilinmeyen mesajlar kelime benzerliğiyle elenmez; ayrı kalır.
Sayfa uyarılarının ve önerilerin kanıt referansları birlikte korunur.

Uzmanlar sırayla `pending → running → succeeded/failed` ilerler. Kesinti aktif
adımı `interrupted` yapar; sonraki adımlar `pending` kalır. Terminal ana görevde
`running` adım, bekleyen adımdan sonra bitmiş/aktif adım veya birden çok aktif adım
kabul edilmez. `complete=true` yalnızca üç başarılı adım ve başarılı ana görevle
mümkündür. SQLite yazma/commit hatasında başarı üretilmez; kalıcı depolama hatası
sonuç kaydını da engelleyebilir ve çağırana hata olarak döner. Sonraki erişimde
kalmış `running` görev kesilmiş sayılır; otomatik yeniden deneme yoktur.

## Eski deneysel kayıtların CLI uyumluluğu

Çıkış kodu 5 ve `audit_input_version_incompatible` yalnızca şeması, müşteri
kapsamı ve durumu doğrulanan raporun manifesti mevcut ham dosyalarla eşleştiğinde
ve kayıtlı görev özeti bu dosyaların eski bayt kuralına tam uyduğunda döner.
Türkçe açıklama yeni görev kimliği gerektiğini belirtir. Bu kontrol hem `run`
hem `status` için geçerlidir; kayıt otomatik dönüştürülmez veya çalıştırılmaz.
Eski kaynak artık mevcut değilse eski sürüm olduğu tahmin edilmez.
Bozuk/belirsiz kayıt, erişim, bulunamayan görev ve depolama hataları ayrı güvenli
kodlarla döner; ham hata, kaynak veya dosya yolu açıklanmaz. Normal görev
çakışması `task_input_conflict` ve çıkış 4 olarak kalır. Bağımsız uzmanların
CLI davranışı değişmez.
