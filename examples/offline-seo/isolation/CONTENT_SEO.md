# Anahtar Kelime ve İçerik SEO Analiz Ajanı

Bu ikinci uzman yeteneği yalnızca kurgusal, yerel HTML örneklerini okur. Anahtar
kelime aracı, arama motoru, Google hesabı, WordPress veya yapay zekâ API'sine
bağlanmaz. Yeni bağımlılığı yoktur. Baş Koordinatör işlem adı: `seo.content`.
Mevcut teknik SEO işlemi `seo.analyze` olarak çalışmaya devam eder.

## Kullanım

Güvenilir yerel yönetici, depo dışında özel bir durum dizini kullanmalıdır:

```bash
PYTHONDONTWRITEBYTECODE=1 python -B examples/offline-seo/isolation/coordinator.py \
  --state-root /workspace/dmp-coordinator-state --customer demo-musteri \
  --task-id content-001 --operation seo.content
```

Diğer kurgusal müşteri `diger-demo` için aynı görev kimliği kullanılabilir; görev
kayıtları ayrı SQLite dosyalarındadır. Aynı müşteride teknik SEO ve içerik SEO için
farklı görev kimlikleri kullanın. İşlem değişirse veya örnek kaynak dosya değişirse
aynı görev kimliği çakışma hatası verir. Önceki aşamadaki örnek dosyalara hedef
kelime alanları eklendiğinden eski görevleri yeniden istemek de çakışabilir;
eski sonucu `--action status` ile inceleyin, yeni analiz için yeni kimlik kullanın.

## Ne ölçüyor?

Her sayfanın `primary_keyword` alanı kurgusal hedef ifadedir. Bu alan bir arama
motorundan gelmez, arama hacmi verisi değildir. En fazla 120 karakter ve 12 kelime
kabul edilir. Alan yoksa hedef kelime uydurulmaz; belirlenmesi önerilir.

- Hedef ifadenin title, H1 ve ilk 100 gövde kelimesinde bulunması.
- Gövdedeki tam kelime dizisi eşleşme sayısı; ek almış biçimler aynı sayılmaz.
- Gövde kelime sayısı ve en sık kullanılan sekiz terim.
- Başlıktaki terimlerin gövdede bulunma oranı ve eksik terimler.
- Eksik başlık, eksik hedef, girişte açıklanmayan konu ve az açıklanmış içerik
  için Türkçe, sayfaya bağlı öneriler.

Örneğin “kurgusal SEO testi” hedefi sayfa gövdesinde yoksa rapor sıfır eşleşmeyi
kanıt olarak gösterir ve konu uygunsa açıklayıcı bir bölüm eklenmesini önerir.
Her öneride müşteri/sayfa kimliği, URL, gözlenen kanıt ve ham kaynak dosyalarının
SHA-256 özetleri bulunur. Öneriler `awaiting_human_review`, `applied=false` ve
`human_approval_required=true` olarak döner. Uygulama yolu yoktur.

## Ölçümün sınırları

Bu, sözcüklere dayalı basit bir incelemedir. Başlık–gövde örtüşmesi anlam uyumunu
kanıtlamaz. Türkçe büyük/küçük harf ve Unicode kelimeler desteklenir; kök bulma,
eş anlamlı kelime analizi, arama niyeti veya anlamsal yapay zekâ değerlendirmesi
yapılmaz. H1 metni gövde sayımına dahildir. Sabit bir ideal anahtar kelime yoğunluğu,
sıralama puanı veya “ideal kelime sayısı” iddiası yoktur. 50 kelimeden kısa sayfalara
verilen öneri yalnızca editörün açıklama ihtiyacını gözden geçirmesi içindir.

HTML çalıştırılmaz; script/style/template/noscript ve görsel olarak gizli işaretlenen
metinler sayılmaz. Tarayıcı oluşturma, harici CSS veya JavaScript değerlendirmesi
yoktur; çıkarılan gövde metni gerçek tarayıcı görünümünün garantisi değildir.
Noindex işareti kanıt olarak raporlanır; kaldırılması veya sayfanın yayımlanması
önerilmez. Arama hacmi, rekabet, Google sıralaması ve trafik ölçülmez.

## Güvenlik

Mevcut güvenli dosya okuyucu, müşteri ve .invalid URL kontrolleri, sayfa/HTML
sınırları ve kaynak özetleri yeniden kullanılır. Koordinatörün özetlediği aynı
kaynak baytları Docker'a paketlenir. Yeni Python dosyası güvenli okuyucuyla imaja
alınır. Her iş yalnızca kendi müşteri verisini içeren imajda, ağsız, salt okunur,
root olmayan işçiyle çalışır. Başlatma öncesi aynı güvenlik doğrulaması, kaynak
sınırları ve oturum temizliği uygulanır. Korumalı olmayan geri dönüş yoktur.

