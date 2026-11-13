import json

from .errors import AppError

MAX_MESSAGE_BYTES = 900_000
PROTOCOL_VERSION = 2
COMMANDS = {"GET_CONFIGURATION", "START_TRANSCRIPTION", "GET_TASK_STATUS", "CANCEL_TASK", "SAVE_ORIGINAL",
            "GET_CACHED_MARKDOWN", "GET_HISTORY_MARKDOWN", "UPDATE_CACHED_MARKDOWN", "DELETE_CACHED_MARKDOWN",
            "GET_HISTORY", "DELETE_HISTORY", "CLEAR_HISTORY",
            "GET_PENDING_SETTINGS_MIGRATION", "COMPLETE_SETTINGS_MIGRATION"}


def encode_message(value):
    raw = json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode("utf-8")
    if len(raw) > MAX_MESSAGE_BYTES:
        raise AppError("MESSAGE_TOO_LARGE", "Content exceeds the native message limit. Clip a shorter source.")
    return raw


def decode_message(raw):
    if len(raw) > MAX_MESSAGE_BYTES:
        raise AppError("MESSAGE_TOO_LARGE", "Native message is too large.")
    try:
        return json.loads(raw.decode("utf-8"), parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
    except (ValueError, UnicodeError):
        raise AppError("INVALID_REQUEST", "Expected a UTF-8 JSON message.") from None


def validate_request(request):
    if not isinstance(request, dict):
        raise AppError("INVALID_REQUEST", "Expected a request object.")
    identifier = request.get("id")
    if not isinstance(identifier, str) or not identifier or len(identifier) > 128:
        raise AppError("INVALID_REQUEST", "A request id is required.")
    version = request.get("protocol_version")
    # Older 0.1.0 clients did not send a version. Keep accepting them while
    # making mismatched versions explicit for current clients.
    if version is not None and (type(version) is not int or version != PROTOCOL_VERSION):
        raise AppError("PROTOCOL_VERSION_MISMATCH", "The extension and local engine use different protocol versions.")
    command = request.get("type")
    if not isinstance(command, str) or command not in COMMANDS or not isinstance(request.get("payload", {}), dict):
        raise AppError("INVALID_REQUEST", "Unknown command or invalid payload.")
    return request["type"], request.get("payload", {})


def required_text(payload, field, limit=700_000):
    value = payload.get(field)
    if not isinstance(value, str) or not value.strip() or len(value.encode("utf-8")) > limit:
        raise AppError("INVALID_REQUEST", f"A nonempty {field} within the size limit is required.")
    return value
