# Kalıcı Docker kaynak sahipliği — çevrimdışı prototip (5.4)

Bu bileşen supervisor veya yeni kimlik/yetki sistemi değildir. Mevcut dört SEO
işlemi ve güvenilir yerel Docker runner için host tarafında kaynak izi ekler.
İşçiye SQLite, Docker soketi, host mount'u veya yeni yetki verilmez. Ağ ve kaynak
sınırları değişmez. Gerçek site, API veya ücretli hizmet kullanılmaz.

## Kayıt yeri ve güven sınırı

Koordinatör yeni görevi kalıcı olarak kabul ettikten sonra `resource_scope` ile
müşteri, görev ve yürütme bağını güvenilir koddan kurar. Yetkili servis çağrısında
mevcut `ExecutionControl.execution_id`, güvenilir CLI'da yeni UUID kullanılır.
`seo.audit` altındaki üç analiz aynı yürütmeye, ayrı oluşturma denemelerine bağlıdır.
İşçi yalnızca o müşterinin imajından başlatılır; koordinatör yolunda başka
müşterinin imajı dahi hazırlanmaz. Görev girdisi özeti kuralları değişmedi.

Kaynak deposu `<state-root>/docker-resources/<customer>/resources.sqlite3`
yolundadır; kilidi `resources.lock` dosyasıdır. `resource_meta` tablosu açık depo
türü (`dmp-docker-ownership`), müşteri ve sürüm 1 işareti taşır. Görev deposunun
`tasks.sqlite3` dosyası, şema kurma veya `running -> interrupted` kurtarma yolu
kaynak deposu açılışında **çalıştırılmaz**. Yalnızca ortak `_private` izin denetimi
ve mevcut descriptor-relative, no-follow dosya açma ilkeleri kullanılır.

Yanlış kökte herhangi bir demo müşterisinin görev dosyası varsa işlem kaynak
kilidi/dosyası oluşturmadan reddedilir. `resources.sqlite3` adının altına kopyalanmış
bir görev veritabanı da önce sınırlı baytlarla bellekte incelenir; tür, şema,
müşteri ve sürüm doğrulanmadan diskte SQLite açılışı yapılmaz. Görev deposu da
kendi müşteri dizininde kaynak veritabanı görürse bunu benimsemez. Görevlerin asıl
`<state-root>/<customer>/tasks.sqlite3` dosyasıyla ortak SQLite işlemi yoktur.

Doğrudan `DockerLab(ownership_root=...)` ve `runner --ownership-root ...` güvenilir
host kökü alabilir; üst dizin önceden mevcut ve güvenilir olmalıdır. Varsayılan,
sistem geçici dizini altındaki kullanıcıya özel
`dmp-offline-resources-v1-<uid>` dizinidir; iş bitince otomatik silinmez, süreç yeniden
başlatılınca okunabilir. Ancak `/tmp` sistemce temizlenebilir: uzun süreli iz ve
gelecekte supervisor için operatör kalıcı diskte özel kök seçmelidir. Bu CLI müşteri
giriş arayüzü değildir. Doğrudan lab görev etiketi `local-demo`, yürütme ID'si
lab'a özgüdür; CLI görevi bir kullanıcı oturumuna bağlıymış gibi sunulmaz.

Dizinler 0700, dosyalar 0600; her yol bileşeninde symlink reddi, tek bağlantılı
normal dosya, müşteri sahipliği, tenant flock ve 64 MiB dosya bütçesi kullanılır.
Her oturumda tam `integrity_check`, sabit tablo şeması/sürüm 1, kayıt alanları ve
benzersizlik doğrulanır. Müşteri başına en fazla 10.000 kaynak denemesi vardır;
otomatik kayıt silme veya şema dönüştürme yoktur. Doluluk operatör işi gerektirir.
PID/fork kopyası reddedilir. Kötü niyetli aynı UID, host yöneticisi veya Docker
daemon yöneticisine karşı koruma/kriptografik imza garantisi verilmez. Üst dizinler
ve durum deposu güvenilir host yönetiminde olmalıdır; mevcut TaskStore'un güven
varsayımları sürer. Çok süreçli oturum iptali geliştirilmemiştir.

