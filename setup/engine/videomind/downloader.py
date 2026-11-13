"""Shared yt-dlp runtime wiring and safe download failure classification."""
from pathlib import Path
from urllib.parse import urlsplit

from .errors import AppError


def js_runtime_options(service_config):
    path = service_config.get("js_runtime_path") if isinstance(service_config, dict) else None
    if not path:
        return {"deno": {}}
    runtime = Path(path)
    if not runtime.is_absolute() or not runtime.is_file():
        raise AppError("YOUTUBE_JS_RUNTIME_MISSING", "Clipper Plus's configured Deno runtime is unavailable. Run Setup to repair it.")
    return {"deno": {"path": str(runtime)}}


def yt_dlp_options(service_config=None):
    return {
        "quiet": True, "no_warnings": True, "logger": _QuietLogger(),
        "noplaylist": True, "socket_timeout": 20, "retries": 2,
        "cachedir": False, "enable_file_urls": False,
        "js_runtimes": js_runtime_options(service_config or {}),
        # Python extra installs EJS locally. Never fetch scripts on each video.
        "remote_components": [],
    }


class _QuietLogger:
    def debug(self, message): pass
    def warning(self, message): pass
    def error(self, message): pass


def classify_download_error(exc, phase="download"):
    """Return safe actionable errors without retaining yt-dlp's private URL text."""
    if isinstance(exc, AppError):
        return exc
    kind = type(exc).__name__
    text = str(exc).lower()
    if any(term in text for term in ("no supported javascript runtime", "no supported js runtime", "javascript runtime is not available")):
        return AppError("YOUTUBE_JS_RUNTIME_MISSING", "YouTube needs the Deno runtime. Run Clipper Plus Setup to install or repair it.")
    if any(term in text for term in ("js challenge", "javascript challenge", "challenge solving failed", "challenge solver")):
        return AppError("YOUTUBE_JS_CHALLENGE_FAILED", "YouTube's JavaScript check could not be solved. Verify Deno and yt-dlp-ejs in Setup.", True)
    if "yt-dlp-ejs" in text or "ejs scripts" in text or "no ejs" in text:
        return AppError("YOUTUBE_EJS_MISSING", "YouTube challenge scripts are missing. Run Setup to repair yt-dlp[default].")
    if "po token" in text or "pot provider" in text:
        return AppError("YOUTUBE_PO_TOKEN_REQUIRED", "YouTube requires a PO Token for this format. Try another available format or video.", True)
    if "unsupported url" in text:
        return AppError("UNSUPPORTED_URL", "This page is not a supported video page.")
    if any(term in text for term in ("no video formats", "requested format is not available")):
        return AppError("VIDEO_FORMAT_UNAVAILABLE", "No supported video or audio format is available.")
    if any(term in text for term in ("private video", "sign in", "login required", "members-only", "authentication required")):
        return AppError("VIDEO_AUTHENTICATION_REQUIRED", "This video is private or YouTube requires sign-in or bot verification from this network.")
    if any(term in text for term in ("video unavailable", "has been removed", "this video is unavailable", "not available in your country")):
        return AppError("VIDEO_UNAVAILABLE", "This video is unavailable or restricted by its publisher.")
    if any(term in text for term in ("timed out", "timeout", "connection reset", "temporary failure", "name or service not known", "urlopen error", "urllib error")):
        return AppError("VIDEO_NETWORK_ERROR", "The video service or network did not respond. Try again.", True)
    if kind in {"HTTPError"} and getattr(exc, "code", None) in {401, 403}:
        return AppError("VIDEO_DOWNLOAD_FAILED", "The video server denied this download. This does not necessarily mean sign-in is required.", True)
    return AppError("VIDEO_DOWNLOAD_FAILED", "Video processing failed. Check the Clipper Plus diagnostic log for its safe error type and stage.", True)


def safe_video_host(url):
    try:
        return (urlsplit(url).hostname or "unknown")[:253]
    except (ValueError, TypeError):
        return "unknown"
