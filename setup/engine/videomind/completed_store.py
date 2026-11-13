"""Atomic completed-Markdown cache and independent lightweight history index."""

from contextlib import contextmanager
from datetime import datetime, timezone
import json
import logging
import os
from pathlib import Path
import re
import threading
import time
from urllib.parse import parse_qs, urlsplit, urlunsplit

from .errors import AppError

_log = logging.getLogger("videomind.cache")
_locks_guard = threading.Lock()
_path_locks: dict[str, threading.RLock] = {}
_YOUTUBE_ID = re.compile(r"^[A-Za-z0-9_-]{6,20}$")
MAX_MARKDOWN_BYTES = 850_000
MAX_TITLE_LENGTH = 1000
_MAX_LOCAL_MARKDOWN_BYTES = MAX_MARKDOWN_BYTES


def _replace_with_retry(source: Path, destination: Path) -> None:
    """Retry transient Windows sharing violations while the store lock is held."""
    delay = 0.025
    for attempt in range(8):
        try:
            os.replace(source, destination)
            return
        except PermissionError:
            if attempt == 7:
                raise
            time.sleep(delay)
            delay = min(delay * 2, 0.25)


def canonical_url(value: str) -> str:
    """Return a stable cache key without discarding meaningful generic query data."""
    if not isinstance(value, str) or not value or len(value) > 8192:
        raise AppError("INVALID_REQUEST", "A valid video URL is required.")
    try:
        parsed = urlsplit(value)
        scheme = parsed.scheme.lower()
        if scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
            raise ValueError()
        host = parsed.hostname.rstrip(".").lower()
        port = parsed.port
    except (ValueError, TypeError):
        raise AppError("INVALID_REQUEST", "A valid video URL is required.") from None

    if host in {"youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com", "youtu.be", "www.youtu.be"}:
        video_id = None
        if host.endswith("youtu.be"):
            video_id = parsed.path.strip("/").split("/", 1)[0]
        else:
            query = parse_qs(parsed.query, keep_blank_values=True)
            if parsed.path.rstrip("/") == "/watch":
                video_id = (query.get("v") or [None])[0]
            else:
                parts = parsed.path.strip("/").split("/")
                if len(parts) >= 2 and parts[0].lower() in {"shorts", "live", "embed", "v"}:
                    video_id = parts[1]
        if video_id and _YOUTUBE_ID.fullmatch(video_id):
            return f"https://www.youtube.com/watch?v={video_id}"

    # For other sites, preserve the raw query byte-for-byte. Only normalize the
    # scheme/host/default port and discard fragments, which are client-side state.
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"  # IPv6 literal
    default_port = (scheme == "http" and port == 80) or (scheme == "https" and port == 443)
    netloc = host if port is None or default_port else f"{host}:{port}"
    path = parsed.path or "/"
    return urlunsplit((scheme, netloc, path, parsed.query, ""))


def _timestamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _path_lock(path: Path) -> threading.RLock:
    key = str(path.resolve()).casefold()
    with _locks_guard:
        return _path_locks.setdefault(key, threading.RLock())


@contextmanager
def _file_lock(path: Path):
    """Serialize updates across threads and processes without an extra package."""
    lock_path = path.with_name(path.name + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with _path_lock(path):
        with lock_path.open("a+b") as stream:
            stream.seek(0, os.SEEK_END)
            if stream.tell() == 0:
                stream.write(b"\0")
                stream.flush()
            stream.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_LOCK, 1)
                try:
                    yield
                finally:
                    stream.seek(0)
                    msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
                try:
                    yield
                finally:
                    fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


