"""Windows per-user message pipes. Payloads are bounded JSON, never pickle."""
import logging
import threading
import time

from .config import current_sid, pipe_address
from .contracts import MAX_MESSAGE_BYTES, decode_message, encode_message
from .errors import AppError

_log = logging.getLogger("videomind.engine")


def security_attributes():
    import pywintypes
    import win32security

    attributes = pywintypes.SECURITY_ATTRIBUTES()
    attributes.SECURITY_DESCRIPTOR = win32security.ConvertStringSecurityDescriptorToSecurityDescriptor(
        f"D:P(A;;GA;;;{current_sid()})", win32security.SDDL_REVISION_1)
    return attributes


def read_pipe(handle):
    import win32file

    code, raw = win32file.ReadFile(handle, MAX_MESSAGE_BYTES + 1)
    if code or len(raw) > MAX_MESSAGE_BYTES:
        raise AppError("MESSAGE_TOO_LARGE", "Native message is too large.")
    return decode_message(raw)


def call_engine(request, address=None, connect_timeout=3.0):
    import pywintypes
    import win32con
    import win32file
    import win32pipe

    raw = encode_message(request)
    handle = None
    connecting = True
    attempts = 0
    try:
        address = address or pipe_address()
        deadline = time.monotonic() + max(0.0, connect_timeout)
        while True:
            try:
                win32pipe.WaitNamedPipe(address, 250)
                handle = win32file.CreateFile(address, win32con.GENERIC_READ | win32con.GENERIC_WRITE,
                                              0, None, win32con.OPEN_EXISTING, 0, None)
                connecting = False
                break
            except pywintypes.error as exc:
                # The listener can be between instances. Retry only before sending:
                # replaying a submitted request could create duplicate tasks/files.
                if exc.winerror not in {2, 121, 231} or time.monotonic() >= deadline:
                    raise
                attempts += 1
                time.sleep(0.025)
        win32pipe.SetNamedPipeHandleState(handle, win32pipe.PIPE_READMODE_MESSAGE, None, None)
        win32file.WriteFile(handle, raw)
        return read_pipe(handle)
    except pywintypes.error as exc:
        winerror = getattr(exc, "winerror", None)
        if connecting and winerror in {2, 121}:
            code = "ENGINE_NOT_READY"
            message = "The Clipper Plus engine did not create its IPC endpoint in time. Check the background task status."
        else:
            code = "IPC_CONNECTION_FAILED"
            message = "The Clipper Plus background IPC connection failed. Check the engine log."
        _log.warning("ipc_connection_failed error_code=%s winerror=%s attempts=%s",
                     code, winerror if isinstance(winerror, int) else "unknown", attempts)
        raise AppError(code, message, True) from None
    finally:
        if handle is not None:
            handle.Close()


def serve(application, address=None, ready=None, stop=None):
    import pywintypes
    import win32file
    import win32pipe

    address = address or pipe_address()
    stop = stop or threading.Event()
    capacity = threading.Semaphore(16)
    ready_announced = False

    def connection(handle):
        try:
            try:
                request = read_pipe(handle)
                response = application.handle(request)
            except AppError as exc:
                response = {"id": None, "status": "error", "error": exc.to_dict()}
            win32file.WriteFile(handle, encode_message(response))
            # Wait until the client has consumed its response before disconnecting.
            win32file.FlushFileBuffers(handle)
        except (pywintypes.error, AppError):
            pass
        finally:
            try:
                win32pipe.DisconnectNamedPipe(handle)
            except pywintypes.error:
                pass
            handle.Close()
            capacity.release()

    while not stop.is_set():
        capacity.acquire()
        handle = win32pipe.CreateNamedPipe(
            address, win32pipe.PIPE_ACCESS_DUPLEX,
            win32pipe.PIPE_TYPE_MESSAGE | win32pipe.PIPE_READMODE_MESSAGE | win32pipe.PIPE_WAIT | 0x8,
            16, MAX_MESSAGE_BYTES + 1, MAX_MESSAGE_BYTES + 1, 3000, security_attributes())
        if ready:
            ready.set()
        if not ready_announced:
            _log.info("ipc_ready protocol_version=2")
            ready_announced = True
        try:
            win32pipe.ConnectNamedPipe(handle, None)
        except pywintypes.error as exc:
            if exc.winerror != 535:  # ERROR_PIPE_CONNECTED (client won the connection race)
                handle.Close()
                capacity.release()
                raise
        threading.Thread(target=connection, args=(handle,), daemon=True).start()
