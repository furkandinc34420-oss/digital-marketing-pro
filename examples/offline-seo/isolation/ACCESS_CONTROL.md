# Çevrimdışı kimlik ve müşteri erişim prototipi

Bu sürüm ağ dinleyicisi, parola girişi, JWT veya gerçek kimlik doğrulama hizmeti
açmaz. Mevcut yerel CLI değişmez; CLI güvenilir yerel yöneticinin aracıdır ve
müşterilerin kimlik doğrulanmış giriş noktası olarak kullanılmamalıdır.

## Model ve roller

`access_control.py` içindeki `User`, `Tenant`, `Membership`, `Role` ve `Identity`
kavramları ayrıdır. Kimlik, güvenilir sağlayıcının issuer/subject çiftinden
kullanıcıya bağlanır. İstekten kullanıcı veya rol alınmaz.

| Rol | Açık üyelik bulunan müşteride yetki |
|---|---|
| Sistem yöneticisi (`User.system_admin`) | Görev oluşturma ve okuma; ayrıca sistem denetim kaydı |
| Müşteri yöneticisi (`Role.TENANT_ADMIN`) | Görev oluşturma, durum ve rapor okuma |
| Salt-okunur (`Role.READER`) | Durum ve rapor okuma |

Sistem yöneticisi de müşteri erişimi için açık üyeliğe ihtiyaç duyar. Sistem
rolü müşteri üyeliği olarak atanamaz. Aktif kullanıcı, aktif müşteri ve güncel
üyelik her çağrıda kontrol edilir. Üyelik kaldırma, kullanıcı/müşteri kapatma
ve rol değişikliği sonraki kabul edilen istekte geçerlidir. Mevcut görevi
iptal etmez. Rol değiştirmek geçmiş görevi otomatik yeniden çalıştırmaz.

## Güvenilir kurulum ve çağırma sınırı

`OfflineIdentityProvider(directory).issue(subject)` yalnızca güvenilir test kurulumuna aittir.
Bu metodu kullanıcı giriş API'sine bağlamak, birinin kullanıcı adını bilerek
onun adına oturum almasına yol açar; kesinlikle kimlik doğrulama değildir.
Opak oturum nesnesi yalnızca üretildiği sağlayıcı ve süreçte geçerlidir. Nesnenin
sahibi kurgusal hesabın test yetkisini kullanır. JSON kimlik, rol, `TrustedCaller`
ve rastgele nesneler oturum olarak kabul edilmez. Tokenları kaydetmeyin/paylaşmayın.

Güvenilir kurulum şu sırayı izler:

1. `AccessDirectory` ve bu dizine bağlı `OfflineIdentityProvider(directory)` oluşturur.
2. Kurgusal kullanıcıları, `demo-musteri`/`diger-demo` müşterilerini ve üyelikleri ekler.
3. Test kullanıcısı için `provider.issue(...)` çağırır.
4. `AccessService(provider, directory, Coordinator(...))` oluşturur.
5. Oturum nesnesiyle yalnızca `submit`, `status`, `report` çağırır.

`status` yalnızca görev kimliği ve durum döndürür. `report` yalnızca rapor
zarfını döndürür; yönetici hata ayrıntılarına veya politika yönetimine erişim
vermez. `submit` da yalnızca görev kimliği ve durum döndürür. Yetki reddinde
koordinatöre, dosyalara veya Docker'a erişilmez. Görev kimliği müşteri sınırını
aşmaz. İşlemler mevcut koordinatör ve Docker yolunu kullanır; korumasız analiz
geri dönüşü veya yeni web yazma yeteneği yoktur.

## Eşzamanlılık, denetim ve kalıcılık

İlk sürüm tek süreçte bellek içindedir. Politika kilidi bir isteğin sonuna kadar
tutulur; prototip çağrıları sırayla işlenir. Yetki kaldırma kilidi aldıktan sonraki
isteklere uygulanır. Bu, uzun Docker işinde diğer istekleri bekletebilir; dağıtık
servis veya yüksek trafik çözümü değildir. Koordinatörün süreçler arası müşteri
kilidi ve SQLite kayıtları değişmez. Yeni süreç kimlik/üyelikleri yeniden güvenilir
biçimde kurmalı; tüm demo oturumları yeniden oluşturulmalıdır.

Son 1.000 erişim olayı bellekte tutulur. Kayıtlar artan sıra, sabit işlem/sonuç
kodları ve takma kullanıcı referansı içerir; ham kimlik bilgisi, görev/müşteri
parametresi, rapor, dosya yolu veya ham hata içermez. Referans hash'i anonimlik
garantisi değildir. Denetim geçmişini yalnızca sistem yöneticisi servis üzerinden
okuyabilir. Yönetici yetki değişiklikleri ve oturum üretimi bu ilk sürümde kalıcı
olarak denetlenmez. Log yeniden başlatmada kaybolur; kurcalamaya dayanıklı değildir.
Bellek içi politika ve oturum sayıları güvenilir kurulumun sorumluluğundadır.

