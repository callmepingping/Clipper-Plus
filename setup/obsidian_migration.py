"""Read supported Obsidian sources and stage an opt-in Extension Settings merge."""
import json
import os
from pathlib import Path
import uuid

from videomind.errors import AppError
from videomind.runtime import atomic_json, ensure_runtime, runtime_paths


def discover_vaults(config_file=None):
    source = Path(config_file) if config_file else Path(os.environ.get("APPDATA", Path.home())) / "obsidian" / "obsidian.json"
    try:
        data = json.loads(source.read_text(encoding="utf-8-sig"))
    except FileNotFoundError:
        return []
    except (OSError, ValueError, TypeError):
        return []
    values = data.get("vaults") if isinstance(data, dict) else None
    if not isinstance(values, dict):
        return []
    found, seen = [], set()
    for record in values.values():
        if not isinstance(record, dict) or not isinstance(record.get("path"), str):
            continue
        path = Path(record["path"]).expanduser()
        try:
            if not path.is_absolute() or not path.is_dir():
                continue
            resolved = path.resolve()
            key = str(resolved).casefold()
            if key in seen:
                continue
            seen.add(key)
            found.append({"name": resolved.name, "path": str(resolved)})
        except OSError:
            continue
    return sorted(found, key=lambda item: item["name"].casefold())


def read_web_clipper_export(path):
    source = Path(path)
    try:
        if source.stat().st_size > 10 * 1024 * 1024:
            raise ValueError()
        data = json.loads(source.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError, TypeError):
        raise AppError("OBSIDIAN_EXPORT_INVALID",
                       "The official Web Clipper settings export could not be read. The export was left untouched.") from None
    if not isinstance(data, dict):
        raise AppError("OBSIDIAN_EXPORT_INVALID", "The official Web Clipper settings export has an unsupported format.")

    vaults = data.get("vaults", [])
    if not isinstance(vaults, list) or any(not isinstance(name, str) or not name.strip() for name in vaults):
        raise AppError("OBSIDIAN_EXPORT_INVALID", "The Web Clipper export has an invalid vault list.")
    ids = data.get("template_list", [])
    if not isinstance(ids, list) or any(not isinstance(identifier, str) for identifier in ids):
        raise AppError("OBSIDIAN_EXPORT_INVALID", "The Web Clipper export has an invalid template list.")
    templates = []
    for identifier in ids:
        template = data.get(f"template_{identifier}")
        if not isinstance(template, dict):
            raise AppError("OBSIDIAN_EXPORT_UNSUPPORTED",
                           "The Web Clipper export must be created with Export all settings so templates are readable.")
        if (not isinstance(template.get("name"), str) or
                not isinstance(template.get("behavior"), str) or
                not isinstance(template.get("noteContentFormat"), str) or
                not isinstance(template.get("properties"), list)):
            raise AppError("OBSIDIAN_EXPORT_INVALID", "A Web Clipper template in the export is incomplete.")
        properties = template["properties"]
        if any(not isinstance(item, dict) or not isinstance(item.get("name"), str) or
               not isinstance(item.get("value"), str) for item in properties):
            raise AppError("OBSIDIAN_EXPORT_INVALID", "A Web Clipper template contains invalid property data.")
        safe_template = {key: template[key] for key in
                         ("name", "behavior", "noteNameFormat", "path", "noteContentFormat",
                          "triggers", "vault", "context") if key in template}
        safe_template["properties"] = [
            {key: item[key] for key in ("name", "value", "type") if key in item}
            for item in properties
        ]
        templates.append(safe_template)
    return {"vaults": list(dict.fromkeys(vaults)), "templates": templates}


def stage_settings_migration(vaults, clipper_export=None, paths=None):
    paths = ensure_runtime(paths or runtime_paths())
    imported = read_web_clipper_export(clipper_export) if clipper_export else {"vaults": [], "templates": []}
    names = [record["name"] for record in vaults]
    names.extend(imported["vaults"])
    settings = {"vaults": list(dict.fromkeys(names)), "templates": imported["templates"]}
    migration_id = str(uuid.uuid4())
    bundle = {"schema": 1, "id": migration_id, "status": "pending", "consent": True,
              "vaults": settings["vaults"], "templates": settings["templates"]}
    encoded = json.dumps(bundle, ensure_ascii=False).encode("utf-8")
    if len(encoded) > 700 * 1024:
        raise AppError("OBSIDIAN_EXPORT_TOO_LARGE",
                       "The settings export is too large for one Native Messaging transfer. Import templates through Clipper Plus Settings instead.")
    atomic_json(paths.migration / "extension-settings.json", bundle)
    return {"id": migration_id, "vault_count": len(settings["vaults"]),
            "template_count": len(settings["templates"]), "path": str(paths.migration / "extension-settings.json")}
