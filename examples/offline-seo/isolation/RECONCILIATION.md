# Artık kaynak uzlaştırma ve operatör uyarıları — 5.6 prototipi

Bu modül yalnızca güvenilir yerel host operatörü içindir. Ağ sunucusu, müşteri
API'si, kimlik doğrulama veya production servisi değildir. Gerçek site/reklam
hesabı bağlanmaz; kaynak sahipliği için mevcut 5.4–5.5 SQLite/etiket/kilit sistemi
kullanılır. 5.5 Supervisor'ın kodu veya yürütme kilidi protokolü gevşetilmedi.

## Kullanım ve müdahale politikası

Varsayılan: Docker'a yalnızca okuma komutları gönderilir. Yerel gözlemler kalıcı
kaydedilir; doğrulanmış belirsiz create/import kimliği kayda bağlanabilir. Bu,
SQLite'a hiç yazmayan bir müşteri raporu sorgusu değildir.

```bash
python examples/offline-seo/isolation/reconciliation.py \
  --ownership-root /guvenilir-kalici-kok/docker-resources \
  --customer demo-musteri
```

`--json` sürüm 1 operatör olay projeksiyonu üretir. Varsayılan metin çıktısı sabit
Türkçe uyarılar gösterir; ham Docker stderr, token, proof, kaynak HTML veya dahili
dosya yolu yazılmaz. Yalnızca seçilen müşterinin görev/yürütme referansları gösterilir.

Güvenilir operatör `--apply` eklerse, yalnızca doğrulanmış yetim kaynaklarda
müdahale denenir. 5.5 `review` mandalı kendiliğinden kaldırılmaz: bu kayıtlar için
ayrıca `--retry-reviewed` açık seçimi gerekir. Bu seçenek kimlik/sahiplik/kilit
kontrollerini atlamaz; her şeyi yeniden doğrular. Gerçek panel onayı veya OIDC
onayı değildir; host operatörü zaten ayrıcalıklı güven sınırının içindedir.

İşlem bir çağrıda en fazla 100 kaynak inceler (`--limit 1..100`, `--offset`).
Çıktı `scan_has_more` ve `next_offset` ile kalan işi açıklar; kısmi tarama veya
çözülememiş uyarı, meşgul kilit veya sağlık kontrolü hatası varsa CLI çıkışı 2,
tamamlanmış ve uyarısız taramada 0'dır.
Kayıt sırası konteynerler önce, sonra imajlar ve kararlı attempt sırasıdır.
Eşzamanlı yeni kayıtlar nedeniyle sayfalar tek atomik envanter görüntüsü değildir;
işlemler idempotenttir, sonraki tam tur tekrar gözlemleyebilir. Otomatik scheduler
veya servis deployment'ı yoktur. `Reconciler.report(customer, limit=100, offset=0)`
uyarı sayfalamasıdır; en fazla 500 kayıt döner. Hata/uyarılar bilgi kayıtlarından
önce sıralanır. Tüm kayıtlar silinmeden kalıcıdır; sayfalanan çıktı uyarı kaybı değildir.

## Durumlar ve güvenli uzlaştırma

1. Önce yerel Docker sürüm/daemon erişimi gözlenir. Başarısızsa kaynak durumları
   değiştirilmez; bilinen kaynaklara `docker_unavailable` uyarısı yazılır. Komut
   ortasında bağlantı/yanıt kaybı da kaynağın yokluğu sayılmaz.
2. Kaynak metadata'sı güncelken görev/yürütme eşleşmesi ve aynı execution lock
   dosyasının kimliği doğrulanır. EX claim alınamazsa kilidi coordinator veya
   başka uzlaştırıcı/supervisor tutuyor olabilir: `busy_count` artar, Docker'a
   mutasyon gönderilmez. Önceki uyarı varsa aynen korunur; yoksa `busy` kaydedilir.
   Kilit tek başına sağlıklı coordinator kanıtı sayılmaz. `active` eski sürümlerin
   kayıt kodu olarak okunabilir; yeni taramalar bu olumlu sağlık sonucunu üretmez.
   PID, eski zaman damgası veya yalnız etiket ölüm kanıtı değildir. Eksik/değişmiş
   lock `missing_lease`, geçici metadata meşguliyeti `busy` olur.
