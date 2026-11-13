"""Canonical project-local runtime paths and safe legacy data migration."""
from contextlib import contextmanager
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import shutil
import tempfile
import threading
from typing import Iterator

from .errors import AppError


@dataclass(frozen=True)
class RuntimePaths:
    project_root: Path
    root: Path
    auth: Path
    cache: Path
    logs: Path
    models: Path
    temp: Path
    migration: Path
    originals: Path
    host: Path


def project_root() -> Path:
    configured = os.environ.get("CLIPPER_PLUS_PROJECT_ROOT")
    return Path(configured).resolve() if configured else Path(__file__).resolve().parents[3]


def runtime_paths(root: str | Path | None = None) -> RuntimePaths:
    project = Path(root).resolve() if root else project_root()
    runtime = project / "runtime"
    return RuntimePaths(project, runtime, runtime / "auth.json", runtime / "cache",
                        runtime / "logs", runtime / "models",
                        runtime / "temp", runtime / "migration", runtime / "originals",
                        runtime / "host")


_thread_lock = threading.RLock()


@contextmanager
def migration_lock(paths: RuntimePaths) -> Iterator[None]:
    paths.migration.mkdir(parents=True, exist_ok=True)
    lock_path = paths.migration / "runtime-migration.lock"
    with _thread_lock, lock_path.open("a+b") as stream:
        stream.seek(0, os.SEEK_END)
        if stream.tell() == 0:
            stream.write(b"\0")
            stream.flush()
        stream.seek(0)
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(stream.fileno(), msvcrt.LK_LOCK, 1)
            try:
                yield
            finally:
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def ensure_runtime(paths: RuntimePaths | None = None) -> RuntimePaths:
    paths = paths or runtime_paths()
    paths.project_root.mkdir(parents=True, exist_ok=True)
    try:
        paths.root.mkdir(parents=True, exist_ok=True)
        for directory in (paths.cache, paths.logs, paths.models,
                          paths.temp, paths.migration, paths.originals, paths.host):
            directory.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(prefix=".clipper-plus-write-check-", dir=paths.root,
                                         delete=True):
            pass
    except OSError:
        raise AppError("RUNTIME_DIRECTORY_UNAVAILABLE",
                       "The project Runtime directory is not writable. Move Clipper Plus to a writable folder and retry.") from None
    return paths


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def copy_verified(source: Path, destination: Path) -> bool:
    """Copy without replacing an existing target and verify before publishing it."""
    if not source.exists():
        return False
    destination.parent.mkdir(parents=True, exist_ok=True)
    if source.is_dir():
        if destination.exists():
            return False
        temporary = destination.with_name(destination.name + ".migration-tmp")
        if temporary.exists():
            shutil.rmtree(temporary)
        shutil.copytree(source, temporary, copy_function=shutil.copy2)
        source_files = sorted(path.relative_to(source) for path in source.rglob("*") if path.is_file())
        copied_files = sorted(path.relative_to(temporary) for path in temporary.rglob("*") if path.is_file())
        if source_files != copied_files or any(_sha256(source / name) != _sha256(temporary / name)
                                               for name in source_files):
            shutil.rmtree(temporary, ignore_errors=True)
            raise AppError("RUNTIME_MIGRATION_VERIFY_FAILED", "A legacy data copy did not pass integrity checks.")
        os.replace(temporary, destination)
        return True
    if destination.exists():
        return False
    temporary = destination.with_name(destination.name + ".migration-tmp")
    try:
        shutil.copy2(source, temporary)
        if _sha256(source) != _sha256(temporary):
            raise AppError("RUNTIME_MIGRATION_VERIFY_FAILED", "A legacy data copy did not pass integrity checks.")
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)
    return True


def atomic_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            json.dump(value, stream, indent=2, ensure_ascii=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def migrate_legacy_data(paths: RuntimePaths, legacy_root: str | Path | None = None,
                        model_cache: str | Path | None = None,
                        original_dir: str | Path | None = None) -> dict:
    """Copy legacy per-user engine data into Runtime, retaining every source."""
    paths = ensure_runtime(paths)
    legacy = Path(legacy_root) if legacy_root else Path(os.environ.get("LOCALAPPDATA", Path.home())) / "VideoMind"
    with migration_lock(paths):
        marker = paths.migration / "runtime-migration.json"
        previous = None
        if marker.is_file():
            try:
                previous = json.loads(marker.read_text(encoding="utf-8"))
                same_originals = (not original_dir or previous.get("originals_source") ==
                                  str(Path(original_dir).expanduser().resolve()))
                if previous.get("schema") == 1 and previous.get("complete") is True and same_originals:
                    return previous
            except (OSError, ValueError, AttributeError):
                pass

        copied: list[str] = list(previous.get("copied", [])) if isinstance(previous, dict) and isinstance(previous.get("copied"), list) else []
        cache_file = legacy / "completed_markdown.json"
        if copy_verified(cache_file, paths.cache / "completed_markdown.json"):
            copied.append("completed_markdown.json")
        for name, destination in (("logs", paths.logs), ("tmp", paths.temp)):
            source = legacy / name
            if source.is_dir():
                destination.mkdir(parents=True, exist_ok=True)
                for item in sorted(source.rglob("*")):
                    relative = item.relative_to(source)
                    target = destination / relative
                    if item.is_dir():
                        target.mkdir(parents=True, exist_ok=True)
                    elif item.is_file() and not target.exists():
                        if copy_verified(item, target):
                            copied.append(f"{name}/{relative.as_posix()}")

        if original_dir:
            source_originals = Path(original_dir).expanduser()
            try:
                same_as_runtime = source_originals.resolve() == paths.originals.resolve()
            except OSError:
                same_as_runtime = False
            if source_originals.is_dir() and not same_as_runtime:
                for item in sorted(source_originals.rglob("*.md")):
                    if item.is_symlink() or not item.is_file():
                        continue
                    relative = item.relative_to(source_originals)
                    target = paths.originals / "migrated" / relative
                    target.parent.mkdir(parents=True, exist_ok=True)
                    if target.exists() and _sha256(target) != _sha256(item):
                        target = target.with_name(f"{target.stem}-{_sha256(item)[:10]}{target.suffix}")
                    if copy_verified(item, target):
                        copied.append(f"originals/{relative.as_posix()}")

        model_roots = [Path(model_cache)] if model_cache else []
        model_roots.extend([
            Path(os.environ.get("HUGGINGFACE_HUB_CACHE", "")) if os.environ.get("HUGGINGFACE_HUB_CACHE") else None,
            Path(os.environ.get("HF_HOME", "")) / "hub" if os.environ.get("HF_HOME") else None,
            Path(os.environ.get("USERPROFILE", Path.home())) / ".cache" / "huggingface" / "hub",
            Path(os.environ.get("USERPROFILE", Path.home())) / ".cache" / "torch" / "hub",
        ])
        for source_root in (path for path in model_roots if path is not None):
            if not source_root.is_dir():
                continue
            for model in sorted(source_root.glob("models--Systran--faster-whisper-*")):
                if model.is_dir() and copy_verified(model, paths.models / "hub" / model.name):
                    copied.append(f"models/{model.name}")

        report = {"schema": 1, "complete": True, "source": str(legacy.resolve()),
                  "destination": str(paths.root.resolve()), "copied": copied,
                  "originals_source": str(Path(original_dir).expanduser().resolve()) if original_dir else None,
                  "legacy_source_retained": True}
        atomic_json(marker, report)
        return report
