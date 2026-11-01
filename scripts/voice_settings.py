"""Local Doubao voice profile and optional protected credential storage."""
from __future__ import annotations

import argparse
import getpass
import json
import os
from pathlib import Path
import sys
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parent))
from studio_runtime import ROOT, data_dir

RESOURCES = {"seed-tts-2.0", "seed-icl-2.0"}


class VoiceConfigError(ValueError):
    pass


def outside_source(path: Path) -> Path:
    path = path.expanduser().resolve()
    if path.is_relative_to(ROOT):
        raise VoiceConfigError("Voice configuration must stay outside the skill source")
    return path


def profile_path() -> Path:
    return outside_source(data_dir() / "voice.json")


def secret_path() -> Path:
    return outside_source(data_dir() / ("doubao-key.dpapi" if os.name == "nt" else "doubao-key.secret"))


def validate_profile(profile: dict) -> dict:
    allowed = {"speaker", "resource_id", "model", "speech_rate", "loudness_rate"}
    if not isinstance(profile, dict) or set(profile) - allowed:
        raise VoiceConfigError("Voice profile contains unsupported fields")
    speaker = profile.get("speaker")
    if not isinstance(speaker, str) or not speaker.strip() or any(c.isspace() for c in speaker):
        raise VoiceConfigError("A nonempty speaker ID without whitespace is required")
    if profile.get("resource_id") not in RESOURCES:
        raise VoiceConfigError("Select seed-tts-2.0 or seed-icl-2.0 for this V3 adapter")
    model = profile.get("model")
    if model is not None and (not isinstance(model, str) or not model.strip() or any(c.isspace() for c in model)):
        raise VoiceConfigError("Model must be a nonempty model ID")
    if model and profile["resource_id"] != "seed-icl-2.0":
        raise VoiceConfigError("The explicit model parameter is reserved for cloned voices")
    for name in ("speech_rate", "loudness_rate"):
        value = profile.get(name, 0)
        if type(value) is not int or not -50 <= value <= 100:
            raise VoiceConfigError("Speech and loudness rates must be integers in -50..100")
    return {"speaker": speaker, "resource_id": profile["resource_id"], "model": model,
            "speech_rate": profile.get("speech_rate", 0), "loudness_rate": profile.get("loudness_rate", 0)}


def load_profile() -> dict:
    path = profile_path()
    if not path.is_file():
        raise VoiceConfigError("Configure the speaker ID and model resource before synthesis")
    return validate_profile(json.loads(path.read_text(encoding="utf-8-sig")))


def private_write(path: Path, payload: bytes) -> None:
    path = outside_source(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "wb") as output:
            output.write(payload)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def windows_protect(payload: bytes, decrypt: bool = False) -> bytes:
    """DPAPI binds a saved key to the current Windows user; no third-party library."""
    import ctypes
    from ctypes import wintypes
    class Blob(ctypes.Structure):
        _fields_ = [("size", wintypes.DWORD), ("data", ctypes.POINTER(ctypes.c_ubyte))]
    buffer = (ctypes.c_ubyte * len(payload)).from_buffer_copy(payload)
    source = Blob(len(payload), buffer)
    result = Blob()
    crypt = ctypes.WinDLL("crypt32", use_last_error=True)
    method = crypt.CryptUnprotectData if decrypt else crypt.CryptProtectData
    method.argtypes = [ctypes.POINTER(Blob), ctypes.c_void_p, ctypes.POINTER(Blob), ctypes.c_void_p,
                       ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(Blob)]
    method.restype = wintypes.BOOL
    if not method(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(result)):
        raise VoiceConfigError("Windows could not protect or unlock the local speech credential")
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    kernel.LocalFree.restype = ctypes.c_void_p
    try:
        return ctypes.string_at(result.data, result.size)
    finally:
        kernel.LocalFree(result.data)


