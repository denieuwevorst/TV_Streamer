#!/usr/bin/env python3
"""TV Streamer v2 - MPEG-TS, HLS of beide."""

import configparser
import html
import json
import logging
import platform
import queue
import random
import re
import shutil
import subprocess
import threading

from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote
from xml.etree import ElementTree as ET

BASE_DIR = Path(__file__).resolve().parent
CONFIG_FILE = BASE_DIR / "settings.txt"

VIDEO_EXTENSIONS = {
    ".mp4", ".mkv", ".avi", ".mov", ".m4v", ".ts",
    ".webm", ".mpeg", ".mpg", ".wmv", ".flv", ".3gp"
}

CHUNK_SIZE = 64 * 1024
CLIENT_QUEUE_CHUNKS = 256


def _norm(path):
    """Uniforme padnotatie, zodat FFmpeg-logregels en bestanden matchen."""
    return str(Path(path).resolve()).replace("\\", "/")


def build_xmltv(settings, schedule):
    fmt = "%Y%m%d%H%M%S +0000"
    channel_id = settings["channel_id"]

    tv = ET.Element("tv", {"generator-info-name": "tv_streamer"})
    channel = ET.SubElement(tv, "channel", {"id": channel_id})
    ET.SubElement(channel, "display-name").text = settings["page_title"]

    for start, stop, title in schedule:
        programme = ET.SubElement(tv, "programme", {
            "start": start.strftime(fmt),
            "stop": stop.strftime(fmt),
            "channel": channel_id,
        })
        ET.SubElement(programme, "title", {"lang": "nl"}).text = title

    return (
        b'<?xml version="1.0" encoding="UTF-8"?>\n'
        b'<!DOCTYPE tv SYSTEM "xmltv.dtd">\n'
        + ET.tostring(tv, encoding="utf-8")
    )


def prescale_logo(logo_file, width):
    """Schaal het logo eenmalig, zodat dit niet per frame hoeft."""
    out = BASE_DIR / "logo_scaled.png"

    try:
        subprocess.run(
            [
                "ffmpeg", "-y", "-v", "error",
                "-i", str(logo_file),
                "-vf", f"scale={width}:-2",
                "-frames:v", "1",
                str(out),
            ],
            check=True,
            timeout=30,
        )
        return out

    except Exception:
        logging.warning("Logo vooraf schalen mislukt; origineel gebruikt.")
        return logo_file


def load_settings():
    config = configparser.ConfigParser()

    if not CONFIG_FILE.exists():
        raise FileNotFoundError(
            f"Configuratiebestand niet gevonden: {CONFIG_FILE}"
        )

    config.read(CONFIG_FILE, encoding="utf-8")
    s = config["stream"]
    web = config["web"] if config.has_section("web") else {}
    logo = config["logo"] if config.has_section("logo") else {}
    logo_file = (BASE_DIR / logo.get("file", "./logo.png")).resolve()
    logo_enabled = (
        str(logo.get("enabled", "true")).strip().lower()
        in {"1", "true", "yes", "on"}
    )
    if logo_enabled and not logo_file.is_file():
        logging.warning("Logo niet gevonden: %s", logo_file)
        logo_enabled = False

    logo_width = int(logo.get("width", "160"))
    logo_overlay_file = logo_file
    if logo_enabled:
        logo_overlay_file = prescale_logo(logo_file, logo_width)

    mode = s.get("output_mode", "both").strip().lower()
    title_mode = web.get("current_title_mode", "metadata").strip().lower()
    if title_mode not in {"none", "filename", "metadata"}:
        raise ValueError("current_title_mode moet none, filename of metadata zijn.")

    hwaccel = s.get("hwaccel", "none").strip().lower()
    if hwaccel not in {"none", "qsv", "vaapi"}:
        raise ValueError("hwaccel moet none, qsv of vaapi zijn.")

    if mode not in {"web", "stream", "both"}:
        raise ValueError(
            "output_mode moet web, stream of both zijn."
        )

    return {
        "logo_enabled": logo_enabled,
        "logo_file": logo_file,
        "logo_overlay_file": logo_overlay_file,
        "logo_width": logo_width,
        "logo_margin": int(logo.get("margin", "20")),
        "page_title": web.get("page_title", "DTV").strip() or "DTV",
        "current_title_mode": title_mode,
        "channel_id": re.sub(
            r"[^A-Za-z0-9._-]", "",
            web.get("channel_id", "dtv").strip()
        ) or "dtv",
        "epg_hours": max(1, int(web.get("epg_hours", "24"))),
        "folder": Path(
            s.get("folder", "./videos")
        ).expanduser().resolve(),

        "host": s.get("host", "0.0.0.0"),
        "port": s.getint("port", 8080),
        "mode": mode,
        "shuffle": s.getboolean("shuffle", False),

        "width": s.getint("width", 1280),
        "height": s.getint("height", 720),

        "video_bitrate": s.get(
            "video_bitrate", "2500k"
        ),

        "audio_bitrate": s.get(
            "audio_bitrate", "128k"
        ),

        "fps": s.getint("fps", 25),
        "preset": s.get("preset", "veryfast"),
        "hwaccel": hwaccel,
        "gpu_filters": s.getboolean("gpu_filters", False),
        "qsv_preset": s.get("qsv_preset", "veryfast").strip(),
        "qsv_device": s.get(
            "qsv_device",
            "" if platform.system() == "Windows" else "/dev/dri/renderD128"
        ).strip(),

        "hls_time": s.getint("hls_time", 4),
        "hls_list_size": s.getint(
            "hls_list_size", 8
        ),

        "hls_dir": (
            BASE_DIR / s.get("hls_dir", "hls")
        ).resolve(),
    }


