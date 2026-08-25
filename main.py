from __future__ import annotations

import shutil
import sys
import tempfile
from pathlib import Path

from PySide6.QtCore import QObject, QThread, Signal, Slot
from PySide6.QtWidgets import (
    QApplication, QFileDialog, QFormLayout, QHBoxLayout, QLabel, QLineEdit,
    QMainWindow, QMessageBox, QPushButton, QDoubleSpinBox, QTableWidget,
    QTableWidgetItem, QTextEdit, QVBoxLayout, QWidget,
)

from processor import (
    CalibrationPoint, apply_clock_mapping, build_possessions, build_segments,
    download_playbyplay, extract_game_id, load_events, load_nba_csv, load_nba_json,
    map_events_with_calibration, possession_segments, probe_duration, render,
)


def seconds_to_clock(value: float) -> str:
    minutes, seconds = divmod(value, 60)
    return f"{int(minutes):02d}:{seconds:05.2f}"


class RenderWorker(QObject):
    finished = Signal(str, int, float)
    failed = Signal(str)
    status = Signal(str)
    segments_ready = Signal(list)

    def __init__(self, video, plays, output, source, calibration, lead, live_lead, tail):
        super().__init__()
        self.video, self.plays, self.output, self.source = video, plays, output, source
        self.calibration = calibration
        self.lead, self.live_lead, self.tail = lead, live_lead, tail

    @Slot()
    def run(self):
        try:
            duration = probe_duration(self.video)
            self.status.emit("Play-by-play okunuyor...")
            if self.source.startswith("http"):
                game_id = extract_game_id(self.source)
                if not game_id:
                    raise ValueError("NBA linkinden Game ID bulunamadi.")
                with tempfile.TemporaryDirectory() as temp_dir:
                    try:
                        path = download_playbyplay(game_id, Path(temp_dir) / "playbyplay.json")
                        events = load_nba_json(path)
                    except Exception as error:
                        if not self.plays:
                            raise ValueError(f"NBA servisi 403 verdi ve CSV fallback secilmedi: {error}") from error
                        self.status.emit("NBA servisi engelledi; secilen CSV fallback olarak kullaniliyor...")
                        events = load_nba_csv(self.plays, game_id)
                        if not events:
                            raise ValueError(f"NBA JSON ve CSV fallback ile {game_id} bulunamadi.") from error
            elif self.source:
                events = load_nba_csv(self.plays, self.source)
            else:
                events = load_events(self.plays)
            events = [event for event in events if event.period == 1]
            if not events:
                raise ValueError("1. periyot olayi bulunamadi.")
            mapped = map_events_with_calibration(events, self.calibration)
            if not mapped:
                raise ValueError("Kalibrasyon ile hicbir CSV olayi eslesmedi.")
            segments = possession_segments(mapped, duration, self.lead, self.live_lead, self.tail)
            if not segments:
                raise ValueError("Hic possession segmenti olusturulamadi.")
            self.segments_ready.emit(segments)
            self.status.emit(f"{len(segments)} pozisyon hazir. FFmpeg calisiyor...")
            render(self.video, self.output, segments, self.status.emit)
            total = sum(segment.end - segment.start for segment in segments)
            self.finished.emit(self.output, len(segments), total)
        except Exception as error:
            self.failed.emit(str(error))


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("NBA Condensed v0.1 Alpha")
        self.resize(900, 700)
        self.thread = None
        self.worker = None
        self.video = QLineEdit()
        self.plays = QLineEdit()
        self.output = QLineEdit()
        self.link = QLineEdit()
        self.lead = self._spin(5.5)
        self.live_lead = self._spin(1.0)
        self.tail = self._spin(2.5)
        self.calibration = []
        self.table = QTableWidget(0, 3)
        self.table.setHorizontalHeaderLabels(["Oyun saati", "Video saniyesi", "Periyot"])
        self.log = QTextEdit(); self.log.setReadOnly(True)
        self.start = QPushButton("Segmentleri hesapla ve olustur")
        self.start.clicked.connect(self.start_render)
        self._build_ui()

    @staticmethod
    def _spin(value):
        spin = QDoubleSpinBox(); spin.setRange(0, 7200); spin.setDecimals(2); spin.setValue(value)
        return spin

    def _build_ui(self):
        form = QFormLayout()
        form.addRow("Full game video", self._path_row(self.video, "Video sec", "Video Files (*.mp4 *.mkv *.mov *.webm)"))
        form.addRow("Sezon PBP CSV", self._path_row(self.plays, "CSV sec", "CSV Files (*.csv)"))
        self.link.setPlaceholderText("NBA linki opsiyonel; CDN 403 olursa CSV kullanilir")
        form.addRow("NBA mac linki", self.link)
        form.addRow("Cikti", self._path_row(self.output, "Cikti sec", "MP4 Video (*.mp4)", True))
        form.addRow("Normal lead", self.lead)
        form.addRow("Canli lead", self.live_lead)
        form.addRow("Tail", self.tail)

        info = QLabel("Ilk prototip yalnizca 1. periyodu isler. Kalibrasyon tablosuna ayni periyot icin en az iki nokta girin. Oyun saati saniye cinsindendir: 12:00 = 720.")
        info.setWordWrap(True)
        calibration = QHBoxLayout()
        for label, value in (("Oyun saati", "720"), ("Video saniyesi", "0")):
            calibration.addWidget(QLabel(label)); field = QLineEdit(value); field.setObjectName(label); calibration.addWidget(field)
        self.clock_input, self.video_input = calibration.itemAt(1).widget(), calibration.itemAt(3).widget()
        add = QPushButton("Kalibrasyon noktasi ekle"); add.clicked.connect(self.add_calibration)
        clear = QPushButton("Temizle"); clear.clicked.connect(self.table.clearContents)
        buttons = QHBoxLayout(); buttons.addLayout(calibration); buttons.addWidget(add); buttons.addWidget(clear)

        layout = QVBoxLayout(); layout.addWidget(info); layout.addLayout(form); layout.addLayout(buttons); layout.addWidget(self.table); layout.addWidget(self.start); layout.addWidget(self.log)
        root = QWidget(); root.setLayout(layout); self.setCentralWidget(root)

    def add_calibration(self):
        try:
            clock = float(self.clock_input.text().replace(":", "").strip())
            if ":" in self.clock_input.text():
                m, s = self.clock_input.text().split(":", 1); clock = int(m) * 60 + float(s)
            video = float(self.video_input.text().replace(",", "."))
        except ValueError:
            QMessageBox.warning(self, "Gecersiz nokta", "Oyun saati 12:00 veya 720, video saniyesi sayi olmali.")
            return
        row = self.table.rowCount(); self.table.insertRow(row)
        self.table.setItem(row, 0, QTableWidgetItem(str(clock)))
        self.table.setItem(row, 1, QTableWidgetItem(str(video)))
        self.table.setItem(row, 2, QTableWidgetItem("1"))

    @staticmethod
    def _path_row(field, text, filter_text, save=False):
        row = QWidget(); layout = QHBoxLayout(row); layout.setContentsMargins(0, 0, 0, 0); layout.addWidget(field)
        button = QPushButton(text); layout.addWidget(button)
        button.clicked.connect(lambda: MainWindow._choose(field, filter_text, save)); return row

    @staticmethod
    def _choose(field, filter_text, save):
        fn = QFileDialog.getSaveFileName if save else QFileDialog.getOpenFileName
        path, _ = fn(None, "Cikti sec" if save else "Dosya sec", str(Path.home()), filter_text)
        if path: field.setText(path)

    def read_calibration(self):
        points = []
        for row in range(self.table.rowCount()):
            points.append(CalibrationPoint(float(self.table.item(row, 0).text()), float(self.table.item(row, 1).text())))
        return {1: points}

    def start_render(self):
        video, plays, output, source = self.video.text().strip(), self.plays.text().strip(), self.output.text().strip(), self.link.text().strip()
        if not video or not output or (not plays and not source):
            QMessageBox.warning(self, "Eksik bilgi", "Video, cikti ve CSV veya NBA linki gerekli."); return
        if self.table.rowCount() < 2:
            QMessageBox.warning(self, "Kalibrasyon eksik", "En az iki kalibrasyon noktasi ekleyin."); return
        if shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None:
            QMessageBox.critical(self, "FFmpeg yok", "ffmpeg ve ffprobe PATH icinde olmali."); return
        if not Path(output).suffix: output += ".mp4"; self.output.setText(output)
        self.start.setEnabled(False); self.log.clear()
        self.thread = QThread(self)
        self.worker = RenderWorker(video, plays, output, source, self.read_calibration(), self.lead.value(), self.live_lead.value(), self.tail.value())
        self.worker.moveToThread(self.thread); self.thread.started.connect(self.worker.run)
        self.worker.status.connect(self.log.append); self.worker.failed.connect(self.failed); self.worker.finished.connect(self.finished)
        self.worker.finished.connect(self.thread.quit); self.worker.failed.connect(self.thread.quit); self.thread.finished.connect(lambda: self.start.setEnabled(True)); self.thread.start()

    @Slot(str)
    def failed(self, message): QMessageBox.critical(self, "Hata", message); self.log.append(message)

    @Slot(str, int, float)
    def finished(self, output, count, duration): QMessageBox.information(self, "Tamamlandi", f"{count} pozisyon, {duration / 60:.1f} dakika\n{output}")


if __name__ == "__main__":
    app = QApplication(sys.argv); app.setStyle("Fusion"); window = MainWindow(); window.show(); sys.exit(app.exec())
