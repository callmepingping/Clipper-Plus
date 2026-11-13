import argparse
import logging
from logging.handlers import RotatingFileHandler
import os
import sys
import traceback

from .api import Application
from .config import current_sid, load_config
from .errors import AppError
from .ipc import security_attributes, serve
from .engine_lifecycle import prepare_runtime_configuration
from .runtime import ensure_runtime, runtime_paths


def main(argv=None):
    parser = argparse.ArgumentParser(description="Clipper Plus independent background engine")
    parser.add_argument("--config", help="Path to setup-generated auth.json")
    args = parser.parse_args(argv)
    if os.name != "nt":
        parser.exit(1, "Clipper Plus requires Windows 10 or 11.\n")
    paths = ensure_runtime(runtime_paths())
    configured = prepare_runtime_configuration(args.config)
    os.environ["VIDEOMIND_CONFIG"] = str(configured)
    os.environ["CLIPPER_PLUS_PROJECT_ROOT"] = str(paths.project_root)
    os.environ["HF_HOME"] = str(paths.models)
    os.environ["HUGGINGFACE_HUB_CACHE"] = str(paths.models / "hub")
    os.environ["TRANSFORMERS_CACHE"] = str(paths.models / "transformers")
    os.environ["TORCH_HOME"] = str(paths.models / "torch")
    log_dir = paths.logs
    log_dir.mkdir(parents=True, exist_ok=True)
    diagnostic = RotatingFileHandler(log_dir / "transcription.log", maxBytes=2_000_000,
                                     backupCount=2, encoding="utf-8")
    diagnostic.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    logging.getLogger("videomind.transcription").addHandler(diagnostic)
    logging.getLogger("videomind.transcription").setLevel(logging.ERROR)
    logging.getLogger("videomind.cache").addHandler(diagnostic)
    logging.getLogger("videomind.cache").setLevel(logging.ERROR)
    engine_log = logging.getLogger("videomind.engine")
    engine_log.addHandler(diagnostic)
    engine_log.setLevel(logging.INFO)
    engine_log.info("engine_starting process_id=%s python_version=%s", os.getpid(), sys.version.split()[0])
    mutex = None
    try:
        import win32api
        import win32event
        mutex = win32event.CreateMutex(security_attributes(), False, f"Local\\VideoMind-Engine-{current_sid()}")
        if win32api.GetLastError() == 183:
            engine_log.info("duplicate_engine_instance_ignored")
            mutex.Close()
            mutex = None
            return 0
        engine_log.info("configuration_loading")
        load_config()
        engine_log.info("configuration_loaded")
        from functools import partial
        from pathlib import Path
        import shutil
        from .config import config_path
        from .transcription import transcribe_video
        temporary_root = paths.temp
        temporary_root.mkdir(parents=True, exist_ok=True)
        # Only this engine's named task directories; the mutex excludes active work.
        for abandoned in temporary_root.glob("videomind-*"):
            if abandoned.is_dir() and not abandoned.is_symlink() and abandoned.resolve().parent == temporary_root.resolve():
                shutil.rmtree(abandoned, ignore_errors=True)
        # Completed Markdown is persisted independently of the transient task state.
        # In-memory progress/error state remains scoped to the running engine.
        # Each new request sees reconfigured settings; active workers retain their
        # submission-time snapshot and are never interrupted by setup.
        engine_log.info("engine_initialization_complete")
        serve(Application(transcriber=partial(transcribe_video, temp_root=temporary_root)))
    except AppError as exc:
        engine_log.error("engine_start_failed error_code=%s", exc.code)
        parser.exit(1, f"{exc.code}: {exc.message}\n")
    except Exception as exc:
        frames = "\n".join(
            f'  File "{frame.filename}", line {frame.lineno}, in {frame.name}'
            for frame in traceback.extract_tb(exc.__traceback__)
        )[-8000:]
        engine_log.error("engine_start_failed exception_type=%s traceback_frames=\n%s",
                         type(exc).__name__, frames)
        parser.exit(1, "Clipper Plus background engine failed during initialization. Check the engine log.\n")
    finally:
        engine_log.info("engine_stopped")
        if mutex is not None:
            mutex.Close()


if __name__ == "__main__":
    main()