class CompletedMarkdownStore:
    """Single-file cache and history store; all mutations are atomic transactions."""

    def __init__(self, path: str | Path):
        self.path = Path(path)

    @staticmethod
    def _empty() -> dict:
        return {"version": 1, "entries": {}, "history": {}}

    def _read_unlocked(self) -> dict:
        if not self.path.exists():
            return self._empty()
        try:
            if self.path.stat().st_size > 128 * 1024 * 1024:
                raise ValueError("store is too large")
            value = json.loads(self.path.read_text(encoding="utf-8-sig"))
            if (not isinstance(value, dict) or value.get("version") != 1
                    or not isinstance(value.get("entries"), dict)
                    or not isinstance(value.get("history"), dict)):
                raise ValueError("invalid store schema")
            return value
        except (OSError, ValueError, TypeError) as exc:
            _log.error("completed_markdown_read_failed exception_type=%s", type(exc).__name__)
            raise AppError("MARKDOWN_STORE_UNAVAILABLE", "Completed Markdown storage is unreadable. Check the Clipper Plus data directory.") from None

    def _read(self) -> dict:
        try:
            with _file_lock(self.path):
                return self._read_unlocked()
        except AppError:
            raise
        except (OSError, ValueError, TypeError, UnicodeError) as exc:
            _log.error("completed_markdown_lock_failed stage=read exception_type=%s", type(exc).__name__)
            raise AppError("MARKDOWN_STORE_UNAVAILABLE", "Completed Markdown storage is unavailable. Check the Clipper Plus data directory.") from None

    def _write_unlocked(self, value: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name(f".{self.path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
        try:
            with temporary.open("w", encoding="utf-8", newline="\n") as stream:
                json.dump(value, stream, ensure_ascii=False, separators=(",", ":"))
                stream.flush()
                os.fsync(stream.fileno())
            _replace_with_retry(temporary, self.path)
        except (OSError, ValueError, TypeError, UnicodeError) as exc:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
            _log.error("completed_markdown_write_failed stage=atomic_replace exception_type=%s", type(exc).__name__)
            raise AppError("MARKDOWN_PERSISTENCE_FAILED", "Completed Markdown could not be saved to the Clipper Plus data directory.") from None

    def _update(self, callback):
        try:
            with _file_lock(self.path):
                data = self._read_unlocked()
                result = callback(data)
                self._write_unlocked(data)
                return result
        except AppError:
            raise
        except OSError as exc:
            _log.error("completed_markdown_lock_failed exception_type=%s", type(exc).__name__)
            raise AppError("MARKDOWN_PERSISTENCE_FAILED", "Completed Markdown could not be safely updated.") from None

    def store_task_result(self, source_url: str, task_id: str, result: dict) -> None:
        if not isinstance(result, dict) or not isinstance(result.get("markdown"), str) or not result["markdown"]:
            _log.error("completed_markdown_persist_failed stage=validate_result error_code=MARKDOWN_PERSISTENCE_FAILED")
            raise AppError("MARKDOWN_PERSISTENCE_FAILED", "The completed task did not contain Markdown to save.")
        if len(result["markdown"].encode("utf-8")) > MAX_MARKDOWN_BYTES:
            _log.error("completed_markdown_persist_failed stage=validate_result error_code=MARKDOWN_PERSISTENCE_FAILED")
            raise AppError("MARKDOWN_PERSISTENCE_FAILED", "The completed Markdown is too large for the local cache.")
        key = canonical_url(source_url)
        title = result.get("title") if isinstance(result.get("title"), str) else "Video"
        title = title[:MAX_TITLE_LENGTH]
        completed_at = _timestamp()
        entry = {"url": source_url, "title": title, "markdown": result["markdown"],
                 "completed_at": completed_at, "task_id": task_id}

        def save(data):
            data["entries"][key] = entry
            previous = data["history"].get(key, {})
            history = {"url": source_url, "title": title, "completed_at": completed_at,
                       "task_id": task_id, "markdown_available": True}
            if isinstance(previous, dict) and previous.get("local_path"):
                history["local_path"] = previous["local_path"]
            data["history"][key] = history

        self._update(save)

    def get_markdown(self, url: str) -> dict | None:
        key = canonical_url(url)
        data = self._read()
        entry = data["entries"].get(key)
        if isinstance(entry, dict) and isinstance(entry.get("markdown"), str):
            return {key: entry.get(key) for key in ("url", "title", "markdown", "completed_at", "task_id")} | {"source": "cache"}
        return None

    def get_history_markdown(self, url: str) -> dict | None:
        key = canonical_url(url)
        data = self._read()
        entry = data["entries"].get(key)
        if isinstance(entry, dict) and isinstance(entry.get("markdown"), str):
            return {key: entry.get(key) for key in ("url", "title", "markdown", "completed_at", "task_id")} | {"source": "cache"}
        history = data["history"].get(key)
        local_path = history.get("local_path") if isinstance(history, dict) else None
        if not isinstance(local_path, str):
            return None
        path = Path(local_path)
        try:
            if path.is_symlink() or not path.is_file():
                return None
            if path.stat().st_size > _MAX_LOCAL_MARKDOWN_BYTES:
                raise AppError("HISTORY_FILE_TOO_LARGE", "The saved Markdown file is too large to reopen in Clipper Plus.")
            markdown = path.read_text(encoding="utf-8-sig")
            if len(markdown.encode("utf-8")) > MAX_MARKDOWN_BYTES:
                raise AppError("HISTORY_FILE_TOO_LARGE", "The saved Markdown file is too large to reopen in Clipper Plus.")
        except AppError:
            raise
        except (OSError, UnicodeError):
            return None
        return {"url": history.get("url", url), "title": history.get("title", path.stem),
                "markdown": markdown, "completed_at": history.get("completed_at", ""),
                "task_id": history.get("task_id", ""), "source": "file"}

    def update_markdown(self, url: str, markdown: str, title: str | None = None) -> dict:
        key = canonical_url(url)
        if not isinstance(markdown, str) or not markdown.strip() or len(markdown.encode("utf-8")) > MAX_MARKDOWN_BYTES:
            raise AppError("INVALID_REQUEST", "Markdown must be nonempty and within the cache size limit.")
        now = _timestamp()

        def update(data):
            entry = data["entries"].get(key)
            history = data["history"].get(key)
            if not isinstance(entry, dict):
                if not isinstance(history, dict):
                    raise AppError("MARKDOWN_NOT_FOUND", "There is no completed Markdown cache for this video.")
                entry = {"url": history.get("url", url), "title": history.get("title", "Video"),
                         "completed_at": history.get("completed_at", now), "task_id": history.get("task_id", "")}
            if title:
                entry["title"] = title
            entry["markdown"] = markdown
            entry["edited_at"] = now
            data["entries"][key] = entry
            if isinstance(history, dict):
                if title:
                    history["title"] = title
                history["markdown_available"] = True
            return {"saved_at": now}

        return self._update(update)

    def record_local_file(self, url: str, title: str, local_path: str) -> None:
        key = canonical_url(url)
        if not isinstance(local_path, str) or not local_path or len(local_path) > 32767 or not Path(local_path).is_absolute():
            raise AppError("INVALID_REQUEST", "The saved Markdown path must be an absolute local path.")
        now = _timestamp()

        def update(data):
            entry = data["entries"].get(key)
            previous = data["history"].get(key, {})
            history = {"url": (entry or {}).get("url") or url,
                       "title": title or (entry or {}).get("title") or (previous or {}).get("title") or "Video",
                       "completed_at": (entry or {}).get("completed_at") or (previous or {}).get("completed_at") or now,
                       "task_id": (entry or {}).get("task_id") or (previous or {}).get("task_id") or "",
                       "markdown_available": isinstance(entry, dict), "local_path": local_path}
            data["history"][key] = history

        self._update(update)

    def delete_markdown(self, url: str, local_path: str | None = None) -> bool:
        key = canonical_url(url)
        if local_path is not None and (not isinstance(local_path, str) or not local_path or len(local_path) > 32767 or not Path(local_path).is_absolute()):
            raise AppError("INVALID_REQUEST", "The downloaded Markdown path must be an absolute local path.")

        def delete(data):
            entry = data["entries"].pop(key, None)
            history = data["history"].get(key)
            if isinstance(history, dict):
                history["markdown_available"] = False
                if local_path:
                    history["local_path"] = local_path
            elif isinstance(entry, dict):
                history = {key_name: entry.get(key_name) for key_name in ("url", "title", "completed_at", "task_id")}
                history["markdown_available"] = False
                if local_path:
                    history["local_path"] = local_path
                data["history"][key] = history
            return isinstance(entry, dict)

        return self._update(delete)

    def list_history(self, query: str = "", limit: int = 50, offset: int = 0) -> dict:
        if not isinstance(query, str) or len(query) > 256:
            raise AppError("INVALID_REQUEST", "History search text must be 256 characters or fewer.")
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 50:
            raise AppError("INVALID_REQUEST", "History page size must be between 1 and 50.")
        if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
            raise AppError("INVALID_REQUEST", "History offset must be a nonnegative integer.")
        data = self._read()
        result = []
        for key, item in data["history"].items():
            if not isinstance(item, dict):
                continue
            path = item.get("local_path")
            file_available = False
            if isinstance(path, str):
                try:
                    file_available = Path(path).is_file() and not Path(path).is_symlink()
                except OSError:
                    pass
            title = item.get("title", "Video")
            url_value = item.get("url", key)
            if not isinstance(title, str) or not isinstance(url_value, str):
                continue
            completed_at = item.get("completed_at", "")
            task_id = item.get("task_id", "")
            result.append({"url": url_value, "title": title, "completed_at": completed_at if isinstance(completed_at, str) else "",
                           "task_id": task_id if isinstance(task_id, str) else "", "markdown_available": key in data["entries"],
                           "file_available": file_available})
        result.sort(key=lambda item: item["completed_at"], reverse=True)
        needle = query.casefold().strip()
        if needle:
            result = [item for item in result if needle in item["title"].casefold() or needle in item["url"].casefold()]
        total = len(result)
        return {"items": result[offset:offset + limit], "total": total, "limit": limit, "offset": offset}

    def delete_history(self, url: str, delete_cache: bool = False) -> bool:
        key = canonical_url(url)

        def delete(data):
            existed = data["history"].pop(key, None) is not None
            if delete_cache:
                data["entries"].pop(key, None)
            return existed

        return self._update(delete)

    def clear_history(self, delete_cache: bool = False) -> int:
        def clear(data):
            count = len(data["history"])
            data["history"].clear()
            if delete_cache:
                data["entries"].clear()
            return count

        return self._update(clear)
