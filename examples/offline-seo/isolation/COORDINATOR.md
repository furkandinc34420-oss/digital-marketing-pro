# Baş Koordinatör: ilk çevrimdışı sürüm

Bu deterministik Python yöneticisi bir yapay zekâ modeli çağırmaz; ücretli API,
WordPress bağlantısı veya canlı yazma yoktur. `seo.analyze` ve `seo.content` desteklenir.
SEO uzmanı mevcut `DockerLab` üzerinden analiz yapar; güvenlik ayarları, müşteri
imajları ve temizleme kontrolleri değiştirilmez. SQLite dosyaları işçiye verilmez.

## Yerel kullanım

Güvenilir yerel yönetici, depo dışındaki özel bir durum dizinini seçer. Üst dizin
önceden var olmalı; durum dizini yöneticiye ait ve 0700 izinli olmalıdır. Örnek:

```bash
PYTHONDONTWRITEBYTECODE=1 python -B examples/offline-seo/isolation/coordinator.py \
  --state-root /workspace/dmp-coordinator-state --customer demo-musteri --task-id seo-001
PYTHONDONTWRITEBYTECODE=1 python -B examples/offline-seo/isolation/coordinator.py \
  --state-root /workspace/dmp-coordinator-state --customer diger-demo --task-id seo-001
PYTHONDONTWRITEBYTECODE=1 python -B examples/offline-seo/isolation/coordinator.py \
  --state-root /workspace/dmp-coordinator-state --customer demo-musteri --task-id seo-001 --action status
```

Her müşteri için ayrı `tasks.sqlite3` ve `tasks.lock` oluşturulur. Kayıtlarda görev
kimliği, işlem, çağıran, durum, oluşturma/güncelleme zamanı, sonuç ve sınırlı hata
bilgisi bulunur. Kaynak örnek dosyalar değiştirilmez. Durum verisini Git'e eklemeyin.

## Kimlik ve güven sınırı

`TrustedCaller` güvenilir host programının yapılandırmasından oluşturulmalıdır.
İstek metninden, JSON gövdesinden veya müşteri tarafından gönderilen yetki listesinden
oluşturulamaz. API, talep edilen müşteri kimliğini bu bağlamdaki izinlerle karşılaştırır;
yetkisiz istek dosya oluşturmadan reddedilir. CLI yalnızca yerel yönetici içindir:
`--customer` bir giriş yapma veya yetki kanıtı değildir. Gerçek kullanıcı kimlik
doğrulaması, oturum, rol yönetimi ve uzaktan çağrı servisi henüz yoktur.

Ana süreç iki müşteriye ve Docker soketine erişebilen güvenilir yöneticidir.
SQLite ayrımı uygulama sınırıdır; aynı işletim sistemi kullanıcısıyla çalışan kötü
niyetli programlara veya host yöneticisine karşı koruma değildir. Durum kökünün
üst dizinleri de güvenilir yönetici kontrolünde tutulmalıdır. Sembolik bağlantılar,
çoklu sabit bağlantılar, yabancı müşteri veritabanı ve güvensiz izinler reddedilir.
SQLite journal dosyaları özel müşteri dizininde kalır. Disk şifreleme/yedekleme yoktur.

## Tekrarlar ve kesintiler

Görev kimliği müşteri içinde tekildir. Aynı kimlik mevcut sonucu/durumu getirir;
başarısız veya kesilmiş görev de otomatik yeniden çalışmaz. Yeni bilinçli bir iş
ancak yeni görev kimliğiyle gönderilir. Görev kimliği içerik özeti değildir: aynı
analizi farklı kimlikle istemek yeni iş sayılır. İşlem türü değiştirilemez.

Görev `running` olarak diske yazılmadan işçi başlatılmaz. Müşteri kilidi işlem
boyunca tutulur; eşzamanlı aynı müşteri çağrısı beklemek yerine hata verir. Süreç
ölürse işletim sistemi kilidi bırakır. Sonraki müşteri erişimi eski `running`
kaydını `interrupted` yapar; Docker işi otomatik yeniden başlatılmaz. Başarılı
sonuç yalnızca işçi ve oturum temizliği tamamlandıktan sonra `succeeded` olur.

Bu, dağıtık bir "tam bir kez" garantisi değildir. Süreç işçi tamamlandıktan ama
sonuç kaydedilmeden ölürse sonuç belirsiz kalır. Kesilmiş işte Docker kaynakları
kalabilir; yeni işten önce yönetici mevcut izolasyon belgesindeki oturum etiketlerini
incelemelidir. Otomatik artık kaynak kurtarma yoktur. Hataların ham metni müşteri
kaydı içine kopyalanmaz; `failed` kaydı yönetici incelemesi gerektiğini bildirir.
Depolama/izin hataları çağırana istisna olarak döner; başarı gibi sunulmaz.

## Modüler genişleme ve test

`Specialist.execute(customer)` uzman arayüzüdür; `SeoSpecialist` ilk uygulamadır.
Yeni uzman yalnızca güvenilir kod/yapılandırma ile işlem tablosuna eklenmelidir.
İstek üzerinden Python modülü, komut veya dosya yolu seçilemez. GEO, Ads, Meta ve
24 uzmanın otomatik yönlendirilmesi bu sürümde uygulanmadı.

```bash
PYTHONDONTWRITEBYTECODE=1 python -B -m unittest discover -s tests -p test_offline_coordinator.py -v
```

Birim testleri sahte uzmanla yetki, ayrı kayıtlar, tekrar, kesinti, kilit, bağlantı
ve sonuç denetimini sınar. Ek entegrasyon testi iki müşteriyi gerçek Docker SEO
analizinden geçirir. Docker yoksa yalnızca bu entegrasyon testi açıkça atlanır;
birim testleri gerçek konteyner izolasyonunun yerine geçmez.

