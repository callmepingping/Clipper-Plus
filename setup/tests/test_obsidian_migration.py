import json
from pathlib import Path
import sys
import tempfile
import unittest

from videomind.errors import AppError
from videomind.runtime import runtime_paths

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from obsidian_migration import discover_vaults, read_web_clipper_export, stage_settings_migration


class ObsidianMigrationTests(unittest.TestCase):
    def test_discovers_only_existing_deduplicated_vault_paths(self):
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            vault_a, vault_b = root / "A Vault", root / "B Vault"
            vault_a.mkdir()
            vault_b.mkdir()
            config = root / "obsidian.json"
            config.write_text(json.dumps({"vaults": {
                "a": {"path": str(vault_a)}, "a-copy": {"path": str(vault_a)},
                "b": {"path": str(vault_b)}, "missing": {"path": str(root / "missing")},
            }}), encoding="utf-8")
            found = discover_vaults(config)
            self.assertEqual([item["name"] for item in found], ["A Vault", "B Vault"])

    def test_import_reads_only_official_export_template_fields(self):
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            exported = root / "clipper-export.json"
            exported.write_text(json.dumps({
                "vaults": ["Notes"], "template_list": ["template-id"],
                "template_template-id": {
                    "name": "Video", "behavior": "create", "noteNameFormat": "{{title}}",
                    "path": "Videos", "noteContentFormat": "{{content}}", "properties": [],
                    "apiKey": "must not migrate", "provider": "must not migrate",
                },
                "provider_api_key": "must not migrate",
            }), encoding="utf-8")
            result = read_web_clipper_export(exported)
            self.assertEqual(result["vaults"], ["Notes"])
            self.assertEqual(result["templates"][0]["name"], "Video")
            self.assertNotIn("apiKey", result["templates"][0])
            self.assertNotIn("provider", result["templates"][0])

    def test_unexported_template_store_is_not_guessed(self):
        with tempfile.TemporaryDirectory() as root:
            exported = Path(root) / "incomplete.json"
            exported.write_text(json.dumps({"template_list": ["x"]}), encoding="utf-8")
            with self.assertRaises(AppError) as raised:
                read_web_clipper_export(exported)
            self.assertEqual(raised.exception.code, "OBSIDIAN_EXPORT_UNSUPPORTED")

    def test_staging_requires_an_explicit_caller_and_keeps_source_export(self):
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            export = root / "export.json"
            export.write_text(json.dumps({"vaults": [], "template_list": []}), encoding="utf-8")
            paths = runtime_paths(root / "project")
            staged = stage_settings_migration([{"name": "Vault", "path": str(root)}], export, paths)
            bundle = json.loads(Path(staged["path"]).read_text(encoding="utf-8"))
            self.assertTrue(bundle["consent"])
            self.assertEqual(bundle["status"], "pending")
            self.assertEqual(bundle["vaults"], ["Vault"])
            self.assertTrue(export.is_file())


if __name__ == "__main__":
    unittest.main()
