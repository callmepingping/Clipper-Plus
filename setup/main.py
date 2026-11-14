"""English-only setup orchestrator; runtime, environment and Windows tasks are separate subsystems."""
import argparse
import base64
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import platform
import re
import shutil
import subprocess
import sys
import tempfile
from urllib.request import Request, urlopen
import zipfile

SETUP = Path(__file__).resolve().parent
sys.path.insert(0, str(SETUP / "engine"))

from videomind.config import (config_path, current_sid, legacy_config_path,
                              load_config, migrate_legacy_configuration, validate_config)
from videomind.engine_lifecycle import ensure_engine_ready
from videomind.errors import AppError
from videomind.ipc import call_engine
from videomind.runtime import atomic_json, ensure_runtime, migrate_legacy_data, runtime_paths
from environment import ensure_python_environment, inspect_python_dependencies, inspect_system
from obsidian_migration import discover_vaults, stage_settings_migration
from windows_integration import (configure_logon_task, query_logon_task,
                                 register_native_host, stop_engine_process_tree, task_configuration, task_name)


def run(command):
    result = subprocess.run([str(part) for part in command], check=False)
    if result.returncode:
        raise AppError("INSTALLATION_FAILED", "A setup step failed. Review the diagnostic output and rerun Setup.")
    return result


def ask(label, default=""):
    response = input(f"{label}{f' [{default}]' if default else ''}: ").strip()
    return response or default


def select_extension_id(config=None, supplied=None):
    current = (config or {}).get("service", {}).get("extension_id", "")
    extension_id = supplied if supplied is not None else ask(
        "Chrome Extension ID (copy the ID of the currently loaded Clipper Plus from chrome://extensions)", current)
    if not re.fullmatch(r"[a-p]{32}", extension_id):
        raise AppError("INVALID_CONFIGURATION", "Copy the 32-character extension ID from chrome://extensions and rerun Setup.")
    return extension_id


def _deno_version(candidate):
    if not candidate or not Path(candidate).is_file():
        return None
    try:
        result = subprocess.run([str(candidate), "--version"], capture_output=True, text=True,
                                timeout=10, check=False,
                                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        match = re.search(r"(?m)^deno\s+(\d+)\.(\d+)\.(\d+)", result.stdout)
        if result.returncode == 0 and match:
            version_tuple = tuple(map(int, match.groups()))
            if version_tuple >= (2, 3, 0):
                return str(Path(candidate).resolve()), ".".join(map(str, version_tuple))
    except (OSError, subprocess.TimeoutExpired):
        pass
    return None


def find_deno():
    candidates = [str(runtime_paths().root / "tools" / "deno" / "deno.exe"), shutil.which("deno")]
    profile = Path(os.environ.get("USERPROFILE", Path.home()))
    local = Path(os.environ.get("LOCALAPPDATA", profile / "AppData" / "Local"))
    candidates.extend([str(profile / ".deno" / "bin" / "deno.exe"),
                       str(local / "Microsoft" / "WinGet" / "Links" / "deno.exe"),
                       str(local / "Programs" / "Deno" / "deno.exe")])
    try:
        candidates.extend(str(folder / "deno.exe") for folder in
                          (local / "Microsoft" / "WinGet" / "Packages").glob("DenoLand.Deno_*") if folder.is_dir())
    except OSError:
        pass
    seen = set()
    for candidate in candidates:
        if not candidate or candidate.casefold() in seen:
            continue
        seen.add(candidate.casefold())
        runtime = _deno_version(candidate)
        if runtime:
            return runtime
    return None


def find_winget():
    local = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
    for candidate in (shutil.which("winget"), str(local / "Microsoft" / "WindowsApps" / "winget.exe")):
        if not candidate or not Path(candidate).is_file():
            continue
        try:
            result = subprocess.run([candidate, "--version"], capture_output=True, timeout=15, check=False,
                                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            if result.returncode == 0:
                return str(Path(candidate).resolve())
        except (OSError, subprocess.TimeoutExpired):
            pass
    return None


def _download_file(url, destination, limit):
    request = Request(url, headers={"User-Agent": "Clipper-Plus-Setup"})
    with urlopen(request, timeout=45) as response, Path(destination).open("wb") as output:
        if not response.url.startswith("https://"):
            raise ValueError("The download did not use HTTPS.")
        size = 0
        while block := response.read(1024 * 1024):
            size += len(block)
            if size > limit:
                raise ValueError("The download exceeded its size limit.")
            output.write(block)
        if size == 0:
            raise ValueError("The download was empty.")


def _run_powershell(script, timeout=180):
    encoded = base64.b64encode(script.encode("utf-16le")).decode("ascii")
    try:
        result = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded],
                                capture_output=True, timeout=timeout, check=False)
        return result.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def bootstrap_winget():
    """Try Microsoft's per-user App Installer registration, then its signed bundle."""
    existing = find_winget()
    if existing:
        return existing
    print("WinGet is missing. Checking the Windows App Installer registration.")
    _run_powershell("$ErrorActionPreference='Stop'; Add-AppxPackage -RegisterByFamilyName "
                    "-MainPackage Microsoft.DesktopAppInstaller_8wekyb3d8bbwe")
    winget = find_winget()
    if winget:
        return winget
    paths = ensure_runtime(runtime_paths())
    print("Downloading the signed Microsoft App Installer package for WinGet.")
    try:
        with tempfile.TemporaryDirectory(prefix="winget-", dir=paths.temp) as directory:
            bundle = Path(directory) / "Microsoft.DesktopAppInstaller.msixbundle"
            _download_file("https://aka.ms/getwinget", bundle, 512 * 1024 * 1024)
            quoted = "'" + str(bundle).replace("'", "''") + "'"
            _run_powershell("$ErrorActionPreference='Stop'; Add-AppxPackage -Path " + quoted,
                            timeout=600)
    except (OSError, ValueError):
        pass
    return find_winget()


