import copy
import io
import json
import os
from pathlib import Path
import struct
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from videomind.api import Application
from videomind.config import load_config, validate_config
from videomind.contracts import MAX_MESSAGE_BYTES, decode_message, encode_message, validate_request
from videomind.completed_store import CompletedMarkdownStore
from videomind.errors import AppError
from videomind.jobs import JobManager
from videomind.native_host import bridge, read_message, write_message
from videomind.storage import safe_filename, save_markdown


def configuration(root):
    return {"version": 1, "language": "zh-CN", "storage": {"original_dir": str(root)}, "whisper": {"model": "base", "device": "cpu", "compute_type": "int8"}, "service": {"extension_id": "a" * 32}}


def wait_terminal(manager, identifier):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        task = manager.status(identifier)
        if task["state"] in {"COMPLETED", "CANCELLED", "FAILED"}:
            return task
        time.sleep(0.01)
    raise AssertionError("Task did not finish")


class ConfigTests(unittest.TestCase):
    def test_valid_config_and_utf8_bom(self):
        with tempfile.TemporaryDirectory() as root:
            config = configuration(root)
            target = Path(root) / "auth.json"
            target.write_text(json.dumps(config), encoding="utf-8-sig")
            self.assertEqual(load_config(target), config)

    def test_legacy_ai_and_summary_directory_are_tolerated_but_not_used(self):
        with tempfile.TemporaryDirectory() as root:
            config = configuration(root)
            config["ai"] = {"provider": "openai-compatible", "credential_ref": "VideoMind.AI.APIKey"}
            config["storage"]["summary_dir"] = str(Path(root) / "old-summary")
            self.assertEqual(validate_config(config), config)

    def test_missing_and_invalid_configuration_require_setup(self):
        for value in ({}, None, {"version": True}, []):
            with self.subTest(value=value), self.assertRaises(AppError) as caught:
                validate_config(value)
            self.assertEqual(caught.exception.code, "SETUP_REQUIRED")
        with tempfile.TemporaryDirectory() as root, self.assertRaises(AppError):
            load_config(Path(root) / "missing")

    def test_plaintext_secrets_and_relative_storage_are_rejected(self):
        with tempfile.TemporaryDirectory() as root:
            for mutation in (lambda c: c.update(api_key="never-print-this"), lambda c: c["storage"].update(original_dir="relative"), lambda c: c.update(language="fr")):
                config = configuration(root)
                mutation(config)
                with self.assertRaises(AppError) as caught:
                    validate_config(config)
                self.assertNotIn("never-print", str(caught.exception))


class ProtocolTests(unittest.TestCase):
    def test_native_frame_unicode_and_multiple_requests(self):
        stream = io.BytesIO()
        request = {"id": "一", "type": "GET_CONFIGURATION", "payload": {}}
        write_message(stream, request)
        write_message(stream, request)
        self.assertEqual(struct.unpack("<I", stream.getvalue()[:4])[0], len(encode_message(request)))
        stream.seek(0)
        output = io.BytesIO()
        bridge(stream, output, lambda item: {"id": item["id"], "status": "ok", "data": {"language": "ja"}})
        output.seek(0)
        self.assertEqual(read_message(output)["id"], "一")
        self.assertEqual(read_message(output)["data"]["language"], "ja")
        self.assertIsNone(read_message(output))

    def test_native_bridge_keeps_stdout_framed_after_internal_error(self):
        source, output = io.BytesIO(), io.BytesIO()
        write_message(source, {"id": "correlated", "type": "GET_CONFIGURATION", "payload": {}})
        source.seek(0)
        bridge(source, output, lambda _request: (_ for _ in ()).throw(RuntimeError("private detail")))
        output.seek(0)
        response = read_message(output)
        self.assertEqual(response["id"], "correlated")
        self.assertEqual(response["error"]["code"], "NATIVE_HOST_INTERNAL_ERROR")
        self.assertNotIn("private detail", str(response))

    def test_rejects_oversize_truncation_malformed_and_nan(self):
        for raw in (b"\xff", b"{", b"NaN"):
            with self.subTest(raw=raw), self.assertRaises(AppError):
                decode_message(raw)
        for raw in (struct.pack("<I", MAX_MESSAGE_BYTES + 1), struct.pack("<I", 100) + b"{}", b"\x01"):
            with self.subTest(raw=raw), self.assertRaises(AppError):
                read_message(io.BytesIO(raw))

    def test_request_validation(self):
        for request in (None, [], {"id": 1, "type": "GET_CONFIGURATION"}, {"id": "x", "type": "DELETE_FILE"}, {"id": "x", "type": "GET_CONFIGURATION", "payload": []}):
            with self.subTest(request=request), self.assertRaises(AppError):
                validate_request(request)

    def test_protocol_version_is_explicit_and_mismatches_are_reported(self):
        request = {"id": "request-1", "type": "GET_CONFIGURATION", "payload": {}, "protocol_version": 2}
        self.assertEqual(validate_request(request), ("GET_CONFIGURATION", {}))
        # Compatibility with extensions that predate the versioned envelope.
        self.assertEqual(validate_request({key: value for key, value in request.items() if key != "protocol_version"}),
                         ("GET_CONFIGURATION", {}))
        request["protocol_version"] = 99
        with self.assertRaises(AppError) as raised:
            validate_request(request)
        self.assertEqual(raised.exception.code, "PROTOCOL_VERSION_MISMATCH")


