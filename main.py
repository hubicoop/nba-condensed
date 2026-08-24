from __future__ import annotations

import shutil
import re
import sys
import tempfile
from pathlib import Path

from PySide6.QtCore import QObject, QThread, Signal, Slot
from PySide6.QtWidgets import (QApplication, QFileDialog, QFormLayout, QHBoxLayout,
                               QLabel, QLineEdit, QMainWindow, QMessageBox, QProgressBar,
                               QPushButton, QDoubleSpinBox, QTextEdit, QVBoxLayout, QWidget,
                               QSpinBox)

from processor import (_clock_to_seconds, apply_clock_mapping, build_segments, download_playbyplay,
                       extract_game_id, load_events, load_nba_csv, load_nba_json,
                       probe_duration, render, validate_clock_mapping)


def detect_clock_mapping(video: str, events, status):
    """Use an optional OCR backend to map visible scoreboard clocks to video time."""
    try:
        import cv2
        import pytesseract
    except ImportError as error:
        raise ValueError("Scoreboard esleme icin opencv-python ve pytesseract kurulmalı.") from error

    capture = cv2.VideoCapture(video)
    fps = capture.get(cv2.CAP_PROP_FPS) or 30.0
    frame_count = capture.get(cv2.CAP_PROP_FRAME_COUNT) or 0
    duration = frame_count / fps if frame_count else 0
    sample_every = max(1, int(fps * 1.0))
    # Ilk prototip yalnizca 1. periyot icin calisir. Reklam/tekrar OCR'i
    # sonraki periyotlari yanlis algilasa bile dikkate almayiz.
    events = [event for event in events if event.period == 1]
    mapping = {}
    frame_number = 0
    last_report = -1
    status("Scoreboard OCR taramasi basliyor...")
    while frame_number < frame_count:
        ok, frame = capture.read()
        if not ok:
            break
        height, width = frame.shape[:2]
        # NBA yayinlarinda scoreboard siklikla alt bantta yer alir.
        crop = frame[int(height * 0.70):, :]
        text = pytesseract.image_to_string(crop, config="--psm 6")
        clock_match = re.search(r"(?<!\d)(\d{1,2})\s*[:.]\s*(\d{2})(?!\d)", text)
        period_match = re.search(r"(?:[Pp]|[Qq]|[Pp]eriod)\s*1\b|\b1(?:ST|st)\b", text)
        if clock_match and period_match:
            minutes, seconds = int(clock_match.group(1)), int(clock_match.group(2))
            period = 1
            if minutes <= 12 and seconds < 60:
                clock_seconds = minutes * 60 + seconds
                mapping.setdefault(period, []).append((clock_seconds, frame_number / fps))
        frame_number += sample_every
        percent = int((frame_number / frame_count) * 100) if frame_count else 0
        if percent != last_report:
            last_report = percent
            status(f"Scoreboard OCR taraniyor: %{percent}")
    capture.release()
    # OCR, reklam veya tekrar karelerinde olmayan bir periyodu okuyabilir.
    # Bir periyot ancak CSV'deki clock araligiyla da eslesiyorsa kabul edilir.
    csv_period_clocks = {}
    for event in events:
        try:
            csv_period_clocks.setdefault(event.period, []).append(
                _clock_to_seconds(event.clock)
            )
        except ValueError:
            continue

    cleaned = {}
    for period, points in mapping.items():
        if period not in csv_period_clocks:
            continue
        csv_min = min(csv_period_clocks[period])
        csv_max = max(csv_period_clocks[period])
        points = [(clock, video_time) for clock, video_time in points
                  if csv_min - 5 <= clock <= csv_max + 5]
        if not points:
            continue
        # Aynı oyun saatinin ardışık OCR okumalarını tek noktaya indir.
        unique = {}
        for clock, video_time in points:
            unique.setdefault(round(clock), video_time)
        ordered = sorted(unique.items(), key=lambda item: item[1])
        filtered = []
        for point in ordered:
            if not filtered or point[0] <= filtered[-1][0] + 2:
                filtered.append(point)
        cleaned[period] = filtered
    if not cleaned:
        raise ValueError("Scoreboard OCR oyun saati okuyamadi. Daha sonra bolge secimi eklenecek.")
    # A test video may contain only the first quarter while the selected CSV
    # contains the entire game. Only periods visible in the video are required.
    validate_clock_mapping(events, cleaned, {1})
    return cleaned


