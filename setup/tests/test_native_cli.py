"""Exercise the actual bridge entry point, not just its frame helper."""
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from videomind.native_host import read_message, write_message


class NativeEntryTests(unittest.TestCase):
    def invoke(self, configuration, origin):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "auth.json"
            if configuration is not None:
                path.write_text(json.dumps(configuration), encoding="utf-8")
            frame = io.BytesIO()
            write_message(frame, {"id": "correlated", "type": "GET_CONFIGURATION", "payload": {}})
            environment = dict(os.environ, VIDEOMIND_CONFIG=str(path))
            result = subprocess.run([sys.executable, "-m", "videomind.native_host", origin], input=frame.getvalue(), capture_output=True, env=environment, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr)
            return read_message(io.BytesIO(result.stdout))

    def test_missing_config_error_carries_request_id(self):
        response = self.invoke(None, "chrome-extension://" + "a" * 32 + "/")
        self.assertEqual(response["id"], "correlated")
        self.assertEqual(response["error"]["code"], "SETUP_REQUIRED")

    def test_unregistered_origin_is_rejected_before_engine_contact(self):
        with tempfile.TemporaryDirectory() as root:
            config = {"version": 1, "language": "en", "storage": {"original_dir": root}, "whisper": {"model": "base", "device": "cpu", "compute_type": "int8"}, "service": {"extension_id": "a" * 32}}
            response = self.invoke(config, "chrome-extension://" + "b" * 32 + "/")
            self.assertEqual(response["id"], "correlated")
            self.assertEqual(response["error"]["code"], "NATIVE_ORIGIN_DENIED")


if __name__ == "__main__":
    unittest.main()
