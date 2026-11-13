import json
from pathlib import Path
import tempfile
import unittest

from videomind.config import migrate_legacy_configuration
from videomind.runtime import copy_verified, ensure_runtime, migrate_legacy_data, runtime_paths


class RuntimeTests(unittest.TestCase):
    def test_paths_are_all_under_project_runtime(self):
        with tempfile.TemporaryDirectory() as root:
            paths = runtime_paths(root)
            self.assertEqual(paths.root, Path(root) / "runtime")
            for item in (paths.auth, paths.cache, paths.logs,
                         paths.models, paths.temp, paths.migration, paths.originals, paths.host):
                self.assertTrue(item == paths.root or paths.root in item.parents)

    def test_migration_verifies_copy_and_retains_all_sources(self):
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            paths = ensure_runtime(runtime_paths(root / "project"))
            legacy = root / "old-appdata"
            (legacy / "logs").mkdir(parents=True)
            (legacy / "tmp").mkdir()
            (legacy / "completed_markdown.json").write_text('{"entries": {"url": "中文"}}', encoding="utf-8")
            (legacy / "logs" / "engine.log").write_text("old log", encoding="utf-8")
            old_originals = root / "old-originals"
            old_originals.mkdir()
            (old_originals / "note.md").write_text("# 日本語\n\n中文", encoding="utf-8")
            report = migrate_legacy_data(paths, legacy_root=legacy, original_dir=old_originals)
            self.assertTrue(report["complete"])
            self.assertTrue(report["legacy_source_retained"])
            self.assertIn("中文", (paths.cache / "completed_markdown.json").read_text(encoding="utf-8"))
            self.assertEqual((paths.logs / "engine.log").read_text(encoding="utf-8"), "old log")
            self.assertEqual((paths.originals / "migrated" / "note.md").read_text(encoding="utf-8"), "# 日本語\n\n中文")
            self.assertTrue((legacy / "completed_markdown.json").is_file())
            marker = json.loads((paths.migration / "runtime-migration.json").read_text(encoding="utf-8"))
            self.assertEqual(marker["destination"], str(paths.root.resolve()))

    def test_verified_copy_never_overwrites_runtime_data(self):
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            source, target = root / "source", root / "target"
            source.write_text("old", encoding="utf-8")
            target.write_text("new", encoding="utf-8")
            self.assertFalse(copy_verified(source, target))
            self.assertEqual(target.read_text(encoding="utf-8"), "new")
            self.assertEqual(source.read_text(encoding="utf-8"), "old")

    def test_legacy_engine_config_migration_preserves_runtime_choices_and_removes_setup_language(self):
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            paths = runtime_paths(root / "project")
            legacy = root / "legacy-auth.json"
            old = {
                "version": 1, "language": "ja", "ai": {"base_url": "secret config"},
                "storage": {"original_dir": str(root / "old-originals"), "summary_dir": str(root / "summaries")},
                "whisper": {"model": "small", "device": "cpu", "compute_type": "int8", "language": "ja"},
                "service": {"pipe_name": "VideoMind-test", "extension_id": "a" * 32,
                            "js_runtime_path": str(root / "deno.exe")},
            }
            legacy.write_text(json.dumps(old), encoding="utf-8")
            migrated, copied = migrate_legacy_configuration(legacy, paths=paths)
            self.assertTrue(copied)
            self.assertEqual(migrated["version"], 2)
            self.assertNotIn("language", migrated)
            self.assertNotIn("ai", migrated)
            self.assertNotIn("summary_dir", migrated["storage"])
            self.assertEqual(migrated["storage"]["original_dir"], str(paths.originals))
            self.assertEqual(migrated["whisper"], old["whisper"])
            self.assertEqual(migrated["service"], old["service"])
            self.assertTrue(legacy.is_file())


if __name__ == "__main__":
    unittest.main()
