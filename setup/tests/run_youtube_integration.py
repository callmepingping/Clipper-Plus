"""Live, unauthenticated YouTube integration test. Run explicitly, not in unit CI."""
from importlib.metadata import version
from pathlib import Path
import shutil
import subprocess
import tempfile
import threading

import av
from faster_whisper.audio import decode_audio
import yt_dlp

from videomind.downloader import classify_download_error, yt_dlp_options
from videomind.transcription import _safe_diagnostic, transcribe_video

VIDEO_URL = "https://www.youtube.com/watch?v=jNQXAC9IVRw"


class QuietLogger:
    def __init__(self):
        self.ejs_missing = False
        self.challenge_failed = False
        self.runtime_missing = False

    def _record(self, message):
        text = str(message).lower()
        self.ejs_missing |= "ejs" in text and any(term in text for term in ("missing", "not installed", "not available"))
        self.challenge_failed |= "challenge" in text and any(term in text for term in ("failed", "could not", "unable"))
        self.runtime_missing |= "javascript runtime" in text and any(term in text for term in ("no supported", "missing", "not available"))

    def debug(self, message): self._record(message)
    def warning(self, message): self._record(message)
    def error(self, message): self._record(message)

    def report(self):
        print("yt-dlp diagnostics: EJS missing=%s; challenge failed=%s; runtime missing=%s" %
              (self.ejs_missing, self.challenge_failed, self.runtime_missing))


def main():
    print("yt-dlp", version("yt-dlp"), "yt-dlp-ejs", version("yt-dlp-ejs"), "PyAV", av.__version__)
    runtime = shutil.which("deno")
    if not runtime:
        raise SystemExit("FAIL: Deno executable is unavailable to this Python process.")
    version_result = subprocess.run([runtime, "--version"], capture_output=True, text=True, timeout=10)
    if version_result.returncode or tuple(map(int, version_result.stdout.split()[1].split(".")[:3])) < (2, 3, 0):
        raise SystemExit("FAIL: Deno is unavailable or older than 2.3.0.")
    print("Deno", version_result.stdout.splitlines()[0], runtime)
    options = yt_dlp_options({"js_runtime_path": runtime})
    diagnostics = QuietLogger()
    options["logger"] = diagnostics
    options["extractor_args"] = {"youtube": {"player_client": ["default"]}}
    with yt_dlp.YoutubeDL(options) as ydl:
        ydl._js_runtimes  # Force yt-dlp to probe the configured executable.
        info = ydl.extract_info(VIDEO_URL, download=False)
        print("Metadata extracted:", bool(info.get("id")), "title available:", bool(info.get("title")))
        formats = [item for item in info.get("formats", []) if item.get("acodec") not in (None, "none") and item.get("url")]
        if not formats:
            raise SystemExit("FAIL: no audio-only format is available.")
        print("Audio formats:", len(formats), "challenge runtime:", ydl._js_runtimes["deno"].info.version)
        diagnostics.report()
        with tempfile.TemporaryDirectory(prefix="videomind-youtube-") as temp:
            options.update({"format": "bestaudio/best", "outtmpl": str(Path(temp) / "audio.%(ext)s"),
                            "postprocessors": [{"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "128"}],
                            "logger": diagnostics})
            try:
                with yt_dlp.YoutubeDL(options) as downloader:
                    downloader.extract_info(VIDEO_URL, download=True)
            except Exception:
                diagnostics.report()
                raise
            mp3 = Path(temp) / "audio.mp3"
            if not mp3.is_file():
                raise SystemExit("FAIL: yt-dlp/FFmpeg did not produce an MP3.")
            print("MP3 downloaded and converted:", mp3.stat().st_size, "bytes")
            decoded = decode_audio(str(mp3), sampling_rate=16000)
            print("PyAV MP3 decode:", len(decoded), "samples")
    print("PASS: live metadata, audio format, download, MP3 conversion, and PyAV decoding.")

    # Exercise the exact spawned-worker pipeline used by the Windows backend,
    # including service_config propagation, progress IPC, MP3 download and Whisper.
    with tempfile.TemporaryDirectory(prefix="videomind-full-pipeline-") as temp_root:
        result = transcribe_video(
            VIDEO_URL,
            {},
            {"model": "base", "device": "cpu", "compute_type": "int8"},
            lambda *args: None,
            threading.Event(),
            temp_root=temp_root,
            service_config={"js_runtime_path": runtime},
        )
        if not result.get("segments") or not result.get("markdown", "").strip():
            raise SystemExit("FAIL: the complete VideoMind pipeline returned no transcript.")
        print("Full VideoMind pipeline: title available=%s; transcript segments=%s; Markdown characters=%s" %
              (bool(result.get("title")), len(result["segments"]), len(result["markdown"])))
        if list(Path(temp_root).iterdir()):
            raise SystemExit("FAIL: temporary audio was not cleaned after transcription.")
    print("PASS: complete VideoMind YouTube-to-Whisper pipeline.")


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception as exc:
        # Never print yt-dlp's raw exception: it can contain the target URL.
        safe = classify_download_error(exc)
        diagnostic = _safe_diagnostic(exc)
        raise SystemExit(f"FAIL: {safe.code}: {safe.message} ({type(exc).__name__}). Safe diagnostic: {diagnostic}. Network/provider restrictions may be external to VideoMind.") from None