Eski deneysel `tasks.sqlite3` kaynak biçimi ve eski `/tmp/dmp-offline-ownership-*`
alanı otomatik benimsenmez, dönüştürülmez veya silinmez. Eski kök açıkça verilirse
güvenli hata alınır; operatör kayıtları koruyup yeni biçim için ayrı kök seçmelidir.
Eski kaynakların yok olduğu veya temizlendiği varsayılmaz.

## Kayıt ve Docker kanıtı

Her deneme şu alanları taşır: `customer`, `task`, `execution`, `attempt`, `proof`,
`kind`, `state`, `resource`. Yürütme/deneme/rastgele kanıt UUID'dir. Kaynak ID ve
kanıt veritabanı içinde tekildir; müşteri alanı dosyanın sahibine uymalıdır.
Başka müşteride aynı görev ID'si farklı kayıt/proof üretir.

İlk altı alan Docker `dmp-owner-*` etiketlerine de yazılır. `proof` tahmin edilmesi
zor bir eşleştirme değeridir; oturum sırrı, kullanıcı yetkisi veya imza değildir.
Worker yanıtı sahiplik kanıtı kabul edilmez. Sabit yerel Unix soketi üzerinden
`docker inspect` ile tam `Id`, kaynak türü ve bütün etiketler karşılaştırılır;
kalıcı kayıttaki bağ ve yaşam döngüsü de yeniden doğrulanır. Sadece yerel kaydın
varlığı, sadece lab etiketi veya ID'nin biçiminin doğru olması yeterli değildir.
Yanlış müşteri/görev/proof, kimlik çakışması ve bozuk kayıt reddedilir. Docker
yöneticisi etiketleri taklit edebileceğinden daemon güvenilir sınırın içindedir.

Konteyner oluşturulmadan önce seçilmiş imajın kalıcı/Docker sahipliği de
doğrulanır; başka müşterinin geçerli imaj ID'sinin bellek eşlemesine konması
reddedilir. Başlatmadan önce mevcut tam konteyner güvenlik politikası ayrıca kontrol edilir.
Temizleme ve iptal durdurması da sahipliği yeniden doğrular. Cevabı kaybolmuş bir
oluşturma, o lab'ın mevcut rastgele etiketiyle keşfedilse bile kaydedilmiş deneme
ve tam Docker kanıtı eşleşmeden silinmez. Tanınmayan kaynak operatör incelemesi
hatasıdır; genel `prune` veya yabancı kaynağı benimseme yoktur. Sahiplik kontrolünü kapatan üretim bayrağı yoktur. Native Docker sınırında
create için doğrulanmış imaj/fiks istek, import için kalıcı niyet ve sabit sahiplik
etiketleri gerekir. Start/stop/kill/rm ve image rm için tam ID, etiket, müşteri,
görev/yürütme ve kalıcı kayıt tekrar karşılaştırılır. Start ayrıca mevcut tüm
konteyner güvenlik ayarlarını doğrular. Yeni lab veya belleğe eklenen `_persistent`
alanı bu kontrolleri kapatamaz. Sabit komut izin listesi `container rm`, `rmi`,
`exec` gibi alternatif/yetkisiz komut yollarını da gönderimden önce reddeder.

Sahte Docker testleri gerçek geçici SQLite kayıtlarıyla fixture hazırlar; sadece
Docker cevabı/arıza noktası taklit edilir. Dar dispatch birim testlerindeki açık
mock'lar, gerçek Docker entegrasyonunun yerine geçmez.

## Durum geçişleri ve kaçınılmaz belirsizlik

1. `not_sent`: Niyet komuttan önce FULL synchronous SQLite commit'iyle kalıcıdır.
   Bu kayıt olmadan `create`/`import` gönderilmez.
