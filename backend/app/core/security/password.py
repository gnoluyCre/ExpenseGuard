"""密码哈希。

用 argon2-cffi 而非 passlib:passlib 顶层 `import crypt`，而 `crypt`
在 Python 3.13 已被 PEP 594 移除，会直接 ImportError；且 passlib 已停止维护。

**不自己调 memory_cost / time_cost 等参数。** argon2-cffi 的默认值已经对齐
RFC 9106 的推荐档，手工调参在没有实测依据的情况下只会让安全性变差。
"""

import contextlib

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerifyMismatchError

_hasher = PasswordHasher()

#: 用于消除用户名枚举时序差的哑哈希。
#:
#: 模块导入时算一次即可 —— 它的内容无关紧要，只有「验证它要花多久」
#: 这件事有意义。
_DUMMY_HASH = _hasher.hash("dummy-password-for-timing-equalization")


def hash_password(plain: str) -> str:
    """把明文密码哈希成 `$argon2id$...` 串。"""
    return _hasher.hash(plain)


def verify_password(password_hash: str, plain: str) -> bool:
    """校验密码。失败返回 False，不抛异常。"""
    try:
        return _hasher.verify(password_hash, plain)
    except (VerifyMismatchError, InvalidHashError):
        return False


def verify_dummy() -> None:
    """对哑哈希做一次校验，只为消耗与真实校验相当的时间。

    用户不存在时必须调用它。否则「用户不存在」会比「密码错误」
    快一个数量级（前者不做 argon2 运算），攻击者据此就能枚举出
    哪些用户名是有效的 —— 而在企业内部系统里，有效用户名本身
    就是有价值的情报。
    """
    with contextlib.suppress(VerifyMismatchError, InvalidHashError):
        _hasher.verify(_DUMMY_HASH, "wrong")


def needs_rehash(password_hash: str) -> bool:
    """判断哈希是否用了过时的参数，需要在下次登录成功后重算。"""
    try:
        return _hasher.check_needs_rehash(password_hash)
    except InvalidHashError:
        return True
