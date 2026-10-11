"""Teradata password storage in the OS credential store via `keyring`
(Windows Credential Manager, macOS Keychain, Linux Secret Service).

Passwords are never written to config.toml. Every function degrades to
"not available" instead of raising, so a machine without a keyring backend
still works - the password is then kept only for the running session.
"""

from __future__ import annotations

SERVICE = "tdagent-teradata"


def _account(host: str, user: str) -> str:
    return f"{user}@{host}"


def _keyring():
    try:
        import keyring
    except ImportError:
        return None
    return keyring


def get_password(host: str, user: str) -> str | None:
    kr = _keyring()
    if kr is None or not host or not user:
        return None
    try:
        return kr.get_password(SERVICE, _account(host, user))
    except Exception:
        return None


def set_password(host: str, user: str, password: str) -> tuple[bool, str]:
    kr = _keyring()
    if kr is None:
        return False, "keyring 패키지가 없습니다 (pip install keyring)."
    try:
        kr.set_password(SERVICE, _account(host, user), password)
        return True, ""
    except Exception as e:
        if type(e).__name__ == "NoKeyringError":
            return False, "이 PC 에서 OS 자격 증명 저장소를 찾을 수 없습니다"
        return False, str(e).splitlines()[0][:200]


def delete_password(host: str, user: str) -> None:
    kr = _keyring()
    if kr is None or not host or not user:
        return
    try:
        kr.delete_password(SERVICE, _account(host, user))
    except Exception:
        pass  # nothing stored, or no backend