def install_deno_direct():
    """Install the official, SHA-256-verified Deno binary into project Runtime."""
    architecture = platform.machine().lower()
    target = "aarch64" if architecture in {"arm64", "aarch64"} else "x86_64" if architecture in {"amd64", "x86_64"} else None
    if target is None:
        raise AppError("DENO_INSTALL_FAILED", "This Windows CPU architecture is not supported by the automatic Deno installer.")
    asset = f"deno-{target}-pc-windows-msvc.zip"
    base = "https://github.com/denoland/deno/releases/latest/download/"
    paths = ensure_runtime(runtime_paths())
    destination = paths.root / "tools" / "deno" / "deno.exe"
    print("Downloading Deno directly from its official Release and verifying SHA-256.")
    try:
        with tempfile.TemporaryDirectory(prefix="deno-", dir=paths.temp) as directory:
            temporary = Path(directory)
            checksum = temporary / (asset + ".sha256sum")
            archive = temporary / asset
            _download_file(base + asset + ".sha256sum", checksum, 16 * 1024)
            hashes = re.findall(r"(?i)\b[a-f0-9]{64}\b", checksum.read_text(encoding="utf-8-sig"))
            if len(hashes) != 1:
                raise ValueError("The Deno checksum was invalid.")
            _download_file(base + asset, archive, 512 * 1024 * 1024)
            with archive.open("rb") as stream:
                digest = hashlib.file_digest(stream, "sha256").hexdigest()
            if digest.casefold() != hashes[0].casefold():
                raise ValueError("The Deno download failed SHA-256 verification.")
            with zipfile.ZipFile(archive) as package:
                files = [item for item in package.infolist() if not item.is_dir()]
                if len(files) != 1 or files[0].filename != "deno.exe" or files[0].file_size > 768 * 1024 * 1024:
                    raise ValueError("The Deno archive did not contain the expected executable.")
                staged = temporary / "deno.exe"
                with package.open(files[0]) as source, staged.open("wb") as output:
                    shutil.copyfileobj(source, output)
            runtime = _deno_version(staged)
            if not runtime:
                raise ValueError("The downloaded Deno executable did not pass its version check.")
            destination.parent.mkdir(parents=True, exist_ok=True)
            os.replace(staged, destination)
    except (OSError, ValueError, zipfile.BadZipFile, UnicodeError):
        raise AppError("DENO_INSTALL_FAILED", "Automatic Deno download failed. Check the network connection and rerun Setup.") from None
    runtime = _deno_version(destination)
    if not runtime:
        raise AppError("DENO_INSTALL_FAILED", "Deno was downloaded but could not be verified. Rerun Setup.")
    return runtime


def install_deno():
    winget = find_winget() or bootstrap_winget()
    if winget:
        print("Installing Deno with WinGet for the current Windows user.")
        try:
            result = subprocess.run([winget, "install", "--id", "DenoLand.Deno", "--exact", "--scope", "user",
                                     "--source", "winget", "--silent", "--disable-interactivity",
                                     "--accept-package-agreements", "--accept-source-agreements"],
                                    timeout=600, check=False)
            if result.returncode == 0:
                runtime = find_deno()
                if runtime:
                    return runtime
        except (OSError, subprocess.TimeoutExpired):
            pass
        print("WinGet could not install a verified Deno runtime; using the official Deno download.")
    return install_deno_direct()