def _keychain(operation: str, value: str | None = None):
    """Access macOS Keychain in-process; secrets never become command arguments."""
    import ctypes as c
    security = c.CDLL("/System/Library/Frameworks/Security.framework/Security")
    foundation = c.CDLL("/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")
    pointer = c.c_void_p
    foundation.CFStringCreateWithCString.argtypes = [pointer, c.c_char_p, c.c_uint32]
    foundation.CFStringCreateWithCString.restype = pointer
    foundation.CFDataCreate.argtypes = [pointer, pointer, c.c_long]
    foundation.CFDataCreate.restype = pointer
    foundation.CFDictionaryCreateMutable.argtypes = [pointer, c.c_long, pointer, pointer]
    foundation.CFDictionaryCreateMutable.restype = pointer
    foundation.CFDictionarySetValue.argtypes = [pointer, pointer, pointer]
    foundation.CFDictionarySetValue.restype = None
    foundation.CFDataGetLength.argtypes = [pointer]
    foundation.CFDataGetLength.restype = c.c_long
    foundation.CFDataGetBytePtr.argtypes = [pointer]
    foundation.CFDataGetBytePtr.restype = pointer
    foundation.CFRelease.argtypes = [pointer]
    foundation.CFRelease.restype = None
    security.SecItemCopyMatching.argtypes = [pointer, c.POINTER(pointer)]
    security.SecItemCopyMatching.restype = c.c_int32
    security.SecItemAdd.argtypes = [pointer, c.POINTER(pointer)]
    security.SecItemAdd.restype = c.c_int32
    security.SecItemUpdate.argtypes = [pointer, pointer]
    security.SecItemUpdate.restype = c.c_int32
    owned = []
    dictionaries = []
    result = pointer()

    def constant(name):
        return pointer.in_dll(security, name).value

    def allocate(reference):
        if not reference:
            raise VoiceConfigError("macOS Keychain could not allocate its request")
        owned.append(reference)
        return reference

    def dictionary():
        reference = allocate(foundation.CFDictionaryCreateMutable(None, 0, None, None))
        dictionaries.append(reference)
        return reference

    def put(target, name, item):
        foundation.CFDictionarySetValue(target, constant(name), item)

    try:
        query = dictionary()
        put(query, "kSecClass", constant("kSecClassGenericPassword"))
        for name, text in (("kSecAttrService", b"story-studio.doubao-tts"),
                           ("kSecAttrAccount", b"api-key")):
            put(query, name, allocate(foundation.CFStringCreateWithCString(None, text, 0x08000100)))
        if operation == "write":
            payload = value.encode("utf-8")
            buffer = c.create_string_buffer(payload)
            data = allocate(foundation.CFDataCreate(None, buffer, len(payload)))
            attributes = dictionary()
            put(attributes, "kSecValueData", data)
            status = security.SecItemUpdate(query, attributes)
            if status == -25300:  # errSecItemNotFound
                put(query, "kSecValueData", data)
                status = security.SecItemAdd(query, None)
            if status:
                raise VoiceConfigError("macOS Keychain could not save the speech credential")
            return None
        if operation not in {"read", "present"}:
            raise VoiceConfigError("Unsupported macOS Keychain operation")
        truth = pointer.in_dll(foundation, "kCFBooleanTrue").value
        put(query, "kSecReturnData" if operation == "read" else "kSecReturnAttributes", truth)
        put(query, "kSecMatchLimit", constant("kSecMatchLimitOne"))
        status = security.SecItemCopyMatching(query, c.byref(result))
        if status == -25300:
            return None if operation == "read" else False
        if status or not result.value:
            raise VoiceConfigError("Unlock or authorize macOS Keychain before using the speech credential")
        if operation == "present":
            return True
        size = foundation.CFDataGetLength(result)
        if not 0 < size <= 65536:
            raise VoiceConfigError("macOS Keychain returned an invalid speech credential")
        return c.string_at(foundation.CFDataGetBytePtr(result), size).decode("utf-8").strip()
    finally:
        if result.value:
            foundation.CFRelease(result)
        # Dictionaries use no retain callbacks; release them before their values.
        for reference in reversed(dictionaries):
            foundation.CFRelease(reference)
        for reference in reversed(owned):
            if reference not in dictionaries:
                foundation.CFRelease(reference)


def _keychain_write(value: str) -> None:
    _keychain("write", value)


def save_api_key(value: str, *, allow_plaintext: bool = False) -> None:
    value = value.strip()
    if not value or len(value) > 65536 or any(c.isspace() for c in value):
        raise VoiceConfigError("The speech API key must be a nonempty single token")
    if sys.platform == "darwin":
        _keychain_write(value)
        return
    if os.name != "nt" and not allow_plaintext:
        raise VoiceConfigError("Linux file storage is plaintext; explicitly select --allow-plaintext-key-file or use the host secret environment")
    payload = value.encode("utf-8")
    private_write(secret_path(), windows_protect(payload) if os.name == "nt" else payload)


def api_key_present() -> bool:
    if os.environ.get("DOUBAO_TTS_API_KEY", "").strip():
        return True
    return bool(_keychain("present")) if sys.platform == "darwin" else secret_path().is_file()


def read_api_key() -> str:
    value = os.environ.get("DOUBAO_TTS_API_KEY", "").strip()
    if value:
        return value
    if sys.platform == "darwin":
        value = _keychain("read")
        if not value:
            raise VoiceConfigError("Configure the speech credential in macOS Keychain or the host secret environment; legacy plaintext files are not read automatically")
        return value
    path = secret_path()
    if not path.is_file():
        raise VoiceConfigError("Configure DOUBAO_TTS_API_KEY or a local speech credential")
    payload = path.read_bytes()
    return (windows_protect(payload, decrypt=True) if os.name == "nt" else payload).decode("utf-8").strip()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("check", "configure"))
    parser.add_argument("--speaker")
    parser.add_argument("--resource-id", choices=sorted(RESOURCES))
    parser.add_argument("--model")
    parser.add_argument("--speech-rate", type=int, default=0)
    parser.add_argument("--loudness-rate", type=int, default=0)
    inputs = parser.add_mutually_exclusive_group()
    inputs.add_argument("--key-stdin", action="store_true", help="Save a key received through private stdin; never use command arguments")
    inputs.add_argument("--prompt-key", action="store_true", help="Hidden local input, when the host provides an interactive terminal")
    parser.add_argument("--allow-plaintext-key-file", action="store_true", help="Explicitly allow a Linux plaintext file protected by mode 0600")
    args = parser.parse_args()
    try:
        if args.action == "configure":
            profile = validate_profile({"speaker": args.speaker, "resource_id": args.resource_id,
                                        "model": args.model, "speech_rate": args.speech_rate,
                                        "loudness_rate": args.loudness_rate})
            if args.key_stdin or args.prompt_key:
                key = sys.stdin.readline(65538) if args.key_stdin else getpass.getpass("Doubao speech API key: ")
                save_api_key(key, allow_plaintext=args.allow_plaintext_key_file)
            private_write(profile_path(), (json.dumps(profile, ensure_ascii=False, indent=2) + "\n").encode())
        try:
            profile = load_profile()
            profile_ready = True
        except ValueError:
            profile, profile_ready = None, False
        report = {"profile_ready": profile_ready, "api_key_present": api_key_present(),
                  "voice": profile, "network_verified": False, "billable_request_sent": False}
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0 if profile_ready and report["api_key_present"] else 1
    except (OSError, ValueError):
        print(json.dumps({"ready": False, "error": "Voice configuration failed; check the selected profile and local credential access"}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
