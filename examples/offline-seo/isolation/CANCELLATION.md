# Güvenli görev iptali — çevrimdışı prototip

## Gerçekte desteklenen kapsam

`AccessService.submit` görevi güvenilir biçimde o çağrının opak oturumu, değişmez
hesap kimliği, müşterisi ve görev kimliğiyle bağlar. Bağ istek JSON'undan alınmaz.
Aynı servis/sağlayıcıdaki tekil oturum iptali ve hesap bazında toplu iptal, ilgili
çalışan kontrolleri haberdar eder. Hesap kapatma/kimlik değişimi, müşteri kapatma,
üyelik kaldırma ve okuyucuya rol düşürme de ilgili kapsamı iptal eder. Politika
mutasyonu sırasında bildirimden sonra SQL yazımı başarısız olsa bile iptal geri
alınmaz; depo zaten güvenli biçimde kapanır. Bu muhafazakâr davranıştır.

Görev boyunca ortak politika kilidi tutulmaz. Başlangıçta, kritik yürütme
noktalarında ve cevap öncesinde güncel kimlik/üyelik/rol doğrulanır. Süre dolması,
saat veya SQLite sağlık hatası sonraki kontrol noktasında iptale dönüşür. Docker
attach çıktısı beklenirken en fazla 250 ms aralıkla kontrol yapılır; depodaki bir
kilit beklemesi/hatası bu gecikmeyi artırabilir. Gerçek zaman garantisi yoktur.

Koordinatör, Docker hazırlığı/başlatması ve birleşik denetimin uzmanları arasına
kontrol noktaları koyar. İptal edilen birleşik denetim yeni uzmana geçmez.
Docker'da önce yalnızca sahipliği ve güvenlik ayarları yeniden doğrulanmış kendi
konteynerine `stop --time 2` gönderilir. Docker bu sürenin sonunda zorunlu
sonlandırma yapabilir. Çalışma sürüyorsa ayrıca `kill` denenir; ardından gerçek
`State.Running` tekrar okunur. CLI cevabı tek başına durma kanıtı değildir.
PID 1 veya SIGTERM'i yok sayan işçi zorunlu sonlandırma gerektirebilir. Mevcut
network=none, kullanıcı, salt okunur kök, kaynak ve mount sınırlamaları korunur.
Temizlik hâlâ görev/lab'a özgü etiket ve kaynak kimliklerini kullanır.

Özel Python adaptörleri yalnızca işbirlikçi kontrol noktalarında durdurulabilir.
Kontrol noktası olmayan bir fonksiyon veya kontrol dışı başlattığı çocuk süreç
zorla durdurulmaz. Fonksiyon dönene kadar yalnızca iptal istendiği gösterilir;
başarılı sonuç yayımlanmaz. Güvenilmeyen adaptör aynı süreçte çalıştırılmamalıdır.

## Durumlar ve kalıcı kayıt

| Kontrol durumu | Anlamı |
|---|---|
| requested | İptal istendi; durma henüz doğrulanmadı. |
| stopping | Docker sonlandırma protokolü yürütülüyor. |
| stopped | Yürütme kontrolü geri döndü; desteklenen Docker işçisi durdu/temizlendi veya işbirlikçi fonksiyon sonlandı. |
| already_completed | Terminal kayıt yayımlama yarışı tamamlanma lehine bitti; geriye dönük iptal iddiası yok. |
| stop_failed | Durdurma veya temizlik protokolü başarısız; operatör incelemesi gerekir. Temizlik ayrıca denenir. |

`service.execution_states(admin_session, customer)` yalnızca aktif sistem yöneticisi
ve o müşteride açık üyelik ile okunabilir. Başka müşterilerin görevleri dönmez. Ara durumlar süreç içindedir; en fazla 1000 aktif kontrol ve
128 yakın terminal geçmişi tutulur. Tamamlanan kontroller oturum nesnesi veya
kimlik doğrulama callback'i tutmaz. Geçmiş kalıcı denetim kaydı değildir.

Kalıcı SQLite görev modeli değiştirilmez: durdurulmuş yürütme `interrupted`,
durdurma/temizlik hatası `failed` olur. Başarılı veya kısmi uzman sonucu iptal
sonucu olarak korunmaz (`result=NULL`). Terminal hata kodu `Cancellation: stopped`
veya başarısız yürütme hatasıdır. Hassas kaynak/temizlik ayrıntıları müşteri
raporuna konmaz; mevcut güvenilir yönetici tanılama yolu kullanılır. Durdurulamayan
kaynakların etiket/kimlik bilgileri temizlik hatasında operatöre bırakılır.

Başarı commit'i ile iptal aynı süreçte kısa bir yayımlama kilidiyle sıralanır.
Tamamlama kazanırsa kayıt geriye dönük değiştirilmez; iptal kazanırsa başarılı
sonuç yazılmaz. SQLite commit hatasında durma veya kalıcı sonuç kesinmiş gibi
sunulmaz. Aynı görev kimliği iptal sonrası otomatik tekrar çalıştırılmaz; mevcut
girdi özeti ve çakışma kuralları korunur. Yeniden başlatma/SIGKILL sonrası bellek
bağı yoktur; mevcut `running -> interrupted` kurtarması otomatik tekrar yapmaz.

Aynı koordinatörün aynı müşteri görevleri yerel kilitte en fazla 180 saniye
bekletilir; beklerken iptal kontrol edilir. Müşteriler farklı yerel kilitler
kullanır. Mevcut süreçler arası nonblocking dosya kilidi korunur. Bu, dağıtık görev
sahipliği değildir; farklı koordinatör/süreçler meşgul hatası alabilir.

## Açık sınırlar