class StorageTests(unittest.TestCase):
    def test_titles_cannot_escape_output_and_reserved_names_are_safe(self):
        self.assertEqual(safe_filename("CON"), "_CON")
        self.assertEqual(safe_filename(".."), "Untitled")
        with tempfile.TemporaryDirectory() as root:
            path = Path(save_markdown(configuration(root), "raw", "../../CON:<bad>"))
            self.assertEqual(path.parent, Path(root))
            self.assertEqual(path.read_text(), "raw")

    def test_duplicate_original_never_overwrites_existing_file(self):
        with tempfile.TemporaryDirectory() as root:
            config = configuration(root)
            first = save_markdown(config, "raw", "Same")
            duplicate = save_markdown(config, "other", "Same")
            self.assertNotEqual(first, duplicate)
            self.assertEqual(Path(first).read_text(), "raw")

    def test_unavailable_directory(self):
        with tempfile.TemporaryDirectory() as root:
            config = configuration(Path(root) / "missing")
            with self.assertRaises(AppError) as caught:
                save_markdown(config, "raw", "Title")
            self.assertEqual(caught.exception.code, "OUTPUT_DIRECTORY_INVALID")


class JobTests(unittest.TestCase):
    def test_concurrent_start_and_cancel_wait_for_cleanup(self):
        manager = JobManager()
        started, release = threading.Event(), threading.Event()
        def worker(progress, cancel):
            started.set()
            release.wait(3)
            return {"markdown": "raw", "title": "Title"}
        identifier = manager.start("transcription", worker)
        self.assertTrue(started.wait(2))
        with self.assertRaises(AppError) as caught:
            manager.start("transcription", worker)
        self.assertEqual(caught.exception.code, "TASK_BUSY")
        self.assertNotEqual(manager.cancel(identifier)["state"], "CANCELLED")
        with self.assertRaises(AppError):
            manager.start("transcription", worker)
        release.set()
        self.assertEqual(wait_terminal(manager, identifier)["state"], "CANCELLED")

    def test_internal_error_redacted_and_restart_empty(self):
        manager = JobManager()
        def worker(*_):
            raise RuntimeError("secret-value")
        identifier = manager.start("transcription", worker)
        self.assertNotIn("secret-value", json.dumps(wait_terminal(manager, identifier)))
        self.assertIsNone(JobManager().status())


class ApplicationTests(unittest.TestCase):
    def test_transcription_save_and_recovery(self):
        with tempfile.TemporaryDirectory() as root:
            config = configuration(root)
            transcriber = lambda *_, **__: {"markdown": "# Original\n\n[00:00:01] Words", "title": "Video"}
            cache_store = CompletedMarkdownStore(Path(root) / "completed_markdown.json")
            app = Application(config, transcriber=transcriber, cache_store=cache_store)
            def call(command, payload=None):
                result = app.handle({"id": "test", "type": command, "payload": payload or {}})
                self.assertNotEqual(result["status"], "error", result)
                return result["data"]
            self.assertNotIn("credential", json.dumps(call("GET_CONFIGURATION")))
            self.assertNotIn("ai", call("GET_CONFIGURATION"))
            identifier = call("START_TRANSCRIPTION", {"url": "https://example.com/v"})["task_id"]
            transcription_task = wait_terminal(app.jobs, identifier)
            original = transcription_task["result"]
            self.assertEqual(transcription_task["source_url"], "https://example.com/v")
            task = call("GET_TASK_STATUS")["task"]
            self.assertEqual(task["source_url"], "https://example.com/v")
            cached = call("GET_CACHED_MARKDOWN", {"url": "https://example.com/v"})["entry"]
            self.assertEqual(cached["markdown"], original["markdown"])
            history = call("GET_HISTORY", {"limit": 50, "offset": 0})
            self.assertEqual(history["items"][0]["task_id"], identifier)
            first = call("SAVE_ORIGINAL", {**original, "source_url": "https://example.com/v"})["path"]
            self.assertEqual(Path(first).read_text(), original["markdown"])
            self.assertTrue(call("GET_HISTORY", {})["items"][0]["file_available"])

    def test_summary_commands_are_not_supported(self):
        with tempfile.TemporaryDirectory() as root:
            app = Application(configuration(root), cache_store=CompletedMarkdownStore(Path(root) / "completed_markdown.json"))
            for command in ("GENERATE_SUMMARY", "SAVE_SUMMARY", "VIDEO_INSPECT"):
                result = app.handle({"id": "removed", "type": command, "payload": {}})
                self.assertEqual(result["status"], "error")

    def test_injected_transcriber_receives_current_service_configuration(self):
        with tempfile.TemporaryDirectory() as root:
            config = configuration(root)
            config["service"] = {"js_runtime_path": str(Path(root) / "deno.exe")}
            received = []

            def transcriber(url, metadata, whisper_config, progress, cancel, service_config=None):
                received.append(service_config)
                return {"title": "Video", "markdown": "# Original"}

            app = Application(config, transcriber=transcriber, cache_store=CompletedMarkdownStore(Path(root) / "completed_markdown.json"))
            response = app.handle({"id": "runtime-test", "type": "START_TRANSCRIPTION",
                                   "payload": {"url": "https://example.com/video"}})
            self.assertEqual(response["status"], "accepted")
            wait_terminal(app.jobs, response["data"]["task_id"])
            self.assertEqual(received, [config["service"]])

    def test_malformed_input_does_not_expose_exception(self):
        with tempfile.TemporaryDirectory() as root:
            app = Application(configuration(root), cache_store=CompletedMarkdownStore(Path(root) / "completed_markdown.json"))
            for request in (None, {"id": "x", "type": "GENERATE_SUMMARY", "payload": {"markdown": ""}}, {"id": "x", "type": "GET_TASK_STATUS", "payload": {"task_id": "unknown"}}):
                self.assertEqual(app.handle(request)["status"], "error")


if __name__ == "__main__":
    unittest.main()
