"""Хэширование паролей локальных и тестовых учёток (argon2id, библиотека argon2-cffi, MIT).

Пароли внешних пользователей у нас не хранятся вообще — их проверяет внешний сервис.
"""

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

_hasher = PasswordHasher()  # параметры по умолчанию библиотеки (RFC 9106, low-memory profile)

# Хэш «ни от чего»: сверяем с ним, когда учётки нет, чтобы время ответа не выдавало,
# существует ли локальный пользователь с таким логином.
_DUMMY_HASH = _hasher.hash("dummy-password-for-constant-time")

MIN_PASSWORD_LENGTH = 10


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(password_hash: str | None, password: str) -> bool:
    try:
        return _hasher.verify(password_hash or _DUMMY_HASH, password) and password_hash is not None
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


def validate_new_password(password: str) -> None:
    if len(password) < MIN_PASSWORD_LENGTH:
        raise ValueError(f"Пароль должен быть не короче {MIN_PASSWORD_LENGTH} символов")
