"""Validated per-user settings; secrets are never part of this document."""
import json
import os
from pathlib import Path

from .errors import AppError
from .runtime import atomic_json, ensure_runtime, migration_lock, runtime_paths


def current_sid():
    # Setup must obtain the identity before pywin32 has been installed in its venv.
    import ctypes
    from ctypes import wintypes
    if os.name != "nt":
        raise AppError("PLATFORM_UNSUPPORTED", "Clipper Plus requires Windows 10 or 11.")
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    security = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel.GetCurrentProcess.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    security.OpenProcessToken.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE)]
    security.GetTokenInformation.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
    security.ConvertSidToStringSidW.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)]
    token = wintypes.HANDLE()
    if not security.OpenProcessToken(kernel.GetCurrentProcess(), 0x8, ctypes.byref(token)):
        raise AppError("USER_IDENTITY_UNAVAILABLE", "Could not identify the current Windows user.")
    text = ctypes.c_void_p()
    try:
        size = wintypes.DWORD()
        security.GetTokenInformation(token, 1, None, 0, ctypes.byref(size))
        buffer = ctypes.create_string_buffer(size.value)
        if not security.GetTokenInformation(token, 1, buffer, size, ctypes.byref(size)):
            raise AppError("USER_IDENTITY_UNAVAILABLE", "Could not identify the current Windows user.")
        sid = ctypes.cast(buffer, ctypes.POINTER(ctypes.c_void_p))[0]
        if not security.ConvertSidToStringSidW(sid, ctypes.byref(text)):
            raise AppError("USER_IDENTITY_UNAVAILABLE", "Could not identify the current Windows user.")
        return ctypes.wstring_at(text)
    finally:
        if text:
            kernel.LocalFree(text)
        kernel.CloseHandle(token)


def pipe_address():
    return rf"\\.\pipe\VideoMind-{current_sid()}"


def config_path(path=None):
    explicit = path or os.environ.get("VIDEOMIND_CONFIG")
    return Path(explicit).resolve() if explicit else runtime_paths().auth


def legacy_config_path():
    base = Path(os.environ.get("LOCALAPPDATA", Path.home()))
    return base / "VideoMind" / "auth.json"


def migrate_legacy_configuration(source=None, destination=None, paths=None):
    """Migrate the legacy AppData auth file without changing active user settings."""
    source = Path(source or legacy_config_path())
    paths = ensure_runtime(paths or runtime_paths())
    destination = Path(destination or paths.auth)
    with migration_lock(paths):
        if destination.exists():
            return load_config(destination), False
        try:
            raw = json.loads(source.read_text(encoding="utf-8-sig"))
        except FileNotFoundError:
            return None, False
        except (OSError, ValueError, TypeError):
            raise AppError("LEGACY_CONFIGURATION_INVALID",
                           "The existing Clipper Plus configuration could not be read. The original file was left untouched.") from None
        if not isinstance(raw, dict):
            raise AppError("LEGACY_CONFIGURATION_INVALID",
                           "The existing Clipper Plus configuration is invalid. The original file was left untouched.")
        migrated = dict(raw)
        migrated.pop("ai", None)
        migrated.pop("language", None)
        storage = dict(migrated.get("storage") or {})
        storage.pop("summary_dir", None)
        storage["original_dir"] = str(paths.originals)
        migrated["storage"] = storage
        migrated["version"] = 2
        validate_config(migrated)
        atomic_json(destination, migrated)
    return migrated, True


def validate_config(value):
    def invalid():
        raise AppError("SETUP_REQUIRED", "Configuration is missing or invalid. Run Clipper Plus Setup.")

    if not isinstance(value, dict) or type(value.get("version")) is not int or value["version"] not in {1, 2}:
        invalid()
    if value["version"] == 1 and value.get("language") not in {"en", "zh-CN", "ja"}:
        invalid()
    def scan(node):
        if isinstance(node, dict):
            for key, item in node.items():
                if str(key).lower() in {"api_key", "apikey", "password", "token", "secret", "authorization"}:
                    invalid()
                scan(item)
        elif isinstance(node, list):
            for item in node:
                scan(item)
    scan(value)
    for section in ("storage", "whisper"):
        if not isinstance(value.get(section), dict):
            invalid()
    path = value["storage"].get("original_dir")
    if not isinstance(path, str) or not Path(path).is_absolute():
        invalid()
    whisper = value["whisper"]
    if not isinstance(whisper.get("model"), str) or not whisper["model"].strip():
        invalid()
    if whisper.get("device") not in {"cpu", "cuda", "auto"}:
        invalid()
    if whisper.get("compute_type") not in {"int8", "float16", "float32", "int8_float16", "int8_float32", "default", "auto"}:
        invalid()
    if whisper.get("language", "auto") not in {"auto", "zh", "en", "ja"}:
        invalid()
    service = value.get("service", {})
    if not isinstance(service, dict):
        invalid()
    runtime_path = service.get("js_runtime_path")
    if runtime_path is not None and (not isinstance(runtime_path, str) or not Path(runtime_path).is_absolute()):
        invalid()
    return value


def load_config(path=None):
    try:
        target = config_path(path)
        if target.stat().st_size > 65536:
            raise ValueError()
        return validate_config(json.loads(target.read_text(encoding="utf-8-sig")))
    except AppError:
        raise
    except (OSError, ValueError, TypeError):
        raise AppError("SETUP_REQUIRED", "Configuration is missing or invalid. Run Clipper Plus Setup.") from None