## Güvenlik incelemesi sonrası eklenen kurallar

Her göreve işlem türü ve müşterinin iki kaynak dosyasının ham baytlarından SHA-256
özeti bağlanır. Aynı kimlik ve aynı özet mevcut kaydı döndürür; aynı kimlikle farklı
kaynak veya işlem `TaskConflict` hatasıdır. Boşluk/biçim değişikliği de kaynak
farkıdır. Docker imajına bu özetin hesaplandığı aynı anlık görüntü verilir;
kaynaklar paketleme için yeniden okunmaz. Özet kod sürümünü kapsamaz. Yeni kodla
bilinçli yeniden analiz için yeni görev kimliği kullanılmalıdır.

Müşteri başına en fazla 1.000 görev ve 64 MiB ana SQLite dosyası kabul edilir.
SQLite sayfa üst sınırı uygulanır; yeni işten önce en büyük sonuç, yönetici hata
kaydı ve ek SQLite alanı için kapasite ayrılır. Kapasite yetersizse Docker işi
başlamaz. Yan dosyalara da boyut denetimi yapılır; SQLite geçici journal alanı
ana dosya sınırına ek disk kullanabilir. Bunlar host disk kotası veya bellek
kotası değildir; otomatik eski görev silme ve arşivleme yapılmaz.

Mevcut dolu veritabanında sahiplik tablosu/kaydı yoksa müşteri sahipliği atanmaz;
açılış reddedilir. `input_digest` alanı bulunmayan eski geliştirme veritabanları
sessizce dönüştürülmez: yönetici incelemesi ve açık taşıma işlemi gerekir. Kaydedilmiş
sonuçlar okunurken de müşteri kimliği, çevrimdışı öneri modu ve alt önerilerin
müşteri bilgileri denetlenir.

`admin_errors` iç tablosu göreve ve müşteriye bağlı sınırlı yönetici ayrıntıları
saklar. Docker temizleme hatalarında oturum etiketi ve kaynak bilgileri burada
bulunabilir. Hata mesajı en fazla 2.400 karakter tutulur; kesilme işareti vardır.
Normal `run/get` raporları bu tabloyu içermez. Güvenilir yerel yönetici Python'dan
`Coordinator.admin_diagnostics(customer, task_id)` ile okur. Bu yöntem müşteri
servisine açılmamalıdır. Tablo aynı özel SQLite dosyasındadır: fiziksel olarak
ayrı bir güvenlik alanı değildir. Müşteriye veritabanı dosyası teslim edilmez.
Ham yönetici ayrıntıları paylaşılmadan önce incelenmelidir. Disk yazma hatasında
tanı kaydının da saklanamaması mümkündür; CLI genel hata kodu döndürür.

CLI çıkış kodları: başarılı görev `0`, başarısız görev `1`, geçersiz istek/kilit/
depolama hatası `2`, kesilmiş görev `3`, girdi çakışması `4`. Ayrıntılı dahili
istisnalar CLI hata yanıtına kopyalanmaz. `status` komutunda da görev durumuna
uygun kod kullanılır.

Ayrı süreç testleri canlı müşteri kilidini ve SIGKILL sonrasında kilidin bırakılıp
görevin `interrupted` olmasını doğrular. SIGKILL testi Docker yerine kontrollü,
bekleyen sahte uzman kullanır; gerçek Docker işçisinin zorla kapanma sonrası
temizlendiği iddiasında bulunmaz ve Docker kaynağı oluşturmaz. Normal iki müşteri
SEO entegrasyonu ayrıca gerçek Docker ile çalıştırılır. Zorla kapanmada gerçek
Docker kaynaklarının otomatik kurtarılması hâlâ yoktur.

## İkinci SEO yeteneği

Dördüncü aşamada `seo.content` eklendi. CLI'da `--operation seo.content` seçin;
varsayılan işlem `seo.analyze` olarak kalır. İki işlem aynı kaynak özeti, görev
çakışması, yetki ve Docker güvenlik kontrollerinden geçer. Ayrıntılar için
[anahtar kelime ve içerik SEO belgesi](CONTENT_SEO.md). GEO, Ads ve Meta eklenmedi.

## Teknik SEO uzmanı

`seo.technical` işlemi canonical, robots meta, H1 ve bağlantıları çevrimdışı
inceler. `--operation seo.technical` ile seçilir; varsayılan işlem değişmez.
[Ölçülen kanıtlar ve sınırlar](TECHNICAL_SEO.md). Üç SEO işlemi aynı yetki, görev
kaydı, girdi çakışması ve Docker güvenlik kurallarından geçer.

## Birleşik denetim

`--operation seo.audit` üç SEO uzmanını aynı kaynak görüntüsüyle yönetir; bağımsız
uzman işlemleri korunur. Kısmi başarısızlık, ara kayıt, kaynak kanıtları ve rapor
sınırları için [birleşik denetim belgesi](SEO_AUDIT.md).

## Çevrimdışı erişim katmanı

Yeni `AccessService`, kimlik sağlayıcı arayüzü ve her istekte güncel müşteri/rol
kontrolü için [prototipin güven sınırları](ACCESS_CONTROL.md). Mevcut CLI güvenilir
yerel kullanım aracıdır; bu katman gerçek kullanıcı girişi veya API açmaz.

## Kalıcı kaynak izi (5.4)

[Kalıcı Docker kaynak sahipliği](RESOURCE_OWNERSHIP.md), host tarafında müşteri,
görev ve yürütmeyi Docker etiketleriyle doğrulanmış kaynaklara bağlar.
Süreç ölümü belirsiz kaynakları otomatik temizlemez; dış supervisor henüz yoktur.
