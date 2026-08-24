# NBA Condensed

CachyOS/Linux icin Python + PySide6 masaustu uygulamasi. Uygulama, kullanicinin yasal olarak eristigi full game videosu ve play-by-play dosyasindan, inbound sonrasindaki hazirlik suresini kirparak pozisyonlari birlestirir.

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

`video_time` yoksa uygulama bu surumde dosyayi reddeder. Mac saati ile video saati arasinda reklam ve mola farklari oldugu icin sessizce hatali klip uretmek yerine bu durum acikca bildirilmektedir.

## Calistirma

```bash
cd /home/hubi/nba-condensed
python main.py
```

Uygulama varsayilan olarak inbound'dan 5.5 saniye sonrasini baslangic, terminal olaydan 2.5 saniye sonrasini bitis kabul eder. Savunma ribaundu, steal, turnover ve jump ball gibi canli possession baslangiclari 1.5 saniyelik payla korunur; offensive rebound ayni pozisyonun devamidir.
