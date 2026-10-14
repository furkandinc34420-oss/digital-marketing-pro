# Çevrimdışı SEO deneme ortamı

Tamamen kurgusal **Demo Müşteri** ve izolasyon kontrolü için ikinci bir kurgusal
müşteri içerir. JSON sayfaları WordPress benzeri örneklerdir; WordPress dışa
aktarımı, bağlantısı veya canlı ölçüm değildir. `.invalid` adresleri yalnızca
etikettir; hiçbir adrese istek gönderilmez. API veya yeni paket gerekmez.

Python 3.10+ ile depo kökünden çalıştırın. Betikler bu deponun `scripts/` klasöründedir:

```bash
python -B scripts/offline_seo.py --root examples/offline-seo/customers --customer demo-musteri
PYTHONDONTWRITEBYTECODE=1 python -B -m unittest discover -s tests -p test_offline_seo.py -v
```

Çıktı standart çıktıya JSON olarak gelir. `-B`, Python önbellek dosyalarının
oluşturulmasını engeller. Araç giriş dosyalarını değiştirmez,
değişiklik uygulamaz ve onay vermez. İsterseniz çıktıyı müşteri alanı dışındaki
yerel bir inceleme dosyasına yönlendirebilirsiniz. `--apply` seçeneği yoktur.

Mevcut `tech-seo-auditor.py` içindeki saf `check_meta` HTML analizi yeniden
kullanılır. Başlık, açıklama, canonical, viewport ve robots bulguları üretilir.
Puan yalnızca meta etiketi kurallarına dayanır; sıralama, HTTP erişimi, indekslenme
veya Core Web Vitals ölçümü değildir. Taslak sayfanın `noindex` bulgusu gösterilir;
kaldırılması otomatik önerilmez. Öneri metinleri kurgusal veri dosyasından gelir,
yapay zekâ veya ücretli API ile üretilmez. Metinsiz öneriler `needs_authoring`,
metinli öneriler `awaiting_human_review` durumundadır. Hepsi uygulanmamıştır.

## Müşteri sınırı

Her çağrı açık müşteri kimliğine bağlanır. Genel aktif marka ayarını kullanmaz.
Dosya adı sabittir; yol kaçışı, sembolik bağlantı, birden fazla sabit bağlantısı
olan dosyalar, farklı müşteri kimliği, farklı site, gerçek alan adı ve yinelenen
sayfa kimliği reddedilir. URL içinde port (0, boş veya 443 dahil), kullanıcı
bilgisi (boş kullanıcı dahil), boşluk ve hatalı alan adı kabul edilmez.
Öneriler müşteri, sayfa, önceki değer ve giriş SHA-256 özetleri taşır.
`source_sha256` ham `pages.json` baytlarının özetidir. `source_hashes`, hem
`pages.json` hem `profile.json` ham baytlarının özetlerini içerir. BOM veya satır
sonu değişirse özet de değişir. Profil değişiklikleri de öneriye bağlanır.
Bu özet bir onay veya değişiklik uygulama yetkisi değildir.

Bu **uygulama seviyesinde veri karışmasını engelleyen sınırdır**. Aynı işletim
sistemi kullanıcısıyla çalışan kötü niyetli kodu, dosya değiştirmeyi veya başka
bir müşteriyle yeni çağrı başlatmayı engellemez. Kök klasör ve müşteri seçimi
güvenilir çağıranın sorumluluğundadır. Gerçek müşterilere geçmeden ayrı ortamlar,
dosya izinleri, güvenilir kimlik/yetki kontrolü ve ajanın veremediği insan onayı
gereklidir.