Güvenilir yerel CLI görevleri bir oturuma bağlı değildir; oturum iptaliyle
ilişkilendirilip durdurulmuş gibi gösterilmez. Başka süreçteki/başka sağlayıcıdaki
veya yeniden başlatmadan önceki görevlere bellek içi iptal gönderilemez. Üretimde
kalıcı sahiplik/iptal deposu, supervisor ve yeniden bağlanma tasarımı gerekir.
SIGKILL ile host ölürse Docker işçisini durduracak garantili bir dış supervisor
bu aşamada yoktur. Temizlik hatası operatör müdahalesi gerektirebilir.

## SQLite sağlık kontrolü

`directory.checked()` tek iş parçacığında politika kilidi tutulurken iç içe
okumaların tam dosya/metadata doğrulamasını tekrar etmesini önler. Giriş ve çıkış
sınırlarında doğrulama vardır; PID ve kapalı/poisoned kontrolleri atlanmaz.
Kilit bırakıldıktan sonra veya farklı iş parçacığında sağlık bilgisi taşınmaz.
Yazma işlemlerindeki dosya kimliği, SQLite atomik revizyon/CAS, commit öncesi ve
sonrası kontroller korunur. `fork` kopyası kilit alınmadan reddedilir.

Ölçülen sağlıklı yol: bir durum sorgusu 12 yerine 6 tam sağlık/metadata kontrolü;
1000 aktif oturumda yeni oturumun reddi 1003 yerine 4 kontrol. Bu çağrı sayısıdır,
disk gecikmesi veya üretim kapasitesi garantisi değildir. 250 ms yürütme kontrolleri
ve tek sahip SQLite tasarımı hâlâ yüksek trafik çözümü değildir.

## Gelecekteki yan etkiler — yalnızca kapalı arayüz

`CriticalOperation` müşteri, görev, işlem türü, kaynak özeti, kesin değişiklik
özeti, onay referansı ve azami işlem/bütçe birimini taşır.
`OfflineSideEffectGate.check` **her zaman reddeder**; gerçek onay veya bütçe
uygulaması/izni üretmez. WordPress, Shopify ve reklam servisleri bağlanmamıştır.

Gerçek yürütücü her gönderimden hemen önce güncel müşteri yetkisini, görevin iptal
ve sahiplik durumunu, kesin işlem sürümüne bağlı tek kullanımlık insan onayını ve
atomik bütçe/işlem rezervasyonunu doğrulamalıdır. Dış servis gönderimi yerel
kontrolle atomik değildir: arada yetki iptali olabilir. Gönderilmiş reklam harcaması
veya site değişikliğinin mutlaka geri alınacağı varsayılamaz. Servis idempotency
anahtarı, gönderim/sonuç uzlaştırması ve gerekiyorsa telafi süreci ayrıca gerekir.
Bu arayüz canlı yazma güvenliğinin tamamlandığı anlamına gelmez.

Güvenilir ham `TrustedCaller` koordinatörü eski yerel CLI/süreç testlerinde fork
sonrasında yeni süreç içi görev kilitleri oluşturabilir; mevcut dosya kilidi
korunur. Bu, oturum mirasını etkinleştirmez: erişim servisi, sağlayıcı, kimlik
deposu ve etkin yürütme kontrolünün PID reddi değişmez.

Aynı görev kimliğine eşzamanlı başvurular ayrı, host tarafından üretilmiş
`execution_id` taşır. `owns_execution=true` yalnızca yeni kalıcı yürütmeyi kabul
eden başvuruda bulunur. Bekleyen/idempotent bir başvurunun iptali başka oturumun
aynı kimlikte çalışan görevini durdurmaz. `owns_execution=false` olan durdurulmuş
kontrol yalnızca o başvurunun bittiğini belirtir; ana görevin durma kanıtı değildir.

## Başlatma yarışı ve kaynak gönderim kaydı düzeltmesi

`ExecutionControl.dispatch` güncel yetkiyi kontrol eder, ardından iptal durumunu
kısa kontrol kilidi altında tekrar doğrulayıp yalnızca `Popen` oluşturmasını
sıralar. İptal önce tamamlandıysa CLI başlatılmaz. Başlatma kazanırsa iptal
oluşturulmuş süreçten sonra sıralanır; bekleme ve çıktı okuma kilit dışında kalır.
Bu kilit Docker komutunun tamamını veya konteynerin çalışma süresini kapsamaz.

`creation_states` her kaynak türünün son gönderim denemesini gösterir:

- `not_sent`: Son iptal kontrolü veya kesin fork/exec hatası nedeniyle CLI gönderilmedi.
- `sent_unconfirmed`: CLI başladı fakat kimlik cevabı doğrulanmadı. Spawn sırasında
  beklenmedik kesintide de temkinli olarak bu durum kullanılır. Kaynak oluşmadığı
  varsayılmaz; mevcut etiketli keşif ve çözülemeyen kaynak uyarıları korunur.
- `identified`: Cevaptaki kaynak kimliği doğrulandı ve sahip olunan kaynaklara eklendi.
  Bu alan kaynağın hâlâ mevcut olduğunu belirtmez; temizlik ayrıca izlenir.

Bekleyen kaynak işareti artık `create`/`import` çağrısından önce değil, gerçekten
CLI başlatıldığında veya spawn sonucu belirsiz kaldığında eklenir. Gönderim öncesi
iptal, tek başına `stop_failed` üretmez. Gerçek durdurma/temizlik hataları hâlâ
hata olarak saklanır. Docker durma kanıtındaki `Running`/`Restarting` alanları da
boolean olarak doğrulanır; bozuk veya yeniden başlamakta olan durum durdu sayılmaz.
