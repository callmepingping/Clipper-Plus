"""Cancellable local speech recognition for video.

yt-dlp integration follows YoutubeDL.extract_info(download=False), progress_hooks,
and FFmpegExtractAudio from the upstream API. Heavy libraries are imported only
inside worker processes. Windows job objects ensure cancellation also stops
FFmpeg descendants, rather than leaving them writing into a deleted directory.
"""
from __future__ import annotations

import datetime as dt
import errno
import ipaddress
import json
import logging
import math
import multiprocessing as mp
import os
from pathlib import Path
import re
import shutil
import signal
import socket
import tempfile
import threading
import time
from urllib.parse import urlsplit

from .errors import AppError
from .downloader import classify_download_error, yt_dlp_options

_diagnostic_log = logging.getLogger("videomind.transcription")


def _log_exception(stage, exc):
    """Log traceback frames but never exception text, arguments, URLs, or headers."""
    import traceback
    frames = "\n".join(
        f'  File "{frame.filename}", line {frame.lineno}, in {frame.name}'
        for frame in traceback.extract_tb(exc.__traceback__)
    )[-12000:]
    _diagnostic_log.error("stage=%s exception_type=%s diagnostic=%s traceback_frames=\n%s",
                          stage, type(exc).__name__, _safe_diagnostic(exc), frames)


def _safe_diagnostic(exc):
    """Keep actionable exception text while removing URLs and credential fields."""
    text = str(exc)[:4000]
    text = re.sub(r"https?://[^\s'\"<>]+", "[URL REDACTED]", text, flags=re.IGNORECASE)
    text = re.sub(r"(?i)(authorization\s*:\s*)(?:bearer\s+)?[^\s,;]+",
                  r"\1[REDACTED]", text)
    text = re.sub(r"(?i)(authorization|cookie|set-cookie|api[_-]?key|token|password)(\s*[:=]\s*)([^\s,;]+)",
                  r"\1\2[REDACTED]", text)
    text = re.sub(r"(?i)(bearer\s+)[A-Za-z0-9._~+/-]+=*", r"\1[REDACTED]", text)
    return text[:1500]


def _cancelled(event):
    if event.is_set():
        raise AppError("CANCELLED", "Task cancelled.")


def _validate_url(url):
    if not isinstance(url, str) or not url or len(url) > 8192:
        raise AppError("INVALID_URL", "Enter a valid public video URL.")
    try:
        parsed = urlsplit(url)
        if (parsed.scheme not in {"http", "https"} or not parsed.hostname
                or parsed.username is not None or parsed.password is not None
                or parsed.port not in {None, 80, 443}):
            raise ValueError()
        host = parsed.hostname.rstrip(".").lower()
        if host == "localhost" or host.endswith((".localhost", ".local", ".internal")):
            raise ValueError()
        addresses = socket.getaddrinfo(host, parsed.port or 443, type=socket.SOCK_STREAM)
        if not addresses or any(not ipaddress.ip_address(item[4][0].split("%")[0]).is_global
                                for item in addresses):
            raise ValueError()
    except socket.gaierror:
        raise AppError("NETWORK_ERROR", "The video host could not be reached.", True) from None
    except (ValueError, TypeError):
        raise AppError("INVALID_URL", "Only public HTTP or HTTPS video URLs are allowed.") from None
    return url


