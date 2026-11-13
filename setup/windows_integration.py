"""Windows-owned host registration and optional per-user logon task."""
import json
import hashlib
import locale
import os
from pathlib import Path
import re
import subprocess
import time
import xml.etree.ElementTree as ET

from videomind.config import current_sid
from videomind.errors import AppError
from videomind.runtime import RuntimePaths, atomic_json, ensure_runtime

HOST_NAME = "com.videomind.host"
TASK_PREFIX = "VideoMind-"


def task_name():
    return TASK_PREFIX + current_sid()


def task_xml(pythonw, configuration, setup_directory):
    namespace = "http://schemas.microsoft.com/windows/2004/02/mit/task"
    ET.register_namespace("", namespace)

    def child(parent, name, text=None, **attrs):
        item = ET.SubElement(parent, f"{{{namespace}}}{name}", attrs)
        item.text = text
        return item

    root = ET.Element(f"{{{namespace}}}Task", {"version": "1.2"})
    trigger = child(child(root, "Triggers"), "LogonTrigger")
    child(trigger, "Enabled", "true")
    child(trigger, "UserId", current_sid())
    principal = child(child(root, "Principals"), "Principal", id="CurrentUser")
    child(principal, "UserId", current_sid())
    child(principal, "LogonType", "InteractiveToken")
    child(principal, "RunLevel", "LeastPrivilege")
    settings = child(root, "Settings")
    child(settings, "MultipleInstancesPolicy", "IgnoreNew")
    child(settings, "DisallowStartIfOnBatteries", "false")
    child(settings, "StopIfGoingOnBatteries", "false")
    child(settings, "ExecutionTimeLimit", "PT0S")
    action = child(child(root, "Actions", Context="CurrentUser"), "Exec")
    child(action, "Command", str(pythonw))
    child(action, "Arguments", subprocess.list2cmdline(["-m", "videomind.startup", "--config", str(configuration)]))
    child(action, "WorkingDirectory", str(setup_directory))
    return ET.tostring(root, encoding="utf-16", xml_declaration=True)


def stop_engine_process_tree(pythonw, configuration):
    """Stop only this installation's launcher and its interpreter child."""
    def literal(value):
        return "'" + str(Path(value).resolve()).replace("'", "''") + "'"

    script = ("$p=" + literal(pythonw) + "; $c=" + literal(configuration) + "; "
              "$m=@(Get-CimInstance Win32_Process | Where-Object { $_.Name -ieq 'pythonw.exe' -and $_.CommandLine -and "
              "$_.CommandLine.IndexOf($p,[StringComparison]::OrdinalIgnoreCase) -ge 0 -and "
              "$_.CommandLine.IndexOf('-m videomind.server',[StringComparison]::OrdinalIgnoreCase) -ge 0 -and "
              "$_.CommandLine.IndexOf($c,[StringComparison]::OrdinalIgnoreCase) -ge 0 }); "
              "$ids=@($m | ForEach-Object { $_.ProcessId }); "
              "$m | Where-Object { $ids -notcontains $_.ParentProcessId } | ForEach-Object { $_.ProcessId }")
    inspected = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
                               capture_output=True, text=True, check=False)
    if inspected.returncode:
        raise AppError("ENGINE_STOP_FAILED", "Could not safely identify the existing Clipper Plus engine process.")
    ids = [line.strip() for line in inspected.stdout.splitlines() if line.strip().isdecimal() and int(line) > 0]
    for process_id in ids:
        stopped = subprocess.run(["taskkill", "/T", "/F", "/PID", process_id], capture_output=True, check=False)
        if stopped.returncode:
            raise AppError("ENGINE_STOP_FAILED", "The existing background engine did not stop cleanly. Retry Setup after the current task finishes.")


def _native_host_fingerprint(project_root):
    root = Path(project_root)
    inputs = [root / "setup" / "native_entry.py", root / "setup" / "requirements.txt",
              root / "setup" / "requirements-build.txt", root / "setup" / "engine" / "pyproject.toml"]
    inputs.extend(sorted((root / "setup" / "engine" / "videomind").glob("*.py")))
    digest = hashlib.sha256()
    for source in inputs:
        if not source.is_file():
            continue
        digest.update(source.relative_to(root).as_posix().encode("utf-8"))
        digest.update(source.read_bytes())
    return digest.hexdigest()


def register_native_host(python, paths: RuntimePaths, configuration, extension_id, build_host):
    paths = ensure_runtime(paths)
    host_exe = paths.host / "videomind-host.exe"
    fingerprint = _native_host_fingerprint(paths.project_root)
    fingerprint_file = paths.host / "build-fingerprint.txt"
    try:
        previous_fingerprint = fingerprint_file.read_text(encoding="ascii").strip() if fingerprint_file.is_file() else ""
    except (OSError, UnicodeError):
        previous_fingerprint = ""
    if not host_exe.is_file() or previous_fingerprint != fingerprint:
        build_host(python, paths.host)
    if not host_exe.is_file():
        raise AppError("NATIVE_HOST_BUILD_FAILED", "The Native Messaging Host executable was not created.")
    if previous_fingerprint != fingerprint:
        fingerprint_file.write_text(fingerprint, encoding="ascii")
    (paths.host / "config-location.txt").write_text(str(configuration.resolve()), encoding="utf-8")
    manifest = {"name": HOST_NAME, "description": "Clipper Plus Native Messaging bridge",
                "path": str(host_exe.resolve()), "type": "stdio",
                "allowed_origins": [f"chrome-extension://{extension_id}/"]}
    manifest_path = paths.host / "native-host.json"
    atomic_json(manifest_path, manifest)
    if os.name == "nt":
        import winreg
        registry_path = rf"Software\Google\Chrome\NativeMessagingHosts\{HOST_NAME}"
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, registry_path) as registry:
            winreg.SetValueEx(registry, "", 0, winreg.REG_SZ, str(manifest_path.resolve()))
    return manifest_path


