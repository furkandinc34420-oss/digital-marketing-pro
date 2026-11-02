# Bağımsız Docker supervisor — 5.5 çevrimdışı prototip

Bu araç güvenilir host yöneticisi içindir; müşteri API'si veya üretim servisi
kurulumu değildir. Coordinator tarafından başlatılmaz. Ayrı terminal/OS sürecinden
çalıştırılır; koordinatörün thread'i veya onun alt süreci değildir:

```bash
python examples/offline-seo/isolation/supervisor.py \
  --ownership-root /guvenilir-kalici-durum/docker-resources \
  --customer demo-musteri
```

Diğer kurgusal müşteri için `--customer diger-demo` ile ayrı çalıştırılır.
`--once` tek tarama yapar; insan incelemesi, geçici `busy` veya depo hatasında
çıkış kodu 2'dir. `busy`, kaynak kilidi değil kısa süreli metadata kilidi
çakışmasıdır; kalıcı review mandalı oluşturmaz ve sonraki taramada tekrar gözlenir.
Döngü bir saniye bekleyerek tekrar gözlem yapar. Depo kilidi/sağlık hatasında
hiçbir Docker mutasyonu yapılmaz; sabit hata mesajıyla gözlem tekrar denenir.
Gerçek zaman, yüksek erişilebilirlik veya supervisor'ın kendisinin otomatik
tekrar başlatılması garantisi yoktur. Sistem servisi/deployment eklenmedi.

## Yaşam sinyali ve güven sınırı

Coordinator, mevcut görev kabulünden sonra ve uzmanı çağırmadan önce aynı kaynak
kökündeki müşteri dizininde yürütmeye özel `execution-<id>.lock` dosyasını özel
izinlerle ve O_EXCL/O_NOFOLLOW ile oluşturur; flock EX kilidini yürütme boyunca
tutar. Görev, değişmez yürütme ID'si ve dosyanın device/inode bilgisi 5.4'ün aynı
`resources.sqlite3` deposuna kaydedilir. Kayıt commit edilmeden uzman başlatılmaz.
Yürütme ID'si tekrar kullanılmaz. Lock dosyaları otomatik silinmez/değiştirilmez.

Supervisor dosyayı no-follow açar, sahiplik/izin/tek bağlantı/tür ve kayıtlı inode'u
kontrol eder; yalnızca nonblocking EX kilidi alabildiğinde işlem yapar. Kilit
alınamıyorsa görev **active** kabul edilir; yaşı veya PID'si ne olursa olsun
konteynere dokunulmaz. SIGSTOP, takılmış ama canlı coordinator veya saatin/PID'nin
tekrar kullanılması yanlış ölüm kararı üretmez. Eksik/değişmiş lock güvenli ret ve
insan incelemesidir. Aynı yürütme kilidi diğer supervisor'ları da dışlar.

Bu protokol tek host, yerel güvenilir Linux dosya sistemi ve işbirlikçi güvenilir
host süreçleri varsayar. NFS/çok host/host reboot sonrası otomatik uzlaştırma
kanıtlanmamıştır. Fork ile lock FD miras alınırsa sağ kalan çocuk kilidi tutabilir:
bu yanlış öldürme yerine durdurmayı geciktirir. PID/fork erişim kontrolleri kalır;
uygulama düzeyinde çok süreçli kimlik iptali hâlâ yoktur. Aynı UID'deki kötü niyetli
host kodu, root veya Docker yöneticisi güven sınırının içindedir.

## Tek sahiplik kaydı, sürüm ve işlem sırası