3. `not_sent` kaynak varmış gibi kabul edilmez. `sent_unconfirmed` için attempt
   etiketi yalnızca arama ipucudur. Tek tam ID, tür ve altı sahiplik alanı 5.4
   `verify` ile eşleşmelidir; sıfır/çoklu eşleşme `pending_unresolved`, yanlış
   kimlik `ownership_mismatch` olur. Belirsiz oluşturma otomatik tekrar gönderilmez.
4. `identified` tam kimlik Docker'ın başarılı, sınırlandırılmış listesinde yoksa
   `absent_observed` kaydedilir. Bu, doğrulanmış kimliği olan **eski kaydın** yokluk
   gözlemidir; bilinmeyen kaynağa sahiplik atamak değildir. `--apply` ve gereken
   açık review tekrar seçimiyle, orphan claim altında kaynak kaydı `removed` ve
   varsa supervisor sonucu `removed` aynı SQLite işlemiyle tamamlanır. Docker'a
   silme gönderilmez. Daemon hatası veya bozuk/eksik yanıt yokluk sayılmaz.
5. Mevcut konteynerin tüm kimlik/etiketleri yeniden doğrulanır. Önce kalıcı
   `cleanup_pending`, sonra mevcut 5.5 stop/kill/inspect/remove protokolü uygulanır.
   Native Docker sınırı her mutasyondan önce kanıtı tekrar kontrol eder. Durma ve
   kaldırma doğrulanmadan başarı yazılmaz. Başarısızlık `cleanup_failed` ve
   konteyner için mevcut `review` mandalıdır; karar claim bırakılmadan kaydedilir.
6. İmajda aynı yürütmenin belirsiz/mevcut konteyner kaydı varsa `image_blocked`.
   Tamamen çözümlenmiş yürütmede, tam imaj kimliği/proof doğrulanarak yalnızca
   `image rm <tam-ID>` denenir; **force yoktur**. Docker'ın yabancı/başka konteyner
   referansı koruması aşılmaz. Sonrasında yokluk doğrulanır. Başarısız imaj temizliği
   kalıcı uyarıdır. Sağlıklı başka yürütmenin kilidi ve kaynakları korunur.
7. Kayıt `removed` iken o tam ID Docker'da görünürse `removed_but_present` ve
   insan incelemesi gerekir; otomatik durdurma/silme yapılmaz. Genel prune yoktur.

SQLite ile Docker atomik değildir. Kaynak kaldırılıp cevabı veya sonraki commit
kaybolursa metadata hâlâ identified/cleanup_pending/review kalabilir. Yeniden
başlatmada bu belirsizlik korunur; yeni gözlem ve açık operatör uygulaması gerekir.
Depo bozuksa/commit sonucu belirsizse ilgili nesne kapanır; aynı işlemde taze
bağlantıyla güvenli olmayan tekrar yapılmaz. Uyarı bile yazılamıyorsa CLI bunu
sabit hata mesajıyla belirtir, 'uyarı kesin kaydedildi' demez.

**SEO görevleri değişmez:** bu modül tasks.sqlite3 açmaz, task recovery çalıştırmaz,
sonuç veya hata alanı güncellemez. Kaynak temizliği, başarısız/kesilmiş analizi
başarılı yapmaz. Gerçek test görev veritabanının baytlarının değişmediğini de sınar.

## Depo ve 5.7 ile sınır

Aynı müşteri kaynak DB'sine `reconciliation_observations` eklenir (şema 3).
Tam olarak doğrulanmış sürüm 1/2, kayıt/ilişki/bütünlük kontrollerinden sonra tek
transaction ile 3'e yükseltilir. Bozuk veya yanlış türde DB onarılmaz/sıfırlanmaz;
geçersiz eski metadata yükseltilmez. Kaynak kanıtları ve execution lease aynen
kalır. Eski binary sürüm 3'ü açamaz; operatör yükseltmeden önce güvenilir yedek
almalıdır. 64 MiB DB ve 10.000 kaynak/lease sınırları sürer.

Her attempt için yalnızca son gözlem tutulur. Aynı kod tekrarında ek kayıt,
revision artışı veya commit yapılmaz; tekrarlı gözlem byte düzeyinde idempotency
regresyonuyla sınanır. Kod değişince revision artar. Bozuk referans, bilinmeyen
kod, geçersiz revision veya identified kaynağı removed gösteren kayıt reddedilir.

