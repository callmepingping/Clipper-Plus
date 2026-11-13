"""The Setup and Native Host use one Ready handshake and one start coordinator."""
from contextlib import nullcontext
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from videomind.engine_lifecycle import _health_check, ensure_engine_ready, prepare_runtime_configuration
from videomind.errors import AppError


def write_config(path):
    path.write_text(json.dumps({
        "version": 2,
        "storage": {"original_dir": str(path.parent / "originals")},
        "whisper": {"model": "base", "device": "cpu", "compute_type": "int8"},
        "service": {"extension_id": "a" * 32},
    }), encoding="utf-8")


class LifecycleTests(unittest.TestCase):
    def test_health_check_requires_matching_protocol_ready_response(self):
        with patch("videomind.engine_lifecycle.call_engine", return_value={
                "id": "engine-ready-check", "status": "ok", "data": {"protocol_version": 2}}):
            self.assertEqual(_health_check(Path("auth.json"))["status"], "ok")
        with patch("videomind.engine_lifecycle.call_engine", return_value={
                "id": "engine-ready-check", "status": "ok", "data": {"protocol_version": 1}}):
            with self.assertRaises(AppError) as raised:
                _health_check(Path("auth.json"))
        self.assertEqual(raised.exception.code, "PROTOCOL_VERSION_MISMATCH")

    def test_missing_explicit_configuration_does_not_create_project_runtime(self):
        with tempfile.TemporaryDirectory() as root:
            project = Path(root) / "project"
            missing = Path(root) / "other" / "auth.json"
            with patch.dict("os.environ", {"CLIPPER_PLUS_PROJECT_ROOT": str(project),
                                            "VIDEOMIND_CONFIG": str(missing)}):
                with self.assertRaises(AppError) as raised:
                    prepare_runtime_configuration()
            self.assertEqual(raised.exception.code, "SETUP_REQUIRED")
            self.assertFalse((project / "runtime").exists())

    def test_host_does_not_migrate_legacy_configuration_on_first_request(self):
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            project = root / "project"
            legacy = root / "legacy-auth.json"
            write_config(legacy)
            with patch.dict("os.environ", {"CLIPPER_PLUS_PROJECT_ROOT": str(project)}, clear=False), \
                    patch("videomind.engine_lifecycle.legacy_config_path", return_value=legacy):
                with self.assertRaises(AppError) as raised:
                    prepare_runtime_configuration()
            self.assertEqual(raised.exception.code, "SETUP_REQUIRED")
            self.assertFalse((project / "runtime").exists())
            self.assertTrue(legacy.is_file())

    def test_healthy_engine_is_reused_without_launch(self):
        with tempfile.TemporaryDirectory() as root:
            config = Path(root) / "auth.json"
            write_config(config)
            launcher = unittest.mock.Mock()
            health = unittest.mock.Mock(return_value={"status": "ok"})
            result = ensure_engine_ready(config, launcher=launcher, health_check=health)
            self.assertEqual(result["state"], "READY")
            self.assertFalse(result["started"])
            launcher.assert_not_called()

    def test_engine_start_waits_for_health_ready_not_only_process_creation(self):
        with tempfile.TemporaryDirectory() as root:
            config = Path(root) / "auth.json"
            write_config(config)
            process = unittest.mock.Mock()
            process.poll.return_value = None
            launcher = unittest.mock.Mock(return_value=process)
            from videomind.engine_lifecycle import _start_lock
            health = unittest.mock.Mock(side_effect=[
                AppError("IPC_CONNECTION_FAILED", "not ready"),
                AppError("IPC_CONNECTION_FAILED", "not ready"),
                {"status": "ok"},
            ])
            with patch("videomind.engine_lifecycle._start_lock", return_value=nullcontext()):
                result = ensure_engine_ready(config, timeout=1, launcher=launcher, health_check=health)
            self.assertTrue(result["started"])
            self.assertEqual(result["state"], "READY")
            self.assertEqual(launcher.call_count, 1)
            self.assertEqual(health.call_count, 3)

    def test_setup_required_when_engine_launch_is_disabled_and_engine_is_not_ready(self):
        with tempfile.TemporaryDirectory() as root:
            config = Path(root) / "auth.json"
            write_config(config)
            health = unittest.mock.Mock(side_effect=AppError("IPC_CONNECTION_FAILED", "offline"))
            result = ensure_engine_ready(config, start=False, health_check=health)
            self.assertEqual(result["state"], "NOT_RUNNING")
            self.assertFalse(result["started"])


if __name__ == "__main__":
    unittest.main()
