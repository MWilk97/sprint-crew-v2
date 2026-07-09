"""Validators — optional utility module (not part of notification stories)."""


def validate_email(address: str) -> bool:
    if not address or "@" not in address:
        return False
    local, _, domain = address.partition("@")
    return bool(local and domain and "." in domain)