def verify_ytdlp_install(python, deno_path):
    code = ("import sys; from importlib.metadata import version; from yt_dlp import YoutubeDL; "
            "print(version('yt-dlp')); print(version('yt-dlp-ejs')); "
            "y=YoutubeDL({'quiet':True,'js_runtimes':{'deno':{'path':sys.argv[1]}},'remote_components':[]}); "
            "i=y._js_runtimes['deno'].info; "
            "assert i and i.supported and i.path; print(i.version); print(i.path)")
    result = subprocess.run([str(python), "-c", code, str(deno_path)], capture_output=True,
                            text=True, timeout=20, check=False)
    lines = result.stdout.splitlines()
    if result.returncode or len(lines) < 4 or lines[0] != "2026.10.26" or not lines[1]:
        raise AppError("YTDLP_RUNTIME_INVALID", "yt-dlp, yt-dlp-ejs, or the configured JavaScript runtime could not be verified.")
    return lines[0], lines[1]


def build_native_host(python, host_dir):
    host_dir = Path(host_dir)
    run([python, "-m", "PyInstaller", "--noconfirm", "--clean", "--onefile", "--name", "videomind-host",
         "--paths", SETUP / "engine", "--hidden-import", "win32timezone", "--hidden-import", "win32event",
         "--distpath", host_dir, "--workpath", SETUP / ".build" / "pyinstaller",
         "--specpath", SETUP / ".build" / "pyinstaller", SETUP / "native_entry.py"])


def detect_installation(paths=None, dependency_health=None):
    paths = paths or runtime_paths()
    runtime_auth = paths.auth.is_file()
    legacy_auth = legacy_config_path().is_file()
    _, python, _ = (SETUP / ".build" / "venv", SETUP / ".build" / "venv" / "Scripts" / "python.exe",
                    SETUP / ".build" / "venv" / "Scripts" / "pythonw.exe")
    venv_present = python.is_file()
    if not runtime_auth and not legacy_auth and not venv_present and not paths.root.exists():
        return "Fresh Installation"
    if not runtime_auth and not legacy_auth and not venv_present:
        return "Partial Installation"
    config_file = paths.auth if runtime_auth else legacy_config_path()
    loaded = None
    if config_file.is_file():
        try:
            loaded = load_config(config_file)
        except AppError:
            return "Broken Installation"
    if runtime_auth and venv_present and loaded and loaded.get("version") == 2:
        if dependency_health is None:
            dependency_health = inspect_python_dependencies(python)
        if dependency_health["missing"] or not dependency_health["pip_check"]:
            return "Upgrade / Repair"
        return "Existing Installation"
    if legacy_auth and venv_present:
        return "Upgrade / Repair"
    if runtime_auth and loaded and loaded.get("version") == 1 and venv_present:
        return "Upgrade / Repair"
    return "Partial Installation"


def check_environment():
    paths = runtime_paths()
    checks = inspect_system(paths.project_root)
    for label, ok in checks.items():
        print(f"[{'OK' if ok else 'MISSING'}] {label}")
    environment, python, _ = (SETUP / ".build" / "venv", SETUP / ".build" / "venv" / "Scripts" / "python.exe",
                              SETUP / ".build" / "venv" / "Scripts" / "pythonw.exe")
    dependencies_ready = False
    dependencies = None
    if python.is_file():
        dependencies = inspect_python_dependencies(python)
        dependencies_ready = not dependencies["missing"] and dependencies["pip_check"]
        print(f"[{'OK' if dependencies_ready else 'REPAIR'}] Python environment: {environment}")
        for package in dependencies["missing"]:
            print(f"[MISSING] {package}")
        if not dependencies["pip_check"]:
            print("[REPAIR] pip check found dependency conflicts")
    else:
        print(f"[MISSING] Python virtual environment: {environment}")
    deno = find_deno()
    print(f"[OK] Deno {deno[1]} ({deno[0]})" if deno else "[MISSING] Deno >= 2.3.0")
    print(f"Installation state: {detect_installation(paths, dependencies)}")
    return all(checks.values()) and dependencies_ready and deno is not None


def _active_task_state():
    try:
        response = call_engine({"id": "setup-active-task-check", "type": "GET_TASK_STATUS",
                                "payload": {}, "protocol_version": 2}, connect_timeout=0.2)
        task = response.get("data", {}).get("task")
        return task.get("state") if isinstance(task, dict) else None
    except AppError:
        return None