Yeni sahiplik sistemi oluşturulmadı. 5.4 `resource_ownership`, altı eşleştirme alanı,
niyet ve not_sent/sent_unconfirmed/identified/removed geçişleri aynen kullanılır.
Aynı DB'ye `execution_leases` ve `supervisor_results` eklenir (şema sürümü 2).
Tam olarak doğrulanmış sürüm 1; bütünlük, alanlar ve benzersizlik kontrolünden
sonra tek SQLite transaction ile yükseltilir. Görev deposu veya bilinmeyen/bozuk
şema yükseltilmez. Kaynak kayıtları ve kanıtlar değiştirilmez. Eski yürütmelere
lease uydurulmaz: bunlar otomatik durdurulmaz. Eski 5.4 binary'si sürüm 2'yi
açamaz; yükseltmeden önce operatör güvenilir yedek almalıdır. Otomatik geri dönüş
veya bozuk veri onarımı yoktur. 64 MiB/10.000 kaynak ve 10.000 yürütme sınırı sürer.

Supervisor yalnızca seçilmiş müşterinin **konteyner** kayıtlarını işler:

1. `not_sent` veya `removed`: mutasyon yok. Lease sahibi aktifse mutasyon yok.
2. `identified`: tam ID üzerinden inspect edilir. `sent_unconfirmed`: attempt
   etiketi yalnızca arama ipucudur; tek sonuç bulunmalı ve tam ID, tür, müşteri,
   görev, yürütme, attempt ve proof mevcut 5.4 `verify` ile eşleşmelidir.
3. `stopping` kararı SQLite'a commit edilir. Hata/belirsiz commit varsa komut yok.
4. Önce `stop --time 2`, CLI için 5 saniye sınır; inspect hâlâ çalışıyor diyorsa
   `kill` (5 saniye) ve yeniden inspect. Running/Restarting boolean olmalıdır;
   restart veya belirsiz durum başarı sayılmaz. Timeout/kayıp cevapta CLI çıkışı
   yerine gerçek inspect esas alınır. Her native mutasyonda sahiplik yeniden doğrulanır.
5. Durma doğrulanınca yalnızca o tam ID `rm --force` ile kaldırılır. Sonrasında
   Docker listesiyle yokluğu doğrulanır; mevcut kaynak kaydı `removed` ve supervisor
   sonucu `removed` **aynı SQLite transaction/commit içinde** yazılır. Bu son kaldırma, daha önce gönderilmiş gecikmiş bir
   start komutunun aynı ID'yi yeniden çalıştırmasını da engeller.
6. Eksik/sahte kanıt, bilinmeyen kaynak, kayıp cevap, doğrulanamayan durma/silme:
   **review**. Ham stderr/HTML/token/proof değil yalnızca sabit durum kodu yazılır.
   Review kalıcı bir mandaldır: tekrar tarama otomatik müdahale denemez; güvenilir
   insan incelemesi gerekir. Bu aşamada mandal kaldıran API/onarım aracı yoktur.

SQLite ile Docker atomik değildir. Silme gerçekleşip commit kaybolursa yanlış
başarı üretilmez; kaynak kaydı identified/stopping veya review kalabilir. Depo
hatası nedeniyle review bile kaydedilemezse önceki kalıcı durum korunur, CLI hata
bildirir; hata kaydının kesin yazıldığı iddia edilmez. Hiç kaynak bulunmayan belirsiz
create, 'oluşmadı' sayılmaz. Sonradan gelen kaynak insan incelemesine kalır.

İmajlar otomatik silinmez. Eski/doğrudan `DockerLab` CLI görevleri kayıtlı lease
kullanmıyorsa otomatik sahiplenilmez. Tam artık kaynak uzlaştırması, kalıcı operatör
uyarıları, restart/reboot yönetimi ve imaj temizliği 5.6/5.8 kapsamındadır. Worker'a
host DB/lock/socket/mount verilmez; ağ, kullanıcı ve kaynak sınırları değişmez.
Supervisor oturum iptalini süreçler arası dağıtmaz ve SEO görevini başarıya çevirmez.
TaskStore'un mevcut interrupted kurtarması korunur; otomatik yeniden analiz yoktur.

## Kabul ve test sınırı

