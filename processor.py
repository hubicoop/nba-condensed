"""Play-by-play driven video segment generation."""

from __future__ import annotations

import csv
import json
import re
import subprocess
import urllib.request
import urllib.error
from dataclasses import dataclass
from pathlib import Path
from typing import Callable


@dataclass(frozen=True)
class Event:
    period: int
    event_type: str
    video_time: float
    clock: str = ""
    description: str = ""


@dataclass(frozen=True)
class Segment:
    start: float
    end: float


@dataclass(frozen=True)
class CalibrationPoint:
    game_seconds: float
    video_seconds: float


@dataclass(frozen=True)
class Possession:
    period: int
    start_clock: str
    end_clock: str
    start_event: str
    end_event: str
    start_video: float
    end_video: float


def _clock_to_seconds(value: object) -> float:
    match = re.search(r"PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+(?:\.\d+)?)S)?", str(value or ""))
    if not match:
        raise ValueError(f"Gecersiz NBA clock degeri: {value}")
    hours = float(match.group(1) or 0)
    minutes = float(match.group(2) or 0)
    seconds = float(match.group(3) or 0)
    return hours * 3600 + minutes * 60 + seconds


def extract_game_id(value: str) -> str:
    match = re.search(r"game/[^/?#]*?(\d{8,10})(?:/|\?|#|$)", value, re.IGNORECASE)
    if match:
        return _normalise_game_id(match.group(1))
    digits = re.sub(r"\D", "", value)
    return _normalise_game_id(digits) if len(digits) >= 8 else ""


def download_playbyplay(game_id: str, destination: str | Path) -> Path:
    game_id = _normalise_game_id(game_id)
    if not game_id:
        raise ValueError("NBA linkinde gecerli bir Game ID bulunamadi.")
    url = f"https://cdn.nba.com/static/json/liveData/playbyplay/playbyplay_{game_id}.json"
    headers = {
        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/131 Safari/537.36",
        "Accept": "application/json,text/plain,*/*",
        "Accept-Language": "en-US,en;q=0.9",
        "Origin": "https://www.nba.com",
        "Referer": "https://www.nba.com/",
    }
    request = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            payload = response.read()
    except urllib.error.HTTPError as error:
        if error.code != 403:
            raise
        result = subprocess.run(
            ["curl", "--fail", "--location", "--silent", "--show-error",
             "--compressed", "-A", headers["User-Agent"], "-H", "Accept: application/json",
             "-H", "Referer: https://www.nba.com/", url],
            capture_output=True, check=False,
        )
        if result.returncode != 0 or not result.stdout:
            raise ValueError("NBA play-by-play servisi 403 Forbidden dondu.") from error
        payload = result.stdout
    destination = Path(destination)
    destination.write_bytes(payload)
    return destination


def load_nba_json(path: str | Path) -> list[Event]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    actions = data.get("game", {}).get("actions", [])
    events: list[Event] = []
    for action in actions:
        try:
            period = int(action.get("period", 0))
            clock = str(action.get("clock", ""))
            _clock_to_seconds(clock)
        except (TypeError, ValueError):
            continue
        events.append(Event(period, str(action.get("actionType", "")), 0.0,
                            clock, str(action.get("description", ""))))
    return events


def _row_to_event(row: list[str], video_time: float | None = None) -> Event:
    # NBA'nin sezon CSV formati: game_id, period, clock, ..., action_type, ...
    if len(row) < 15:
        raise ValueError("NBA CSV satiri beklenenden kisa.")
    period = int(row[1])
    clock = row[2]
    event_type = row[8] or row[7] or row[9]
    description = row[14]
    if video_time is None:
        video_time = 0.0
    return Event(period, event_type, video_time, clock, description)


def _normalise_game_id(value: object) -> str:
    digits = re.sub(r"\D", "", str(value or ""))
    if not digits:
        return ""
    # NBA game IDs are normally 10 digits, but user input may omit leading zeroes.
    return digits.zfill(10)


