"""Read-only prerequisite inspection and minimal virtual-environment repair."""
import os
from pathlib import Path
import shutil
import subprocess
import sys
import venv

from videomind.errors import AppError


def python_environment(setup_dir):
    environment = Path(setup_dir) / ".build" / "venv"
    return environment, environment / "Scripts" / "python.exe", environment / "Scripts" / "pythonw.exe"


def inspect_system(project_root):
    root = Path(project_root)
    checks = {
        "Windows 10/11": os.name == "nt" and sys.getwindowsversion().major >= 10,
        "Python 3.11-3.13": (3, 11) <= sys.version_info[:2] < (3, 14),
        "FFmpeg": shutil.which("ffmpeg") is not None,
        "FFprobe": shutil.which("ffprobe") is not None,
        "Project folder exists": root.is_dir(),
        "Project folder writable": root.is_dir() and os.access(root, os.W_OK),
    }
    free_space = shutil.disk_usage(root).free if root.exists() else 0
    checks["At least 3 GiB free"] = free_space >= 3 * 1024**3
    return checks


def inspect_python_dependencies(python):
    python = Path(python)
    if not python.is_file():
        return {"environment": "missing", "pip_check": False, "missing": ["virtual environment"]}
    code = ("import importlib.util,json,sys; from importlib.metadata import PackageNotFoundError,version; "
            "mods=['faster_whisper','yt_dlp','yt_dlp_ejs','av','win32pipe','PyInstaller']; "
            "packages=['faster-whisper','yt-dlp','yt-dlp-ejs','av','pywin32','pyinstaller']; "
            "data={'python':list(sys.version_info[:3]),'pip':importlib.util.find_spec('pip') is not None,"
            "'modules':{m:importlib.util.find_spec(m) is not None for m in mods},'versions':{}}; "
            "\nfor p in packages:\n try: data['versions'][p]=version(p)\n except PackageNotFoundError: data['versions'][p]=None\n"
            "print(json.dumps(data))")
    try:
        probe = subprocess.run([str(python), "-c", code], capture_output=True, text=True,
                               timeout=30, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return {"environment": "broken", "pip_check": False,
                "missing": ["Python virtual environment probe"]}
    if probe.returncode:
        return {"environment": "present", "pip_check": False, "missing": ["Python dependency probe"]}
    import json
    try:
        data = json.loads(probe.stdout)
    except ValueError:
        return {"environment": "present", "pip_check": False, "missing": ["Python dependency probe"]}
    module_packages = {"faster_whisper": "faster-whisper", "yt_dlp": "yt-dlp", "yt_dlp_ejs": "yt-dlp-ejs",
                       "av": "av", "win32pipe": "pywin32", "PyInstaller": "pyinstaller"}
    missing = [package for module, package in module_packages.items() if not data["modules"].get(module)]
    python_version = tuple(data.get("python", (0, 0)))[:2]
    if not (3, 11) <= python_version < (3, 14):
        missing.append("Python 3.11-3.13 virtual environment")
    if not data.get("pip"):
        missing.append("pip")
    versions = data["versions"]
    if versions.get("av"):
        try:
            if tuple(int(part) for part in versions["av"].split(".")[:2]) >= (19, 0):
                missing.append("av<19")
        except ValueError:
            missing.append("av<19")
    if versions.get("yt-dlp") and versions["yt-dlp"] != "2026.10.26":
        missing.append("yt-dlp==2026.10.26")
    if versions.get("faster-whisper") and versions["faster-whisper"] != "1.2.1":
        missing.append("faster-whisper==1.2.1")
    try:
        pip_check = subprocess.run([str(python), "-m", "pip", "check"], capture_output=True,
                                   timeout=30, check=False).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        pip_check = False
    return {"environment": "present", "pip_check": pip_check, "missing": sorted(set(missing))}


def install_requirements(python, setup_dir, run_command):
    root = Path(setup_dir)
    pip_cache = root.parent / "runtime" / "cache" / "pip"
    pip_cache.mkdir(parents=True, exist_ok=True)
    run_command([python, "-m", "pip", "--cache-dir", pip_cache, "install", "-r", root / "requirements.txt",
                 "-r", root / "requirements-build.txt", "-e", root / "engine"])
    result = inspect_python_dependencies(python)
    if not result["pip_check"] or result["missing"]:
        raise AppError("DEPENDENCY_VERIFICATION_FAILED",
                       "The Python environment still has missing or incompatible project dependencies after repair.")
    return result


def ensure_python_environment(setup_dir, run_command):
    environment, python, pythonw = python_environment(setup_dir)
    if not python.exists():
        venv.EnvBuilder(with_pip=True).create(environment)
    health = inspect_python_dependencies(python)
    if health["missing"] or not health["pip_check"]:
        install_requirements(python, setup_dir, run_command)
    return {"environment": environment, "python": python, "pythonw": pythonw,
            "dependencies_installed": bool(health["missing"] or not health["pip_check"])}
