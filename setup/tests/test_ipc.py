"""Real Windows IPC tests without service registration or credential access."""
import io
from concurrent.futures import ThreadPoolExecutor
import json
import multiprocessing
import os
from pathlib import Path
import struct
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from uuid import uuid4

from videomind.api import Application
from videomind.completed_store import CompletedMarkdownStore
from videomind.contracts import MAX_MESSAGE_BYTES
from videomind.errors import AppError
from videomind.ipc import call_engine, serve
from videomind.native_host import read_message, write_message


def _delayed_transcriber(url, metadata, config, progress, cancel, service_config=None):
    progress("TRANSCRIBING", 0.5, "Test worker is processing independently.")
    # Keep processing past the bridge's lifetime. Cancellation completes cleanup
    # before the task manager exposes terminal state.
    deadline = time.monotonic() + 0.8
    while time.monotonic() < deadline:
        if cancel.wait(0.02):
            time.sleep(0.05)
            raise AppError("CANCELLED", "Task cancelled.")
    return {"title": "Lifecycle test", "markdown": "# Original\n\n[00:00:01] Complete after Chrome disconnects."}


def _engine_process(config, address, ready, cache_path):
    serve(Application(config, transcriber=_delayed_transcriber,
                     cache_store=CompletedMarkdownStore(cache_path)), address=address, ready=ready)


@unittest.skipUnless(os.name == "nt", "Windows named pipes")
class NativeLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        root = self.folder.name
        self.config = {"version": 1, "language": "en",
                       "storage": {"original_dir": str(Path(root) / "notes")},
                       "whisper": {"model": "base", "device": "cpu", "compute_type": "int8"}}
        Path(self.config["storage"]["original_dir"]).mkdir()
        self.cache_path = str(Path(root) / "completed_markdown.json")
        self.address = rf"\\.\pipe\VideoMind-test-{uuid4().hex}"
        self.engine = None
        self.start_engine()

    def tearDown(self):
        self.stop_engine()
        self.folder.cleanup()

    def start_engine(self):
        context = multiprocessing.get_context("spawn")
        ready = context.Event()
        self.engine = context.Process(target=_engine_process, args=(self.config, self.address, ready, self.cache_path))
        self.engine.start()
        self.assertTrue(ready.wait(10), "Independent engine did not create its pipe")

    def stop_engine(self):
        if self.engine is not None:
            self.engine.terminate()
            self.engine.join(5)
            if self.engine.is_alive():
                self.engine.kill()
                self.engine.join(5)
            self.engine.close()
            self.engine = None

    def call(self, command, payload=None):
        return call_engine({"id": uuid4().hex, "type": command, "payload": payload or {}}, self.address)

    def wait_terminal(self, identifier):
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            response = self.call("GET_TASK_STATUS", {"task_id": identifier})
            self.assertEqual(response["status"], "ok", response)
            task = response["data"]["task"]
            if task["state"] in {"COMPLETED", "FAILED", "CANCELLED"}:
                return task
            time.sleep(0.03)
        self.fail("Independent task did not reach terminal state")

    def native_process(self, request):
        frame = io.BytesIO()
        write_message(frame, request)
        return self.native_bytes(frame.getvalue())

    def native_bytes(self, frame):
        script = ("import sys; from functools import partial; "
                  "from videomind.native_host import bridge; from videomind.ipc import call_engine; "
                  "import msvcrt, os; msvcrt.setmode(sys.stdin.fileno(),os.O_BINARY); "
                  "msvcrt.setmode(sys.stdout.fileno(),os.O_BINARY); "
                  "bridge(sys.stdin.buffer,sys.stdout.buffer,partial(call_engine,address=sys.argv[1]))")
        process = subprocess.Popen([sys.executable, "-c", script, self.address], stdin=subprocess.PIPE,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, creationflags=subprocess.CREATE_NO_WINDOW)
        try:
            output, error = process.communicate(frame, timeout=10)
        finally:
            if process.poll() is None:
                process.kill()
                process.communicate(timeout=5)
        self.assertEqual(process.returncode, 0, error.decode("utf-8", errors="replace"))
        return read_message(io.BytesIO(output))

    def test_engine_completes_after_native_bridge_exits_and_recovers_result(self):
        response = self.native_process({"id": "start", "type": "START_TRANSCRIPTION", "payload": {"url": "https://example.com/video"}})
        self.assertEqual(response["status"], "accepted", response)
        identifier = response["data"]["task_id"]
        self.assertTrue(self.engine.is_alive())
        task = self.wait_terminal(identifier)
        self.assertEqual(task["state"], "COMPLETED", task)
        # A newly launched bridge sees the result owned by the independent engine.
        recovered = self.native_process({"id": "reopen", "type": "GET_TASK_STATUS", "payload": {}})
        self.assertEqual(recovered["data"]["task"]["task_id"], identifier)
        self.assertIn("Complete after Chrome", recovered["data"]["task"]["result"]["markdown"])
        cached = self.call("GET_CACHED_MARKDOWN", {"url": "https://example.com/video"})
        self.assertEqual(cached["data"]["entry"]["markdown"], recovered["data"]["task"]["result"]["markdown"])

    def test_cancel_busy_and_restart_never_resume(self):
        identifier = self.call("START_TRANSCRIPTION", {"url": "https://example.com/video"})["data"]["task_id"]
        busy = self.call("START_TRANSCRIPTION", {"url": "https://example.com/other"})
        self.assertEqual(busy["error"]["code"], "TASK_BUSY")
        cancelled = self.call("CANCEL_TASK", {"task_id": identifier})
        self.assertEqual(cancelled["status"], "ok")
        self.assertEqual(self.wait_terminal(identifier)["state"], "CANCELLED")
        self.stop_engine()
        self.start_engine()
        self.assertIsNone(self.call("GET_TASK_STATUS")["data"]["task"])
        self.assertEqual(self.call("GET_TASK_STATUS", {"task_id": identifier})["error"]["code"], "TASK_NOT_FOUND")

    def test_native_malformed_frame_is_bounded_and_service_still_responds(self):
        for raw in (struct.pack("<I", MAX_MESSAGE_BYTES + 1), struct.pack("<I", 100) + b"{}"):
            response = self.native_bytes(raw)
            self.assertEqual(response["status"], "error")
        response = self.native_process({"id": "health", "type": "GET_CONFIGURATION", "payload": {}})
        self.assertEqual(response["status"], "ok", response)
        self.assertNotIn("credential", json.dumps(response))
        self.assertNotIn("ai", response["data"])

    def test_real_pipe_preserves_large_unicode_markdown_on_save(self):
        markdown = "# 原文\n\n" + "中日 English\n" * 20000
        response = self.call("SAVE_ORIGINAL", {"title": "Unicode", "markdown": markdown})
        self.assertEqual(response["status"], "ok", response)
        self.assertEqual(Path(response["data"]["path"]).read_text(encoding="utf-8"), markdown)

    def test_concurrent_clients_recover_listener_instance_races(self):
        with ThreadPoolExecutor(max_workers=12) as pool:
            responses = list(pool.map(lambda _: self.call("GET_CONFIGURATION"), range(48)))
        self.assertEqual(len(responses), 48)
        self.assertTrue(all(response["status"] == "ok" for response in responses))


if __name__ == "__main__":
    unittest.main()