## Gerçek panel/API öncesindeki eksikler

`IdentityProvider.authenticate(credential)` gelecekte OIDC adaptörü için arayüzdür;
mevcut kod OIDC doğrulamaz. Gerçek adaptör güvenilir issuer, imza/algoritma,
audience, süre, nonce/replay ve ilgili iptal kontrollerini tamamlamadan güvenilir
hesap bağlantısı (`VerifiedAccount`) döndürmemelidir. Sağlayıcı istekten seçilmemeli; rol/üyelikler dış token
iddialarından doğrudan alınmamalıdır.

Kalıcı kimlik ve üyelik deposu, süreçler arası yetki güncellemesi, oturum süresi ve
kalıcı/dağıtık iptali, yönetici işlemleri için ayrı yetkilendirme/denetim, güvenli taşıma,
istek sınırları ve kalıcı güvenlik logları henüz yoktur. Dış erişime açmayın.
Python özel alanları veya klasör izinleri kötü niyetli aynı süreç/UID kodunu
engellemez. Host, politika yöneticisi, sağlayıcı ve koordinatör güvenilirdir;
Docker işçilerinin mevcut işletim sistemi izolasyonu ayrı sınırdır.

Testler: `python -B -m unittest discover -s tests -p test_offline_access.py`.
Sahte koordinatör uzmanı testleri yalnızca erişim akışını sınar; ayrıca iki
müşteriyle gerçek Docker analizi ve mevcut tam güvenlik paketi çalıştırılır.

## Güvenlik incelemesi sonrası kurallar

Oturumlar `VerifiedAccount` ile değişmez dahili `user_id`, doğrulanmış kimlik
bağlantısı ve bağlantı sürümüne bağlanır. Her istekte bu üç alan güncel hesapla
karşılaştırılır. Kimlik bağlantısının veya aktiflik durumunun değişmesi sürümü
artırır; kimlik eski değerine dönse dahi eski oturum geri gelmez. Eski subject
başka hesaba verilirse eski oturum o hesaba geçemez. Rol ve üyelik değişiklikleri
ise mevcut oturumla sonraki istekte yeniden kontrol edilir. Oturum oluşturma
bilinmeyen veya kapalı hesap için reddedilir. Gelecekteki OIDC adaptörü de
bağlantı sürümü ve dahili hesap bağını güvenilir dizinden kurmalıdır.

`status` ve `report` artık `Coordinator.peek` kullanır. Var olmayan dizin,
kilit veya veritabanı oluşturulmaz. Var olan güvenli dosyalar yalnızca okumayla
açılır; ortak müşteri kilidi alınır; en fazla 64 MiB SQLite görüntüsü RAM'e
kopyalanır. Disk SQLite'a bağlanılmaz: günlük kurtarma, yan dosya, şema geçişi
ve kayıt güncellemesi yapılmaz. Veritabanı sahipliği, bağlantı güvenliği ve rapor
şemaları doğrulanır. Mevcut journal/WAL/SHM varsa bakım gerektiği için okuma
reddedilir. Dosya erişim zamanları işletim sisteminin olağan okuma davranışına
tabidir; içerik, değiştirme zamanı ve görev kayıtları değişmez.

Kilit alınabildiği halde kayıtta `running` kalmışsa yalnızca görüntüleme için
`interrupted` ve eksik rapor üretilir. Bu sonuç veritabanına yazılmaz.
`AccessService.recover` açık kalıcı bakım yoludur: sistem yöneticisi ve ilgili
müşteride açık üyelik gerekir. Mevcut yerel CLI'nin kurtarma davranışı korunur.
Salt-okunur işlemlerin erişim denetim olayları bellekte tutulmaya devam eder;
hiçbir durum dosyasına yazılmaz.

Sağlayıcı, dizin ve servis oluşturuldukları PID'ye bağlıdır. PID değişiminde
kimlik, politika, oturum ve denetim işlemleri, miras alınan kilitlere girmeden
reddedilir. `fork` ile kopyalanan nesneler kullanılamaz. Yeni süreç kendi boş
dizin/sağlayıcısını kurmalıdır; eski oturumlar kabul edilmez. Bu kontrol gerçek
çok süreç desteği değildir; bunun için ortak kimlik, yetki ve iptal deposu gerekir.

Salt-okunur RAM görüntüsü, Python SQLite modülünün `Connection.deserialize`
yeteneğini gerektirir. Bu yetenek bulunmayan kurulumlarda güvenliği gevşeten disk
bağlantısına dönülmez; okuma hata vererek kapanır. Mevcut bulut ortamında özellik
vardır ve gerçek veritabanı testleriyle doğrulanır.