SHUFFLE_ROUNDS = 50


def build_sequence(files, shuffle):
    """Afspeelvolgorde. Met shuffle: meerdere willekeurige rondes achter
    elkaar, zodat iedere ronde een andere volgorde heeft."""
    if not shuffle:
        return list(files)

    sequence = []

    for _ in range(SHUFFLE_ROUNDS):
        order = list(files)
        random.shuffle(order)

        # Voorkom dat hetzelfde bestand twee keer achter elkaar komt.
        if sequence and len(order) > 1 and order[0] == sequence[-1]:
            order[0], order[-1] = order[-1], order[0]

        sequence.extend(order)

    return sequence


def make_concat_file(folder, playlist, shuffle=False):
    files = sorted(
        (
            p for p in folder.iterdir()
            if p.is_file()
            and p.suffix.lower() in VIDEO_EXTENSIONS
        ),
        key=lambda p: p.name.lower()
    )

    if not files:
        raise RuntimeError(
            f"Geen videobestanden gevonden in: {folder}"
        )

    sequence = build_sequence(files, shuffle)

    lines = []

    for path in sequence:
        value = str(path.resolve())
        value = value.replace("\\", "/")
        value = value.replace("'", "'\\''")

        lines.append(f"file '{value}'")

    playlist.write_text(
        "\n".join(lines) + "\n",
        encoding="utf-8"
    )

    return files, sequence


