# Çevrimdışı oturum güvenliği — gerçek OIDC değildir

Bu prototip HTTP sunucusu, parola girişi, JWT, OIDC istemcisi veya dış bağlantı
oluşturmaz. `OfflineIdentityProvider.issue(subject)` yalnızca güvenilir kurgusal
test kurulumu içindir. Kullanıcı adı/e-posta/subject bilmek kimlik kanıtı değildir;
bu metot müşteri giriş API'sine açılmamalıdır. Yerel CLI güvenilir yönetici aracı
olmaya devam eder; kimlik doğrulanmış müşteri arayüzü değildir.

## Uygulanan oturum kuralları

- Mevcut `PersistentAccessDirectory` kullanıcı, müşteri, üyelik ve rollerini kullanır.
  Yeni SQL tablosu veya şema geçişi yoktur; mevcut sürüm 1 korunur.
- Oturum değişmez dahili `user_id`, issuer/subject bağlantısı ve hesap nesliyle
  bağlıdır. Her doğrulamada aktif hesap, bağlantı, nesil ve depo sağlığı kontrol
  edilir. Bağlantıyı değiştirip geri almak veya hesabı kapatıp açmak eski oturumu
  geri getirmez. Kimlikten otomatik hesap oluşturulmaz veya e-posta birleştirilmez.
- Kimlik doğrulama bir hesabı belirler; `AccessService` ayrıca her işlemde aktif
  müşteriyi, açık üyeliği ve rolü kontrol eder. Oturum müşteri/rol iddiası taşımaz.
- Varsayılan mutlak süre 900 saniyedir. Güvenilir kurulum 1–3600 saniye seçebilir.
  Kullanım süreyi uzatmaz; son zamanın tam eşitliğinde erişim reddedilir.
- Varsayılan toplam sınır sağlayıcı başına 1000 oturumdur; güvenilir kurulum
  1–10000 seçebilir. Doluysa aktif oturumlar atılmaz, yeni üretim reddedilir.
  Yeni üretimde süresi geçmiş veya geçersiz hesap bağlı oturumlar temizlenir.
- `revoke(token)` tek oturumu, `revoke_all(user_id)` bu sağlayıcıdaki hesabın tüm
  oturumlarını iptal eder. Bunlar güvenilir yerel yönetim işlemleridir; müşteri
  veya parola sıfırlama uç noktası değildir. Başka kullanıcının oturumları etkilenmez.
- Sistem duvar saati yerine monoton saat kullanılır. Enjekte edilen test saati
  güvenilir host girdisidir. Saat hatası/geriye gidiş/sonlu olmayan değer, tüm
  oturumları siler ve sağlayıcıyı kalıcı olarak kapatır. Yeni sağlayıcı gerekir.
- Belirteçler serileştirme ile taşınamayan opak nesne yetenekleridir; yeniden
  oluşturulan nesne geçerli olmaz. Sahte JSON, string,
  kullanıcı/rol bilgisi veya başka sağlayıcının belirteci kabul edilmez. Aktarım,
  çerez ve tarayıcı depolaması bu prototipte yoktur.
- Oturumlar ve iptal kayıtları SQLite'a yazılmaz. Yeniden başlatmada oturumlar
  kaybolur; kalıcı kullanıcı/üyelikler kalır. Nesnelerin PID kontrolü, `fork`
  sonrası kilit alınmadan erişimi reddeder. Çok süreçli kullanım desteklenmez.
- SQLite hatası, kilit kaybı veya revizyon uyuşmazlığında mevcut depo kapanma
  kuralları geçerlidir. Yeni oturum üretimi ve doğrudan kimlik doğrulama da reddedilir.
- Görev başlangıçta yetkili kabul edilmişse süre dolması/iptal işi geri almaz.
  Ancak servis, cevabı vermeden önce oturumu ve yetkiyi tekrar kontrol eder;
  iptal edilmiş/süresi dolmuş oturuma başarılı cevap veya rapor verilmez. Kalıcı
  görev bitmiş olabilir; yeni yetkili oturum durumu sorgular. Otomatik tekrar yoktur.
- Öneri hesaplaması ortak politika kilidi dışında kalır; sonuç öncesi yeniden
  kontrol korunur. Diğer istekler hâlâ seri prototip davranışı kullanır. İptal,
  doğrulama kontrolünden önce gerçekleşmişse reddedilir; verilmiş cevap geri alınamaz.