class RenderWorker(QObject):
    finished = Signal(str, int, float)
    failed = Signal(str)
    status = Signal(str)

    def __init__(self, video: str, plays: str, output: str, lead: float, tail: float, source: str):
        super().__init__()
        self.video, self.plays, self.output = video, plays, output
        self.lead, self.tail, self.source = lead, tail, source

    @Slot()
    def run(self):
        try:
            self.status.emit("Video suresi okunuyor...")
            duration = probe_duration(self.video)
            self.status.emit("Play-by-play okunuyor...")
            if self.source.startswith("http"):
                game_id = extract_game_id(self.source)
                if not game_id:
                    raise ValueError("NBA linkinden Game ID bulunamadi.")
                self.status.emit(f"NBA verisi indiriliyor: {game_id}")
                try:
                    with tempfile.TemporaryDirectory() as temp_dir:
                        json_path = download_playbyplay(game_id, Path(temp_dir) / "playbyplay.json")
                        events = load_nba_json(json_path)
                except Exception as error:
                    if not self.plays:
                        raise ValueError(f"NBA verisi indirilemedi: {error}") from error
                    self.status.emit("NBA servisi engelledi; secilen sezon CSV'sine geciliyor...")
                    events = load_nba_csv(self.plays, game_id)
                    if not events:
                        raise ValueError(f"NBA JSON ve CSV fallback ile {game_id} bulunamadi.") from error
            elif self.source:
                events = load_nba_csv(self.plays, self.source)
                if not events:
                    raise ValueError(f"{self.source} icin CSV satiri bulunamadi.")
            else:
                events = load_events(self.plays)
            # Prototip asamasi: yalnizca ilk periyot islenir.
            events = [event for event in events if event.period == 1]
            if not events:
                raise ValueError("CSV icinde 1. periyot olayi bulunamadi.")
            if self.source.startswith("http") or self.source:
                mapping = detect_clock_mapping(self.video, events, self.status.emit)
                events = apply_clock_mapping(events, mapping)
                if not events:
                    raise ValueError("Scoreboard OCR ile oyun saati eslesmesi bulunamadi.")
            segments = build_segments(events, duration, self.lead, self.tail)
            self.status.emit(f"{len(segments)} pozisyon bulundu. FFmpeg calisiyor...")
            for index, segment in enumerate(segments[:5], 1):
                self.status.emit(f"Pozisyon {index}: {segment.start:.1f}s - {segment.end:.1f}s")
            render(self.video, self.output, segments, self.status.emit)
            output_duration = sum(segment.end - segment.start for segment in segments)
            self.finished.emit(self.output, len(segments), output_duration)
        except Exception as error:
            self.failed.emit(str(error))


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("NBA Condensed")
        self.resize(760, 520)
        self.thread: QThread | None = None
        self.worker: RenderWorker | None = None
        self.video = QLineEdit()
        self.plays = QLineEdit()
        self.output = QLineEdit()
        self.game_link = QLineEdit()
        self.scoreboard_x = QSpinBox(); self.scoreboard_x.setRange(0, 10000); self.scoreboard_x.setValue(0)
        self.scoreboard_y = QSpinBox(); self.scoreboard_y.setRange(0, 10000); self.scoreboard_y.setValue(0)
        self.scoreboard_w = QSpinBox(); self.scoreboard_w.setRange(0, 10000); self.scoreboard_w.setValue(0)
        self.scoreboard_h = QSpinBox(); self.scoreboard_h.setRange(0, 10000); self.scoreboard_h.setValue(0)
        self.lead = QDoubleSpinBox(); self.lead.setRange(0, 30); self.lead.setValue(5.5); self.lead.setSuffix(" sn")
        self.tail = QDoubleSpinBox(); self.tail.setRange(0, 30); self.tail.setValue(2.5); self.tail.setSuffix(" sn")
        self.status_label = QLabel("Video, play-by-play ve cikti dosyasini secin.")
        self.log = QTextEdit(); self.log.setReadOnly(True)
        self.start_button = QPushButton("Condensed olustur")
        self.start_button.clicked.connect(self.start_render)
        self._build_ui()

    def _build_ui(self):
        form = QFormLayout()
        form.addRow("Full game video", self._path_row(self.video, "Video sec", "Video Files (*.mp4 *.mkv *.mov *.webm)"))
        form.addRow("Play-by-play", self._path_row(self.plays, "PBP sec", "Data Files (*.csv *.json)"))
        self.game_link.setPlaceholderText("https://www.nba.com/game/gsw-vs-cle-0021600457/play-by-play")
        form.addRow("NBA mac linki", self.game_link)
        form.addRow("Scoreboard bolgesi", QLabel("Simdilik otomatik tam kare OCR kullaniliyor"))
        form.addRow("Cikti", self._path_row(self.output, "Cikti sec", "MP4 Video (*.mp4)", save=True))
        form.addRow("Inbound sonrasi kirp", self.lead)
        form.addRow("Pozisyon sonu payi", self.tail)
        intro = QLabel("Tum pozisyonlar korunur. Varsayilan kural: inbound + 5.5 saniye ile basla, terminal olaydan 2.5 saniye sonra bitir.")
        intro.setWordWrap(True)
        layout = QVBoxLayout(); layout.addWidget(intro); layout.addLayout(form); layout.addWidget(self.start_button)
        layout.addWidget(self.status_label); layout.addWidget(self.log)
        root = QWidget(); root.setLayout(layout); self.setCentralWidget(root)

    @staticmethod
    def _path_row(field: QLineEdit, button_text: str, file_filter: str, save: bool = False):
        button = QPushButton(button_text)
        row = QWidget(); layout = QHBoxLayout(row); layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(field); layout.addWidget(button)
        button.clicked.connect(lambda: MainWindow._choose(field, file_filter, save))
        return row

    @staticmethod
    def _choose(field: QLineEdit, file_filter: str, save: bool = False):
        dialog = QFileDialog.getSaveFileName if save else QFileDialog.getOpenFileName
        path, _ = dialog(None, "Cikti dosyasini sec" if save else "Dosya sec", str(Path.home()), file_filter)
        if path: field.setText(path)

    def start_render(self):
        video, plays, output = self.video.text().strip(), self.plays.text().strip(), self.output.text().strip()
        source = self.game_link.text().strip()
        if output and not Path(output).suffix:
            output = f"{output}.mp4"
            self.output.setText(output)
        if not video or not output:
            QMessageBox.warning(self, "Eksik bilgi", "Video ve cikti dosyasini secin.")
            return
        if not source and not plays:
            QMessageBox.warning(self, "Eksik bilgi", "NBA mac linki veya sezon PBP CSV'si secin.")
            return
        if shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None:
            QMessageBox.critical(self, "FFmpeg bulunamadi", "ffmpeg ve ffprobe CachyOS PATH icinde olmali.")
            return
        self.start_button.setEnabled(False); self.log.clear(); self.status_label.setText("Baslatiliyor...")
        self.thread = QThread(self); self.worker = RenderWorker(video, plays, output, self.lead.value(), self.tail.value(), source)
        self.worker.moveToThread(self.thread); self.thread.started.connect(self.worker.run)
        self.worker.status.connect(self._status); self.worker.finished.connect(self._finished); self.worker.failed.connect(self._failed)
        self.worker.finished.connect(self.thread.quit); self.worker.failed.connect(self.thread.quit); self.thread.finished.connect(self._thread_done)
        self.thread.start()

    @Slot(str)
    def _status(self, message): self.status_label.setText(message); self.log.append(message)

    @Slot(str, int, float)
    def _finished(self, output, count, duration):
        self._status(f"Tamamlandi: {count} segment, tahmini cikti {duration / 60:.1f} dakika")
        QMessageBox.information(self, "Tamamlandi", f"Condensed video olusturuldu:\n{output}")

    @Slot(str)
    def _failed(self, message):
        self.status_label.setText("Hata")
        self.log.append(f"HATA: {message}")
        QMessageBox.critical(self, "Islem basarisiz", message)

    def _thread_done(self): self.start_button.setEnabled(True); self.thread = None; self.worker = None


if __name__ == "__main__":
    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    window = MainWindow(); window.show()
    sys.exit(app.exec())