`tests/test_offline_supervisor.py` gerçek subprocess coordinator, iki kurgusal
müşteri ve ayrı oturumda başlatılmış supervisor süreçleri kullanır. Yavaş fixture
uzman yalnızca testte Python sleep/SIGTERM reddiyle gözlem süresi yaratır; üretim
runner'ında güvenlik bayrağı eklenmez. Test bir koordinatörü SIGKILL ile öldürür,
supervisor'ın yaşamını ve diğer müşterinin çalışan konteynerini koruduğunu,
kaldırmayı ve sonra ikinci müşterinin kesintisini doğrular. Temizlik sadece tam
5.4 kanıtı doğrulanmış test kaynaklarını hedefler. Süre yerel monotonic saatle
SIGKILL'den kalıcı ve Docker ile doğrulanmış kaldırmaya kadar ölçülür; SLA değildir.

Sahte etiket/ID, yanlış lease, PID'siz aktiflik kararı, bozuk Docker durumu,
kontrollü/zorunlu sonlandırma, daemon/timeout, SQLite commit/kayıp commit onayı,
sürüm 1 geçişi, tekrarlı tarama ve iki supervisor claim yarışları ayrıca sınanır.
Üretim seviyesinde stres/çok host/power loss sertifikası değildir.


## İlk geliştirme doğrulaması — bağımsız inceleme öncesi

- Yeni test dosyası: **26 başarılı, 0 atlanan, 0 başarısız**.
- Mevcut kaynak sahipliği, dispatch, iptal, oturum, erişim, öneri, koordinatör ve
  supervisor grubu: **197 başarılı, 0 atlanan, 0 başarısız**.
- Güncel tam paket: **1047 toplam, 1038 başarılı, 9 atlanan, 0 başarısız**.
- Yeni gerçek Docker testi atlanmadı. SIGKILL'den kalıcı/Docker doğrulanmış
  kaldırmaya kadar süre: bağımsız koşuda **2,945 s**, regresyon grubunda **3,057 s**,
  tam pakette **2,952 s**. Bu tek yerel ortamın ölçümüdür; SLA değildir.
- İnceleme sırasında timeout/review mandalı, ikinci supervisor'ın güncel sonucu
  yeniden okuması, bozuk v1 kaydın yazmadan reddi ve sonuç/yaşam döngüsü tutarlılığı
  ayrıca kontrol edildi. Mevcut dört SEO işlemi ve öneriler tam pakette korunur.

Bu sonuçlar bağımsız güvenlik incelemesine sunulacak **prototip** kanıtıdır;
üretim güvenlik onayı değildir. Commit/push/deployment yapılmadı.


## Eleştirel incelemede doğrulanan ve giderilen bulgular

İki orta ve bir düşük bulgu, düzeltmeden önce üç regresyon testinde gerçekten
başarısız sonuç üretilerek doğrulandı. Çapraz müşteri durdurma bulgusu doğrulanmadı.

- **Orta / geçici kilit çakışması:** orphan claim öncesindeki resources.lock
  meşguliyeti kalıcı review'a dönüşerek sonradan yetim işçinin gözetimini
  engelliyordu. Artık mutasyonsuz `busy` döner; kalıcı mandal yoktur.
- **Orta / review yazma yarışı:** durdurma hatasında execution claim önce
  bırakılıp sonra review yazılıyordu. İkinci supervisor bu aralıkta tekrar
  başlayabiliyordu. Review commit'i artık claim bırakılmadan yapılır.
- **Düşük / terminal kayıtların ayrılması:** kaynak removed ve supervisor removed
  iki ayrı commit idi. Aradaki SIGKILL removed/stopping bırakabiliyordu. Artık
  beklenen stopping kararı, kaynak ID'si ve mevcut sahiplik bağları kontrol edilip
  iki tablo aynı transaction ile güncellenir. Önceki deneysel kayıtları otomatik
  onarma veya dış Docker işlemini SQLite ile atomik yapma iddiası yoktur.

