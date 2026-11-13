import errno
import inspect
import multiprocessing
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import threading
import time
import types
import unittest
from unittest.mock import Mock, patch

from videomind.errors import AppError
from videomind import transcription as module


PUBLIC = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))]


def wait_worker(connection):
    connection.send(("progress", ("TRANSCRIBING", 0.5, "Working.")))
    time.sleep(30)
    return {"unexpected": True}


def success_worker(connection):
    connection.send(("progress", ("TRANSCRIBING", 0.5, "Working.")))
    return {"text": "A real spawned worker."}


def descendant_worker(connection):
    process = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                               creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
    connection.send(("progress", ("DOWNLOADING", 0.1, str(process.pid))))
    process.wait()
    return {}


class UrlTests(unittest.TestCase):
    def test_rejects_local_and_non_http_urls(self):
        for url in ("file:///C:/secret", "https://localhost/video", "http://example.local/v",
                    "http://user:password@example.com/video", "http://example.com:22/v", "invalid"):
            with self.subTest(url=url), self.assertRaises(AppError) as caught:
                module._validate_url(url)
            self.assertEqual(caught.exception.code, "INVALID_URL")

    def test_rejects_private_and_mixed_dns_answers(self):
        for address in ("127.0.0.1", "10.0.0.5", "169.254.169.254", "::1", "192.168.1.2"):
            answers = PUBLIC + [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, 443))]
            with patch.object(socket, "getaddrinfo", return_value=answers), self.assertRaises(AppError):
                module._validate_url("https://public.example/video")

    def test_public_url_and_dns_failure(self):
        with patch.object(socket, "getaddrinfo", return_value=PUBLIC):
            self.assertEqual(module._validate_url("https://example.com/watch?v=1"), "https://example.com/watch?v=1")
        with patch.object(socket, "getaddrinfo", side_effect=socket.gaierror()), self.assertRaises(AppError) as caught:
            module._validate_url("https://example.com/video")
        self.assertEqual(caught.exception.code, "NETWORK_ERROR")


class MarkdownTests(unittest.TestCase):
    def test_merge_preserves_nonempty_upstream_metadata(self):
        result = module._merge_metadata({"title": "Upstream", "author": "", "description": None},
                                        {"title": "Downloader", "author": "Channel", "description": "Video description"}, "https://example.com/v")
        self.assertEqual(result["title"], "Upstream")
        self.assertEqual(result["author"], "Channel")
        self.assertEqual(result["description"], "Video description")

    def test_formatter_handles_yaml_characters_timestamps_and_source_speakers(self):
        markdown = module.format_markdown({"title": "A: \"quoted\"\nTitle", "url": "https://example.com/v", "author": "A\nB", "markdown": "Upstream content"},
                                          [{"start": 3661.9, "text": "Source words", "speaker": "Alice"}, {"start": 0, "text": "Unlabelled"}])
        self.assertIn('title: "A: \\"quoted\\" Title"', markdown)
        self.assertIn("[01:01:01] Alice: Source words", markdown)
        self.assertIn("[00:00:00] Unlabelled", markdown)
        self.assertNotIn("Speaker 1", markdown)
        self.assertIn("Upstream content", markdown)
        self.assertIn('author: "A\\nB"', markdown)


class DiagnosticTests(unittest.TestCase):
    def test_distinct_decoder_failures_are_not_returned_to_users_or_logs(self):
        import logging
        import io
        stream = io.StringIO()
        handler = logging.StreamHandler(stream)
        logger = logging.getLogger("videomind.transcription")
        logger.addHandler(handler)
        try:
            try:
                raise TypeError("metadata_errors rejected; url=https://private.example/watch?token=secret Authorization: Bearer secret")
            except TypeError as exc:
                module._log_exception("whisper_audio_transcription", exc)
                error = module._safe_error(exc, "transcription")
        finally:
            logger.removeHandler(handler)
        self.assertEqual(error.code, "TRANSCRIPTION_FAILED")
        self.assertIn("exception_type=TypeError", stream.getvalue())
        self.assertIn("whisper_audio_transcription", stream.getvalue())
        self.assertIn("metadata_errors rejected", stream.getvalue())
        self.assertNotIn("private.example", stream.getvalue())
        self.assertNotIn("secret", stream.getvalue())

    def test_safe_diagnostic_preserves_reason_but_redacts_video_url_and_secrets(self):
        detail = module._safe_diagnostic(RuntimeError(
            "Challenge failed https://youtube.example/watch?v=private&token=abc Authorization: Bearer xyz Cookie: session=123"))
        self.assertIn("Challenge failed", detail)
        self.assertNotIn("youtube.example", detail)
        self.assertNotIn("private", detail)
        self.assertNotIn("abc", detail)
        self.assertNotIn("xyz", detail)
        self.assertNotIn("123", detail)

    def test_pyav_18_metadata_errors_decoder_regression(self):
        import av
        self.assertEqual(av.__version__, "18.1.0")
        # Real WAV/MP3 integration and CPU INT8 decoding are exercised manually in
        # this environment; this guards the deployed version bound in CI.
        from pathlib import Path
        requirements = Path(__file__).parents[1] / "requirements.txt"
        self.assertIn("av>=11,<19", requirements.read_text(encoding="utf-8"))