def load_nba_csv(path: str | Path, game_id: str, video_times: dict[int, float] | None = None) -> list[Event]:
    """Load a headed or headerless tab-separated NBA season export for one game."""
    wanted = _normalise_game_id(game_id)
    events: list[Event] = []
    with Path(path).open(newline="", encoding="utf-8-sig") as handle:
        first_line = handle.readline()
        handle.seek(0)
        delimiter = "\t" if first_line.count("\t") > first_line.count(",") else ","
        has_header = first_line.strip().lower().startswith("gameid")
        if has_header:
            reader = csv.DictReader(handle, delimiter=delimiter)
            for row in reader:
                if _normalise_game_id(row.get("gameid", row.get("game_id", ""))) != wanted:
                    continue
                period = int(row.get("period", 0) or 0)
                video_time = (video_times or {}).get(period, 0.0)
                events.append(Event(period, row.get("type", ""), video_time,
                                    row.get("clock", ""), row.get("desc", "")))
        else:
            reader = csv.reader(handle, delimiter=delimiter)
            for row in reader:
                if not row or len(row) < 15 or _normalise_game_id(row[0]) != wanted:
                    continue
                period = int(row[1])
                video_time = (video_times or {}).get(period, 0.0)
                events.append(_row_to_event(row, video_time))
    return events


def load_nba_csv_with_clock(path: str | Path, game_id: str) -> list[Event]:
    """Load NBA CSV events while retaining game-clock seconds for calibration."""
    events = load_nba_csv(path, game_id)
    return events


def apply_clock_mapping(events: list[Event], clock_map: dict[int, list[tuple[float, float]]]) -> list[Event]:
    """Interpolate each period's game-clock reading to a video timestamp."""
    mapped: list[Event] = []
    for event in events:
        points = sorted(clock_map.get(event.period, []), key=lambda point: point[0], reverse=True)
        if not points:
            continue
        game_seconds = _clock_to_seconds(event.clock)
        if len(points) == 1:
            video_time = points[0][1] + (points[0][0] - game_seconds)
        else:
            # points are (game-clock seconds, video seconds), descending by clock.
            if game_seconds > points[0][0]:
                # Before the first OCR observation, use the period's first observed
                # clock only; extrapolation here is a common source of large offsets.
                video_time = points[0][1]
            elif game_seconds < points[-1][0]:
                video_time = points[-1][1]
            else:
                lower = next(point for point in points if point[0] <= game_seconds)
                upper = next(point for point in reversed(points) if point[0] >= game_seconds)
                if upper[0] == lower[0]:
                    video_time = lower[1]
                else:
                    ratio = (game_seconds - lower[0]) / (upper[0] - lower[0])
                    video_time = lower[1] + ratio * (upper[1] - lower[1])
        mapped.append(Event(event.period, event.event_type, video_time, event.clock, event.description))
    return mapped


def map_clock_to_video(clock: str, points: list[CalibrationPoint]) -> float:
    if len(points) < 2:
        raise ValueError("En az iki kalibrasyon noktasi gerekli.")
    ordered = sorted(points, key=lambda point: point.game_seconds, reverse=True)
    value = _clock_to_seconds(clock)
    if value > ordered[0].game_seconds or value < ordered[-1].game_seconds:
        raise ValueError(f"{clock} kalibrasyon araligi disinda.")
    for upper, lower in zip(ordered, ordered[1:]):
        if lower.game_seconds <= value <= upper.game_seconds:
            ratio = (upper.game_seconds - value) / (upper.game_seconds - lower.game_seconds)
            return upper.video_seconds + ratio * (lower.video_seconds - upper.video_seconds)
    return ordered[-1].video_seconds


def map_events_with_calibration(events: list[Event], calibration: dict[int, list[CalibrationPoint]]) -> list[Event]:
    mapped = []
    for event in events:
        points = calibration.get(event.period, [])
        if len(points) < 2:
            continue
        try:
            video_time = map_clock_to_video(event.clock, points)
        except ValueError:
            continue
        mapped.append(Event(event.period, event.event_type, video_time, event.clock, event.description))
    return mapped


