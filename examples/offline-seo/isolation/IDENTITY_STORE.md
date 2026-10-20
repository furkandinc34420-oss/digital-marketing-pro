# Kalıcı çevrimdışı kimlik ve yetki deposu

`identity_store.PersistentAccessDirectory`, mevcut `AccessDirectory` arayüzünün
SQLite üzerinde kalıcı sürümüdür. Gerçek giriş, OIDC doğrulaması veya ağ servisi
oluşturmaz. Yalnızca kurgusal demo müşterileri desteklenir.

## Güvenilir kurulum

Güvenilir yerel yönetici `PersistentAccessDirectory('/mutlak/ozel/kimlik-dizini')`
oluşturur. Üst dizin önceden mevcut olmalıdır. Dizin, dosyalar ve bütün yol
bileşenleri sembolik bağlantılara karşı kontrol edilir; kök yalnızca sahibi için
0700, dosyalar 0600 erişimine sahip olmalıdır. Dosyalar normal, tek sabit bağlantılı
ve mevcut UID'ye ait olmalıdır. Mevcut boş veya tanınmayan veritabanı benimsenmez.

Dizin `OfflineIdentityProvider(directory)` ve
`AccessService(provider, directory, Coordinator(...))` ile kullanılır.
Kullanıcı/müşteri/üyelik yapılandırması yalnızca güvenilir yöneticinin mevcut
`set_user`, `set_tenant`, `set_membership`, `remove_membership` metodlarıyla yapılır.
Bu metodlar müşteri paneline bağlanmamalıdır; kullanıcı taleplerine açık yönetici
API'si veya otomatik varsayılan yönetici hesabı yoktur.

İş bitince `directory.close()` çağrılmalıdır. Açık nesnenin ömrü boyunca özel
`identity.lock` kilidi tutulur. İkinci nesne veya süreç aynı depoyu açamaz. PID
koruması ve tek süreç içinde iş parçacığı kilidi korunur. Çok süreçli hizmet
desteği etkinleştirilmemiştir. Doğrudan SQLite yazan aynı UID'deki kötü niyetli
koda karşı dosya kilidi bir güvenlik sınırı değildir.

## Kalıcı kayıtlar ve kimlik bağları

Tablolar: `users`, `tenants`, `memberships`, `roles`, `metadata`.
Dahili `user_id` hesap anahtarıdır; veritabanı tetikleyicileri ID güncellemesini ve
hesap silerek ID yeniden kullanımını engeller. Hesaplar silinmez, kapatılır.
Kimlik bağlantısında `(issuer, subject)` benzersizdir. Bağlantı veya aktiflik
değiştiğinde artan oturum sürümü kalıcı olarak yazılır. Üyelikler kullanıcı,
müşteri ve izin verilen role yabancı anahtarlarla bağlıdır. Sistem yöneticisi
bayrağı kullanıcı kaydındadır; sistem rolü müşteri üyeliği olarak atanamaz.

Yeni sağlayıcı yeni oturum nesneleri üretir. SQLite'a oturum nesnesi, token,
parola, rapor veya kimlik doğrulama sırrı yazılmaz. Yeniden açılışta eski
sağlayıcı kapalı dizine bağlıdır ve eski oturumları kullanamaz. Yeni sağlayıcı da
eski nesneleri tanımaz. Kimliğin başka hesaba atanması eski oturumun yetkilerini
taşımaz; önceki sürüm ve hesap bağı kontrolleri korunur.

Yetkiler açılışta doğrulanıp belleğe yüklenir. Depo tek sahibiyle çalıştığı için
bütün desteklenen değişiklikler aynı kilit ve metotlardan geçer; her müşteri
isteği güncel hesap ve üyelikleri kontrol eder. Merkezi kimlik deposu güvenilir
host içindir; müşteriye veya Docker işçisine dosya/SQL erişimi verilmez.

## Atomiklik ve hata davranışı

Her politika değişikliği `BEGIN IMMEDIATE` ve `synchronous=FULL` ile tek işlemde
kaydedilir. Başka istek bu sırada ara yetki göremez. SQL/commit hatası rollback
ve bellek durumunu geri alma ile sonuçlanır. Kalıcı sonucun belirsiz olabileceği
her yazma hatasında nesne kullanıma kapanır; kapatılıp doğrulanarak yeniden
açılmalıdır. Geçersiz yapılandırma doğrulama hataları yetki kazandırmaz.
SIGKILL sonrası yarım SQLite işlemi normal rollback journal kurtarmasından geçer;
bozuk veritabanı otomatik sıfırlanmaz veya yeni yöneticiyle doldurulmaz.

16 MiB veritabanı, 1.000 kullanıcı ve yalnızca iki demo müşterisi sınırı vardır.
Dosya ve yan dosya boyutları kontrol edilir; SQLite sayfa kotası uygulanır.

## Şema sürümü ve geçiş kararı