`report()` kaynaklar ve koşulları tek tenant kilidi/SQLite snapshot'ında okur.
**Son kalıcı gözlem** projeksiyonudur; canlı Docker sağlık garantisi
veya anlık envanter değildir. `alert_id` müşteriye/attempt'e bağlı kararlı anahtar,
`event_id` aynı anahtar/revision için kararlı tekrar önleme anahtarıdır. Alanlar:
`schema_version=1`, component, customer/task/execution/attempt referansları, kind,
code, severity, Türkçe message, revision, alert_id, event_id. Bunlar ileride 5.7
kaydına aktarılabilecek açık bir veri sözleşmesidir; hash'ler kimlik doğrulama,
imza veya güvenilirlik kanıtı değildir.

**5.7 tamamlanmadı:** append-only güvenlik geçmişi, actor/SSO kimliği, değiştirilemez
olay zinciri, tüm politika olayları, kalıcı bildirim kuyruğu, saklama/silme politikası
ve dış e-posta/Slack bildirimi yoktur. Gözlem değişince önceki kod geçmişi burada
tutulmaz; gelecekteki güvenlik denetim deposu bunu ayrıca tasarlamalıdır.

## Kabul kapsamı ve kalan riskler

Testler iki kurgusal müşteriyi, sağlıklı yürütmenin korunmasını, restart/uyarı
kalıcılığını, kayıp cevap ve commit onayını, gerçek SIGKILL sonrası intent kaydını,
şema geçişi, bozuk metadata, sabit event anahtarlarını, çıktı/işlem sayfalamasını
ve native imaj kaldırma öncesi değişen sahipliğin reddini kapsar. Gerçek Docker
deneyinde iki ayrı coordinator/worker çalışır; biri öldürülür, güvenli orphan
konteyner ve imaj temizliği yapılır; diğer müşteri aktif kalır. Gerçek Docker CLI
ayrıca geçersiz yerel socket üzerinde sınanır. Ortak daemon durdurulmaz; stop/remove
ve commit hatalarının diğer çeşitleri kontrollü taklitlerle sınanır.

Kapsam bilinen kalıcı kaynak kayıtlarıdır. DB'de hiç izi olmayan Docker kaynakları
global taranmaz/benimsenmez; genel host envanter uzlaştırması ve kayıp depo kurtarma
kapsam dışıdır. Supervisor'a yeni otomatik cleanup politikası enjekte edilmedi;
operatör aracı ayrı çağrılır. Çok host/NFS, host reboot, fiziksel disk/güç kaybı,
uzun süreli yük ve otomatik alarm teslimi kanıtlanmadı. Aynı UID/root/daemon
yöneticisi güvenilir kalır. Fork çocuğu lease tutarsa müdahale gecikebilir.
Bu bir çevrimdışı prototiptir; üretim güvenliği veya tam 5.7 denetimi değildir.


## İlk geliştirme doğrulaması — bağımsız inceleme öncesi

- Yeni uzlaştırma dosyası: **35 başarılı, 0 atlanan, 0 başarısız**.
- Ara regresyon grubu (kaynak deposu, supervisor, uzlaştırma, dispatch, iptal):
  **131 başarılı, 0 atlanan, 0 başarısız**; son eklenen batch testi ayrıca yeni dosya
  koşusunda ve tam pakette doğrulandı.
- Güncel tam paket: **1090 toplam, 1081 başarılı, 9 atlanan, 0 başarısız**.
- İki müşterili gerçek Docker testi ve gerçek erişilemeyen yerel socket testi
  atlanmadı. Daemon'ın kendisi durdurulmadı; diğer arızalar kontrollü enjeksiyondur.
- Kritik yollar gözden geçirildi: image rm öncesi son native sahiplik kontrolü,
  removed gözlemi ile kaynak durumu tutarlılığı, commit/lost-ack sonrası durma,
  tenant/task/execution kilidi ve hata yaşayan task DB'sinin bayt değişmezliği.
- README'nin önceden kalan BOM farkı korundu; yalnızca test sayıları güncellendi.

