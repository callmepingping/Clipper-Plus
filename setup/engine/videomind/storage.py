import re
from pathlib import Path

from .errors import AppError


def safe_filename(title):
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f\x7f]', "_", title).strip().rstrip(". ")[:120].rstrip(". ")
    if not name:
        name = "Untitled"
    if re.match(r"^(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\.|$)", name, re.IGNORECASE):
        name = "_" + name
    return name


def save_markdown(config, markdown, title):
    directory = Path(config["storage"]["original_dir"]).resolve()
    if not directory.is_dir():
        raise AppError("OUTPUT_DIRECTORY_INVALID", "The configured output directory is unavailable. Run setup again.")
    stem = safe_filename(title)
    for index in range(10000):
        path = directory / f"{stem}{f' ({index})' if index else ''}.md"
        try:
            # Exclusive creation also rejects existing symbolic links and races.
            with path.open("x", encoding="utf-8", newline="\n") as stream:
                stream.write(markdown)
            return str(path)
        except FileExistsError:
            continue
        except OSError as exc:
            # Remove only the new file created by this attempt after a partial write.
            if 'stream' in locals() and stream.name == str(path):
                try:
                    path.unlink(missing_ok=True)
                except OSError:
                    pass
            code = "DISK_FULL" if getattr(exc, "errno", None) == 28 or getattr(exc, "winerror", None) == 112 else "OUTPUT_DIRECTORY_INVALID"
            raise AppError(code, "Could not save Markdown to the configured directory.") from None
    raise AppError("OUTPUT_DIRECTORY_INVALID", "Too many files with this title. Choose a different title.")