class PipelineTests(unittest.TestCase):
    def setUp(self):
        self.url = patch.object(module, "_validate_url", side_effect=lambda url: url)
        self.which = patch.object(module.shutil, "which", return_value="ffmpeg")
        self.url.start()
        self.which.start()
        self.addCleanup(self.url.stop)
        self.addCleanup(self.which.stop)

    def test_pipeline_cleans_audio_and_produces_metadata_and_progress(self):
        def run(target, arguments, *rest, **kwargs):
            if target == module._download_worker:
                audio = Path(arguments[1]) / "audio.mp3"
                audio.write_bytes(b"fake audio")
                return {"audio": str(audio), "metadata": {"title": "Video", "author": "Channel"}}
            return [{"start": 5, "end": 8, "text": "Transcribed sentence."}]
        progress = Mock()
        service_config = {"js_runtime_path": r"C:\\VideoMind\\deno.exe"}
        calls = []
        def recording_run(target, arguments, *rest, **kwargs):
            calls.append((target, arguments))
            return run(target, arguments, *rest, **kwargs)
        with tempfile.TemporaryDirectory() as root, patch.object(module, "_run_child", side_effect=recording_run):
            result = module.transcribe_video("https://example.com/v", {"title": "Upstream title", "markdown": "Original page description"}, {}, progress, threading.Event(), root, service_config=service_config)
            self.assertEqual(list(Path(root).iterdir()), [])
        self.assertEqual(calls[0][0], module._download_worker)
        self.assertEqual(calls[0][1][2], service_config)
        self.assertEqual(result["title"], "Upstream title")
        self.assertIn("[00:00:05] Transcribed sentence.", result["markdown"])
        self.assertIn("Original page description", result["markdown"])
        self.assertNotIn("markdown", result["metadata"])
        self.assertEqual(progress.call_args.args[0], "GENERATING_MARKDOWN")

    def test_failed_worker_cleans_temp_directory(self):
        with tempfile.TemporaryDirectory() as root, patch.object(module, "_run_child", side_effect=AppError("DOWNLOAD_FAILED", "Failed.")):
            with self.assertRaises(AppError):
                module.transcribe_video("https://example.com/v", {}, {}, Mock(), threading.Event(), root)
            self.assertEqual(list(Path(root).iterdir()), [])

    def test_cancelled_task_never_starts_download(self):
        event = threading.Event()
        event.set()
        with patch.object(module, "_run_child") as run, self.assertRaises(AppError) as caught:
            module.transcribe_video("https://example.com/v", {}, {}, Mock(), event)
        self.assertEqual(caught.exception.code, "CANCELLED")
        run.assert_not_called()

    def test_insufficient_disk_space_and_missing_ffmpeg(self):
        with patch.object(module.shutil, "disk_usage", return_value=types.SimpleNamespace(free=1)), self.assertRaises(AppError) as caught:
            module.transcribe_video("https://example.com/v", {}, {}, Mock(), threading.Event())
        self.assertEqual(caught.exception.code, "INSUFFICIENT_DISK_SPACE")
        with patch.object(module.shutil, "which", return_value=None), self.assertRaises(AppError) as caught:
            module.transcribe_video("https://example.com/v", {}, {}, Mock(), threading.Event())
        self.assertEqual(caught.exception.code, "FFMPEG_UNAVAILABLE")

    def test_no_speech_is_explicit_and_cleans_audio(self):
        with tempfile.TemporaryDirectory() as root, patch.object(module, "_run_child", side_effect=[{"audio": "fake.mp3", "metadata": {}}, []]):
            with self.assertRaises(AppError) as caught:
                module.transcribe_video("https://example.com/v", {}, {}, Mock(), threading.Event(), root)
            self.assertEqual(caught.exception.code, "NO_SPEECH_DETECTED")
            self.assertEqual(list(Path(root).iterdir()), [])

    def test_whisper_detects_language_and_never_invents_speakers(self):
        model = Mock()
        model.transcribe.return_value = ([types.SimpleNamespace(start=0, end=1, text=" Real words ")], types.SimpleNamespace(duration=1))
        factory = Mock(return_value=model)
        with patch.dict("sys.modules", {"faster_whisper": types.SimpleNamespace(WhisperModel=factory)}):
            segments = module._whisper_worker("audio.mp3", {"model": "base", "device": "cpu", "compute_type": "int8"}, Mock())
        model.transcribe.assert_called_once_with("audio.mp3", language=None, vad_filter=True)
        self.assertEqual(segments, [{"start": 0, "end": 1, "text": "Real words"}])

    def test_whisper_transcription_language_is_separate_from_ui_language(self):
        model = Mock()
        model.transcribe.return_value = ([], types.SimpleNamespace(duration=1))
        factory = Mock(return_value=model)
        with patch.dict("sys.modules", {"faster_whisper": types.SimpleNamespace(WhisperModel=factory)}):
            module._whisper_worker("audio.mp3", {"language": "zh", "model": "base", "device": "cpu", "compute_type": "int8"}, Mock())
        model.transcribe.assert_called_once_with("audio.mp3", language="zh", vad_filter=True)

    def test_video_markdown_keeps_unicode_and_special_symbols(self):
        markdown = module.format_markdown({"title": "中文 日本語 🌏", "url": "https://example.com/v",
                                           "description": "引号 ' 和符号 & < >"},
                                          [{"start": 1, "end": 2, "text": "你好 / Hello / こんにちは 🎬"}])
        self.assertIn("中文 日本語 🌏", markdown)
        self.assertIn("你好 / Hello / こんにちは 🎬", markdown)
        self.assertIn("[00:00:01]", markdown)

    def test_whisper_model_failure_is_safe(self):
        with patch.dict("sys.modules", {"faster_whisper": types.SimpleNamespace(WhisperModel=Mock(side_effect=RuntimeError("secret download error")))}):
            with self.assertRaises(AppError) as caught:
                module._whisper_worker("audio.mp3", {}, Mock())
        self.assertEqual(caught.exception.code, "WHISPER_MODEL_FAILED")
        self.assertNotIn("secret", str(caught.exception))

    def test_download_uses_audio_format_and_constant_output_template(self):
        with tempfile.TemporaryDirectory() as directory:
            fake = Mock()
            def extract(url, download):
                (Path(directory) / "audio.mp3").write_bytes(b"audio")
                return {"title": "Downloaded"}
            fake.extract_info.side_effect = extract
            factory = Mock()
            factory.return_value.__enter__ = Mock(return_value=fake)
            factory.return_value.__exit__ = Mock(return_value=False)
            with patch.dict("sys.modules", {"yt_dlp": types.SimpleNamespace(YoutubeDL=factory)}):
                result = module._download_worker("https://example.com/v", directory, {}, Mock())
            options = factory.call_args.args[0]
            self.assertEqual(options["format"], "bestaudio/best")
            self.assertEqual(Path(result["audio"]), Path(directory) / "audio.mp3")
            self.assertEqual(options["postprocessors"][0]["preferredcodec"], "mp3")
            self.assertNotIn("title", options["outtmpl"])