Bu ilk kalıcı şema sürüm 1'dir; `application_id`, `user_version`, tam şema,
rol kataloğu, `integrity_check` ve yabancı anahtar bütünlüğü açılışta doğrulanır.
Eklenmiş/değişmiş tablo veya tetikleyici, eksik sürüm metaverisi, bilinmeyen
sürüm ve bozulma güvenli hata oluşturur. Bellek içi eski yapı otomatik taşınmaz.

Henüz başka kalıcı sürüm bulunmadığından otomatik migration yoktur. Gelecekteki
geçiş; güvenilir yöneticiyle, depo kapalıyken alınmış doğrulanabilir yedek,
önceki sürümün açık doğrulaması, tek SQLite işlemi içinde şema/veri/sürüm değişimi
ve işlem sonrası bütünlük testleri gerektirmelidir. Başarısız geçiş eski sürümü
korumalıdır; bilinmeyen sürüm hiçbir zaman sessizce benimsenmemelidir.

## Kalan sınırlar

- Gerçek OIDC, parola/MFA ve dış erişim hizmeti yoktur.
- Kalıcı güvenlik denetim geçmişi yoktur; mevcut 1.000 olaylık bellek kaydı sürer.
- Veritabanı şifreli veya imzalı değildir; gerçek müşteri bilgisi koymayın.
- Yedekleme/geri yükleme ve üretim yönetici iş akışı henüz uygulanmamıştır.
- Süreç başına oturumlar, başlamış görevlerin iptal edilmemesi ve Docker
  koordinatörünün güvenilir host yetkileri önceki sınırlarla aynıdır.
- Müşteri rapor okumaları mevcut yazmasız `peek` yolunda kalır. Yetkili politika
  yönetimi ayrı kimlik deposuna yazar; müşteri rapor dosyalarını değiştirmez.

Testler: `python -B -m unittest discover -s tests -p test_offline_identity_store.py`.

## Bağımsız inceleme sonrası güçlendirmeler

Açılış tek bir okuma görüntüsünde tam `integrity_check` ve yabancı anahtar
kontrolü yapar. Kullanıcı ID, issuer/subject, müşteri ID ve üyelik anahtarlarında
tekrar varsa önbelleğe hiçbir satır yüklenmez. Benzersizlik indeksinin bozulduğu,
`quick_check` sonucunun hâlâ başarılı olduğu durum da reddedilir. Bozuk dosya
onarılmaz, birleştirilmez veya yeni veritabanıyla değiştirilmez.

Kök yolun bileşenleri, kilit ve veritabanının cihaz/inode/UID kimlikleri açık
tanıtıcılarla birlikte izlenir. Normal erişimlerde, yazma işlemi içinde ve commit
öncesi/sonrasında yollarla tekrar karşılaştırılır; izinler ve sabit bağlantı
sayısı yeniden denetlenir. Silinme, dosya/dizin değiştirme, rakip kilit sahibi
veya revizyon uyuşmazlığı depoyu kullanıma kapatır. Eski önbellekle tekrar deneme
yapılmaz; güvenilir yönetici incelemesi ve yeniden açılış gerekir.

`metadata.revision` her başarılı politika değişikliğinde monoton artar. Beklenen
revizyon `BEGIN IMMEDIATE` içinde koşullu güncellenir; eşleşmezse kullanıcı veya
üyelik yazılmaz. Revizyon artışı ve politika değişikliği aynı işlemde commit olur.
Dosya kontrolü ile SQL arasında kilit yolu değişse bile SQLite yazma kilidi ve
revizyon karşılaştırması eski önbelleğin sonraki sahibin değişikliğini ezmesini
engeller. Bu tasarım çok süreçli hizmeti etkinleştirmez.

Her sahip bağlantısında `recursive_triggers=ON` ayarlanıp doğrulanır. Böylece
`INSERT OR REPLACE` işleminin hesap ID veya benzersiz kimlik çakışmasıyla yaptığı
örtük silme de silme yasağına takılır. Şema DDL'si değişmediğinden mevcut geçerli
sürüm 1 dosyaları desteklenir; daha zayıf doğrulamaya geri dönüş yoktur.

Commit gerçekleşip ardından hata bildirilirse rollback'in gerçekleşmiş commit'i
geri aldığı varsayılmaz. Nesne kapalı kalır; yeniden açılış gerçek kalıcı sonucu
okur. Rollback hatası, gerçek `SQLITE_FULL` ve `SQLITE_BUSY` aynı güvenli kapanma
yolundadır. SIGKILL sonrasında işlem atomikliği SQLite'ın normal günlük kurtarma
mekanizmasına dayanır; bu, bozuk indeksi onarmak değildir.

Sınır: Dosya karşılaştırmaları birer gözlemdir. Aynı UID'deki kötü niyetli kodun
bir sistem çağrısının içinde yol değiştirmesine, doğrudan SQL yazmasına veya
koruma nesnelerini değiştirmesine karşı işletim sistemi izolasyonu sağlamaz.
Kilit dosyası silinirse başka süreç yeni dosyayı kilitleyebilir; eski sahibin
bunu fark edip kapanması ve revizyon kontrolü burada zorunlu ek korumalardır.
Üretimde dış süreçlerin doğrudan yazması yasaklanmalı ve tek sahip korunmalıdır.