def validate_clock_mapping(events: list[Event], clock_map: dict[int, list[tuple[float, float]]],
                           periods: set[int] | None = None) -> None:
    required = periods or {event.period for event in events}
    missing = sorted(period for period in required if period not in clock_map)
    if missing:
        raise ValueError(f"Scoreboard kalibrasyonu eksik: periyot {', '.join(map(str, missing))}")
    for period, points in clock_map.items():
        valid = [(clock, video) for clock, video in points if 0 <= clock <= 720 and video >= 0]
        if len(valid) < 3:
            raise ValueError(f"Periyot {period} icin yeterli scoreboard referansi yok.")
        ordered = sorted(valid, key=lambda point: point[1])
        monotonic = [ordered[0]]
        for point in ordered[1:]:
            # Tek karelik OCR hatalarini atla; yayin tekrarlarinda saat ileri gidebilir.
            if point[0] <= monotonic[-1][0] + 2:
                monotonic.append(point)
        if len(monotonic) < 3:
            raise ValueError(f"Periyot {period} icin scoreboard referanslari guvenilmez.")


TERMINAL_TERMS = (
    "made shot", "made 2", "made 3", "turnover",
    "defensive rebound", "period end", "end period", "quarter end",
    "offensive foul", "violation",
)
INBOUND_TERMS = ("inbound", "in-bound", "ball in")


def _normalise(value: object) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip().lower())


def load_events(path: str | Path) -> list[Event]:
    source = Path(path)
    if source.suffix.lower() == ".json":
        data = json.loads(source.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            data = data.get("events", data.get("plays", []))
    else:
        with source.open(newline="", encoding="utf-8-sig") as handle:
            data = list(csv.DictReader(handle))

    events: list[Event] = []
    for row in data:
        if "video_time" not in row or str(row["video_time"]).strip() == "":
            raise ValueError("Her olayda video_time alani bulunmali.")
        try:
            period = int(row.get("period", 0) or 0)
            video_time = float(str(row["video_time"]).replace(",", "."))
        except (TypeError, ValueError) as exc:
            raise ValueError(f"Gecersiz olay: {row}") from exc
        events.append(Event(period, str(row.get("event_type", "")), video_time,
                            str(row.get("clock", "")), str(row.get("description", ""))))
    return sorted(events, key=lambda event: event.video_time)


def _contains(event: Event, terms: tuple[str, ...]) -> bool:
    text = _normalise(f"{event.event_type} {event.description}")
    return any(term in text for term in terms)


def _is_steal(event: Event) -> bool:
    return "steal" in _normalise(event.description)


def _is_offensive_rebound(event: Event) -> bool:
    text = _normalise(event.description)
    match = re.search(r"off\s*:\s*(\d+)\s+def\s*:\s*(\d+)", text)
    if match:
        return int(match.group(1)) > 0
    return "offensive rebound" in text or "off reb" in text


def _is_defensive_rebound(event: Event) -> bool:
    text = _normalise(event.description)
    match = re.search(r"off\s*:\s*(\d+)\s+def\s*:\s*(\d+)", text)
    if match:
        return int(match.group(2)) > 0
    return event.event_type.lower() == "rebound" and not _is_offensive_rebound(event)


def _is_live_start(event: Event) -> bool:
    return (_is_defensive_rebound(event) or _is_steal(event)
            or _contains(event, ("jump ball", "period start")))


def _is_terminal(event: Event) -> bool:
    return (_contains(event, ("made shot", "turnover", "offensive foul", "violation"))
            or _is_defensive_rebound(event)
            or _contains(event, ("period end", "end period", "quarter end")))


def build_possessions(events: list[Event]) -> list[tuple[Event, Event, float]]:
    """Group CSV actions into possessions; return start event, end event, lead."""
    events = sorted(events, key=lambda event: event.video_time)
    possessions: list[tuple[Event, Event, float]] = []
    current: list[Event] = []
    for event in events:
        if current and event.period != current[-1].period:
            possessions.append((current[0], current[-1], 1.5))
            current = []
        if not current:
            current = [event]
        else:
            current.append(event)
        if _is_terminal(event):
            possessions.append((current[0], current[-1], 1.5))
            current = []
    if current:
        possessions.append((current[0], current[-1], 1.5))
    return possessions


def possession_segments(events: list[Event], video_duration: float,
                        normal_lead: float = 5.5, live_lead: float = 1.0,
                        tail: float = 2.5) -> list[Segment]:
    segments = []
    for start_event, end_event, _ in build_possessions(events):
        lead = live_lead if _is_live_start(start_event) else normal_lead
        start = max(0.0, start_event.video_time - lead)
        end = min(video_duration, end_event.video_time + tail)
        if end > start + 0.25:
            segments.append(Segment(start, end))
    merged = []
    for segment in segments:
        if merged and segment.start <= merged[-1].end:
            merged[-1] = Segment(merged[-1].start, max(merged[-1].end, segment.end))
        else:
            merged.append(segment)
    return merged


def build_segments(events: list[Event], video_duration: float,
                   lead_seconds: float = 5.5, tail_seconds: float = 2.5) -> list[Segment]:
    """Build chronological possession windows from the NBA event chain."""
    starts: list[tuple[Event, float]] = []
    for index, event in enumerate(events):
        previous = events[index - 1] if index else None
        if _contains(event, INBOUND_TERMS):
            starts.append((event, lead_seconds))
        elif _is_live_start(event):
            starts.append((event, 1.5))
        elif previous is None or event.period != previous.period:
            starts.append((event, 1.5))
    starts.sort(key=lambda item: item[0].video_time)
    segments: list[Segment] = []
    for index, (start_event, lead) in enumerate(starts):
        start = start_event.video_time + lead
        next_start_time = starts[index + 1][0].video_time if index + 1 < len(starts) else video_duration
        relevant = [event for event in events
                    if start_event.video_time < event.video_time < next_start_time
                    and event.period == start_event.period
                    and not _is_offensive_rebound(event)]
        terminal = relevant[-1] if relevant else None
        # Free throws and fouls stay inside the current window. The next live
        # start or the next recorded event closes the previous possession.
        end = (terminal.video_time + tail_seconds) if terminal else (next_start_time - lead)
        start = max(0.0, min(start, video_duration))
        end = max(0.0, min(end, video_duration))
        if end > start + 0.25:
            segments.append(Segment(start, end))

    merged: list[Segment] = []
    for segment in segments:
        if merged and segment.start <= merged[-1].end:
            merged[-1] = Segment(merged[-1].start, max(merged[-1].end, segment.end))
        else:
            merged.append(segment)
    return merged


def probe_duration(video_path: str | Path) -> float:
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", str(video_path)],
        capture_output=True, text=True, check=True,
    )
    return float(result.stdout.strip())