def _safe_error(exc, phase):
    if isinstance(exc, AppError):
        return exc
    if isinstance(exc, (ModuleNotFoundError, ImportError)):
        return AppError("SETUP_REQUIRED", "A video processing dependency is missing. Run setup.")
    if isinstance(exc, OSError) and exc.errno == errno.ENOSPC:
        return AppError("INSUFFICIENT_DISK_SPACE", "There is not enough disk space to process this video.")
    # Used only to classify; original text is never returned or logged.
    text = str(exc).lower()
    if "no space left" in text or "disk full" in text:
        return AppError("INSUFFICIENT_DISK_SPACE", "There is not enough disk space to process this video.")
    if "unsupported url" in text or "no video formats" in text:
        return AppError("UNSUPPORTED_URL", "This page does not provide a supported video.")
    if any(token in text for token in ("sign in", "login", "authentication", "private video", "401", "403")):
        return AppError("VIDEO_AUTHENTICATION_REQUIRED", "This video requires authentication or access permission.")
    if any(token in text for token in ("unavailable", "not available", "removed", "404", "geo-restricted")):
        return AppError("VIDEO_UNAVAILABLE", "This video is unavailable.")
    if "ffmpeg" in text or "ffprobe" in text:
        return AppError("FFMPEG_UNAVAILABLE", "FFmpeg and FFprobe must be installed and available on PATH.")
    if phase == "model":
        return AppError("WHISPER_MODEL_FAILED", "The Whisper model could not be downloaded or loaded. Check the model and device settings.", True)
    if phase == "transcription":
        return AppError("TRANSCRIPTION_FAILED", "Speech recognition failed. Check the Whisper device and compute type.", True)
    return AppError("DOWNLOAD_FAILED", "Audio download failed. Check the video URL and network connection.", True)


def _attach_job(pid):
    """Assign the waiting Windows child to a kill-on-close job before it starts."""
    if os.name != "nt":
        return None
    job = None
    try:
        import win32api
        import win32con
        import win32job
        job = win32job.CreateJobObject(None, "")
        info = win32job.QueryInformationJobObject(job, win32job.JobObjectExtendedLimitInformation)
        info["BasicLimitInformation"]["LimitFlags"] |= win32job.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        win32job.SetInformationJobObject(job, win32job.JobObjectExtendedLimitInformation, info)
        handle = win32api.OpenProcess(win32con.PROCESS_SET_QUOTA | win32con.PROCESS_TERMINATE, False, pid)
        try:
            win32job.AssignProcessToJobObject(job, handle)
        finally:
            handle.Close()
        return job
    except Exception:
        if job is not None:
            job.Close()
        raise AppError("SETUP_REQUIRED", "Windows process isolation is unavailable. Install pywin32 and restart the background engine.") from None


def _child_entry(connection, ready, target, arguments):
    if os.name != "nt":
        os.setsid()
    ready.wait()  # Windows parent assigns job first: no untracked descendants.
    try:
        result = target(*arguments, connection)
        connection.send(("result", result))
    except BaseException as exc:
        error = _safe_error(exc, "download")
        diagnostic_exc = exc.__cause__ or exc
        connection.send(("error", {"error": error.to_dict(), "exception_type": type(exc).__name__,
                                    "diagnostic_type": type(diagnostic_exc).__name__,
                                    "diagnostic": _safe_diagnostic(diagnostic_exc), "stage": getattr(target, "__name__", "worker")}))
    finally:
        connection.close()


