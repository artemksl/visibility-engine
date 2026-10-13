"""Resolve an explicit credential reference without account discovery.

Imports never access credentials. Only the requested environment variable,
macOS keychain item, or Windows generic credential target is read. Errors carry
stable codes rather than store messages, account names, or credential contents.
"""
from __future__ import annotations

import asyncio
import codecs
import os
import re
import sys


class CredentialLookupError(RuntimeError):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def valid_store_reference(reference: object) -> bool:
    """Check explicit store syntax without inspecting the OS or a store."""
    if not isinstance(reference, str):
        return False
    if reference.startswith("keychain:"):
        parts = reference.split(":", 2)
        return len(parts) == 3 and all(parts[1:])
    if reference.startswith("credential-manager:"):
        target = reference.removeprefix("credential-manager:")
        return bool(target) and not any(char in target for char in "\x00\r\n")
    return False


def _decode_windows_blob(blob: object) -> str:
    if isinstance(blob, str):
        value = blob
    elif isinstance(blob, bytes):
        try:
            if blob.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)):
                value = blob.decode("utf-16")
            elif blob.startswith(codecs.BOM_UTF8):
                value = blob.decode("utf-8-sig")
            elif b"\x00" not in blob:
                value = blob.decode("utf-8")
            elif (len(blob) % 2 == 0 and blob
                  and all(byte == 0 for byte in blob[1::2])
                  and all(32 <= byte <= 126 for byte in blob[::2])):
                # Windows' UI stores ordinary ASCII tokens as UTF-16LE.
                # Alternating NUL bytes make this encoding unambiguous. Other
                # untagged UTF-16 layouts are refused rather than guessed.
                value = blob.decode("utf-16-le")
            else:
                raise CredentialLookupError("invalid")
        except UnicodeError:
            raise CredentialLookupError("invalid") from None
    else:
        raise CredentialLookupError("invalid")
    if "\x00" in value:
        raise CredentialLookupError("invalid")
    return value.strip()


def _windows_credential(target: str) -> str:
    try:
        import win32cred
        credential = win32cred.CredRead(target, win32cred.CRED_TYPE_GENERIC, 0)
    except Exception:
        # Store errors may identify an account or target. Never forward them.
        raise CredentialLookupError("unavailable") from None
    if not isinstance(credential, dict) or "CredentialBlob" not in credential:
        raise CredentialLookupError("invalid")
    return _decode_windows_blob(credential["CredentialBlob"])


async def resolve_reference(reference: str) -> str:
    """Read only the explicit reference, preserving the 15-second deadline."""
    if not isinstance(reference, str):
        raise CredentialLookupError("reference_invalid")
    if reference.startswith("env:"):
        key = reference.removeprefix("env:")
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
            raise CredentialLookupError("reference_invalid")
        return os.environ.get(key, "").strip()
    if reference.startswith("keychain:"):
        parts = reference.split(":", 2)
        if sys.platform != "darwin" or not valid_store_reference(reference):
            raise CredentialLookupError("reference_invalid")
        process = await asyncio.create_subprocess_exec(
            "/usr/bin/security", "find-generic-password", "-s", parts[1],
            "-a", parts[2], "-w", stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        try:
            output, _ = await asyncio.wait_for(process.communicate(), 15)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            process.kill()
            await process.wait()
            raise CredentialLookupError("unavailable") from None
        try:
            return output.decode("utf-8").strip() if process.returncode == 0 else ""
        except UnicodeError:
            raise CredentialLookupError("invalid") from None
    if reference.startswith("credential-manager:"):
        target = reference.removeprefix("credential-manager:")
        if sys.platform != "win32" or not valid_store_reference(reference):
            raise CredentialLookupError("reference_invalid")
        try:
            return await asyncio.wait_for(asyncio.to_thread(_windows_credential, target), 15)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            raise CredentialLookupError("unavailable") from None
    raise CredentialLookupError("reference_invalid")
