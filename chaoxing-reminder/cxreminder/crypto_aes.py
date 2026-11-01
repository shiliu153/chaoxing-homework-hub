"""学习通登录口令加密（AES-128-CBC / PKCS7 / key 与 IV 相同）。

学习通网页端在提交登录表单前会用固定密钥对明文口令做一次 AES 加密，
再 base64 后作为 password 字段提交。这里复刻该逻辑。
"""
from __future__ import annotations

import base64

_AES_KEY = b"u2oh6Vu^HWe4_AES"


def _load_aes():
    try:
        from Crypto.Cipher import AES  # pycryptodome
        from Crypto.Util.Padding import pad
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError(
            "缺少依赖 pycryptodome，请先执行：pip install pycryptodome"
        ) from exc
    return AES, pad


def encrypt_password(password: str) -> str:
    """把明文口令转换为学习通表单所需的密文。"""
    AES, pad = _load_aes()
    cipher = AES.new(_AES_KEY, AES.MODE_CBC, _AES_KEY)
    raw = cipher.encrypt(pad(password.encode("utf-8"), AES.block_size))
    return base64.b64encode(raw).decode("ascii")
