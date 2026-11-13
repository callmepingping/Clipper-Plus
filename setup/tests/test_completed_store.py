"""Persistence, URL identity, history, and completion commit regressions."""

from concurrent.futures import ThreadPoolExecutor
import json
import os
import multiprocessing
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from videomind.completed_store import CompletedMarkdownStore, canonical_url
from videomind.api import Application
from videomind.errors import AppError
from videomind.jobs import JobManager


def _save_from_process(path, index):
    CompletedMarkdownStore(path).store_task_result(
        f"https://example.com/process/{index}", f"process-{index}",
        {"title": f"Process {index}", "markdown": f"# Process {index}"})


class CanonicalUrlTests(unittest.TestCase):
    def test_youtube_url_variants_map_to_one_video_without_merging_different_ids(self):
        urls = (
            "https://youtube.com/watch?v=abc123DEF_0&feature=share",
            "https://www.youtube.com/watch?v=abc123DEF_0",
            "https://m.youtube.com/watch?v=abc123DEF_0&t=4s",
            "https://youtu.be/abc123DEF_0?si=share",
            "https://www.youtube.com/shorts/abc123DEF_0",
            "https://youtube.com/live/abc123DEF_0",
            "https://youtube.com/embed/abc123DEF_0",
        )
        self.assertEqual({canonical_url(url) for url in urls}, {"https://www.youtube.com/watch?v=abc123DEF_0"})
        self.assertNotEqual(canonical_url(urls[0]), canonical_url("https://youtu.be/another456ID"))

    def test_generic_query_parameters_are_preserved_and_fragment_is_dropped(self):
        self.assertEqual(canonical_url("HTTPS://WWW.Example.com:443/video?id=a&track=2#captions"),
                         "https://www.example.com/video?id=a&track=2")
        self.assertNotEqual(canonical_url("https://example.com/video?id=a&utm_source=x"),
                            canonical_url("https://example.com/video?id=a"))
        self.assertEqual(canonical_url("http://example.com:80/video?b=2&a=1"),
                         "http://example.com/video?b=2&a=1")


class CompletedMarkdownStoreTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.root = Path(self.folder.name)
        self.path = self.root / "completed_markdown.json"
        self.store = CompletedMarkdownStore(self.path)
        self.url = "https://youtu.be/abc123DEF_0"

    def tearDown(self):
        self.folder.cleanup()

    def seed(self, url=None, task_id="task-1", markdown="# 测试\n\n東京 English"):
        self.store.store_task_result(url or self.url, task_id, {"title": "视频 🎞", "markdown": markdown})

    def test_task_result_is_utf8_atomic_and_survives_new_store_instance(self):
        self.seed()
        raw = self.path.read_bytes()
        self.assertNotIn(b"\\u", raw)
        data = json.loads(raw.decode("utf-8"))
        self.assertEqual(data["version"], 1)
        self.assertEqual(len(data["entries"]), 1)
        entry = CompletedMarkdownStore(self.path).get_markdown("https://m.youtube.com/watch?v=abc123DEF_0")
        self.assertEqual(entry["markdown"], "# 测试\n\n東京 English")
        self.assertEqual(entry["title"], "视频 🎞")
        self.assertEqual(entry["source"], "cache")
        self.assertEqual(list(self.root.glob("*.tmp")), [])

    def test_concurrent_urls_do_not_overwrite_each_other(self):
        stores = [CompletedMarkdownStore(self.path), CompletedMarkdownStore(self.path)]
        urls = [f"https://example.com/video/{index}?resource={index}" for index in range(36)]

        def save(index):
            stores[index % len(stores)].store_task_result(urls[index], f"task-{index}",
                                                          {"title": f"Video {index}", "markdown": f"# {index}"})

        with ThreadPoolExecutor(max_workers=12) as pool:
            list(pool.map(save, range(len(urls))))
        data = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(len(data["entries"]), len(urls))
        self.assertEqual(len(data["history"]), len(urls))
        for index, url in enumerate(urls):
            self.assertEqual(self.store.get_markdown(url)["markdown"], f"# {index}")

    def test_separate_process_writes_do_not_overwrite_each_other(self):
        context = multiprocessing.get_context("spawn")
        processes = [context.Process(target=_save_from_process, args=(str(self.path), index)) for index in range(8)]
        for process in processes:
            process.start()
        for process in processes:
            process.join(10)
        # Join every worker before asserting so a failure cannot race tempdir
        # cleanup while another process still owns the cache or lock files.
        for process in processes:
            self.assertFalse(process.is_alive(), "Concurrent cache writer did not exit.")
        for process in processes:
            self.assertEqual(process.exitcode, 0)
        data = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(len(data["entries"]), len(processes))
        self.assertEqual(len(data["history"]), len(processes))

    def test_delete_cache_keeps_history_and_downloaded_file_reopens_without_cache(self):
        self.seed()
        saved = self.root / "Downloaded Markdown.md"
        saved.write_text("# Downloaded 東京", encoding="utf-8")
        self.assertTrue(self.store.delete_markdown(self.url, str(saved.resolve())))
        self.assertIsNone(self.store.get_markdown("https://youtube.com/watch?v=abc123DEF_0"))
        restored = self.store.get_history_markdown("https://www.youtube.com/watch?v=abc123DEF_0&list=playlist")
        self.assertEqual(restored["markdown"], "# Downloaded 東京")
        self.assertEqual(restored["source"], "file")
        rows = self.store.list_history()
        self.assertEqual(rows["total"], 1)
        self.assertFalse(rows["items"][0]["markdown_available"])
        self.assertTrue(rows["items"][0]["file_available"])

    def test_history_deletion_does_not_delete_a_local_markdown_file(self):
        self.seed()
        saved = self.root / "saved.md"
        saved.write_text("Keep me", encoding="utf-8")
        self.store.record_local_file(self.url, "Video", str(saved.resolve()))
        self.assertTrue(self.store.delete_history(self.url, delete_cache=True))
        self.assertIsNone(self.store.get_markdown(self.url))
        self.assertTrue(saved.is_file())
        self.assertEqual(self.store.list_history()["total"], 0)

    def test_history_query_pagination_and_clear_preserve_cache_by_default(self):
        for index in range(4):
            self.store.store_task_result(f"https://example.com/video/{index}", f"task-{index}",
                                         {"title": f"Bird {index}", "markdown": f"# {index}"})
        page = self.store.list_history(query="BIRD", limit=2, offset=1)
        self.assertEqual((page["total"], page["limit"], page["offset"]), (4, 2, 1))
        self.assertEqual(len(page["items"]), 2)
        self.assertEqual(self.store.clear_history(), 4)
        self.assertEqual(self.store.list_history()["total"], 0)
        self.assertIsNotNone(self.store.get_markdown("https://example.com/video/0"))

    def test_edit_updates_only_existing_cache_or_history(self):
        with self.assertRaises(AppError) as caught:
            self.store.update_markdown(self.url, "# arbitrary")
        self.assertEqual(caught.exception.code, "MARKDOWN_NOT_FOUND")
        self.seed()
        edited = "# 改过的标题\n\n[00:00:03] 修改后的字幕"
        self.store.update_markdown("https://www.youtube.com/watch?v=abc123DEF_0", edited, "改过的标题")
        self.assertEqual(self.store.get_markdown(self.url)["markdown"], edited)
        self.assertEqual(self.store.list_history()["items"][0]["title"], "改过的标题")

    def test_atomic_replace_failure_preserves_previous_cache(self):
        self.seed(markdown="# previous")
        old_bytes = self.path.read_bytes()
        with patch("videomind.completed_store.os.replace", side_effect=OSError("disk full")):
            with self.assertRaises(AppError) as caught:
                self.store.store_task_result("https://example.com/new", "task-2", {"title": "New", "markdown": "# new"})
        self.assertEqual(caught.exception.code, "MARKDOWN_PERSISTENCE_FAILED")
        self.assertEqual(self.path.read_bytes(), old_bytes)
        self.assertEqual(self.store.get_markdown(self.url)["markdown"], "# previous")
        self.assertEqual(list(self.root.glob("*.tmp")), [])

    def test_transient_replace_permission_error_is_retried(self):
        real_replace = os.replace
        attempts = []

        def flaky_replace(source, destination):
            attempts.append(1)
            if len(attempts) < 3:
                raise PermissionError("transient Windows sharing violation")
            return real_replace(source, destination)

        with patch("videomind.completed_store.os.replace", side_effect=flaky_replace):
            self.seed(markdown="# retried")
        self.assertEqual(len(attempts), 3)
        self.assertEqual(self.store.get_markdown(self.url)["markdown"], "# retried")

    def test_large_history_file_is_not_read_into_native_response(self):
        self.seed()
        huge = self.root / "huge.md"
        huge.write_bytes(b"x" * 850_001)
        self.store.record_local_file(self.url, "Video", str(huge.resolve()))
        self.store.delete_markdown(self.url)
        with self.assertRaises(AppError) as caught:
            self.store.get_history_markdown(self.url)
        self.assertEqual(caught.exception.code, "HISTORY_FILE_TOO_LARGE")

    def test_api_commands_cover_cache_edit_download_history_and_safe_file_restore(self):
        config = {"version": 1, "language": "en", "storage": {"original_dir": str(self.root)},
                  "whisper": {"model": "base", "device": "cpu", "compute_type": "int8"}}
        app = Application(config, cache_store=self.store)

        def call(command, payload=None):
            response = app.handle({"id": command, "type": command, "payload": payload or {}})
            self.assertEqual(response["status"], "ok", response)
            return response["data"]

        self.seed()
        hit = call("GET_CACHED_MARKDOWN", {"url": "https://m.youtube.com/watch?v=abc123DEF_0"})["entry"]
        self.assertEqual(hit["markdown"], "# 测试\n\n東京 English")
        edited = "# Edited\n\n[00:00:02] 新しい字幕"
        call("UPDATE_CACHED_MARKDOWN", {"url": self.url, "markdown": edited, "title": "Edited"})
        self.assertEqual(call("GET_HISTORY_MARKDOWN", {"url": self.url})["entry"]["markdown"], edited)
        downloaded = self.root / "download.md"
        downloaded.write_text(edited, encoding="utf-8")
        call("DELETE_CACHED_MARKDOWN", {"url": self.url, "local_path": str(downloaded.resolve())})
        history_entry = call("GET_HISTORY_MARKDOWN", {"url": self.url})["entry"]
        self.assertEqual(history_entry["source"], "file")
        self.assertEqual(history_entry["markdown"], edited)
        rows = call("GET_HISTORY", {"query": "edited", "limit": 10, "offset": 0})
        self.assertEqual((rows["total"], rows["items"][0]["title"]), (1, "Edited"))
        call("DELETE_HISTORY", {"url": self.url, "delete_cache": True})
        self.assertTrue(downloaded.is_file())
        self.assertEqual(call("GET_HISTORY", {})["total"], 0)

    def test_cached_markdown_wins_even_when_latest_task_for_url_failed(self):
        config = {"version": 1, "language": "en", "storage": {"original_dir": str(self.root)},
                  "whisper": {"model": "base", "device": "cpu", "compute_type": "int8"}}
        app = Application(config, cache_store=self.store)
        self.seed(task_id="completed-before-popup-closed", markdown="# Persisted success")
        task_id = app.jobs.start("transcription", lambda *_: (_ for _ in ()).throw(
            AppError("VIDEO_DOWNLOAD_FAILED", "Download failed.")), source_url=self.url)
        deadline = time.monotonic() + 2
        task = app.jobs.status(task_id)
        while task["state"] not in {"COMPLETED", "FAILED", "CANCELLED"} and time.monotonic() < deadline:
            time.sleep(0.005)
            task = app.jobs.status(task_id)
        self.assertEqual(task["state"], "FAILED")
        response = app.handle({"id": "cached", "type": "GET_CACHED_MARKDOWN", "payload": {"url": self.url}})
        self.assertEqual(response["status"], "ok")
        self.assertEqual(response["data"]["entry"]["markdown"], "# Persisted success")

    def test_task_without_markdown_fails_and_does_not_persist(self):
        config = {"version": 1, "language": "en", "storage": {"original_dir": str(self.root)},
                  "whisper": {"model": "base", "device": "cpu", "compute_type": "int8"}}
        app = Application(config, cache_store=self.store)
        task_id = app.jobs.start("transcription", lambda *_: {"title": "Video", "markdown": ""},
                                 source_url=self.url)
        deadline = time.monotonic() + 2
        task = app.jobs.status(task_id)
        while task["state"] not in {"COMPLETED", "FAILED", "CANCELLED"} and time.monotonic() < deadline:
            time.sleep(0.005)
            task = app.jobs.status(task_id)
        self.assertEqual(task["state"], "FAILED")
        self.assertEqual(task["error"]["code"], "MARKDOWN_PERSISTENCE_FAILED")
        self.assertIsNone(self.store.get_markdown(self.url))


