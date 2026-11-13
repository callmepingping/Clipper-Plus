from .config import config_path, load_config
from .contracts import PROTOCOL_VERSION, COMMANDS, encode_message, required_text, validate_request
from .errors import AppError
from .jobs import JobManager
from .storage import save_markdown
from .completed_store import CompletedMarkdownStore, MAX_MARKDOWN_BYTES
from .runtime import atomic_json, runtime_paths
import logging
import json
from datetime import datetime, timezone

_log = logging.getLogger("videomind.engine")


class Application:
    def __init__(self, config=None, *, transcriber=None, cache_store=None):
        self.config = config
        self.cache_store = cache_store or CompletedMarkdownStore(runtime_paths().cache / "completed_markdown.json")
        self.jobs = JobManager(on_completed=lambda task_id, url, result: self.cache_store.store_task_result(url, task_id, result))
        self.transcriber = transcriber

    def handle(self, request):
        identifier = request.get("id") if isinstance(request, dict) and isinstance(request.get("id"), str) else None
        candidate_type = request.get("type") if isinstance(request, dict) else None
        request_type = candidate_type if isinstance(candidate_type, str) and candidate_type in COMMANDS else "unknown"
        raw_version = request.get("protocol_version") if isinstance(request, dict) else None
        request_version = ("unspecified" if raw_version is None else raw_version
                           if type(raw_version) is int and -1 <= raw_version <= 99 else "invalid")
        _log.info("request_received command=%s protocol_version=%s", request_type, request_version)
        try:
            command, payload = validate_request(request)
            config = self.config if self.config is not None else load_config()
            status, data = "ok", {}
            if command == "GET_CONFIGURATION":
                data = {"language": "en", "protocol_version": PROTOCOL_VERSION}
            elif command == "START_TRANSCRIPTION":
                from .transcription import transcribe_video
                url = required_text(payload, "url", 8192)
                metadata = payload.get("metadata", {})
                if not isinstance(metadata, dict) or len(encode_message(metadata)) > MAX_MARKDOWN_BYTES + 100_000:
                    raise AppError("INVALID_REQUEST", "Invalid page metadata.")
                if self.transcriber:
                    worker = lambda progress, cancel: self.transcriber(
                        url, metadata, config["whisper"], progress, cancel,
                        service_config=config.get("service", {}),
                    )
                else:
                    worker = lambda progress, cancel: transcribe_video(url, metadata, config["whisper"], progress, cancel, service_config=config.get("service", {}))
                task_id = self.jobs.start("transcription", worker, source_url=url)
                status, data = "accepted", {"task_id": task_id}
            elif command == "GET_TASK_STATUS":
                task_id = payload.get("task_id")
                if task_id is not None and not isinstance(task_id, str):
                    raise AppError("INVALID_REQUEST", "Invalid task id.")
                data = {"task": self.jobs.status(task_id)}
            elif command == "CANCEL_TASK":
                data = {"task": self.jobs.cancel(required_text(payload, "task_id", 128))}
            elif command == "SAVE_ORIGINAL":
                markdown = required_text(payload, "markdown", MAX_MARKDOWN_BYTES)
                title = required_text(payload, "title", 1000)
                source_url = payload.get("source_url")
                if source_url is not None:
                    source_url = required_text(payload, "source_url", 8192)
                    self.cache_store.update_markdown(source_url, markdown, title)
                path = save_markdown(config, markdown, title)
                if source_url:
                    self.cache_store.record_local_file(source_url, title, path)
                data = {"path": path}
            elif command == "GET_CACHED_MARKDOWN":
                url = required_text(payload, "url", 8192)
                language = payload.get("ui_language", "en")
                if language not in {"en", "zh-CN", "ja"}:
                    language = "en"
                data = {"entry": self.cache_store.get_markdown(url), "language": language}
            elif command == "GET_HISTORY_MARKDOWN":
                url = required_text(payload, "url", 8192)
                data = {"entry": self.cache_store.get_history_markdown(url)}
            elif command == "UPDATE_CACHED_MARKDOWN":
                url = required_text(payload, "url", 8192)
                markdown = required_text(payload, "markdown", MAX_MARKDOWN_BYTES)
                title = payload.get("title")
                if title is not None and (not isinstance(title, str) or len(title) > 1000):
                    raise AppError("INVALID_REQUEST", "Invalid video title.")
                data = self.cache_store.update_markdown(url, markdown, title)
            elif command == "DELETE_CACHED_MARKDOWN":
                url = required_text(payload, "url", 8192)
                local_path = payload.get("local_path")
                if local_path is not None and (not isinstance(local_path, str) or not local_path):
                    raise AppError("INVALID_REQUEST", "Invalid downloaded Markdown path.")
                data = {"deleted": self.cache_store.delete_markdown(url, local_path)}
            elif command == "GET_HISTORY":
                query = payload.get("query", "")
                limit = payload.get("limit", 50)
                offset = payload.get("offset", 0)
                data = self.cache_store.list_history(query, limit, offset)
            elif command == "DELETE_HISTORY":
                url = required_text(payload, "url", 8192)
                delete_cache = payload.get("delete_cache", False)
                if type(delete_cache) is not bool:
                    raise AppError("INVALID_REQUEST", "Invalid cache deletion option.")
                data = {"deleted": self.cache_store.delete_history(url, delete_cache)}
            elif command == "CLEAR_HISTORY":
                delete_cache = payload.get("delete_cache", False)
                if type(delete_cache) is not bool:
                    raise AppError("INVALID_REQUEST", "Invalid cache deletion option.")
                data = {"deleted_count": self.cache_store.clear_history(delete_cache)}
            elif command == "GET_PENDING_SETTINGS_MIGRATION":
                migration_path = runtime_paths().migration / "extension-settings.json"
                migration = None
                try:
                    if migration_path.stat().st_size <= 750 * 1024:
                        candidate = json.loads(migration_path.read_text(encoding="utf-8"))
                        if (isinstance(candidate, dict) and candidate.get("schema") == 1 and
                                candidate.get("status") == "pending" and candidate.get("consent") is True):
                            migration = {"id": candidate.get("id"), "vaults": candidate.get("vaults", []),
                                         "templates": candidate.get("templates", [])}
                except FileNotFoundError:
                    pass
                except (OSError, ValueError, TypeError):
                    raise AppError("SETTINGS_MIGRATION_READ_FAILED",
                                   "The staged settings migration could not be read. Setup left the source data unchanged.", True) from None
                data = {"migration": migration}
            elif command == "COMPLETE_SETTINGS_MIGRATION":
                migration_id = required_text(payload, "id", 128)
                migration_path = runtime_paths().migration / "extension-settings.json"
                try:
                    candidate = json.loads(migration_path.read_text(encoding="utf-8"))
                except (OSError, ValueError, TypeError):
                    raise AppError("SETTINGS_MIGRATION_NOT_FOUND", "The staged settings migration is no longer available.") from None
                if (not isinstance(candidate, dict) or candidate.get("id") != migration_id or
                        candidate.get("status") != "pending"):
                    raise AppError("SETTINGS_MIGRATION_STALE", "The staged settings migration has changed.")
                candidate["status"] = "complete"
                candidate["completed_at"] = datetime.now(timezone.utc).isoformat()
                atomic_json(migration_path, candidate)
                data = {"completed": True}
            response = {"id": identifier, "status": status, "data": data}
            encode_message(response)
            return response
        except Exception as exc:
            error = exc if isinstance(exc, AppError) else AppError("INTERNAL_ERROR", "The engine could not process this request.")
            _log.warning("request_rejected command=%s protocol_version=%s error_code=%s",
                         request_type, request_version, error.code)
            return {"id": identifier, "status": "error", "error": error.to_dict()}
