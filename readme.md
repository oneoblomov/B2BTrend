# B2BTrend

[![Python tests (3.11)](https://github.com/oneoblomov/B2BTrend/actions/workflows/python-tests.yml/badge.svg?branch=main&event=push)](https://github.com/oneoblomov/B2BTrend/actions/workflows/python-tests.yml)
[![Python tests (3.12)](https://github.com/oneoblomov/B2BTrend/actions/workflows/python-tests.yml/badge.svg?branch=main&event=push)](https://github.com/oneoblomov/B2BTrend/actions/workflows/python-tests.yml)
[![Integration test](https://github.com/oneoblomov/B2BTrend/actions/workflows/python-tests.yml/badge.svg?branch=main&event=workflow_dispatch)](https://github.com/oneoblomov/B2BTrend/actions/workflows/python-tests.yml)

**Hafif, sade ve üretime hazır bir Google Trends analiz arayüzü**.

## Tanıtım

B2BTrend, Google Trends verisini anahtar kelime / Topic ID bazında hızlıca çekip görselleştiren ve raporlayan Python + Streamlit uygulamasıdır. Şehir/ülke kırılımı, zamana göre ilgi düzeyi, cache/takip grafikleri ve uyarı sinyalleri sağlar.

## Öne çıkan avantajlar

- Kullanıcı dostu arayüz ve minimal bağımlılıklar
- Güvenilir cache (parquet) + TTL + maksimum boyut sınırı
- Workspace bazli tek CSV veri dosyasi + JSON metadata
- Topic ID modu veya ulke bazli arama metni (TR:tavuk, US:chicken vb.)
- Varsayilan workspace secimi ve otomatik acilis
- Çoklu dil destekleri (TR / EN) + `locales/` üstü çeviri
- `pytest` tabanlı birim testi ve CI entegrasyonu

## Kurulum

1. Python 3.11/3.12 tavsiye edilir.
2. Proje köküne geçin:

```bash
cd /home/kaplan/Desktop/Azim-Tav/B2BTrend
```

3. Sanal ortam oluşturun ve aktif edin:

```bash
python -m venv .venv
source .venv/bin/activate
```

4. Bağımlılıkları yükleyin:

```bash
pip install -r requirements.txt
```

5. Ortam dosyasını kopyalayın:

```bash
cp .env.example .env
```

6. Uygulamayı başlatın:

```bash
streamlit run app.py
```

## Yapı ve önemli dosyalar

- `app.py`: Streamlit arayüzü başlatma.
- `src/trend_fetcher.py`: Google Trends API çağrılarını yürütür, cache ve snapshot oluşturur.
- `src/analytics.py`: trend, uyarı, sinyal analizleri.
- `src/reports.py`: görüntü raporları, PDF, JSON çıktı.
- `src/config.py`: app ayarları, cache parametreleri, varsayılanlar.
- `data/workspaces/<workspace_id>/dataset.csv`: workspace verisi (tek CSV).
- `data/workspaces/<workspace_id>/metadata.json`: workspace ayarlari ve metadata.
- `data/workspaces/settings.json`: varsayilan workspace ayari.
- `data/cache/`: geçici parquet cache dosyaları.
- `locales/`: TR/EN çeviri metinleri.

## Kullanım akışı

1. Arayüzde anahtar kelime / Topic ID ve ülke/şehir seçilir.
2. `fetch_trends_dataset()` çağrılır (şehir + timeline API çağrıları).
3. Çekilen veriler dairesel cache’e kaydedilir (`data/cache/`), TTL ve boyut uygulanır.
4. Workspace verisi `data/workspaces/<workspace_id>/dataset.csv` dosyasina tek CSV olarak yazilir.
5. Workspace ayarlari (arama metni, ulkeler vb.) `metadata.json` icinde tutulur.
6. `src/analytics.py` ile trend sinyalleri ve istatistikler hesaplanır.

## Özelleştirme ve gelişmiş kullanım

- `src/config.py` içindeki `CACHE_TTL_HOURS`, `CACHE_MAX_BYTES`, `DEFAULT_LANG` gibi değerler değiştirilebilir.
- Kendi sorgu iş akışı için:

```python
from src.trend_fetcher import fetch_trends_dataset

result = fetch_trends_dataset(
    query='poultry',
    geo='TR',
    timeframe='today 12-m',
    by_city=True,
    cache_enabled=True,
)
print(result)
```

- Farklı diller önizlemesi için `locales/tr.json`, `locales/en.json` düzenlenebilir.

## Testler

Projede `pytest` ile testler bulunur:

```bash
pytest -q
```

Örnek test dosyası: `tests/test_trend_fetcher.py`

## Hata ayıklama ve loglama

- `logging` modülünü `app.py`, `src/trend_fetcher.py` vb. dosyalarda kontrol edin.
- `data/cache/` ve `data/active/` dizin izinlerini doğrulayın.
- Google Trends API limitlerine takılmamak için istek sıklığını azaltın.

## Katkıda bulunma

1. Fork yapın.
2. Yeni bir dal açın (`git checkout -b feature/...`).
3. Değişikleri commit edin.
4. Pull request gönderin.

## Lisans

MIT