class CompletionOrderingTests(unittest.TestCase):
    def test_invalid_command_type_is_reported_without_unhandled_type_error(self):
        app = Application(config={"version": 1, "language": "en"}, cache_store=CompletedMarkdownStore("unused.json"))
        response = app.handle({"id": "bad-type", "type": [], "payload": {}})
        self.assertEqual(response["status"], "error")
        self.assertEqual(response["error"]["code"], "INVALID_REQUEST")

    def test_completed_state_is_visible_only_after_persistence_callback_returns(self):
        entered, release, persisted = threading.Event(), threading.Event(), threading.Event()

        def persist(task_id, url, result):
            entered.set()
            release.wait(2)
            persisted.set()

        manager = JobManager(on_completed=persist)
        task_id = manager.start("transcription", lambda *_: {"title": "Video", "markdown": "# Done"},
                                source_url="https://example.com/video")
        self.assertTrue(entered.wait(2))
        status_result = []
        status_read = threading.Event()
        reader = threading.Thread(target=lambda: (status_result.append(manager.status(task_id)), status_read.set()))
        reader.start()
        self.assertFalse(status_read.wait(0.03), "Status must not expose completion before cache persistence commits.")
        release.set()
        self.assertTrue(status_read.wait(2))
        reader.join(2)
        task = status_result[0]
        deadline = time.monotonic() + 2
        while task["state"] not in {"COMPLETED", "FAILED", "CANCELLED"} and time.monotonic() < deadline:
            time.sleep(0.005)
            task = manager.status(task_id)
        self.assertTrue(persisted.is_set())
        self.assertEqual(task["state"], "COMPLETED")

    def test_persistence_failure_marks_failed_and_never_reports_completed(self):
        def fail(*_):
            raise AppError("MARKDOWN_PERSISTENCE_FAILED", "Could not persist result.")

        manager = JobManager(on_completed=fail)
        task_id = manager.start("transcription", lambda *_: {"title": "Video", "markdown": "# Done"})
        deadline = time.monotonic() + 2
        task = manager.status(task_id)
        while task["state"] not in {"COMPLETED", "FAILED", "CANCELLED"} and time.monotonic() < deadline:
            time.sleep(0.005)
            task = manager.status(task_id)
        self.assertEqual(task["state"], "FAILED")
        self.assertEqual(task["error"]["code"], "MARKDOWN_PERSISTENCE_FAILED")
        self.assertIsNone(task["result"])

    def test_cancel_during_persistence_waits_then_observes_completed_state(self):
        entered, release = threading.Event(), threading.Event()
        cancel_result = []

        def persist(*_):
            entered.set()
            release.wait(2)

        manager = JobManager(on_completed=persist)
        task_id = manager.start("transcription", lambda *_: {"title": "Video", "markdown": "# Done"})
        self.assertTrue(entered.wait(2))
        cancelling = threading.Thread(target=lambda: cancel_result.append(manager.cancel(task_id)))
        cancelling.start()
        time.sleep(0.03)
        release.set()
        cancelling.join(2)
        task = manager.status(task_id)
        self.assertEqual(task["state"], "COMPLETED")
        self.assertEqual(cancel_result[0]["state"], "COMPLETED")


if __name__ == "__main__":
    unittest.main()
