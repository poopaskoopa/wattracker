"""Windows DPAPI helpers for secrets bound to the current Windows user.

The module imports safely on every platform.  Native Windows APIs are loaded
only when a protect/unprotect operation is requested, and callers may inject a
small backend object in tests without touching the host credential store.
"""
from __future__ import annotations

import base64
import ctypes
import sys
from ctypes import wintypes
from typing import Optional, Protocol


CRYPTPROTECT_UI_FORBIDDEN = 0x1
_PREFIX = "dpapi1$"
_MAX_BLOB_BYTES = 1024 * 1024


class DPAPIError(RuntimeError):
    """DPAPI is unavailable or refused/could not decode a secret."""


class _Backend(Protocol):
    def protect(self, plaintext: bytes, entropy: bytes) -> bytes: ...

    def unprotect(self, ciphertext: bytes, entropy: bytes) -> bytes: ...


class _DATA_BLOB(ctypes.Structure):
    _fields_ = [
        ("cbData", wintypes.DWORD),
        ("pbData", ctypes.POINTER(ctypes.c_ubyte)),
    ]


def _input_blob(data: bytes) -> "tuple[_DATA_BLOB, ctypes.Array]":
    # Keep the buffer alive for the duration of the native call.  Entropy and
    # passwords used here are non-empty, but allocate one byte defensively.
    buf = ctypes.create_string_buffer(data, max(1, len(data)))
    ptr = ctypes.cast(buf, ctypes.POINTER(ctypes.c_ubyte))
    return _DATA_BLOB(len(data), ptr), buf


class _NativeDPAPI:
    def __init__(self) -> None:
        if not sys.platform.startswith("win"):
            raise DPAPIError("Windows DPAPI is only available on Windows")
        try:
            self._crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
            self._kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        except (AttributeError, OSError) as exc:
            raise DPAPIError("Windows DPAPI is unavailable") from exc

        self._crypt32.CryptProtectData.argtypes = [
            ctypes.POINTER(_DATA_BLOB),
            wintypes.LPCWSTR,
            ctypes.POINTER(_DATA_BLOB),
            ctypes.c_void_p,
            ctypes.c_void_p,
            wintypes.DWORD,
            ctypes.POINTER(_DATA_BLOB),
        ]
        self._crypt32.CryptProtectData.restype = wintypes.BOOL
        self._crypt32.CryptUnprotectData.argtypes = [
            ctypes.POINTER(_DATA_BLOB),
            ctypes.c_void_p,
            ctypes.POINTER(_DATA_BLOB),
            ctypes.c_void_p,
            ctypes.c_void_p,
            wintypes.DWORD,
            ctypes.POINTER(_DATA_BLOB),
        ]
        self._crypt32.CryptUnprotectData.restype = wintypes.BOOL
        self._kernel32.LocalFree.argtypes = [ctypes.c_void_p]
        self._kernel32.LocalFree.restype = ctypes.c_void_p

    def _output(self, result: _DATA_BLOB) -> bytes:
        try:
            if not result.pbData or not result.cbData:
                raise DPAPIError("Windows DPAPI returned an empty secret")
            return ctypes.string_at(result.pbData, result.cbData)
        finally:
            if result.pbData:
                self._kernel32.LocalFree(
                    ctypes.cast(result.pbData, ctypes.c_void_p)
                )

    def protect(self, plaintext: bytes, entropy: bytes) -> bytes:
        source, source_buf = _input_blob(plaintext)
        extra, extra_buf = _input_blob(entropy)
        result = _DATA_BLOB()
        # References make the native-buffer lifetime explicit.
        _ = source_buf, extra_buf
        if not self._crypt32.CryptProtectData(
            ctypes.byref(source),
            "wattracker Zwift credential",
            ctypes.byref(extra),
            None,
            None,
            CRYPTPROTECT_UI_FORBIDDEN,
            ctypes.byref(result),
        ):
            code = ctypes.get_last_error()
            raise DPAPIError(f"Windows DPAPI protection failed (error {code})")
        return self._output(result)

    def unprotect(self, ciphertext: bytes, entropy: bytes) -> bytes:
        source, source_buf = _input_blob(ciphertext)
        extra, extra_buf = _input_blob(entropy)
        result = _DATA_BLOB()
        _ = source_buf, extra_buf
        # Passing NULL for the optional description output avoids allocating a
        # second native buffer containing metadata we do not need.
        if not self._crypt32.CryptUnprotectData(
            ctypes.byref(source),
            None,
            ctypes.byref(extra),
            None,
            None,
            CRYPTPROTECT_UI_FORBIDDEN,
            ctypes.byref(result),
        ):
            code = ctypes.get_last_error()
            raise DPAPIError(f"Windows DPAPI unprotection failed (error {code})")
        return self._output(result)


def is_available() -> bool:
    if not sys.platform.startswith("win"):
        return False
    try:
        _NativeDPAPI()
        return True
    except DPAPIError:
        return False


def entropy_for(service: str, user_id: int) -> bytes:
    """Non-secret context that prevents cross-service/user blob reuse."""
    return f"{service}\0user:{int(user_id)}".encode("utf-8")


def protect_password(
    password: str,
    service: str,
    user_id: int,
    *,
    backend: Optional[_Backend] = None,
) -> str:
    if not password:
        raise DPAPIError("cannot protect an empty password")
    api = backend or _NativeDPAPI()
    try:
        ciphertext = api.protect(
            password.encode("utf-8"), entropy_for(service, user_id)
        )
    except DPAPIError:
        raise
    except Exception as exc:
        raise DPAPIError("Windows DPAPI protection failed") from exc
    if not ciphertext or len(ciphertext) > _MAX_BLOB_BYTES:
        raise DPAPIError("Windows DPAPI returned an invalid secret")
    return _PREFIX + base64.b64encode(ciphertext).decode("ascii")


def unprotect_password(
    marker: str,
    service: str,
    user_id: int,
    *,
    backend: Optional[_Backend] = None,
) -> str:
    if not marker or not marker.startswith(_PREFIX):
        raise DPAPIError("invalid Windows credential marker")
    encoded = marker[len(_PREFIX):]
    if not encoded or len(encoded) > ((_MAX_BLOB_BYTES + 2) // 3) * 4:
        raise DPAPIError("corrupt Windows credential marker")
    try:
        ciphertext = base64.b64decode(
            encoded, validate=True
        )
    except (ValueError, TypeError) as exc:
        raise DPAPIError("corrupt Windows credential marker") from exc
    if not ciphertext or len(ciphertext) > _MAX_BLOB_BYTES:
        raise DPAPIError("corrupt Windows credential marker")
    api = backend or _NativeDPAPI()
    try:
        plaintext = api.unprotect(ciphertext, entropy_for(service, user_id))
        return plaintext.decode("utf-8")
    except DPAPIError:
        raise
    except Exception as exc:
        raise DPAPIError("Windows DPAPI could not decode the credential") from exc
