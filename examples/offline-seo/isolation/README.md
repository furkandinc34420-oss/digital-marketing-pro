# Müşteri başına izole çevrimdışı test işçisi

**Script location:** Mevcut SEO araçları deponun `scripts/` klasöründedir.
Bu klasördeki `runner.py` güvenilir yerel denetleyici, `worker.py` yalnızca
konteyner içinde çalışan işçidir. Yeni paket, ücretli hizmet veya WordPress
bağlantısı yoktur.

## Uygulanan gerçek sınırlar

İki kurgusal müşteri (`demo-musteri`, `diger-demo`) için ayrı yerel imajlar
oluşturulur. Her imaj yalnızca o müşterinin örnek verisini, mevcut SEO kodunu,
işçiyi, mevcut Python standart kütüphanesini ve gerekli sistem kütüphanelerini
içerir. `site-packages`, host ev dizinleri, anahtarlar ve host ayarları kopyalanmaz.

Her iş ayrı konteynerde çalışır:

- `network=none`: yalnızca loopback; yayımlanan port yok.
- Host klasörü, Docker soketi veya başka müşteri alanı bağlanmaz.
- Kök dosya sistemi ve müşteri girdileri salt okunur.
- `/output` ve `/tmp` işçiye özel, ayrı ayrı 8 MiB geçici alanlardır.
- UID/GID `65534:65534`, yetenekler kapalı, `no-new-privileges` açık.
- Host PID alanı ve üst sürecin ortam değişkenleri paylaşılmaz.
- Bellek 256 MiB, süreç sayısı 32, CPU kullanımı 0,5 CPU ile sınırlı.
- Swap dahil bellek üst sınırı 256 MiB; özel `/dev/shm` alanı 8 MiB.
- Docker günlük sürücüsü kapalıdır; komut çıktısı denetleyicide sınırlandırılır.

Mount/PID/ağ ad alanları ve Linux izinleri, uygulama müşteri kimliği kontrolüne
ek bir işletim sistemi sınırı sağlar. Çapraz dosya açma/yazma ve sembolik bağlantı
denemeleri uygulama kontrollerini çağırmadan işçinin içinden yapılır. Kendi çıktı
alanına yazmak mümkündür; bütün dosya yazmalarını engellediğimizi iddia etmiyoruz.
Mevcut SEO analizi yeniden kullanılır, canlı değişiklik uygulanmaz.

## Bu Codex ortamındaki gözlem

Doğrudan `bwrap`/`unshare` kullanıcı ad alanı kurulumu UID eşleme dosyaları salt
okunur olduğu için engellendi. Landlock ABI sorgusu da desteklenmedi. Bunları
başarılı izolasyon olarak sunmuyoruz. Yerel Docker daemon üzerinden yukarıdaki
konteyner sınırları gerçek süreçlerle kurulup doğrulandı.

İnternetten imaj çekilmez, build veya paket kurulumu yapılmaz. Mevcut yerel Python
çalışma zamanından tar arşivleri oluşturulup Docker'a aktarılır; `--pull=never`
kullanılır. Yalnızca `unix:///var/run/docker.sock` adresine bağlanılır; uzak Docker
bağlamları ve kullanıcının Docker kimlik bilgisi ayarları kullanılmaz.

## Çalıştırma