2. `sent_unconfirmed`: Gönderim niyeti, `Popen` öncesi kalıcı yazılır. İptal ve
   süreç oluşturma kilidi bu küçük kayıt/başlatma bölümünü sıralar; Docker komutunu
   beklerken tutulmaz. Dosya sistemi fsync gecikmesi bu bölümü uzatabilir; gerçek
   zaman garantisi yoktur. SQLite ve Docker atomik olmadığı için bu commit ile
   gerçek gönderim arasında süreç ölürse **gönderilmemiş olsa bile belirsiz** kalır.
   Bu durum kesin gönderim kanıtı değildir; kaynağın çıkmayacağı da varsayılmaz.
3. `identified`: CLI'nın ID cevabı, Docker inspect ve kalıcı sahiplik eşleşmesiyle
   doğrulanınca yazılır. Kalıcı kaynak kimliği daha sonra başka ID'ye bağlanamaz.
4. `removed`: Doğrulanmış kaynağın silme komutu başarılı döndüğünde yazılır.
   Silme gerçekleşip cevap/kayıt kaybolursa kayıt `identified` kalabilir; bu,
   kaynağın hâlâ var olduğu iddiası değildir. Gelecekteki uzlaştırma ayrıca gerekir.

İptal gönderimden önce kazanırsa `not_sent` kalır. Native Popen kesin fork/exec
hatası bildirirse `sent_unconfirmed -> not_sent` geri geçişi mümkündür; gönderim
sonucu belirsizken bu geçiş yapılmaz. Sonuçsuz/başarısız komut otomatik yeniden
gönderilmez. Aynı görev mevcut koordinatör idempotency kuralıyla tekrar çalışmaz.

Her yazı müşteri kilidi altında beklenen önceki durumu kontrol eder. SQLite yazma
veya commit hatasında ilgili `ResourceStore` nesnesi kapanır; commit gerçekleşip
onayı kaybolmuş olabileceğinden aynı nesne yeniden deneme yapmaz. Yeni host süreci
kayıtları okuyabilir ama otomatik tamamlamaz, dönüştürmez, kaynak başlatmaz veya
silmez. Hassas SQLite hata metni/yolu müşteri raporuna taşınmaz; yöneticiye genel
inceleme gereksinimi gösterilir. Hata sonrası temizlik doğrulanamıyorsa mevcut
runner bunu gizlemez; başarılı sonuç yayımlanmaz.

`ResourceStore.records(customer)` ve `verify(...)` yalnızca güvenilir yerel
operatör/runner yardımcılarıdır; müşteri API'sine açılmaz. `records` depoyu açıp
kendi türünü ve şemasını denetleyen, yeni depo oluşturabilen bakım erişimidir; salt-okunur müşteri sorgusu
değildir. Mevcut AccessService status/report/recommendations yolu bu depoyu açmaz.

## Test kanıtı ve sonraki sınır

[Testler](../../../tests/test_offline_resource_store.py) gerçek geçici SQLite
ile iki müşteriyi, sahte sahipliği/ID'yi, bozuk şema/kayıtları, boyut-adet bütçesini,
commit hatasını ve kaybolan commit onayını sınar. Süreç kesinti testleri SIGKILL
sonrası not_sent/sent_unconfirmed/identified kayıtlarının korunduğunu doğrular.
Gerçek yerel Docker testinde import ve create için niyet, gönderim öncesi,
kaybolmuş cevap ve doğrulanmış kimlik noktalarında çocuk süreç öldürülür;
kaynaklar testin güvenilir ebeveyni tarafından kanıt eşleşmesiyle temizlenir.
Bu test temizliği ürün supervisor'ı değildir. Başka entegrasyon iki müşteride dört
SEO işlemini ve taslak önerileri çalıştırır; aynı görev tekrarında yeni kaynak
kaydı oluşmadığını doğrular. Destek yoksa Docker/fork testleri gerekçeyle atlanır.