class SpawnTests(unittest.TestCase):
    def test_worker_signatures_put_ipc_connection_after_worker_arguments(self):
        self.assertEqual(list(inspect.signature(module._download_worker).parameters),
                         ["url", "directory", "service_config", "connection"])
        self.assertEqual(list(inspect.signature(module._whisper_worker).parameters),
                         ["audio", "config", "connection"])

    def test_real_spawn_worker_result(self):
        progress = Mock()
        result = module._run_child(success_worker, (), threading.Event(), progress, timeout=15)
        self.assertEqual(result["text"], "A real spawned worker.")
        progress.assert_called_once()

    def test_real_spawn_cancellation_reaps_child(self):
        event = threading.Event()
        before = {child.pid for child in multiprocessing.active_children()}
        def progress(*args):
            event.set()
        started = time.monotonic()
        with self.assertRaises(AppError) as caught:
            module._run_child(wait_worker, (), event, progress, timeout=15)
        self.assertEqual(caught.exception.code, "CANCELLED")
        self.assertLess(time.monotonic() - started, 10)
        self.assertEqual({child.pid for child in multiprocessing.active_children()}, before)

    @unittest.skipUnless(os.name == "nt", "Windows job-object descendant behavior")
    def test_cancellation_terminates_ffmpeg_like_descendant(self):
        import win32api
        import win32con
        import win32event
        event = threading.Event()
        handles = []
        def progress(state, value, message):
            handles.append(win32api.OpenProcess(win32con.SYNCHRONIZE, False, int(message)))
            event.set()
        try:
            with self.assertRaises(AppError) as caught:
                module._run_child(descendant_worker, (), event, progress, timeout=15)
            self.assertEqual(caught.exception.code, "CANCELLED")
            self.assertEqual(win32event.WaitForSingleObject(handles[0], 3000), win32event.WAIT_OBJECT_0)
        finally:
            for handle in handles:
                handle.Close()


if __name__ == "__main__":
    unittest.main()