Depo kökünden, mevcut Python ortamı ve erişilebilir yerel Docker daemon ile:

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=examples/offline-seo/isolation python -B -m runner
PYTHONDONTWRITEBYTECODE=1 python -B -m unittest discover -s tests -p test_offline_isolation.py -v
```

İlk komut iki müşterinin analiz ve erişim denemelerini JSON olarak gösterir.
Her iş bitince konteyner, oturum bitince yalnızca bu çalışmanın imajları kaldırılır.
Başka kaynaklar silinmez, `prune` kullanılmaz. Temizlik hatası gizlenmez. Normal
tamamlanmada çalışan müşteri servisi kalmaz. Zorla sonlandırma veya daemon arızası
`dmp-offline-isolation` etiketli artık kaynak bırakabilir; bunlar ayrıca incelenmelidir.

Docker/Linux yoksa denetleyici hata kodu 3 ile durur; korumasız geri dönüş yoktur.
Entegrasyon testleri bu durumda açık gerekçeyle atlanır. Atlanma başarı değildir.
Hazırlama/işçi hataları test hatasıdır. Tam paket için üst README'deki ağ yardımcılı
komut kullanılabilir. Yardımcı Docker ikilisini izlemez; denetleyici sabit yerel
soketi kullanır, işçilerin ağı ayrıca işletim sistemi düzeyinde kapalıdır.

## Güven sınırı ve kalan riskler

Ana Codex süreci ve denetleyici iki müşterinin kaynaklarına ve güçlü Docker
yönetici yetkisine sahiptir. Müşteriye denetleyici veya Docker soketi verilmemeli;
bu kod internete açık bir servis yapılmamalıdır. Yalnızca bu başlatma yolundaki
işçi korunur; ana süreçte doğrudan çalıştırılan analiz konteynerde değildir.
Kullanıcı kimliği doğrulaması ve müşteri bazında yetkilendirme servisi henüz yoktur.

Konteynerler çekirdeği paylaşır, sanal makine değildir. Çekirdek/Docker açıkları,
kötü niyetli host yöneticisi veya yan kanallara karşı garanti yoktur. Mevcut Python
çalışma zamanı güvenilir kabul edilir; bağımsız imzalı dağıtım imajı hazırlanmadı.
Bu bir üretim güvenliği sertifikası değildir.

## Testler

Sekiz entegrasyon testi gerçek SEO çıktısını, iki yönde çapraz okuma/yazma ve
sembolik bağlantı reddini, girdilerin korunmasını, özel çıktı alanını, host dosya
ve Docker soketinin görünmemesini, ortam değişkeni ayrımını, ağ arayüzlerini,
UID/yetenekleri, kaynak ayarlarını ve korumasız geri dönüş olmamasını sınar.

## İmaj hazırlama ve kaynak sınırları

Müşteri girdileri, mevcut SEO koduyla ortak olan güvenli dosya okuyucusundan
geçirilir. Bütün göreli dizin bileşenleri ve dosya sembolik bağlantıları reddedilir;
birden fazla sabit bağlantısı olan dosyalar kabul edilmez. Her dosya en fazla
2 MiB okunur. İki müşterinin de kimliği, sayfa kimlikleri, kurgu veri işaretleri,
site alanları ve sayfa/HTML sınırları herhangi bir imaj oluşturulmadan doğrulanır.
Arşive aynı doğrulanmış ham baytlar yazılır; kaynak dosya tekrar açılmaz. Proje
kodu da bağlantı takip etmeyen aynı okuyucuyla alınır. Güvenilir Python/sistem
kütüphanelerinin mevcut sistem takma yolları ise bir kez çözümlenir ve sınırlı
dosya tanıtıcısından okunur; bu çalışma zamanının güvenilirliği varsayımı sürer.

- Müşteri dosyası: 2 MiB; sayfa sayısı: 100; HTML: sayfa başına 100.000 karakter.
- Çalışma zamanı/kod dosyası: 64 MiB.
- Arşiv: müşteri başına başlıklar dahil en fazla 128 MiB; iki müşteride en fazla
  256 MiB. Yazmadan önce hesaplanır ve yazım sırasında tekrar sınırlandırılır.
- Docker ve kütüphane sorgu komutları: stdout ve stderr birlikte en fazla 4 MiB.
- Docker komutu: 45 saniye; kütüphane sorgusu: 10 saniye.

Çıktı veya süre sınırı aşılınca komutun yerel süreç grubu durdurulur. Bu işlem
Docker daemon içindeki işi otomatik durdurmaz; konteyner/imaj temizliği ayrıca
yapılır. Arşiv ve çıktı sınırları bütün host için bir bellek/disk kotası değildir.

## Başlatma kapısı ve hata sonrası temizlik

İşçi çalıştırılmadan önce Docker inspect yanıtı zorunlu denetlenir. Beklenmeyen
imaj/müşteri/komut, ortam değişkeni, host bağlantısı, aygıt, ağ, kullanıcı, yetenek,
ad alanı, geçici alan veya kaynak ayarı varsa işçi başlatılmaz. Eksik veya bozuk
ayar da hatadır. Testler başlatma komutunun bu durumlarda çağrılmadığını kontrol eder.

Oluşturma yanıtı kaybolursa kaynaklar yalnızca oturumun tam ve rastgele etiketiyle
aranır. Silmeden önce kaynak etiketi yeniden doğrulanır. Temizlik iki geçiş dener;
bir kaynağın zaman aşımı diğer kaynakların temizliğini durdurmaz. Silme başarılı
olup yanıt kaybolduğunda sonraki sorguyla uzlaştırılır. Silinemeyen kimlikler,
oturum etiketi ve ilk hata raporlanır; yabancı etiketteki kaynak silinmez.

Daemon erişilemezse, süreç zorla öldürülürse veya yanıtı belirsiz bir oluşturma
gecikerek tamamlanırsa mutlak temizlik garanti edilemez. Kaynak henüz görünmüyorsa
bile "oluşturma doğrulanamadı, gecikmiş kaynak çıkabilir" hatası verilir; temiz
tamamlandı denmez. Bu denetleyici örneği sıralı işler içindir; aynı örnek üzerinde
eşzamanlı iş başlatma desteklenmez.

Docker soketi normal dosya olarak okunmaz. Yokluk bilgisi ve gerçek AF_UNIX
bağlantı denemesinin sonucu ayrı kaydedilir. Erişilebilir sahte Unix soketiyle
regresyon testi yapılır; bağlantı başarılıysa test bunu erişim yok diye sunmaz.

## Genişletilmiş güvenlik testleri

Sekiz gerçek Docker testi yanında Docker gerektirmeyen 15 güvenlik testi vardır.
Bağlantı/sabit bağlantı reddi, müşteri kimliği, değişmeyen anlık görüntü, dosya ve
arşiv bütçeleri, iki çıktı akışının sınırı, komut zaman aşımı, gerçek Unix soketi,
kayıp oluşturma/silme yanıtları, kısmi temizlik hataları, sahiplik kontrolü ve
güvensiz ayarda başlatmama senaryoları sınanır. Bu 15 test Docker yokken de çalışır.
Arıza testleri kontrollü sahte Docker yanıtları kullanır; gerçek izolasyonun
yerine sayılmaz. Docker ayar kontrolleri yıkıcı CPU/OOM stres testi değildir.
