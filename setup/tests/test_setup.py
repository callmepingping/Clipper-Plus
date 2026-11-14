"""Setup orchestration tests use isolated paths and never touch a real install."""
import importlib.util
import hashlib
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import types
import unittest
from unittest.mock import MagicMock, patch
import xml.etree.ElementTree as ET
import zipfile

from videomind.config import legacy_config_path
from environment import install_requirements

SETUP_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SETUP_DIR))
from windows_integration import (_parse_schtasks_xml, configure_logon_task,
                                 query_logon_task, register_native_host, task_xml)
import windows_integration

_SPEC = importlib.util.spec_from_file_location("clipper_setup_test", SETUP_DIR / "main.py")
setup = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(setup)


def valid_config(version=2):
    value = {"version": version, "storage": {"original_dir": "C:/legacy/originals"},
             "whisper": {"model": "base", "device": "cpu", "compute_type": "int8"},
             "service": {"extension_id": "a" * 32}}
    if version == 1:
        value["language"] = "zh-CN"
    return value


class InstallationDetectionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.paths = setup.runtime_paths(self.root)
        self.setup_dir = self.root / "setup"
        self.python = self.setup_dir / ".build" / "venv" / "Scripts" / "python.exe"
        self.python.parent.mkdir(parents=True)
        self.python.write_bytes(b"fake python")
        self.stack = patch.multiple(setup, SETUP=self.setup_dir)
        self.stack.start()
        self.addCleanup(self.stack.stop)
        self.legacy = self.root / "legacy" / "auth.json"
        self.legacy.parent.mkdir()
        self.legacy_patch = patch.object(setup, "legacy_config_path", return_value=self.legacy)
        self.legacy_patch.start()
        self.addCleanup(self.legacy_patch.stop)

    def tearDown(self):
        self.temp.cleanup()

    def test_no_runtime_or_environment_is_fresh(self):
        self.python.unlink()
        self.assertEqual(setup.detect_installation(self.paths), "Fresh Installation")

    def test_legacy_configuration_is_upgrade_repair(self):
        self.legacy.write_text(json.dumps(valid_config(1)), encoding="utf-8")
        self.assertEqual(setup.detect_installation(self.paths), "Upgrade / Repair")

    def test_runtime_v2_configuration_is_existing(self):
        self.paths.root.mkdir()
        self.paths.auth.write_text(json.dumps(valid_config()), encoding="utf-8")
        with patch.object(setup, "inspect_python_dependencies",
                          return_value={"missing": [], "pip_check": True}):
            self.assertEqual(setup.detect_installation(self.paths), "Existing Installation")

    def test_invalid_configuration_is_broken(self):
        self.paths.root.mkdir()
        self.paths.auth.write_text("{}", encoding="utf-8")
        self.assertEqual(setup.detect_installation(self.paths), "Broken Installation")

    def test_missing_environment_or_configuration_is_partial(self):
        self.assertEqual(setup.detect_installation(self.paths), "Partial Installation")