`TrustedCaller` hâlâ güvenilir yerel programın bağlamıdır; gerçek kullanıcı
kimlik doğrulaması değildir. Yönetici iki müşteriye erişebilir. Sistem dışarıya
açılmamalıdır. Diğer güven sınırları [koordinatör belgesinde](COORDINATOR.md).

## Test

```bash
PYTHONDONTWRITEBYTECODE=1 python -B -m unittest discover -s tests -p test_offline_content_seo.py -v
```

Testler tam ifade eşleşmesini, Türkçe harfleri, gizli içeriklerin dışlanmasını,
geçersiz hedefleri, değişmeyen dosyaları/özetleri, müşteri yetkisini, görev
çakışmasını ve güvensiz Docker ayarında başlatma reddini kontrol eder. Gerçek
Docker testi iki müşteri için hem yeni içerik hem eski teknik SEO işlemini ve
CLI üzerinden tekrar isteğini doğrular. Docker yoksa bu entegrasyon testi açık
nedenle atlanır; bu durum başarılı izolasyon testi olarak sunulmaz.

## İnceleme sonrası sağlamlaştırma

Her H1 ayrı bir metin olarak saklanır; aynı başlık içindeki biçimlendirme metni
kelimeleri bölmeden birleştirilir. `aria-hidden` görsel gizlilik kabul edilmez.
Kapanmamış/uyuşmayan etiketler, iç içe veya örtük kapanış gerektiren p/li/H1,
çoğaltılmış öznitelikler ve benzeri belirsizlikler ölçüm uyarısı üretir.
Bu muhafazakâr ayrıştırıcı tarayıcının HTML5 düzeltme motoru değildir: tarayıcının
kabul ettiği bazı kısaltılmış HTML örnekleri de belirsiz sayılabilir.
Belirsiz sayfanın `measurement_reliable` alanı false, sayısal/eşleşme ölçümleri
null olur. Bu sayfaya yalnızca `html_uncertain` inceleme uyarısı verilir; metinde
kelime yokmuş gibi içerik değiştirme önerileri üretilmez. Harici CSS/JS ile
belirlenen görünürlüğün bilinmediği sınırı devam eder.

Mevcut 2 MiB dosya, 100 sayfa ve 100.000 HTML karakteri sınırlarına ek olarak:

- Sayfa başına en fazla 64 açık etiket derinliği ve 10.000 ayrıştırıcı olayı.
- Yeni ayrıştırıcının olaylarında 2 saniyelik işlem bütçesi kontrolü.
- Çıkarılan metin en fazla 100.000 karakter. Tüm görev için mevcut Docker CPU,
  256 MiB bellek, süreç ve 45 saniyelik komut sınırları ayrıca korunur.

Olaylardaki süre kontrolü önleyici işletim sistemi zamanlayıcısı değildir; tek
bir Python çağrısını ortasından kesmez. İşletim sistemi sınırları yalnızca mevcut
Docker çalıştırma yolunda uygulanır. Analiz fonksiyonunu host üzerinde doğrudan
çağırmak konteyner izolasyonu sağlamaz.

Kanıt yalnızca sayfa kaydında saklanır; öneride `evidence_page_id` bulunur.
Görüntülenen title ve H1 metinleri 512 karakter, H1 listesi 32 başlıkla sınırlıdır.
Eksik terim listesi 32, sık terim listesi 8 öğeyle; gösterilen terimler 128
karakterle sınırlanır. Ölçümler tam çıkarılmış metinden hesaplanır; kısaltılmış
başlıklar `display_truncated` ile işaretlenir. Eksik terimlerin toplam sayısı ayrıca
saklanır. Rapor UTF-8 JSON olarak en fazla 2 MiB olabilir; aşımda
`ContentLimitError` verilir, kısmi rapor başarılı sayılmaz. Docker'ın 4 MiB komut
çıktısı sınırı ayrıca durur.

İçerik raporu şema sürümü 1'dir. Koordinatör hem yeni sonuçta hem SQLite'tan okurken
işlem türünü, alanları/türleri, sayfa ve öneri müşteri kimliklerini, kanıt
referanslarını, kaynak özeti biçimini ve uygulanmamış öneri durumunu doğrular.
Beklenmeyen alanlar veya bozuk kayıt reddedilir. Önceki geliştirme sürümünün
şemasız içerik sonuçları otomatik kabul edilmez; yönetici incelemesi ve açık taşıma
gerektirir. Eski `seo.analyze` sonuç biçimi korunmuştur.
