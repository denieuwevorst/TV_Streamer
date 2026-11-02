
#!/usr/bin/env python3
"""TV Streamer v2 - MPEG-TS, HLS of beide."""

import configparser
import html
import json
import logging
import queue
import re
import shutil
import subprocess
import threading

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote

BASE_DIR = Path(__file__).resolve().parent
CONFIG_FILE = BASE_DIR / "settings.txt"

VIDEO_EXTENSIONS = {
    ".mp4", ".mkv", ".avi", ".mov", ".m4v", ".ts",
    ".webm", ".mpeg", ".mpg", ".wmv", ".flv", ".3gp"
}

CHUNK_SIZE = 64 * 1024
CLIENT_QUEUE_CHUNKS = 256


def load_settings():
    config = configparser.ConfigParser()

    if not CONFIG_FILE.exists():
        raise FileNotFoundError(
            f"Configuratiebestand niet gevonden: {CONFIG_FILE}"
        )

    config.read(CONFIG_FILE, encoding="utf-8")
    s = config["stream"]
    web = config["web"] if config.has_section("web") else {}

    mode = s.get("output_mode", "both").strip().lower()
    title_mode = web.get("current_title_mode", "metadata").strip().lower()
    if title_mode not in {"none", "filename", "metadata"}:
        raise ValueError("current_title_mode moet none, filename of metadata zijn.")

    if mode not in {"web", "stream", "both"}:
        raise ValueError(
            "output_mode moet web, stream of both zijn."
        )

    return {
        "page_title": web.get("page_title", "DTV").strip() or "DTV",
        "current_title_mode": title_mode,
        "folder": Path(
            s.get("folder", "./videos")
        ).expanduser().resolve(),

        "host": s.get("host", "0.0.0.0"),
        "port": s.getint("port", 8080),
        "mode": mode,

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

        "hls_time": s.getint("hls_time", 4),
        "hls_list_size": s.getint(
            "hls_list_size", 8
        ),

        "hls_dir": (
            BASE_DIR / s.get("hls_dir", "hls")
        ).resolve(),
    }


def make_concat_file(folder, playlist):
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

    lines = []

    for path in files:
        value = str(path.resolve())
        value = value.replace("\\", "/")
        value = value.replace("'", "'\\''")

        lines.append(f"file '{value}'")

    playlist.write_text(
        "\n".join(lines) + "\n",
        encoding="utf-8"
    )

    return files


class Broadcaster:

    def __init__(self, settings):
        self.s = settings
        self.clients = set()
        self.lock = threading.Lock()
        self.stop_event = threading.Event()
        self.process = None
        self.current_file = ""
        self.current_label = ""

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

        cmd = [
            "ffmpeg",
            "-hide_banner",
            "-loglevel", "info",

            "-re",
            "-stream_loop", "-1",

            "-f", "concat",
            "-safe", "0",
            "-i", str(playlist),

            "-map", "0:v:0",
            "-map", "0:a:0?",

            "-vf",
            (
                f"scale={s['width']}:{s['height']}:"
                "force_original_aspect_ratio=decrease,"
                f"pad={s['width']}:{s['height']}:"
                "(ow-iw)/2:(oh-ih),"
                f"fps={s['fps']}"
            ),

            "-c:v", "libx264",
            "-preset", s["preset"],
            "-b:v", s["video_bitrate"],
            "-pix_fmt", "yuv420p",

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
                files = make_concat_file(
                    self.s["folder"],
                    playlist
                )

                logging.info(
                    "%d videobestand(en) gevonden.",
                    len(files)
                )

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
                self.current_label = self._label_for_file(self.current_file)
                logging.info("Nu afgespeeld: %s", Path(self.current_file).name)
            if line:
                logging.info("FFmpeg: %s", line)

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
                page = f"""<!doctype html>
<html lang="nl"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{safe_title}</title></head><body>
<p>MPEG-TS stream: <a href="/tv">/tv</a></p>
<script>
const baseTitle = {js_title};
async function updateTitle() {{
  try {{
    const response = await fetch('/status', {{cache: 'no-store'}});
    if (!response.ok) return;
    const status = await response.json();
    document.title = status.label ? baseTitle + ' - ' + status.label : baseTitle;
  }} catch (error) {{}}
}}
updateTitle(); setInterval(updateTitle, 2000);
</script></body></html>"""
            else:
                page = f"""<!doctype html>
<html lang="nl"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{safe_title}</title>
<style>
body {{ margin:0; background:#111; color:#eee; font:16px Arial;
display:grid; place-items:center; min-height:100vh; }}
main {{ width:min(960px,96vw); }}
video {{ width:100%; background:#000; }}
</style></head><body><main>
<video id="player" controls autoplay playsinline></video>
</main>
<script src="https://cdn.jsdelivr.net/npm/hls.js@latest"></script>
<script>
const video = document.getElementById('player');
const src = '/hls/stream.m3u8';
const baseTitle = {js_title};
async function updateTitle() {{
  try {{
    const response = await fetch('/status', {{cache: 'no-store'}});
    if (!response.ok) return;
    const status = await response.json();
    document.title = status.label ? baseTitle + ' - ' + status.label : baseTitle;
  }} catch (error) {{}}
}}
updateTitle(); setInterval(updateTitle, 2000);
if (video.canPlayType('application/vnd.apple.mpegurl')) {{
  video.src = src;
}} else if (window.Hls && Hls.isSupported()) {{
  const h = new Hls(); h.loadSource(src); h.attachMedia(video);
}} else {{
  video.outerHTML = '<p>Deze browser ondersteunt HLS niet.</p>';
}}
</script></body></html>"""

            body = page.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
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