class ExtensionIdSelectionTests(unittest.TestCase):
    def test_detects_current_unpacked_extension_and_ignores_stale_record(self):
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            folder = root / "dist"
            folder.mkdir()
            (folder / "manifest.json").write_text(json.dumps({
                "name": "Clipper Plus", "manifest_version": 3, "permissions": ["nativeMessaging"],
            }), encoding="utf-8")
            profile = root / "User Data" / "Default"
            profile.mkdir(parents=True)
            (profile / "Secure Preferences").write_text(json.dumps({"extensions": {"settings": {
                "a" * 32: {"location": 4, "path": None},
                "b" * 32: {"location": 4, "path": str(folder), "disable_reasons": []},
            }}}), encoding="utf-8")
            self.assertEqual(setup.discover_chrome_extensions(root / "User Data"), {"b" * 32: folder})

    def test_existing_installation_uses_detected_extension_instead_of_stale_id(self):
        with patch.object(setup, "discover_chrome_extensions", return_value={"b" * 32: Path("dist")}), \
                patch.object(setup, "ask", side_effect=lambda _label, default="": default) as ask:
            selected = setup.select_extension_id(valid_config())
        self.assertEqual(selected, "b" * 32)
        self.assertEqual(ask.call_args.args[1], "b" * 32)

    def test_unverified_legacy_id_is_not_offered_as_default(self):
        with patch.object(setup, "discover_chrome_extensions", return_value={}), \
                patch.object(setup, "ask", return_value="b" * 32) as ask:
            selected = setup.select_extension_id(valid_config())
        self.assertEqual(selected, "b" * 32)
        self.assertEqual(ask.call_args.args[1], "")

    def test_explicit_extension_id_skips_prompt_and_is_validated(self):
        with patch.object(setup, "ask") as ask:
            self.assertEqual(setup.select_extension_id(valid_config(), "b" * 32), "b" * 32)
            with self.assertRaises(Exception) as raised:
                setup.select_extension_id(valid_config(), "invalid")
        self.assertEqual(getattr(raised.exception, "code", None), "INVALID_CONFIGURATION")
        ask.assert_not_called()


class LegacyExtensionMigrationTests(unittest.TestCase):
    def test_fresh_install_keeps_selected_id_after_copying_legacy_config(self):
        with tempfile.TemporaryDirectory() as root:
            project = Path(root)
            paths = setup.runtime_paths(project)
            legacy = project / "legacy" / "auth.json"
            legacy.parent.mkdir()
            legacy.write_text(json.dumps(valid_config(version=1)), encoding="utf-8")
            checks = {name: True for name in (
                "Windows 10/11", "Python 3.11-3.13", "FFmpeg", "FFprobe",
                "Project folder exists", "Project folder writable", "At least 3 GiB free")}

            def answer(label, default=""):
                if label.startswith("Current Clipper Plus Chrome Extension ID"):
                    return default
                if label.startswith("Apply installation or repair"):
                    return "yes"
                self.fail(f"Unexpected Setup prompt: {label}")

            with patch.object(setup, "SETUP", project / "setup"), \
                    patch.object(setup, "runtime_paths", return_value=paths), \
                    patch.object(setup, "legacy_config_path", return_value=legacy), \
                    patch.object(setup, "inspect_system", return_value=checks), \
                    patch.object(setup, "detect_installation", return_value="Partial Installation"), \
                    patch.object(setup, "discover_chrome_extensions", return_value={"b" * 32: project / "dist"}), \
                    patch.object(setup, "ask", side_effect=answer), \
                    patch.object(setup, "_active_task_state", return_value=None), \
                    patch.object(setup, "query_logon_task", return_value={"exists": False}), \
                    patch.object(setup, "ensure_python_environment", return_value={
                        "python": project / "python.exe", "pythonw": project / "pythonw.exe"}), \
                    patch.object(setup, "_valid_deno", return_value=("C:/deno.exe", "2.9.8")), \
                    patch.object(setup, "migrate_legacy_data", return_value={"copied": []}), \
                    patch.object(setup, "verify_ytdlp_install", return_value=("2026.10.26", "0.8.0")), \
                    patch.object(setup, "register_native_host") as register, \
                    patch.object(setup, "ensure_engine_ready", return_value={"state": "READY"}), \
                    patch.object(setup, "_set_login_startup"), \
                    patch.object(setup, "_stage_obsidian_migration"):
                self.assertEqual(setup.install(), 0)
            config = json.loads(paths.auth.read_text(encoding="utf-8"))
            self.assertEqual(config["service"]["extension_id"], "b" * 32)
            self.assertEqual(register.call_args.args[3], "b" * 32)