Dosyalar POSIX dizin tanıtıcılarına göre `O_NOFOLLOW` ile açılır; sembolik bağlantı
kontrolünden sonra farklı bir yola geçme boşluğu kaldırılmıştır. Okunan dosya
tanıtıcısı sabit kalır; okuma sırasında gözlenen boyut/zaman değişimleri ve dosya
adının başka dosyayla değiştirilmesi reddedilir. Gerekli POSIX özellikleri yoksa
güvensiz yedek yöntem kullanılmaz; işlem hata verir. Bu sürüm Linux/POSIX içindir.
Kök dizin ve üst dizinler güvenilir çağıranın denetiminde olmalıdır. Bu kontroller
kötü niyetli aynı kullanıcıya karşı dosya kilidi veya OS izolasyonu sağlamaz;
iki giriş dosyasının eşzamanlı, işlemsel bir anlık görüntüsü garanti edilmez.

## Girdi sınırları

- Her JSON dosyası en fazla 2 MiB (2.097.152 bayt).
- Bir müşteri girdisi 1–100 sayfa.
- Her sayfanın HTML metni en fazla 100.000 Unicode karakteri.

Dosya boyutu okumadan önce kontrol edilir, ayrıca okuma en fazla sınır + 1 bayt
ile kısıtlanır. Sayfa ve HTML sınırları analizden önce kontrol edilir. Limit aşımı
hata üretir; kısmi öneri çıktısı verilmez. Bunlar küçük demo içindir; büyük site
taraması desteği değildir.

## Sonraki aşama

Önce gerçek müşteri izolasyonu için ayrı çalışma ortamı ve yetkilendirme kurun.
Ardından yalnızca test WordPress ortamında salt okunur bağlantı ekleyin.
Canlı yazma, onay hizmeti, GEO, Ads ve otomatik raporlama bu örnekte yoktur.

## Tam test paketini harici ağ olmadan çalıştırma

Mevcut testlerden bazıları yalnızca makine içindeki sahte HTTP sunucularını
kullanır. `tests/offline_guard` yüklendiği CPython sürecinde seçili denetim
olaylarını kontrol eder: `socket.getaddrinfo`, `gethostbyname`, `gethostbyaddr`,
`socket.connect` ve UDP `socket.sendto`. Harici DNS ve bu yollarla yapılan harici
bağlantı/gönderimler reddedilir; yalnızca makine içi loopback testlerine izin
verilir. Yerel TCP/UDP ve harici işlem reddi testlerle doğrulanır.

Bu yardımcı bütün ağ yollarını kapatmaz: `sendmsg`, yerel kod/uzantılar, Python
dışı alt programlar veya yardımcı yüklenmeden başlatılan süreçler kapsam dışıdır.
Dosya yazma yetkilerini de kontrol etmez. `PYTHONPATH` mirası güvenilir Python
alt süreçlerinde yardımcıyı yükler; `-I`/`-S` veya değiştirilmiş ortam bunu devre
dışı bırakabilir. Kötü niyetli koda karşı OS ağ izolasyonu değildir. Bu demoda
harici hizmet kullanan bir yürütme yolu bulunmaz.

Depo kökünden, mevcut Python ortamında:

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH="$PWD/tests/offline_guard" python -B tests/run_all.py
```

Mevcut içerik puanlama testlerinin lite paketleri ve NLTK verileri gerekir;
bulut ortamında bunlar önceki kurulumdan hazırdır. Bu görevde paket kurulmadı.
Gerekirse mevcut ortamı etkinleştirin ve mevcut dil verisi konumunu belirtin:

```bash
source /workspace/dmp-venv/bin/activate
export NLTK_DATA=/workspace/dmp-nltk-data
```

13 demo test yöntemi genişletilmiş alt durumlarla URL kenar durumlarını, dosya
boyut/sayfa/HTML sınırlarını, ham kaynak özetlerini, dosya değiştirme yarışını,
sabit bağlantıları ve ağ yardımcısını sınar. Salt okunurluk testi müşteri kökünü,
ikinci müşteriyi, JSON dışı dosyaları ve geçici alanın kardeş dosyalarını karşılaştırır;
analizin `os.open`, `io.open` ve `open` çağrılarında yazmayı test sırasında engeller;
bu engelin bir yazma denemesinde çalıştığını da doğrular.
Bu, tüm makinede herhangi bir kodun dosya yazamayacağı garantisi değildir.