def _run_child(target, arguments, cancel_event, on_progress=None, timeout=90):
    _cancelled(cancel_event)
    context = mp.get_context("spawn")
    receive, send = context.Pipe(duplex=False)
    ready = context.Event()
    child = context.Process(target=_child_entry, args=(send, ready, target, arguments))
    job = None
    started = time.monotonic()
    try:
        child.start()
        send.close()
        job = _attach_job(child.pid)
        ready.set()
        while True:
            _cancelled(cancel_event)
            if time.monotonic() - started > timeout:
                raise AppError("VIDEO_PROCESSING_TIMEOUT", "Video processing took too long. Try a shorter video.", True)
            if receive.poll(0.1):
                try:
                    kind, value = receive.recv()
                except EOFError:
                    raise AppError("VIDEO_WORKER_FAILED", "The video processing worker stopped unexpectedly.", True) from None
                if kind == "result":
                    _cancelled(cancel_event)
                    return value
                if kind == "error":
                    if isinstance(value, dict) and "error" in value:
                        payload = value["error"]
                        _diagnostic_log.error("stage=%s error_code=%s exception_type=%s diagnostic=%s",
                                              value.get("stage", "worker"), payload.get("code"),
                                              value.get("diagnostic_type", value.get("exception_type", "Exception")),
                                              value.get("diagnostic", ""))
                    else:  # Compatibility with an older spawned worker.
                        payload = value
                    raise AppError(payload["code"], payload["message"], payload.get("retryable", False))
                if kind == "progress" and on_progress:
                    on_progress(*value)
            elif not child.is_alive():
                raise AppError("VIDEO_WORKER_FAILED", "The video processing worker stopped unexpectedly.", True)
    finally:
        if job is not None:
            job.Close()  # Terminates child and all descendants, including FFmpeg.
        elif child.pid and child.is_alive():
            if os.name != "nt":
                try:
                    os.killpg(child.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            child.terminate()
        if child.pid:
            child.join(timeout=5)
            if child.is_alive():
                child.kill()
                child.join(timeout=5)
            child.close()
        receive.close()
        send.close()


def _normalise_metadata(info, url):
    fields = {"title": info.get("title"), "url": info.get("webpage_url") or url,
              "author": info.get("uploader") or info.get("channel") or info.get("creator"),
              "channel": info.get("channel"), "description": info.get("description"),
              "duration": info.get("duration"), "thumbnail": info.get("thumbnail"),
              "source": info.get("extractor_key") or info.get("extractor")}
    date = info.get("upload_date")
    if isinstance(date, str):
        try:
            fields["published"] = dt.datetime.strptime(date, "%Y%m%d").date().isoformat()
        except ValueError:
            pass
    return {key: value for key, value in fields.items() if value is not None and value != ""}


def _download_worker(url, directory, service_config, connection):
    try:
        import yt_dlp
        options = yt_dlp_options(service_config)
        last = -1

        def progress(data):
            nonlocal last
            total = data.get("total_bytes") or data.get("total_bytes_estimate")
            downloaded = data.get("downloaded_bytes", 0)
            percent = int(min(100, downloaded * 100 / total)) if total else 0
            if percent != last:
                last = percent
                connection.send(("progress", ("DOWNLOADING", 0.05 + 0.4 * percent / 100, "Downloading audio.")))

        options.update({"format": "bestaudio/best", "outtmpl": str(Path(directory) / "audio.%(ext)s"),
                        "progress_hooks": [progress], "restrictfilenames": True,
                        "postprocessors": [{"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "192"}]})
        with yt_dlp.YoutubeDL(options) as downloader:
            info = downloader.extract_info(url, download=True)
        audio = Path(directory) / "audio.mp3"
        if not audio.is_file() or audio.is_symlink():
            raise AppError("DOWNLOAD_FAILED", "Audio conversion did not produce an MP3 file.")
        return {"audio": str(audio), "metadata": _normalise_metadata(info, url)}
    except Exception as exc:
        error = classify_download_error(exc, "download")
        _log_exception(f"yt_dlp_download:{error.code}", exc)
        raise error from exc


def _whisper_worker(audio, config, connection):
    try:
        from faster_whisper import WhisperModel
        model = WhisperModel(config.get("model", "base"), device=config.get("device", "cpu"),
                             compute_type=config.get("compute_type", "int8"))
    except Exception as exc:
        _log_exception("whisper_model_load", exc)
        raise _safe_error(exc, "model") from None
    try:
        configured_language = config.get("language", "auto")
        language = None if configured_language in {None, "", "auto"} else configured_language
        segments, info = model.transcribe(audio, language=language, vad_filter=True)
        output = []
        duration = max(float(getattr(info, "duration", 0) or 0), 1)
        last = -1
        for segment in segments:
            start, end = float(segment.start), float(segment.end)
            text = str(segment.text).strip()
            if not (math.isfinite(start) and math.isfinite(end)):
                continue
            if text:
                output.append({"start": max(0, start), "end": max(0, start, end), "text": text})
            percent = int(min(100, max(0, end) * 100 / duration))
            if percent != last:
                last = percent
                connection.send(("progress", ("TRANSCRIBING", 0.5 + 0.4 * percent / 100, "Transcribing speech.")))
        return output
    except Exception as exc:
        _log_exception("whisper_audio_transcription", exc)
        raise _safe_error(exc, "transcription") from None


def _merge_metadata(page_metadata, video_metadata, url):
    result = dict(video_metadata)
    # Upstream extraction/template data remains authoritative when nonempty.
    for key, value in page_metadata.items():
        if isinstance(key, str) and value is not None and value != "":
            result[key] = value
    result["url"] = url
    result.setdefault("title", "Untitled video")
    return result


def _timestamp(seconds):
    whole = max(0, int(seconds))
    return f"{whole // 3600:02d}:{whole % 3600 // 60:02d}:{whole % 60:02d}"


def format_markdown(metadata, segments):
    """Serialize source metadata independently of the speech engine."""
    title = str(metadata.get("title") or "Untitled video").replace("\r", " ").replace("\n", " ")
    frontmatter = {"title": title, "source": metadata.get("url", ""), "type": "video"}
    for key in ("author", "channel", "published", "duration", "thumbnail"):
        if key in metadata and metadata[key] is not None:
            frontmatter[key] = metadata[key]
    lines = ["---"]
    # JSON scalar/array syntax is valid YAML and safely quotes colons/newlines.
    lines.extend(f"{key}: {json.dumps(value, ensure_ascii=False)}" for key, value in frontmatter.items())
    lines.extend(["---", "", f"# {title}", ""])
    if metadata.get("description"):
        lines.extend(["## Description", "", str(metadata["description"]), ""])
    if metadata.get("markdown"):
        lines.extend(["## Original clipping", "", str(metadata["markdown"]), ""])
    lines.extend(["## Transcript", ""])
    for segment in segments:
        # Only source-provided speaker labels are accepted by this formatter.
        speaker = f"{segment['speaker']}: " if segment.get("speaker") else ""
        lines.extend([f"[{_timestamp(segment['start'])}] {speaker}{segment['text']}", ""])
    return "\n".join(lines).rstrip() + "\n"


def transcribe_video(url: str, page_metadata: dict, whisper_config: dict,
                     on_progress, cancel_event: threading.Event, temp_root=None,
                     service_config=None) -> dict:
    _cancelled(cancel_event)
    _validate_url(url)
    if not isinstance(page_metadata, dict) or not isinstance(whisper_config, dict):
        raise AppError("INVALID_REQUEST", "Video metadata and Whisper settings must be objects.")
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        raise AppError("FFMPEG_UNAVAILABLE", "FFmpeg and FFprobe must be installed and available on PATH.")
    try:
        if temp_root is not None:
            Path(temp_root).mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="videomind-", dir=temp_root) as directory:
            if shutil.disk_usage(directory).free < 256 * 1024 * 1024:
                raise AppError("INSUFFICIENT_DISK_SPACE", "At least 256 MB of free space is required to start processing.")
            on_progress("DOWNLOADING", 0.01, "Downloading audio.")
            downloaded = _run_child(_download_worker, (url, directory, service_config or {}), cancel_event, on_progress, timeout=7200)
            _cancelled(cancel_event)
            on_progress("TRANSCRIBING", 0.5, "Loading Whisper and detecting the spoken language.")
            segments = _run_child(_whisper_worker, (downloaded["audio"], whisper_config), cancel_event, on_progress, timeout=28800)
            _cancelled(cancel_event)
            if not segments:
                raise AppError("NO_SPEECH_DETECTED", "No speech was detected in this video.")
            on_progress("GENERATING_MARKDOWN", 0.95, "Preparing the original Markdown.")
            metadata = _merge_metadata(page_metadata, downloaded["metadata"], url)
            return {"title": str(metadata["title"]), "markdown": format_markdown(metadata, segments),
                    "metadata": {key: value for key, value in metadata.items() if key != "markdown"},
                    "segments": segments}
    except OSError as exc:
        raise _safe_error(exc, "download") from None
