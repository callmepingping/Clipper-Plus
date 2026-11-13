import json
import tempfile
import unittest
from pathlib import Path

from videomind.downloader import classify_download_error, js_runtime_options, yt_dlp_options
from videomind.errors import AppError


class DownloaderTests(unittest.TestCase):
    def test_runtime_absolute_path_is_explicit_for_all_ytdlp_calls(self):
        with tempfile.TemporaryDirectory() as directory:
            deno = Path(directory) / "deno.exe"
            deno.write_bytes(b"test executable")
            runtime = js_runtime_options({"js_runtime_path": str(deno)})
            self.assertEqual(runtime, {"deno": {"path": str(deno)}})
            options = yt_dlp_options({"js_runtime_path": str(deno)})
            self.assertEqual(options["js_runtimes"], runtime)
            self.assertEqual(options["remote_components"], [])

    def test_missing_configured_runtime_is_actionable(self):
        with self.assertRaises(AppError) as caught:
            js_runtime_options({"js_runtime_path": r"C:\missing\deno.exe"})
        self.assertEqual(caught.exception.code, "YOUTUBE_JS_RUNTIME_MISSING")

    def test_youtube_errors_have_distinct_safe_categories(self):
        cases = {
            "No supported JavaScript runtime could be found": "YOUTUBE_JS_RUNTIME_MISSING",
            "n challenge solving failed": "YOUTUBE_JS_CHALLENGE_FAILED",
            "yt-dlp-ejs scripts were not installed": "YOUTUBE_EJS_MISSING",
            "PO Token provider required": "YOUTUBE_PO_TOKEN_REQUIRED",
            "Requested format is not available": "VIDEO_FORMAT_UNAVAILABLE",
            "This video is unavailable": "VIDEO_UNAVAILABLE",
            "Sign in to confirm you are not a bot": "VIDEO_AUTHENTICATION_REQUIRED",
            "<urllib error with https://private.example/watch?secret=x>": "VIDEO_NETWORK_ERROR",
        }
        for message, expected in cases.items():
            with self.subTest(message=message):
                result = classify_download_error(RuntimeError(message))
                self.assertEqual(result.code, expected)
                self.assertNotIn("private.example", json.dumps(result.to_dict()))
                self.assertNotIn("secret", json.dumps(result.to_dict()))

    def test_http_forbidden_is_not_misreported_as_authentication(self):
        error = type("HTTPError", (Exception,), {"code": 403})("private request")
        self.assertEqual(classify_download_error(error).code, "VIDEO_DOWNLOAD_FAILED")


if __name__ == "__main__":
    unittest.main()