class Broadcaster:

    def __init__(self, settings):
        self.s = settings
        self.clients = set()
        self.lock = threading.Lock()
        self.stop_event = threading.Event()
        self.process = None
        self.current_file = ""
        self.current_label = ""
        self.files = []
        self.sequence = []
        self.seq_keys = []
        self.info = {}
        self.info_ready = threading.Event()
        self.anchor = None
        self.started_at = datetime.now(timezone.utc)

    def subscribe(self):
        q = queue.Queue(
            maxsize=CLIENT_QUEUE_CHUNKS
        )

        with self.lock:
            self.clients.add(q)

        return q

    def unsubscribe(self, q):
        with self.lock:
            self.clients.discard(q)

    def publish(self, data):
        stale = []

        with self.lock:
            for q in self.clients:
                try:
                    q.put_nowait(data)
                except queue.Full:
                    stale.append(q)

            for q in stale:
                self.clients.discard(q)

                try:
                    while True:
                        q.get_nowait()
                except queue.Empty:
                    pass

                q.put_nowait(None)

    def ffmpeg_command(self, playlist):
        s = self.s

        base_filter = (
            "setsar=1,"
            f"scale={s['width']}:{s['height']}:"
            "force_original_aspect_ratio=decrease:force_divisible_by=2,"
            f"pad={s['width']}:{s['height']}:"
            "(ow-iw)/2:(oh-ih)/2:color=black,"
            "setsar=1,"
            f"fps={s['fps']}"
        )

        cmd = [
            "ffmpeg",
            "-hide_banner",
            "-loglevel", "info",
        ]

        if s["hwaccel"] == "vaapi":
            cmd += [
                "-init_hw_device", f"vaapi=va:{s['qsv_device']}",
                "-filter_hw_device", "va",
            ]
        elif s["hwaccel"] == "qsv":
            device = s["qsv_device"]
            if device:
                # Op Linux: QSV via VAAPI op het opgegeven render-device.
                cmd += [
                    "-init_hw_device", f"vaapi=va:{device}",
                    "-init_hw_device", "qsv=hw@va",
                ]
            else:
                cmd += ["-init_hw_device", "qsv=hw"]

        gpu_chain = s["hwaccel"] == "vaapi" and s["gpu_filters"]

        cmd += [
            "-re",
            "-stream_loop", "-1",
        ]

        if gpu_chain:
            # Hardware-decode met automatische terugval op software;
            # frames komen in systeemgeheugen en gaan daarna naar de GPU.
            cmd += ["-hwaccel", "vaapi", "-hwaccel_device", "va"]

        cmd += [
            "-f", "concat",
            "-safe", "0",
            "-i", str(playlist),
        ]

        if s["hwaccel"] == "vaapi":
            tail = "format=nv12,hwupload"
        else:
            tail = "format=yuv420p"

        w, h, fps = s["width"], s["height"], s["fps"]
        center = "x=(main_w-overlay_w)/2:y=(main_h-overlay_h)/2"

        if gpu_chain:
            video = (
                f"[0:v:0]setsar=1,fps={fps},format=nv12,hwupload,"
                f"scale_vaapi=w={w}:h={h}:"
                "force_original_aspect_ratio=decrease:"
                "force_divisible_by=2[vid];"
                f"color=c=black:s={w}x{h}:r={fps},"
                "format=nv12,hwupload[bg];"
            )

            if s["logo_enabled"]:
                m = s["logo_margin"]
                cmd += [
                    "-loop", "1",
                    "-framerate", str(fps),
                    "-i", s["logo_overlay_file"].as_posix(),
                ]
                graph = (
                    video
                    + f"[bg][vid]overlay_vaapi={center}[comp];"
                    + "[1:v]format=rgba,hwupload[lg];"
                    + f"[comp][lg]overlay_vaapi=x=main_w-overlay_w-{m}:"
                    f"y={m}[v]"
                )
            else:
                graph = video + f"[bg][vid]overlay_vaapi={center}[v]"

            cmd += ["-filter_complex", graph, "-map", "[v]"]

        elif s["logo_enabled"]:
            m = s["logo_margin"]
            cmd += [
                "-loop", "1",
                "-framerate", str(fps),
                "-i", s["logo_overlay_file"].as_posix(),
                "-filter_complex",
                (
                    f"[0:v:0]{base_filter}[bg];"
                    "[1:v]format=rgba[lg];"
                    f"[bg][lg]overlay=x=main_w-overlay_w-{m}:y={m}:"
                    f"eof_action=repeat:format=auto,{tail}[v]"
                ),
                "-map", "[v]",
            ]
        else:
            cmd += [
                "-map", "0:v:0",
                "-vf", f"{base_filter},{tail}"
                if s["hwaccel"] == "vaapi" else base_filter,
            ]

        cmd += [
            "-map", "0:a:0?",

        ]

        if s["hwaccel"] == "vaapi":
            cmd += [
                "-c:v", "h264_vaapi",
                "-b:v", s["video_bitrate"],
            ]
        elif s["hwaccel"] == "qsv":
            cmd += [
                "-c:v", "h264_qsv",
                "-preset", s["qsv_preset"],
                "-b:v", s["video_bitrate"],
                "-pix_fmt", "nv12",
            ]
        else:
            cmd += [
                "-c:v", "libx264",
                "-preset", s["preset"],
                "-b:v", s["video_bitrate"],
                "-pix_fmt", "yuv420p",
            ]

        cmd += [

            "-c:a", "aac",
            "-b:a", s["audio_bitrate"],
            "-ac", "2",
            "-ar", "48000",
        ]

        mode = s["mode"]

        if mode == "stream":

            cmd += [
                "-mpegts_flags",
                "+resend_headers",
                "-f", "mpegts",
                "pipe:1"
            ]

        elif mode == "web":

            cmd += [
                "-f", "hls",
                "-hls_time", str(s["hls_time"]),
                "-hls_list_size", str(
                    s["hls_list_size"]
                ),

                "-hls_flags",
                "delete_segments+append_list+independent_segments",

                "-hls_segment_filename",
                str(
                    s["hls_dir"] / "segment_%06d.ts"
                ),

                str(
                    s["hls_dir"] / "stream.m3u8"
                )
            ]

        else:
            hls_slave = (
                f"[f=hls:hls_time={s['hls_time']}:"
                f"hls_list_size={s['hls_list_size']}:"
                "hls_flags=delete_segments+append_list+"
                "independent_segments:"
                f"hls_segment_filename={s['hls_dir']}/"
                "segment_%06d.ts]"
                f"{s['hls_dir']}/stream.m3u8"
            )

            cmd += [
                "-mpegts_flags", "+resend_headers",
                "-f", "tee",
                f"[f=mpegts]pipe:1|{hls_slave}"
            ]

        return cmd

    def run_ffmpeg(self):
        playlist = BASE_DIR / "playlist.txt"

        while not self.stop_event.is_set():

            try:
                files, sequence = make_concat_file(
                    self.s["folder"],
                    playlist,
                    self.s["shuffle"]
                )

                logging.info(
                    "%d videobestand(en) gevonden.",
                    len(files)
                )

                self.files = files
                self.sequence = sequence
                self.seq_keys = [_norm(p) for p in sequence]
                self.anchor = None
                self.started_at = datetime.now(timezone.utc)
                self.info_ready.clear()

                threading.Thread(
                    target=self._scan_files,
                    args=(files,),
                    daemon=True
                ).start()

                self.s["hls_dir"].mkdir(
                    parents=True,
                    exist_ok=True
                )

                for p in self.s["hls_dir"].glob(
                    "segment_*.ts"
                ):
                    p.unlink(missing_ok=True)

                (
                    self.s["hls_dir"] / "stream.m3u8"
                ).unlink(missing_ok=True)

                cmd = self.ffmpeg_command(playlist)

                logging.info(
                    "FFmpeg gestart in modus '%s'.",
                    self.s["mode"]
                )

                has_pipe_stream = (
                    self.s["mode"] in {"stream", "both"}
                )

                self.process = subprocess.Popen(
                    cmd,

                    stdout=(
                        subprocess.PIPE
                        if has_pipe_stream
                        else subprocess.DEVNULL
                    ),

                    stderr=subprocess.PIPE,
                    bufsize=0
                )

                threading.Thread(
                    target=self._log_ffmpeg,
                    args=(self.process.stderr,),
                    daemon=True
                ).start()

                if has_pipe_stream:

                    while not self.stop_event.is_set():
                        chunk = self.process.stdout.read(
                            CHUNK_SIZE
                        )

                        if not chunk:
                            break

                        self.publish(chunk)

                else:

                    while (
                        not self.stop_event.is_set()
                        and self.process.poll() is None
                    ):
                        self.stop_event.wait(0.5)

                if self.process.poll() is None:
                    self.process.terminate()

                    try:
                        self.process.wait(timeout=5)

                    except subprocess.TimeoutExpired:
                        self.process.kill()

                logging.warning(
                    "FFmpeg gestopt; nieuwe poging over 5 seconden."
                )

            except Exception:
                logging.exception("Streamfout")

            finally:
                self.process = None

            self.stop_event.wait(5)

    def _locate(self, key):
        """Zoek vanaf de laatst bekende positie vooruit (met wrap-around)
        naar het bestand dat ffmpeg net opende."""
        keys = self.seq_keys
        n = len(keys)

        if n == 0:
            return None

        start = self.anchor[0] if self.anchor else 0

        for offset in range(n):
            i = (start + offset) % n
            if keys[i] == key:
                return i

        return None

    def _duration_for_file(self, path):
        ffprobe = shutil.which("ffprobe")
        if not ffprobe:
            return None

        try:
            result = subprocess.run(
                [ffprobe, "-v", "error",
                 "-show_entries", "format=duration",
                 "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
                capture_output=True, text=True, timeout=30, check=False
            )
            return float(result.stdout.strip())
        except (OSError, subprocess.SubprocessError, ValueError):
            return None

    def _scan_files(self, files):
        if not shutil.which("ffprobe"):
            logging.warning(
                "ffprobe niet gevonden; EPG (/epg.xml) is niet beschikbaar."
            )

        info = {}

        for p in files:
            if self.stop_event.is_set():
                return

            duration = self._duration_for_file(p) or 0.0
            label = self._label_for_file(str(p))

            if not label or label == p.name:
                label = p.stem

            info[_norm(p)] = (duration, label)

        if self.files is files:
            self.info = info
            self.info_ready.set()
            logging.info(
                "EPG-gegevens ingelezen voor %d bestand(en).", len(info)
            )

    def _label_for_file(self, filename):
        mode = self.s.get("current_title_mode", "metadata")
        if mode == "none":
            return ""

        fallback = Path(filename).name
        if mode == "filename":
            return fallback

        ffprobe = shutil.which("ffprobe")
        if not ffprobe:
            logging.warning("ffprobe niet gevonden; bestandsnaam wordt gebruikt.")
            return fallback

        try:
            result = subprocess.run(
                [ffprobe, "-v", "error",
                 "-show_entries", "format_tags=title:stream_tags=title",
                 "-of", "json", filename],
                capture_output=True, text=True, timeout=10, check=False
            )
            if result.returncode == 0:
                data = json.loads(result.stdout or "{}")
                title = data.get("format", {}).get("tags", {}).get("title")
                if not title:
                    for stream in data.get("streams", []):
                        title = stream.get("tags", {}).get("title")
                        if title:
                            break
                if title and str(title).strip():
                    return str(title).strip()
        except (OSError, subprocess.SubprocessError, json.JSONDecodeError) as exc:
            logging.warning("Metadata uitlezen mislukt voor %s: %s", fallback, exc)

        return fallback

    def _log_ffmpeg(self, stderr):
        # De concat-demuxer meldt ieder nieuw geopend videobestand op info-niveau.
        pattern = re.compile(r"Opening ['\"](.+?)['\"] for reading")
        for raw in iter(stderr.readline, b""):
            line = raw.decode("utf-8", errors="replace").strip()
            match = pattern.search(line)
            if match:
                self.current_file = match.group(1)
                position = self._locate(_norm(match.group(1)))
                if position is not None:
                    self.anchor = (position, datetime.now(timezone.utc))
                self.current_label = self._label_for_file(self.current_file)
                logging.info("Nu afgespeeld: %s", Path(self.current_file).name)
            if line:
                logging.info("FFmpeg: %s", line)

    def build_schedule(self):
        """Lijst van (start, stop, titel), gerekend vanaf het laatst
        geopende bestand en de duur van alle bestanden daarna."""
        if not self.info_ready.is_set() or not self.files:
            return None

        entries = [
            self.info.get(_norm(p), (0.0, p.stem))
            for p in self.sequence
        ]

        if sum(d for d, _ in entries) <= 0:
            return None

        index, start = self.anchor or (0, self.started_at)
        end = (
            datetime.now(timezone.utc)
            + timedelta(hours=self.s["epg_hours"])
        )

        schedule = []

        while start < end:
            duration, label = entries[index]

            if duration > 0:
                stop = start + timedelta(seconds=duration)
                schedule.append((start, stop, label))
                start = stop

            index = (index + 1) % len(entries)

        return schedule

    def stop(self):
        self.stop_event.set()

        if (
            self.process
            and self.process.poll() is None
        ):
            self.process.terminate()


class StreamHandler(BaseHTTPRequestHandler):

    protocol_version = "HTTP/1.1"

    broadcaster = None
    settings = None

    def do_GET(self):

        mode = self.settings["mode"]

        if self.path in ("/", "/index.html"):

            page_title = self.settings.get("page_title", "DTV")
            safe_title = html.escape(page_title)
            js_title = json.dumps(page_title, ensure_ascii=False)

            if mode == "stream":
                lib = '<script src="https://cdn.jsdelivr.net/npm/mpegts.js@1.7.3/dist/mpegts.js"></script>'
                start_js = """
if (window.mpegts && mpegts.getFeatureList().mseLivePlayback) {
  const p = mpegts.createPlayer({type: 'mpegts', isLive: true, url: '/tv'});
  p.attachMediaElement(video);
  p.load();
  start();
} else {
  document.body.insertAdjacentHTML('beforeend',
    '<p>Deze browser kan de stream niet afspelen. Open <a href="/tv">/tv</a> in VLC.</p>');
}"""
            else:
                lib = '<script src="https://cdn.jsdelivr.net/npm/hls.js@1.5.17/dist/hls.min.js"></script>'
                start_js = """
const src = '/hls/stream.m3u8';
if (window.Hls && Hls.isSupported()) {
  const h = new Hls({liveSyncDurationCount: 3});
  h.loadSource(src);
  h.attachMedia(video);
  h.on(Hls.Events.MANIFEST_PARSED, start);
} else if (video.canPlayType('application/vnd.apple.mpegurl')) {
  video.src = src;
  video.addEventListener('loadedmetadata', start);
} else {
  document.body.insertAdjacentHTML('beforeend', '<p>Deze browser ondersteunt HLS niet.</p>');
}"""

            page = """<!doctype html>
<html lang="nl"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>__TITLE__</title>
<style>
body { margin:0; background:#111; color:#eee; font:16px Arial;
  display:grid; place-items:center; min-height:100vh; }
main { width:min(960px,96vw); position:relative; }
video { width:100%; background:#000; display:block; }
#unmute { position:absolute; top:12px; right:12px; padding:10px 16px;
  background:rgba(0,0,0,.75); color:#fff; border:1px solid #888;
  border-radius:6px; font:inherit; cursor:pointer; display:none; }
</style></head><body><main>
<video id="player" controls autoplay muted playsinline></video>
<button id="unmute">🔇 Klik voor geluid</button>
</main>
__LIB__
<script>
const video = document.getElementById('player');
const unmuteBtn = document.getElementById('unmute');
const baseTitle = __JSTITLE__;

function refreshButton() {
  unmuteBtn.style.display = (video.muted || video.paused) ? 'block' : 'none';
  unmuteBtn.textContent = video.paused ? '▶ Klik om af te spelen' : '🔇 Klik voor geluid';
}

function start() {
  video.muted = true;               // muted autoplay is always allowed
  const p = video.play();
  if (p) p.catch(refreshButton);
  refreshButton();
}

unmuteBtn.addEventListener('click', () => {
  video.muted = false;
  video.play();
  refreshButton();
});
video.addEventListener('volumechange', refreshButton);
video.addEventListener('play', refreshButton);
video.addEventListener('pause', refreshButton);

async function updateTitle() {
  try {
    const response = await fetch('/status', {cache: 'no-store'});
    if (!response.ok) return;
    const status = await response.json();
    document.title = status.label ? baseTitle + ' - ' + status.label : baseTitle;
  } catch (error) {}
}
updateTitle(); setInterval(updateTitle, 2000);

__START__
</script></body></html>"""

            page = (page.replace("__TITLE__", safe_title)
                        .replace("__LIB__", lib)
                        .replace("__JSTITLE__", js_title)
                        .replace("__START__", start_js))

            body = page.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        if self.path == "/playlist.m3u":
            host = (
                self.headers.get("Host")
                or f"localhost:{self.settings['port']}"
            )
            base = f"http://{host}"
            url = base + (
                "/tv" if mode in {"stream", "both"}
                else "/hls/stream.m3u8"
            )
            channel_id = self.settings["channel_id"]
            name = self.settings["page_title"].replace('"', "'")

            logo_attr = (
                f' tvg-logo="{base}/logo.png"'
                if self.settings["logo_enabled"] else ""
            )

            text = (
                f'#EXTM3U x-tvg-url="{base}/epg.xml"\n'
                f'#EXTINF:-1 tvg-id="{channel_id}" '
                f'tvg-name="{name}"{logo_attr} group-title="TV",{name}\n'
                f"{url}\n"
            )
            self.send_bytes(
                text.encode("utf-8"),
                "audio/x-mpegurl; charset=utf-8"
            )
            return

        if self.path == "/logo.png" and self.settings["logo_enabled"]:
            self.send_bytes(
                self.settings["logo_file"].read_bytes(),
                "image/png"
            )
            return

        if self.path == "/epg.xml":
            schedule = self.broadcaster.build_schedule()

            if schedule is None:
                self.send_error(
                    503,
                    "EPG nog niet beschikbaar (ffprobe nodig)"
                )
                return

            self.send_bytes(
                build_xmltv(self.settings, schedule),
                "application/xml; charset=utf-8"
            )
            return

        if self.path == "/status":
            payload = json.dumps(
                {"label": self.broadcaster.current_label},
                ensure_ascii=False
            ).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return

        if self.path == "/tv":

            if mode not in {"stream", "both"}:
                self.send_error(
                    404,
                    "MPEG-TS-uitvoer staat uit"
                )
                return

            self.stream_mpegts()
            return

        if self.path.startswith("/hls/"):

            if mode not in {"web", "both"}:
                self.send_error(
                    404,
                    "Web/HLS-uitvoer staat uit"
                )
                return

            self.serve_hls()
            return

        self.send_error(404)

    def send_bytes(self, body, content_type):
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def stream_mpegts(self):

        q = self.broadcaster.subscribe()

        self.send_response(200)

        self.send_header(
            "Content-Type",
            "video/mp2t"
        )

        self.send_header(
            "Cache-Control",
            "no-cache, no-store"
        )

        self.send_header("Connection", "close")
        self.end_headers()

        logging.info(
            "MPEG-TS-client verbonden: %s",
            self.client_address
        )

        try:
            while True:

                try:
                    data = q.get(timeout=30)

                except queue.Empty:
                    if self.broadcaster.stop_event.is_set():
                        break
                    continue

                if data is None:
                    break

                self.wfile.write(data)
                self.wfile.flush()

        except (
            BrokenPipeError,
            ConnectionResetError,
            OSError
        ):
            pass

        finally:
            self.broadcaster.unsubscribe(q)
            self.close_connection = True

            logging.info(
                "MPEG-TS-client verbroken: %s",
                self.client_address
            )

    def serve_hls(self):

        relative = unquote(
            self.path[len("/hls/"):]
        )

        if (
            not relative
            or Path(relative).name != relative
            or relative in {".", ".."}
        ):
            self.send_error(404)
            return

        file_path = (
            self.settings["hls_dir"] / relative
        )

        if not file_path.is_file():
            self.send_error(
                404,
                "HLS-bestand nog niet beschikbaar"
            )
            return

        content_type = (
            "application/vnd.apple.mpegurl"
            if file_path.suffix == ".m3u8"
            else "video/mp2t"
        )

        data = file_path.read_bytes()

        self.send_response(200)

        self.send_header(
            "Content-Type",
            content_type
        )

        self.send_header(
            "Content-Length",
            str(len(data))
        )

        self.send_header(
            "Cache-Control",
            "no-cache"
            if file_path.suffix == ".m3u8"
            else "public, max-age=30"
        )

        self.end_headers()
        self.wfile.write(data)

    def log_message(self, fmt, *args):
        logging.info(
            "HTTP %s - %s",
            self.client_address[0],
            fmt % args
        )


def main():

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s"
    )

    s = load_settings()

    if shutil.which("ffmpeg") is None:
        raise SystemExit(
            "FFmpeg niet gevonden. "
            "Installeer FFmpeg en voeg het toe aan PATH."
        )

    if not s["folder"].is_dir():
        raise SystemExit(
            f"Videomap bestaat niet: {s['folder']}"
        )

    if not 1 <= s["port"] <= 65535:
        raise SystemExit(
            "Poort moet tussen 1 en 65535 liggen."
        )

    if (
        s["width"] < 2
        or s["height"] < 2
        or s["fps"] < 1
    ):
        raise SystemExit(
            "Resolutie en fps moeten positief zijn."
        )

    broadcaster = Broadcaster(s)

    StreamHandler.broadcaster = broadcaster
    StreamHandler.settings = s

    server = ThreadingHTTPServer(
        (s["host"], s["port"]),
        StreamHandler
    )

    server.daemon_threads = True

    threading.Thread(
        target=broadcaster.run_ffmpeg,
        daemon=True
    ).start()

    display_host = (
        "localhost"
        if s["host"] in ("0.0.0.0", "::")
        else s["host"]
    )

    logging.info(
        "Uitvoermodus: %s",
        s["mode"]
    )

    logging.info(
        "Webpagina: http://%s:%d/",
        display_host,
        s["port"]
    )

    if s["mode"] in {"stream", "both"}:
        logging.info(
            "MPEG-TS: http://%s:%d/tv",
            display_host,
            s["port"]
        )

    if s["mode"] in {"web", "both"}:
        logging.info(
            "HLS-playlist: http://%s:%d/hls/stream.m3u8",
            display_host,
            s["port"]
        )

    logging.info(
        "M3U-playlist: http://%s:%d/playlist.m3u",
        display_host,
        s["port"]
    )

    logging.info(
        "XMLTV: http://%s:%d/epg.xml",
        display_host,
        s["port"]
    )

    try:
        server.serve_forever()

    except KeyboardInterrupt:
        logging.info("Afsluiten...")

    finally:
        broadcaster.stop()
        server.shutdown()
        server.server_close()


if __name__ == "__main__":
    main()
