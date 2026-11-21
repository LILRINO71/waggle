"""Windows Credential Manager through ctypes (CredReadW / CredWriteW / CredDeleteW), no extra packages.

Secrets live only in the user's Credential Manager ("generic" credentials, persisted for the local machine).
Nothing here logs, prints or puts a secret into an exception message. Off Windows, read() returns None and
write() raises OSError."""
import ctypes
import os

CRED_TYPE_GENERIC = 1
CRED_PERSIST_LOCAL_MACHINE = 2
ERROR_NOT_FOUND = 1168


class _FILETIME(ctypes.Structure):
    _fields_ = [("dwLowDateTime", ctypes.c_uint32), ("dwHighDateTime", ctypes.c_uint32)]


class _CREDENTIAL(ctypes.Structure):
    _fields_ = [("Flags", ctypes.c_uint32), ("Type", ctypes.c_uint32), ("TargetName", ctypes.c_wchar_p),
                ("Comment", ctypes.c_wchar_p), ("LastWritten", _FILETIME), ("CredentialBlobSize", ctypes.c_uint32),
                ("CredentialBlob", ctypes.POINTER(ctypes.c_ubyte)), ("Persist", ctypes.c_uint32),
                ("AttributeCount", ctypes.c_uint32), ("Attributes", ctypes.c_void_p),
                ("TargetAlias", ctypes.c_wchar_p), ("UserName", ctypes.c_wchar_p)]


def _advapi():
    if os.name != "nt":
        raise OSError("Windows Credential Manager is only available on Windows")
    api = ctypes.WinDLL("advapi32", use_last_error=True)
    api.CredReadW.argtypes = [ctypes.c_wchar_p, ctypes.c_uint32, ctypes.c_uint32,
                              ctypes.POINTER(ctypes.POINTER(_CREDENTIAL))]
    api.CredReadW.restype = ctypes.c_int
    api.CredWriteW.argtypes = [ctypes.POINTER(_CREDENTIAL), ctypes.c_uint32]
    api.CredWriteW.restype = ctypes.c_int
    api.CredDeleteW.argtypes = [ctypes.c_wchar_p, ctypes.c_uint32, ctypes.c_uint32]
    api.CredDeleteW.restype = ctypes.c_int
    api.CredFree.argtypes = [ctypes.c_void_p]
    api.CredFree.restype = None
    return api


def read(target):
    """(user name, secret) stored under `target`, or None when there is none (or not on Windows)."""
    try:
        api = _advapi()
    except OSError:
        return None
    ptr = ctypes.POINTER(_CREDENTIAL)()
    if not api.CredReadW(target, CRED_TYPE_GENERIC, 0, ctypes.byref(ptr)):
        return None
    try:
        cred = ptr.contents
        blob = ctypes.string_at(cred.CredentialBlob, cred.CredentialBlobSize) if cred.CredentialBlobSize else b""
        return (cred.UserName or "", blob.decode("utf-16-le"))
    finally:
        api.CredFree(ptr)


def write(target, user, secret):
    """Store (user, secret) under `target`, replacing what was there. Raises OSError (without the secret)."""
    api = _advapi()
    data = str(secret).encode("utf-16-le")
    buf = (ctypes.c_ubyte * len(data)).from_buffer_copy(data) if data else None
    cred = _CREDENTIAL(Flags=0, Type=CRED_TYPE_GENERIC, TargetName=target, Comment="Waggle",
                       CredentialBlobSize=len(data),
                       CredentialBlob=ctypes.cast(buf, ctypes.POINTER(ctypes.c_ubyte)) if buf else None,
                       Persist=CRED_PERSIST_LOCAL_MACHINE, AttributeCount=0, Attributes=None, TargetAlias=None,
                       UserName=str(user))
    if not api.CredWriteW(ctypes.byref(cred), 0):
        raise OSError(f"CredWriteW failed (Windows error {ctypes.get_last_error()})")
    return True


def delete(target):
    """Remove the credential; True when one was removed."""
    try:
        api = _advapi()
    except OSError:
        return False
    return bool(api.CredDeleteW(target, CRED_TYPE_GENERIC, 0))