def _task_output_encodings():
    """Code pages exposed by Windows for console and redirected CLI output."""
    encodings = []
    if os.name == "nt":
        import ctypes
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        for api in ("GetConsoleOutputCP", "GetACP", "GetOEMCP"):
            page = getattr(kernel, api)()
            if page:
                encodings.append(f"cp{page}")
    encodings.append(locale.getpreferredencoding(False))
    return list(dict.fromkeys(encodings))


def _parse_schtasks_xml(raw):
    if not isinstance(raw, bytes) or not raw or len(raw) > 4 * 1024 * 1024:
        raise AppError("SCHEDULED_TASK_INVALID", "The existing startup task returned empty or oversized XML; Setup left it unchanged.")

    if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        encodings = ["utf-16"]
    elif raw.startswith(b"\xef\xbb\xbf"):
        encodings = ["utf-8-sig"]
    else:
        encodings = ["utf-8", *_task_output_encodings()]

    for encoding in dict.fromkeys(encodings):
        try:
            text = raw.decode(encoding, errors="strict")
        except (UnicodeError, LookupError):
            continue
        # schtasks can declare UTF-16 while emitting bytes in the local code
        # page. Once decoded to Unicode, that declaration is no longer useful.
        text = re.sub(r"\A<\?xml\s+[^?]*\?>", "", text, count=1, flags=re.IGNORECASE)
        try:
            root = ET.fromstring(text)
        except ET.ParseError:
            continue
        if root.tag.rsplit("}", 1)[-1] == "Task":
            return root
    raise AppError("SCHEDULED_TASK_INVALID",
                   "The existing startup task XML could not be decoded and validated with the Windows output code pages. Setup left it unchanged.")


def query_logon_task(name=None):
    name = name or task_name()
    result = subprocess.run(["schtasks", "/Query", "/TN", name, "/XML"],
                            capture_output=True, check=False)
    if result.returncode:
        return {"exists": False, "enabled": False, "xml": None}
    try:
        root = _parse_schtasks_xml(result.stdout)
        namespace = {"t": "http://schemas.microsoft.com/windows/2004/02/mit/task"}
        enabled_text = root.findtext("t:Settings/t:Enabled", default="true", namespaces=namespace)
        command = root.findtext("t:Actions/t:Exec/t:Command", default="", namespaces=namespace)
        arguments = root.findtext("t:Actions/t:Exec/t:Arguments", default="", namespaces=namespace)
        trigger = root.find("t:Triggers/t:LogonTrigger", namespace) is not None
        lower_arguments = arguments.lower()
        current_user = current_sid()
        trigger_user = root.findtext("t:Triggers/t:LogonTrigger/t:UserId", default="", namespaces=namespace)
        principal_user = root.findtext("t:Principals/t:Principal/t:UserId", default="", namespaces=namespace)
        identity_matches = current_user in {trigger_user, principal_user}
        new_entry = "videomind.startup" in lower_arguments and "--config" in lower_arguments
        legacy_entry = ("videomind.server" in lower_arguments and
                        re.search(r"(?i)setup[\\/]\.build[\\/]venv[\\/]scripts[\\/]pythonw\.exe$", command) is not None)
        owned = trigger and identity_matches and (new_entry or legacy_entry)
        return {"exists": True, "enabled": enabled_text.lower() == "true", "xml": root,
                "command": command, "arguments": arguments, "owned_logon": owned,
                "legacy_engine_task": trigger and identity_matches and legacy_entry}
    except ET.ParseError:
        raise AppError("SCHEDULED_TASK_INVALID", "The existing Clipper Plus startup task XML is malformed; Setup left it unchanged.") from None


def task_configuration(task):
    if not task or not task.get("exists"):
        return None
    match = re.search(r"(?i)(?:^|\s)--config\s+(?:\"([^\"]+)\"|(\S+))", task.get("arguments", ""))
    value = (match.group(1) or match.group(2)) if match else None
    return Path(value) if value else None


def configure_logon_task(enabled, pythonw, configuration, setup_directory, name=None):
    name = name or task_name()
    if enabled:
        existing = query_logon_task(name)
        if existing["exists"] and not existing.get("owned_logon"):
            raise AppError("SCHEDULED_TASK_OWNERSHIP_UNCLEAR",
                           "An existing task with the Clipper Plus task name is not a recognized Clipper Plus logon task; Setup left it unchanged.")
        content = task_xml(pythonw, configuration, setup_directory)
        task_file = configuration.parent / "login-task.xml"
        task_file.write_bytes(content)
        result = subprocess.run(["schtasks", "/Create", "/TN", name, "/XML", str(task_file), "/F"],
                                capture_output=True, check=False)
        if result.returncode:
            raise AppError("SCHEDULED_TASK_CREATE_FAILED", "Could not create the current user's logon startup task.")
        return True
    existing = query_logon_task(name)
    if not existing["exists"]:
        return False
    if not existing.get("owned_logon"):
        raise AppError("SCHEDULED_TASK_OWNERSHIP_UNCLEAR",
                       "An existing task with the Clipper Plus task name is not a recognized Clipper Plus logon task; Setup left it unchanged.")
    result = subprocess.run(["schtasks", "/Delete", "/TN", name, "/F"], capture_output=True, check=False)
    if result.returncode:
        raise AppError("SCHEDULED_TASK_REMOVE_FAILED", "Could not remove the current user's Clipper Plus logon task.")
    return False