- Denetim olaylarında yalnızca işlem/sonuç kodu ve hesap referansı vardır.
  Token, parola, kaynak rapor, ham kimlik veya SQLite hatası loglanmaz.

## Gelecekteki OIDC sınırı — yalnızca tasarım

`IdentityProvider.authenticate(credential) -> VerifiedAccount` güvenilir sağlayıcı
arayüzüdür. Bugün tek uygulama çevrimdışı fixture sağlayıcısıdır. Gerçek adaptör,
aşağıdaki kontroller tamamlanmadan `VerifiedAccount` veya yerel oturum üretemez.
İstek gövdesinden bu tipte nesne oluşturmak doğrulama sayılmaz.

| Kontrol | Gelecekte uygulanacağı güvenilir bileşen |
|---|---|
| issuer | Sunucuda sabit izinli sağlayıcı ayarı; doğrulanmış id token `iss` ile tam eşleşme. İstek issuer'ı sağlayıcı seçemez. |
| audience ve authorized party | OIDC doğrulayıcıda sabit client ID ile `aud`, çoklu audience durumunda `azp` kontrolü. |
| imza ve algoritma | Olgun OIDC kütüphanesi; sabit algoritma izin listesi, `none` ve algoritma karışıklığını reddetme. |
| exp, iat, nbf | OIDC doğrulayıcıda güvenilir UTC saati ve sınırlı saat toleransı; sonra ayrı yerel mutlak oturum süresi. |
| state, nonce, PKCE | Giriş işlemi deposunda kısa ömürlü, tek kullanımlık, tarayıcıya bağlı state/nonce ve PKCE S256; callback'te tüketme ve tekrar reddi. |
| anahtar rotasyonu | Yalnızca sabit sağlayıcının TLS doğrulanmış discovery/JWKS kaynağı; sınırlı süre/boyutlu önbellek ve yenileme. Bilinmeyen `kid` için sınırlı yenileme, doğrulanamazsa kapalı erişim. Token içindeki `jku`/`x5u` takip edilmez. |
| hesap bağı | Doğrulanmış `(issuer, sub)` için mevcut, açıkça yönetilen dahili hesap eşlemesi. E-posta veya görünen isimle otomatik bağlama yoktur. |
| müşteri yetkisi | Yerel erişim katmanı; dış token rol/tenant alanlarını tek başına yetki saymaz. |

OAuth access token'ı doğrulanmış OIDC kimliği yerine kullanılmamalıdır. Yönlendirme
adresleri, güvenli çerezler, CSRF, oturum sabitleme önleme, çıkış/iptal, hız sınırı,
TLS ve güvenli callback hata yönetimi gerçek entegrasyon aşamasında gerekir.
Bu kontrollerin hiçbiri burada uygulanmış gerçek OIDC gibi sunulmaz. Şimdiki ASCII
fixture subject kısıtlarını gerçek sağlayıcı için sessizce gevşetmek yerine açık
kimlik eşleme ve şema uyumluluğu tasarlanmalıdır.

## Üretim öncesi kalan sınırlar

Sağlayıcı ve politika yöneticisi güvenilir host kodudur; aynı süreç/UID saldırganına
karşı sandbox değildir. Tek aktif sağlayıcı/tek süreç kullanılmalıdır. İptal
`revoke_all` ile sağlayıcıya özeldir; dağıtık iptal veya kalıcı oturum deposu yoktur.
Boşta kalma süresi, refresh token, gerçek login/logout/SSO, MFA, OIDC imza kontrolü,
anahtar deposu ve kalıcı denetim geçmişi yoktur. Dış erişime açmayın.

Sonraki güvenlik adımı, gerçek sağlayıcıya bağlanmadan önce olgun bir OIDC
kütüphanesi için callback/nonce/state/PKCE ve rotasyon hata sözleşmesini yerel
olumlu/olumsuz test vektörleriyle bağımsız incelemeye hazırlamaktır. Sahte imzalı
JWT veya sabit üretim anahtarı eklemek bunun yerine geçmez.

## Sonraki geliştirme: iptal kontrol noktaları

Başlamış görevin yalnızca cevap vermemesine ilişkin önceki sınır, desteklenen
Docker ve işbirlikçi görevlerde [görev iptali](CANCELLATION.md) ile genişletildi.
Üyelik/hesap değişiklikleri artık görevin bitmesini beklemez. İşbirliği yapmayan
adaptör, başka süreçteki veya güvenilir CLI görevi için zorla durdurma garantisi
verilmez. Gerçek API/OIDC/yan etki güvenlik sınırları değişmez.
