# NBA Condensed

## v0.1 Alpha

Bu proje aktif geliştirme aşamasındaki bir alpha build'dir. NBA full game videolarini play-by-play verisi ve scoreboard OCR kullanarak sadece topun oyunda kaldığı anları tutarak 2 saatlik maç tekrarlarını 40 dakikalık tekrarlara donusturmeyi amaclar. Mevcut surum deneysel prototiptir; OCR kalibrasyonu, farkli yayin formatlari ve possession kirpma mantigi henuz gelistirilmektedir. Uretim kalitesinde ve hatasiz cikti garanti edilmez.

Katki, hata raporu ve test geri bildirimleri gelistirme sureci icin faydalidir.

CachyOS/Linux icin Python + PySide6 masaustu uygulamasi. Uygulama, kullanicinin yasal olarak eristigi full game videosu ve play-by-play dosyasindan possession zincirlerini olusturur. v0.1 alpha surumunde yalnizca 1. periyot islenir ve video zamani manuel kalibrasyon noktalariyla eslenir.

## Gereksinimler

- Python 3.11+
- PySide6
- FFmpeg ve ffprobe

Arch/CachyOS:

```bash
sudo pacman -S ffmpeg python-pyside6
python main.py
```

## Play-by-play dosya formati

En guvenilir format CSV'dir. `video_time` alaninin video baslangicindan itibaren saniye cinsinden olmasi gerekir.

```csv
period,clock,event_type,video_time,description
1,12:00,Inbound,35.20,Home ball inbound
1,11:42,Made Shot,53.70,Player makes 2PT shot
1,11:42,Inbound,61.10,Away ball inbound
```

JSON icin ayni alanlara sahip bir event listesi kullanilabilir:

```json
[
  {"period": 1, "clock": "12:00", "event_type": "Inbound", "video_time": 35.2},
  {"period": 1, "clock": "11:42", "event_type": "Made Shot", "video_time": 53.7}
]
```

NBA sezon CSV'sinde `video_time` bulunmasi gerekmez. Uygulama C sutunundaki oyun saatini kullanir; arayuzde en az iki adet `oyun saati -> video saniyesi` kalibrasyon noktasi girilmelidir. Ornegin `12:00 -> 42.0` ve `00:00 -> 1850.0`.

## Calistirma

```bash
cd /home/hubi/nba-condensed
python main.py
```

Varsayilan kirpma parametreleri: normal possession icin 5.5 saniye lead, steal/defensive rebound gibi canli baslangiclar icin 1 saniye lead ve pozisyon sonu icin 2.5 saniye tail. Offensive rebound, foul ve free throw dizileri ayni possession zincirinde tutulur. Ilk periyot testleri tamamlanmadan tum maca gecilmemelidir.
