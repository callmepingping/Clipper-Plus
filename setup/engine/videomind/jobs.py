"""One in-memory operation, owned by the independent engine process."""
from copy import deepcopy
import threading
from uuid import uuid4

from .contracts import encode_message
from .errors import AppError

TERMINAL = {"COMPLETED", "FAILED", "CANCELLED"}


class JobManager:
    def __init__(self, on_completed=None):
        self._lock = threading.RLock()
        self._task = None
        self._cancel = None
        self._on_completed = on_completed

    def _check_available(self):
        if self._task and self._task["state"] not in TERMINAL:
            raise AppError("TASK_BUSY", "Another task is active. Wait for it to finish or cancel it.", True)

    def start(self, kind, worker, source_url=""):
        with self._lock:
            self._check_available()
            self._cancel = threading.Event()
            self._task = {"task_id": uuid4().hex, "kind": kind, "source_url": source_url,
                          "state": "QUEUED", "progress": 0.0,
                          "message": "Task queued.",
                          "result": None, "error": None}
            identifier, cancel = self._task["task_id"], self._cancel
            threading.Thread(target=self._run, args=(identifier, worker, cancel), daemon=True, name=f"videomind-{kind}").start()
            return identifier

    def _progress(self, identifier, state, progress, message):
        if state not in {"DOWNLOADING", "TRANSCRIBING", "GENERATING_MARKDOWN"}:
            return
        with self._lock:
            if self._task["task_id"] == identifier:
                self._task.update(state=state, progress=max(0.0, min(1.0, float(progress))), message=message)

    def _run(self, identifier, worker, cancel):
        try:
            result = worker(lambda *args: self._progress(identifier, *args), cancel)
            # Leave room for the envelope and state fields.
            if len(encode_message(result)) > 850_000:
                raise AppError("MESSAGE_TOO_LARGE", "Result exceeds the native message limit. Clip a shorter source.")
            with self._lock:
                if cancel.is_set():
                    raise AppError("CANCELLED", "Task cancelled.")
                # Keep cancellation and completion persistence linearizable: a
                # concurrent cancel either wins before this point or observes
                # the completed task after the cache commit.
                if self._on_completed and self._task["kind"] == "transcription":
                    self._on_completed(identifier, self._task["source_url"], result)
                self._task.update(state="COMPLETED", progress=1.0, message="Task completed.", result=result)
        except Exception as exc:
            error = exc if isinstance(exc, AppError) else AppError("PROCESSING_FAILED", "The task failed. Check the installation and try again.")
            with self._lock:
                cache_errors = {"MARKDOWN_PERSISTENCE_FAILED", "MARKDOWN_STORE_UNAVAILABLE"}
                cancelled = error.code == "CANCELLED" or (cancel.is_set() and error.code not in cache_errors)
                self._task.update(state="CANCELLED" if cancelled else "FAILED", message="Task cancelled." if cancelled else error.message, result=None, error=AppError("CANCELLED", "Task cancelled.").to_dict() if cancelled else error.to_dict())

    def status(self, identifier=None):
        with self._lock:
            if identifier is not None and (not self._task or identifier != self._task["task_id"]):
                raise AppError("TASK_NOT_FOUND", "This task is no longer available.")
            return deepcopy(self._task)

    def cancel(self, identifier):
        with self._lock:
            self.status(identifier)
            if self._task["state"] not in TERMINAL:
                self._cancel.set()
                self._task["message"] = "Cancelling; waiting for worker cleanup."
            return deepcopy(self._task)