Sekiz ek regresyon; gerçek kernel metadata kilit çakışmasını, review sırasında
ikinci claim reddini, terminal rollback/kayıp commit onayını, üç SQLite sınırında
supervisor SIGKILL'ini, gerçek fork ile miras kalan kilidi, native Docker komutu
öncesi değişen kimlik kanıtını, birden fazla belirsiz create eşleşmesini ve remove
hatalarını kapsar. Son iki tablolu commit bir işlem olarak kalır veya ikisi de
kalıcıdır; physical Docker silmesi olmuşken SQLite rollback olursa hâlâ belirsizlik
vardır ve yanlış başarı yazılmaz.

İki müşterili gerçek Docker testine ayrıca supervisor'ın stopping commit'inden
sonra SIGKILL ile öldürülmesi ve yeni bağımsız monitor ile devam eklenmiştir.
Bu aşamada worker hâlâ çalışır; yeniden açılan monitor sahipliği tekrar doğrulayıp
durdurur/kaldırır. Sağlıklı ikinci müşterinin korunması da yeniden kontrol edilir.
SQLite sınırlarının üçlü SIGKILL testi ise Docker yanıtlarını taklit eder; gerçek
Docker entegrasyonunun yerine sunulmaz.

Açık test/üretim sınırları: tam host/daemon reboot ve güç kaybı, uzun süreli
çoklu supervisor yük testi, NFS/çok host kilit semantiği ve fiziksel disk arızası
kanıtlanmadı. Aynı UID/root/daemon yöneticisinin saldırganlığı bu prototipin güven
sınırının dışındadır. Fork çocuğunun tuttuğu kilit yanlış öldürmeye yol açmaz ama
çocuk bitene kadar orphan müdahalesini geciktirebilir. Servis restart politikası,
review çözüm aracı ve tam uzlaştırma/uyarı sistemi hâlâ ayrı işlerdir.


İnceleme sonrası yeniden çalıştırılan sonuçlar:

- Düzeltmeden önce üç bulgu için üç test başarısızdı; düzeltmeden sonra geçti.
- Güncel supervisor dosyası: **34 başarılı, 0 atlanan, 0 başarısız**.
- Güncel tam paket: **1055 toplam, 1046 başarılı, 9 atlanan, 0 başarısız**.
- Gerçek iki müşterili Docker/SIGKILL testi atlanmadı; supervisor'ın kendisi de
  stopping kaydından sonra öldürülüp yeni bağımsız süreçle devam ettirildi.
  Ölçüm: hedefli koşuda **3,137 s**, tam pakette **3,142 s** (SLA değil).
- Test sonrasında etiketli demo konteyneri veya imajı kalmadığı doğrulandı.
- README'nin önceden kalan yerel BOM farkı korundu; yalnızca sayılar güncellendi.

İnceleme kararı: bu düzeltmelerle **GitHub'a çevrimdışı prototip olarak kaydetmeye
hazır**. Üretim onayı değildir. Bu incelemede commit/push/PR/merge/deployment yoktur.


## 5.6 ile eklenen sınırlı operatör uzlaştırması

[5.6 uzlaştırma belgesi](RECONCILIATION.md) aynı kaynak deposunun doğrulanmış
sürüm 1/2'den transaction ile sürüm 3'e geçişini ve kalıcı son gözlem tablosunu
anlatır. Ayrı operatör komutu bilinen kaynakları inceler; varsayılan Docker
mutasyonu yapmaz. --apply ve gereken açık review tekrar seçimi, mevcut tam sahiplik
ve execution claim kontrollerini atlamaz. 5.5 otomatik review mandalı korunur;
Supervisor'a genel prune veya yeni otomatik imaj temizleme eklenmez. Eski
kaynak/yürütme kanıtları değişmez. 5.7 güvenlik olay geçmişi hâlâ tamamlanmadı.