class SchedulerTests(unittest.TestCase):
    def test_task_query_decodes_unicode_boms_utf8_and_cp936_without_changing_paths(self):
        with tempfile.TemporaryDirectory() as root:
            project = Path(root) / "中文项目"
            pythonw = project / "setup" / ".build" / "venv" / "Scripts" / "pythonw.exe"
            auth = project / "runtime" / "auth.json"
            with patch("windows_integration.current_sid", return_value="S-1-5-21-test"):
                xml_text = task_xml(pythonw, auth, project / "setup").decode("utf-16")
            xml_text = xml_text.replace("?>", "?>\r\n", 1)
            variants = {
                "utf16-le": b"\xff\xfe" + xml_text.encode("utf-16-le"),
                "utf16-be": b"\xfe\xff" + xml_text.encode("utf-16-be"),
                "utf8-bom": b"\xef\xbb\xbf" + xml_text.encode("utf-8"),
                "utf8": xml_text.encode("utf-8"),
                "cp936-with-mismatched-declaration": xml_text.encode("cp936"),
            }
            with patch("windows_integration._task_output_encodings", return_value=["cp936"]):
                for label, raw in variants.items():
                    with self.subTest(encoding=label):
                        parsed = _parse_schtasks_xml(raw)
                        ns = {"t": "http://schemas.microsoft.com/windows/2004/02/mit/task"}
                        self.assertEqual(parsed.findtext("t:Actions/t:Exec/t:Command", namespaces=ns), str(pythonw))
                        self.assertIn(str(auth), parsed.findtext("t:Actions/t:Exec/t:Arguments", namespaces=ns))

            with patch("windows_integration.current_sid", return_value="S-1-5-21-test"), \
                    patch("windows_integration._task_output_encodings", return_value=["cp936"]), \
                    patch("windows_integration.subprocess.run", return_value=subprocess.CompletedProcess([], 0, stdout=variants["cp936-with-mismatched-declaration"], stderr=b"")):
                task = query_logon_task("VideoMind-test")
            self.assertTrue(task["exists"])
            self.assertTrue(task["owned_logon"])
            self.assertEqual(task["command"], str(pythonw))

    def test_undecodable_existing_task_fails_closed_before_replacement(self):
        malformed = subprocess.CompletedProcess([], 0, stdout=b"<?xml version='1.0'?><Task>\xff</Task>", stderr=b"")
        with patch("windows_integration._task_output_encodings", return_value=["cp936"]), \
                patch("windows_integration.subprocess.run", return_value=malformed) as run:
            with self.assertRaises(Exception) as raised:
                configure_logon_task(True, Path("pythonw.exe"), Path("auth.json"), Path("setup"), "VideoMind-test")
        self.assertEqual(getattr(raised.exception, "code", None), "SCHEDULED_TASK_INVALID")
        self.assertEqual(run.call_count, 1)

    def test_task_query_uses_detected_ansi_or_oem_code_page(self):
        path = Path("C:/Clipper/Résumé/pythonw.exe")
        with patch("windows_integration.current_sid", return_value="S-1-5-21-test"):
            xml_text = task_xml(path, path.parent / "auth.json", path.parent).decode("utf-16")
        for page in ("cp1252", "cp437"):
            with self.subTest(code_page=page), \
                    patch("windows_integration._task_output_encodings", return_value=[page]):
                parsed = _parse_schtasks_xml(xml_text.encode(page))
                ns = {"t": "http://schemas.microsoft.com/windows/2004/02/mit/task"}
                self.assertEqual(parsed.findtext("t:Actions/t:Exec/t:Command", namespaces=ns), str(path))

    def test_logon_task_runs_startup_coordinator_and_pins_user(self):
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            pythonw, auth = root / "pythonw.exe", root / "runtime" / "auth.json"
            with patch("windows_integration.current_sid", return_value="S-1-5-21-test"):
                xml = task_xml(pythonw, auth, root / "setup")
            self.assertIsInstance(xml, bytes)
            self.assertTrue(xml.startswith(b"\xff\xfe"))
            self.assertIn("encoding='utf-16'", xml.decode("utf-16").lower())
            task = ET.fromstring(xml)
            ns = {"t": "http://schemas.microsoft.com/windows/2004/02/mit/task"}
            args = task.findtext("t:Actions/t:Exec/t:Arguments", namespaces=ns)
            self.assertIn("videomind.startup", args)
            self.assertNotIn("videomind.server", args)
            self.assertEqual(task.findtext("t:Actions/t:Exec/t:WorkingDirectory", namespaces=ns), str(root / "setup"))
            self.assertEqual(task.findtext("t:Triggers/t:LogonTrigger/t:UserId", namespaces=ns), "S-1-5-21-test")
            self.assertEqual(task.findtext("t:Principals/t:Principal/t:RunLevel", namespaces=ns), "LeastPrivilege")

    def test_unknown_existing_task_is_never_deleted(self):
        with patch("windows_integration.query_logon_task",
                   return_value={"exists": True, "owned_logon": False}), \
                patch("windows_integration.subprocess.run") as run:
            with self.assertRaises(Exception) as raised:
                configure_logon_task(False, "pythonw.exe", "auth.json", "setup")
        self.assertEqual(getattr(raised.exception, "code", None), "SCHEDULED_TASK_OWNERSHIP_UNCLEAR")
        run.assert_not_called()

    def test_native_host_rebuild_tracks_engine_sources_not_only_entrypoint_mtime(self):
        with tempfile.TemporaryDirectory() as root:
            project = Path(root)
            setup_dir = project / "setup"
            source = setup_dir / "engine" / "videomind" / "server.py"
            source.parent.mkdir(parents=True)
            source.write_text("# first build", encoding="utf-8")
            (setup_dir / "native_entry.py").write_text("# host entry", encoding="utf-8")
            paths = setup.runtime_paths(project)
            builds = []

            def build(_python, output):
                builds.append(1)
                (output / "videomind-host.exe").write_bytes(b"host")

            with patch.object(windows_integration, "os", types.SimpleNamespace(name="posix")):
                register_native_host("python.exe", paths, paths.auth, "a" * 32, build)
                register_native_host("python.exe", paths, paths.auth, "a" * 32, build)
                source.write_text("# changed engine module", encoding="utf-8")
                register_native_host("python.exe", paths, paths.auth, "a" * 32, build)
            self.assertEqual(len(builds), 2)