def _valid_deno(config):
    saved = config.get("service", {}).get("js_runtime_path")
    if saved and Path(saved).is_file():
        try:
            result = subprocess.run([saved, "--version"], capture_output=True, text=True,
                                    timeout=10, check=False,
                                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            match = re.search(r"(?m)^deno\s+(\d+)\.(\d+)\.(\d+)", result.stdout)
            if result.returncode == 0 and match and tuple(map(int, match.groups())) >= (2, 3, 0):
                return str(Path(saved).resolve()), ".".join(match.groups())
        except (OSError, subprocess.TimeoutExpired):
            pass
    return find_deno()


def _stage_obsidian_migration(paths):
    migration_file = paths.migration / "extension-settings.json"
    if migration_file.exists():
        try:
            if json.loads(migration_file.read_text(encoding="utf-8")).get("status") in {"pending", "complete"}:
                print("An Obsidian settings migration is already staged or complete; it was left unchanged.")
                return
        except (OSError, ValueError, AttributeError):
            pass
    vaults = discover_vaults()
    print(f"Found {len(vaults)} Obsidian vault(s) from Obsidian's own vault registry.")
    for vault in vaults:
        print(f"  - {vault['name']} (path verified)")
    export_path = ask("Official Web Clipper Export all settings JSON path (blank to skip templates)")
    if export_path and not Path(export_path).is_file():
        print("The export file was not found. No settings migration was staged.")
        return
    if not vaults and not export_path:
        return
    print("Clipper Plus cannot read another extension's chrome.storage. Only the Obsidian vault registry and an official exported JSON file will be used.")
    if ask("Merge the listed vaults and exported templates into Clipper Plus Settings? Type yes", "no").lower() != "yes":
        print("Settings migration declined. Existing Extension Settings are unchanged.")
        return
    result = stage_settings_migration(vaults, export_path or None, paths)
    print(f"[OK] Settings migration staged: {result['vault_count']} vault(s), {result['template_count']} template(s).")
    print("The Extension will merge it into its own Settings the next time Settings opens; existing values are retained.")


def _set_login_startup(config, paths, pythonw):
    existing = query_logon_task()
    if existing["exists"]:
        current = existing["enabled"]
        print(f"Current Windows login startup: {'enabled' if current else 'disabled'}.")
        choice = ask("Keep automatic startup when you sign in? [Y]es / [N]o", "yes" if current else "no").lower()
    else:
        choice = ask("Would you like Clipper Plus to start automatically when you sign in to Windows? [Y] Yes / [N] No").lower()
    if choice in {"y", "yes"}:
        configure_logon_task(True, pythonw, paths.auth, SETUP)
        print("[OK] Clipper Plus will start when the current Windows user signs in.")
    elif existing["exists"] and existing.get("owned_logon"):
        remove = ask("Remove the existing Clipper Plus login task? [y/N]", "no").lower()
        if remove in {"y", "yes"}:
            configure_logon_task(False, pythonw, paths.auth, SETUP)
            print("[OK] Clipper Plus login startup was disabled; the current Engine remains running.")
        else:
            print("The existing login task remains enabled. The current Engine remains running.")
    else:
        print("Automatic startup is disabled. The Native Messaging Host can start the Engine when Chrome next requests it.")


def install(extension_id_override=None):
    paths = runtime_paths()
    system = inspect_system(paths.project_root)
    state = detect_installation(paths)
    print(f"Clipper Plus Setup — {state}")
    for name, ok in system.items():
        if not ok:
            print(f"[MISSING] {name}")
    mandatory = ["Windows 10/11", "Python 3.11-3.13", "Project folder exists", "Project folder writable", "At least 3 GiB free"]
    if any(not system[key] for key in mandatory):
        raise AppError("PREREQUISITES_MISSING", "Resolve the missing Windows, Python, disk, or project-folder prerequisite before Setup can continue.")
    if not system["FFmpeg"] or not system["FFprobe"]:
        raise AppError("FFMPEG_MISSING", "Install FFmpeg and FFprobe, add their bin folder to PATH, then rerun Setup.")

    config_source = paths.auth if paths.auth.is_file() else legacy_config_path()
    config = None
    if config_source.is_file():
        config = load_config(config_source)
    if config is None:
        extension_id = select_extension_id(supplied=extension_id_override)
        config = {"version": 2, "storage": {"original_dir": str(paths.originals)},
                  "whisper": {"model": "base", "device": "cpu", "compute_type": "int8", "language": "auto"},
                  "service": {"pipe_name": f"VideoMind-{current_sid()}", "extension_id": extension_id}}
    else:
        config = dict(config)
        config["version"] = 2
        config.pop("language", None)
        config.pop("ai", None)
        config.setdefault("storage", {}).pop("summary_dir", None)
        config.setdefault("service", {})
        config["service"]["extension_id"] = select_extension_id(config, extension_id_override)
    legacy_original_dir = config.get("storage", {}).get("original_dir")

    print(f"Project Runtime: {paths.root}")
    print(f"Python environment: {SETUP / '.build' / 'venv'}")
    print("Setup preserves the current Whisper, Deno, output-directory and Extension settings. Missing dependencies only will be repaired.")
    if ask("Apply installation or repair? Type yes", "no").lower() != "yes":
        print("Setup cancelled. No changes applied.")
        return 0

    active_state = _active_task_state()
    if active_state and active_state not in {"COMPLETED", "FAILED", "CANCELLED"}:
        raise AppError("ACTIVE_TASK", "A video task is still running. Let it finish before Setup updates the background environment.")

    login_task = query_logon_task()
    _, old_python, old_pythonw = (SETUP / ".build" / "venv", SETUP / ".build" / "venv" / "Scripts" / "python.exe",
                                  SETUP / ".build" / "venv" / "Scripts" / "pythonw.exe")
    if old_pythonw.is_file() and (active_state is not None or login_task["exists"]):
        if login_task["exists"]:
            subprocess.run(["schtasks", "/End", "/TN", task_name()], capture_output=True, check=False)
        candidates = [task_configuration(login_task), config_source, paths.auth, legacy_config_path()]
        seen = set()
        for candidate in candidates:
            if candidate and candidate.is_file() and str(candidate.resolve()).casefold() not in seen:
                seen.add(str(candidate.resolve()).casefold())
                stop_engine_process_tree(old_pythonw, candidate)

    paths = ensure_runtime(paths)
    environment = ensure_python_environment(SETUP, run)
    python, pythonw = environment["python"], environment["pythonw"]
    deno = _valid_deno(config)
    if not deno:
        print("Deno 2.3.0 or later is missing. Setup will install it automatically.")
        deno = install_deno()
    config["service"]["js_runtime_path"] = deno[0]

    # Preserve the old installation until every copied file has passed a hash check.
    if config_source.resolve() != paths.auth.resolve() and config_source.is_file():
        config, migrated = migrate_legacy_configuration(config_source, paths.auth, paths)
        print("[OK] Existing engine configuration copied to Runtime and verified." if migrated else "[OK] Runtime configuration already exists.")
    config["version"] = 2
    config.pop("language", None)
    config.pop("ai", None)
    config.setdefault("storage", {}).pop("summary_dir", None)
    config["storage"]["original_dir"] = str(paths.originals)
    config["service"]["js_runtime_path"] = deno[0]
    validate_config(config)
    atomic_json(paths.auth, config)
    data_migration = migrate_legacy_data(paths, original_dir=legacy_original_dir)
    print(f"[OK] Runtime data migration verified ({len(data_migration.get('copied', []))} item(s)); old sources were retained.")

    versions = verify_ytdlp_install(python, deno[0])
    print(f"[OK] Dependencies: yt-dlp {versions[0]}, yt-dlp-ejs {versions[1]}, Deno {deno[1]}.")

    register_native_host(python, paths, paths.auth, config["service"]["extension_id"], build_native_host)
    print("[OK] Native Messaging Host registered for the current Chrome Extension.")
    ready = ensure_engine_ready(paths.auth)
    print(f"[OK] Background Engine {ready['state']} independently of Setup.")
    _set_login_startup(config, paths, pythonw)
    try:
        _stage_obsidian_migration(paths)
    except AppError as error:
        print(f"[NOTICE] {error.message} Engine setup remains complete.")
    print("Setup is complete. Closing Setup does not stop the background Engine.")
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description="Clipper Plus Setup (English only)")
    parser.add_argument("--check", action="store_true", help="Read-only environment diagnostics")
    parser.add_argument("--install", action="store_true", help="Initialize or repair the local installation")
    parser.add_argument("--extension-id", help="Use the current Chrome extension ID during installation or repair")
    args = parser.parse_args(argv)
    try:
        if args.check:
            return 0 if check_environment() else 1
        if args.install:
            return install(args.extension_id)
        parser.print_help()
        print("No changes were made. Run with --check to diagnose or --install to initialize or repair.")
        return 0
    except AppError as error:
        print(f"[ERROR] {error.code}: {error.message}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\nSetup cancelled. Existing data and configuration were left in place.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
