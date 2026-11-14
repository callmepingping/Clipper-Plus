"""Short-lived Chrome bridge; it never owns download/transcription work."""
import argparse
import os
from pathlib import Path
import struct
import sys

from .config import load_config
from .contracts import MAX_MESSAGE_BYTES, decode_message, encode_message
from .engine_lifecycle import ensure_engine_ready, prepare_runtime_configuration
from .errors import AppError
from .ipc import call_engine


def read_exact(stream, length):
    chunks = bytearray()
    while len(chunks) < length:
        chunk = stream.read(length - len(chunks))
        if not chunk:
            raise AppError("NATIVE_MESSAGE_TRUNCATED", "Incomplete native message.")
        chunks.extend(chunk)
    return bytes(chunks)


def read_message(stream):
    prefix = stream.read(4)
    if not prefix:
        return None
    if len(prefix) < 4:
        prefix += read_exact(stream, 4 - len(prefix))
    length = struct.unpack("<I", prefix)[0]
    if length > MAX_MESSAGE_BYTES:
        raise AppError("MESSAGE_TOO_LARGE", "Native message is too large.")
    return decode_message(read_exact(stream, length))


def write_message(stream, value):
    raw = encode_message(value)
    stream.write(struct.pack("<I", len(raw)) + raw)
    stream.flush()


def bridge(input_stream, output_stream, handler=call_engine):
    while True:
        request = None
        try:
            request = read_message(input_stream)
            if request is None:
                return
            response = handler(request)
            write_message(output_stream, response)
        except AppError as exc:
            identifier = request.get("id") if isinstance(request, dict) else None
            write_message(output_stream, {"id": identifier, "status": "error", "error": exc.to_dict()})
            # No resynchronization after an oversized or truncated frame.
            return
        except Exception:
            identifier = request.get("id") if isinstance(request, dict) else None
            # Keep stdout protocol-only and avoid leaking paths or request data.
            write_message(output_stream, {"id": identifier, "status": "error", "error": {
                "code": "NATIVE_HOST_INTERNAL_ERROR",
                "message": "The Clipper Plus Native Messaging Host could not complete the request. Run Setup with --check to inspect the installation.",
                "retryable": True,
            }})
            return


def configure_frozen_installation(executable):
    # The one-file host extracts Python modules into a temporary directory.
    # Its installed executable stays in <project>/runtime/host/.
    executable = Path(executable).resolve()
    os.environ["CLIPPER_PLUS_PROJECT_ROOT"] = str(executable.parents[2])
    location = executable.with_name("config-location.txt")
    if location.is_file():
        os.environ["VIDEOMIND_CONFIG"] = location.read_text(encoding="utf-8").strip()


def main(argv=None):
    parser = argparse.ArgumentParser(description="Clipper Plus Chrome Native Messaging bridge")
    parser.add_argument("origin", help="Chrome extension origin supplied by Chrome")
    parser.add_argument("--config")
    # Chrome also supplies --parent-window=<handle> on Windows.
    args, _ = parser.parse_known_args(argv)
    if args.config:
        os.environ["VIDEOMIND_CONFIG"] = args.config
    elif getattr(sys, "frozen", False):
        configure_frozen_installation(sys.executable)
    if os.name == "nt":
        import msvcrt
        msvcrt.setmode(sys.stdin.fileno(), os.O_BINARY)
        msvcrt.setmode(sys.stdout.fileno(), os.O_BINARY)

    def forward(request):
        config_file = prepare_runtime_configuration()
        config = load_config(config_file)
        extension_id = config.get("service", {}).get("extension_id", "")
        if not extension_id or args.origin != f"chrome-extension://{extension_id}/":
            raise AppError("NATIVE_ORIGIN_DENIED", "This extension is not registered with Clipper Plus Setup.")
        ensure_engine_ready(config_file)
        return call_engine(request)

    # Read the request before checking configuration/origin so setup errors carry
    # its id and the extension can show the actual cause instead of INVALID_RESPONSE.
    bridge(sys.stdin.buffer, sys.stdout.buffer, forward)


if __name__ == "__main__":
    main()