class EnvironmentTests(unittest.TestCase):
    def test_read_only_check_does_not_repair_environment(self):
        with tempfile.TemporaryDirectory() as root:
            setup_dir = Path(root) / "setup"
            python = setup_dir / ".build" / "venv" / "Scripts" / "python.exe"
            python.parent.mkdir(parents=True)
            python.write_bytes(b"fake")
            healthy = {"environment": "present", "pip_check": True, "missing": []}
            with patch.object(setup, "inspect_python_dependencies", return_value=healthy), \
                    patch.object(setup, "inspect_system", return_value={"Project folder exists": True}), \
                    patch.object(setup, "find_deno", return_value=("deno.exe", "2.9.8")), \
                    patch.object(setup, "detect_installation", return_value="Existing Installation"), \
                    patch.object(setup, "runtime_paths", return_value=MagicMock(project_root=Path(root))), \
                    patch.object(setup, "SETUP", setup_dir), \
                    patch.object(setup, "ensure_python_environment") as install:
                result = setup.check_environment()
            self.assertTrue(result)
            install.assert_not_called()

    def test_dependency_repair_uses_runtime_pip_cache(self):
        with tempfile.TemporaryDirectory() as root:
            setup_dir = Path(root) / "setup"
            setup_dir.mkdir()
            commands = []
            healthy = {"environment": "present", "pip_check": True, "missing": []}
            with patch("environment.inspect_python_dependencies", return_value=healthy):
                install_requirements("python.exe", setup_dir, commands.append)
            command = commands[0]
            cache_index = command.index("--cache-dir")
            self.assertEqual(Path(command[cache_index + 1]), Path(root) / "runtime" / "cache" / "pip")