Bu kanıtla sürüm bağımsız güvenlik incelemesine hazır **çevrimdışı prototiptir**.
Üretim onayı veya 5.7 tamamlanma iddiası değildir. Commit/push/PR/merge/deployment
bu geliştirmede yapılmadı.


## Eleştirel inceleme düzeltmeleri

İki orta ve üç düşük bulgu, düzeltmeden önce başarısız regresyonlarla doğrulandı:

1. **Orta:** Başka süreç execution claim tuttuğunda active yazılıp açık alarm
   kapanabiliyordu. Gerçek ayrı süreç/SQLite kilit testinde cleanup_pending'in
   korunduğu doğrulanır; busy_count ayrı raporlanır, CLI 2 döner.
2. **Orta:** Eski not_sent görüntüsü, ilerlemiş kaynağın temizleme uyarısını
   ezebiliyordu. observation() artık state ve tam resource ID dahil beklenen
   kaynak görüntüsünü metadata kilidi altında karşılaştırır. Güncel değilse
   yazmaz, güvenli hata üretir. Müdahale sonrası gözlem taze kaynak satırına bağlanır.
3. **Düşük:** Python API'sinde 'false' metni truthy sayılabiliyordu. apply ve
   retry_reviewed yalnızca gerçek bool kabul eder; yanlış tür I/O'dan önce reddedilir.
   Argparse CLI zaten bool üretiyordu; bu bir dış kimlik/yetki atlatması değildi.
4. **Düşük:** Ayrı source/condition okumaları arasında yeni kayıt gelirse rapor
   KeyError verebiliyordu. Tek kilit ve tek SQLite transaction snapshot'ı kullanılır.
5. **Düşük:** Boş sayfada daemon hatası CLI 0'a dönüşebiliyordu. Başlangıç sağlık
   hatası health_check_failed ile ayrıca raporlanır ve CLI 2 döner. Hiç kaynak
   yoksa resource-scoped kalıcı uyarı üretilemez; kaynak uydurulmaz. Bu global
   durum o çağrının çıktısındadır; genel bileşen/audit kayıt sistemi değildir.

Varsayılan modun anlamı değişmedi: **Docker mutasyonu yoktur**, ancak metadata
şema geçişi, gözlem ve doğrulanmış pending kaynağın kimlik bağlaması yazıları
vardır. Tamamen salt-okunur dosya modu olarak sunulmaz; bunun için ayrı bir API
ve izin sınırı tasarlanması gerekir. Ayrı regresyon bu davranışı açıkça doğrular.

Açık sınırlar: mevcut kod tek host ve güvenilir yerel operatör/daemon varsayar.
İki gerçek süreç arasında claim/uyarı yarışı test edildi; uzun süreli çoklu
uzlaştırıcı Docker stres testi, güç kaybı/host reboot ve çok host doğrulanmadı.
Sahipsizliği veya tam kimliği doğrulanamayan kaynaklar temizlenmez. Kaynak görevi
başarıya çevrilmez. Uyarılar son durumdur, 5.7 append-only güvenlik geçmişi değildir.


İnceleme sonrası doğrulama:

- Her bulgu için önce hatayı gösteren test çalıştırıldı: dört ilk senaryo ve
  boş daemon sayfası senaryosu düzeltmeden önce başarısız oldu.
- Altı ek regresyonla uzlaştırma dosyası: **41 başarılı, 0 atlanan, 0 başarısız**.
  Ek altıncı test varsayılan pending gözleminin Docker'a mutasyon göndermediğini,
  fakat belgelenmiş SQLite kimlik bağlamasını yaptığını açıkça sınar.
- Tam paket yeniden çalıştırıldı: **1096 toplam, 1087 başarılı, 9 atlanan,
  0 başarısız**. Mevcut 5.4/5.5, SEO ve erişim regresyonları da bu pakettedir.
- Gerçek iki müşteri Docker entegrasyonu ve ayrı süreç claim/uyarı testi geçti.
- README'nin önceki yerel BOM farkı korundu; yalnızca test sayıları güncellendi.

Bu düzeltmelerle 5.6 **GitHub'a çevrimdışı prototip olarak kaydetmeye hazır**.
Üretim onayı değildir. 5.7 güvenlik denetim geçmişi tamamlanmadı. Bu incelemede
commit/push/PR/merge/deployment yapılmadı.