Dış supervisor, yetki iptalinin süreçler arası dağıtımı, otomatik artık kaynak
uzlaştırması, kalıcı operatör uyarıları ve yedekleme bu aşamada yoktur. Ana süreç
SIGKILL ile ölürse çalışan işçi kendi başına durmayabilir; kalıcı kayıt artık hangi
denemenin inceleneceğini korur. Bilinmeyen kaynağın veya kayıp deponun güvenle
silinebileceği iddia edilmez. 5.5–5.8 yapılmadan üretim güvenliği tamamlanmış değildir.

## Kabul kriterleri için test eşleştirmesi

| Kriter | Test kanıtı |
|---|---|
| Aynı görev ID'si, iki müşteri | `test_two_customers_same_task_have_distinct_provenance`; gerçek iki müşterili dört işlem testi |
| Komuttan önce kalıcı niyet ve belirsiz gönderim | `test_cancel_before_dispatch_leaves_durable_not_sent`, `test_lost_docker_reply_remains_uncertain_on_restart` |
| SQL yazma/commit hatası ve kayıp commit cevabı | `test_commit_error_and_lost_ack_never_allow_owner_retry`, `test_send_commit_failure_prevents_process_start`, `test_ambiguous_send_commit_never_spawns_and_survives_restart`; temizlik commit hatası kontrolü |
| SIGKILL ve yeniden başlatma | `test_sigkill_at_persistence_boundaries_never_creates_success`, `test_real_create_import_sigkill_and_lost_reply_boundaries` (2 kaynak türü × 4 nokta) |
| Yanlış ID/proof/müşteri/imaj ve bozuk kayıt | `test_resource_identity_labels_kind_and_binding_must_match`, `test_record_manipulation_wrong_tenant_schema_and_corruption_rejected`, `test_scope_restricts_cross_customer_creation`, `test_false_ownership_cleanup_is_refused` |
| Tekrar ve eski özellikler | `test_two_tenants_four_operations_and_recommendations_keep_provenance`; mevcut oturum/iptal/kimlik/SEO test paketi |

Bu yöntemlerin tamamı yukarıdaki test dosyasındadır. Kabul kanıtı süreç ölümü ve
kontrollü hata enjeksiyonunu kapsar; fiziksel disk kaybı, host ele geçirilmesi veya
Docker çekirdek açığına dayanıklılık sertifikası değildir.

Son uygulama doğrulaması: ilgili grup **202 başarılı / 0 atlanan / 0 başarısız**;
tam paket **1021 toplam / 1012 başarılı / 9 atlanan / 0 başarısız**.
Kaynak sahipliği dosyasındaki 26 test (bu düzeltmede eklenen 7 regresyon dahil)
içinde gerçek Docker testleri atlanmadan çalıştı. Bağımsız dosya koşusu da
26 başarılı / 0 atlanan / 0 başarısız sonuç verdi. Mevcut
9 atlama diğer isteğe bağlı bileşenlere aittir. Son kontrolde bu testlerin etiketli
Docker kaynakları kalmadı. Bu sonuçlar üretim sertifikası veya supervisor kanıtı değildir.

## Bağımsız inceleme sonrası ek regresyonlar

Yanlış görev kökü ve görev DB'sinin kaynak dosya adı altına kopyalanması hiçbir
mevcut dosya baytını değiştirmeden reddedilir. Tür işareti/legacy biçim hataları,
yeni lab ve sahte `_persistent` alanı, tüm native başlatma/sonlandırma/silme
komutlarında eksik/çelişkili kanıt ve tam kimlik uyuşmazlığı sınanır.

Öneri katmanı `MAX_FILE_BYTES` sınırını testlerin `sys.path` değişikliklerinden
almıyor; güvenilir `runner.seo` yükleyicisinden kullanıyor. Temiz Python sürecinde,
`scripts/` yolu ve diğer testlerin importları olmadan gerçek kurgusal HTML'den
öneri üretilmesi ayrıca doğrulanır. Yeni test dosyası bağımsız çalıştırılır:

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH="$PWD/tests/offline_guard:$PWD/examples/offline-seo/isolation" python -B -m unittest discover -s tests -p test_offline_resource_store.py -v
```