class DenoBootstrapTests(unittest.TestCase):
    def test_missing_winget_uses_direct_deno_fallback(self):
        runtime = ("C:/Clipper/runtime/tools/deno/deno.exe", "2.9.8")
        with patch.object(setup, "find_winget", return_value=None), \
                patch.object(setup, "bootstrap_winget", return_value=None), \
                patch.object(setup, "install_deno_direct", return_value=runtime) as direct:
            self.assertEqual(setup.install_deno(), runtime)
        direct.assert_called_once_with()

    def test_existing_app_installer_registration_avoids_bundle_download(self):
        with patch.object(setup, "find_winget", side_effect=[None, "C:/WindowsApps/winget.exe"]), \
                patch.object(setup, "_run_powershell", return_value=True), \
                patch.object(setup, "_download_file") as download:
            self.assertEqual(setup.bootstrap_winget(), "C:/WindowsApps/winget.exe")
        download.assert_not_called()

    def test_missing_app_installer_downloads_microsoft_bundle(self):
        with tempfile.TemporaryDirectory() as root:
            paths = setup.runtime_paths(root)

            def download(_url, destination, _limit):
                Path(destination).write_bytes(b"signed bundle placeholder")

            with patch.object(setup, "runtime_paths", return_value=paths), \
                    patch.object(setup, "find_winget", side_effect=[None, None, "C:/WindowsApps/winget.exe"]), \
                    patch.object(setup, "_run_powershell", return_value=True) as powershell, \
                    patch.object(setup, "_download_file", side_effect=download) as fetch:
                self.assertEqual(setup.bootstrap_winget(), "C:/WindowsApps/winget.exe")
            self.assertEqual(fetch.call_args.args[0], "https://aka.ms/getwinget")
            self.assertEqual(powershell.call_count, 2)

    def test_direct_install_verifies_checksum_before_publishing_executable(self):
        with tempfile.TemporaryDirectory() as root:
            paths = setup.runtime_paths(root)
            stream = io.BytesIO()
            with zipfile.ZipFile(stream, "w") as archive:
                archive.writestr("deno.exe", b"test Deno executable")
            zip_bytes = stream.getvalue()
            digest = hashlib.sha256(zip_bytes).hexdigest()

            def download(url, destination, _limit):
                if url.endswith(".sha256sum"):
                    Path(destination).write_text(f"Algorithm : SHA256\nHash : {digest}\n", encoding="utf-8")
                else:
                    Path(destination).write_bytes(zip_bytes)

            with patch.object(setup, "runtime_paths", return_value=paths), \
                    patch.object(setup, "_download_file", side_effect=download), \
                    patch.object(setup.platform, "machine", return_value="AMD64"), \
                    patch.object(setup, "_deno_version", side_effect=lambda p: (str(p), "2.9.8")):
                result = setup.install_deno_direct()
            self.assertEqual(result[0], str(paths.root / "tools" / "deno" / "deno.exe"))
            self.assertEqual(Path(result[0]).read_bytes(), b"test Deno executable")

    def test_direct_install_rejects_bad_checksum_without_publishing_executable(self):
        with tempfile.TemporaryDirectory() as root:
            paths = setup.runtime_paths(root)

            def download(url, destination, _limit):
                if url.endswith(".sha256sum"):
                    Path(destination).write_text("0" * 64, encoding="utf-8")
                else:
                    Path(destination).write_bytes(b"invalid archive")

            with patch.object(setup, "runtime_paths", return_value=paths), \
                    patch.object(setup, "_download_file", side_effect=download), \
                    patch.object(setup.platform, "machine", return_value="AMD64"):
                with self.assertRaises(Exception) as raised:
                    setup.install_deno_direct()
            self.assertEqual(getattr(raised.exception, "code", None), "DENO_INSTALL_FAILED")
            self.assertFalse((paths.root / "tools" / "deno" / "deno.exe").exists())


if __name__ == "__main__":
    unittest.main()