def render(video_path: str | Path, output_path: str | Path, segments: list[Segment],
           progress: Callable[[str], None] | None = None) -> None:
    if not segments:
        raise ValueError("Video icin hic segment bulunamadi.")
    inputs: list[str] = []
    filters: list[str] = []
    for index, segment in enumerate(segments):
        filters.append(
            f"[0:v]trim=start={segment.start:.3f}:end={segment.end:.3f},setpts=PTS-STARTPTS[v{index}];"
            f"[0:a]atrim=start={segment.start:.3f}:end={segment.end:.3f},asetpts=PTS-STARTPTS[a{index}]"
        )
        inputs.extend([f"[v{index}]", f"[a{index}]"])
    filters.append("".join(inputs) + f"concat=n={len(segments)}:v=1:a=1[outv][outa]")
    command = ["ffmpeg", "-y", "-i", str(video_path), "-filter_complex", ";".join(filters),
               "-map", "[outv]", "-map", "[outa]", "-c:v", "libx264", "-preset", "veryfast",
               "-crf", "20", "-c:a", "aac", "-b:a", "160k", "-movflags", "+faststart", str(output_path)]
    if progress:
        progress(f"{len(segments)} segment birlestiriliyor...")
    result = subprocess.run(command, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "FFmpeg bilinmeyen bir hata ile durdu.").strip()
        raise RuntimeError(f"FFmpeg hata kodu {result.returncode}:\n{detail[-4000:]}")
