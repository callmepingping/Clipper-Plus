"""One process coordinator shared by Setup, the login task and Native Host."""
from contextlib import contextmanager
import os
from pathlib import Path
import subprocess
import threading
import time

from .config import config_path, legacy_config_path, load_config
from .errors import AppError
from .ipc import call_engine
from .runtime import ensure_runtime, runtime_paths


def prepare_runtime_configuration(config=None):
    paths = runtime_paths()
    legacy = legacy_config_path().resolve()
    explicit_config = config or os.environ.get("VIDEOMIND_CONFIG")
    target = config_path(config)
    if explicit_config and Path(explicit_config).resolve() != legacy and not Path(explicit_config).is_file():
        raise AppError("SETUP_REQUIRED", "Clipper Plus is not initialized. Run Clipper Plus Setup.")
    if target.resolve() == legacy:
        target = paths.auth
    if not target.is_file():
        raise AppError("SETUP_REQUIRED", "Clipper Plus is not initialized. Run Clipper Plus Setup.")
    elif target.resolve() == paths.auth.resolve():
        ensure_runtime(paths)
    return target


@contextmanager
def _start_lock(timeout_ms=30000):
    try:
        import win32api
        import win32event
    except ImportError:
        raise AppError("ENGINE_START_COORDINATION_FAILED",
                       "Windows process coordination is unavailable. Run Clipper Plus Setup to repair the Python environment.", True) from None
    from .config import current_sid

    mutex = win32event.CreateMutex(None, False, f"Local\\VideoMind-EngineStart-{current_sid()}")
    try:
        result = win32event.WaitForSingleObject(mutex, timeout_ms)
        if result not in (win32event.WAIT_OBJECT_0, win32event.WAIT_ABANDONED):
            raise AppError("ENGINE_START_COORDINATION_FAILED",
                           "Another Clipper Plus startup is still being coordinated.", True)
        try:
            yield
        finally:
            win32event.ReleaseMutex(mutex)
    finally:
        win32api.CloseHandle(mutex)


def _health_check(config_file: Path):
    try:
        response = call_engine({"id": "engine-ready-check", "type": "GET_CONFIGURATION",
                                "payload": {}, "protocol_version": 2}, connect_timeout=0.15)
    except AppError:
        raise
    except (ImportError, OSError):
        raise AppError("IPC_CONNECTION_FAILED",
                       "The Python IPC components could not connect to the background Engine.", True) from None
    if not isinstance(response, dict) or response.get("id") != "engine-ready-check":
        raise AppError("ENGINE_HEALTH_INVALID", "The background Engine returned an invalid readiness response.")
    if response.get("status") == "error":
        error = response.get("error")
        code = error.get("code") if isinstance(error, dict) else "ENGINE_HEALTH_FAILED"
        raise AppError(code, "The background Engine rejected its readiness check.")
    data = response.get("data")
    if response.get("status") != "ok" or not isinstance(data, dict):
        raise AppError("ENGINE_HEALTH_INVALID", "The background Engine returned an invalid readiness response.")
    if data.get("protocol_version") != 2:
        raise AppError("PROTOCOL_VERSION_MISMATCH",
                       "The background Engine uses a different Native Messaging protocol. Repair the installation with Setup.")
    return response


def _launch_engine(config_file: Path, paths, pythonw: Path | None = None):
    pythonw = pythonw or paths.project_root / "setup" / ".build" / "venv" / "Scripts" / "pythonw.exe"
    if not pythonw.is_file():
        raise AppError("ENGINE_RUNTIME_MISSING", "The Clipper Plus Python environment is missing. Run Setup to repair it.")
    setup_dir = paths.project_root / "setup"
    env = os.environ.copy()
    env["VIDEOMIND_CONFIG"] = str(config_file)
    env["CLIPPER_PLUS_PROJECT_ROOT"] = str(paths.project_root)
    env["HF_HOME"] = str(paths.models)
    env["HUGGINGFACE_HUB_CACHE"] = str(paths.models / "hub")
    env["TRANSFORMERS_CACHE"] = str(paths.models / "transformers")
    env["TORCH_HOME"] = str(paths.models / "torch")
    if os.name == "nt":
        flags = (getattr(subprocess, "DETACHED_PROCESS", 0x00000008) |
                 getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200) |
                 getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000) |
                 getattr(subprocess, "CREATE_BREAKAWAY_FROM_JOB", 0x01000000))
        try:
            return subprocess.Popen([str(pythonw), "-m", "videomind.server", "--config", str(config_file)],
                                    cwd=setup_dir, env=env, stdin=subprocess.DEVNULL,
                                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                    close_fds=True, creationflags=flags)
        except OSError:
            raise AppError("ENGINE_START_FAILED", "Windows could not start the Clipper Plus background Engine. Check the Python path and runtime/logs/engine.log.", True) from None
    try:
        return subprocess.Popen([str(pythonw), "-m", "videomind.server", "--config", str(config_file)],
                                cwd=setup_dir, env=env, stdin=subprocess.DEVNULL,
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                close_fds=True, start_new_session=True)
    except OSError:
        raise AppError("ENGINE_START_FAILED", "The operating system could not start the Clipper Plus background Engine. Check the Python path and runtime/logs/engine.log.", True) from None


def _is_ready(response):
    if not isinstance(response, dict):
        raise AppError("ENGINE_HEALTH_INVALID", "The background Engine returned an invalid readiness response.")
    return response.get("status") == "ok"


def ensure_engine_ready(config=None, timeout=30, *, start=True, launcher=None,
                        health_check=None, pythonw=None):
    """Reuse a healthy engine or start one detached, then verify its Ready response."""
    config_file = prepare_runtime_configuration(config)
    paths = runtime_paths()
    if config_file.resolve() == paths.auth.resolve():
        ensure_runtime(paths)
    load_config(config_file)
    health_check = health_check or _health_check
    try:
        response = health_check(config_file)
        if _is_ready(response):
            return {"state": "READY", "started": False, "config": str(config_file)}
    except AppError as error:
        if error.code not in {"ENGINE_NOT_READY", "IPC_CONNECTION_FAILED"}:
            raise
    if not start:
        return {"state": "NOT_RUNNING", "started": False, "config": str(config_file)}

    with _start_lock():
        try:
            response = health_check(config_file)
            if _is_ready(response):
                return {"state": "READY", "started": False, "config": str(config_file)}
        except AppError as error:
            if error.code not in {"ENGINE_NOT_READY", "IPC_CONNECTION_FAILED"}:
                raise

        try:
            if launcher:
                process = launcher(config_file, paths)
            else:
                process = _launch_engine(config_file, paths, pythonw=pythonw)
        except AppError:
            raise
        except OSError:
            raise AppError("ENGINE_START_FAILED", "The operating system could not start the Clipper Plus background Engine.", True) from None
        deadline = time.monotonic() + timeout
        interval = 0.05
        last_error = None
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise AppError("ENGINE_START_FAILED",
                               "The background engine exited before becoming ready. Check runtime/logs/engine.log.", True)
            try:
                response = health_check(config_file)
                if _is_ready(response):
                    return {"state": "READY", "started": True, "config": str(config_file)}
                last_error = "unexpected_health_response"
            except AppError as error:
                if error.code not in {"ENGINE_NOT_READY", "IPC_CONNECTION_FAILED"}:
                    raise
                last_error = error.code
            threading.Event().wait(interval)
            interval = min(interval * 1.5, 0.5)
        raise AppError("ENGINE_READY_TIMEOUT",
                       f"The background engine did not become ready within {timeout} seconds ({last_error or 'no response'}). Check runtime/logs/engine.log.", True)
