"""P-256 / ES256 签名，给煤炉接口的 DPoP 头用。

煤炉要求每个请求带一个用临时密钥签名的 JWT。密钥每次启动随机生成、用完即扔，
这里只需要"签得出服务器认的签名"，所以用纯 Python 写，省掉一个加密库依赖。
不要拿它去保护任何真正的秘密。
"""

from __future__ import annotations

import base64
import hashlib
import json
import secrets

P = 0xFFFFFFFF00000001000000000000000000000000FFFFFFFFFFFFFFFFFFFFFFFF
A = P - 3
N = 0xFFFFFFFF00000000FFFFFFFFFFFFFFFFBCE6FAADA7179E84F3B9CAC2FC632551
G = (
    0x6B17D1F2E12C4247F8BCE6E563A440F277037D812DEB33A0F4A13945D898C296,
    0x4FE342E2FE1A7F9B8EE7EB4A7C0F9E162BCE33576B315ECECBB6406837BF51F5,
)


def _add(p1, p2):
    if p1 is None:
        return p2
    if p2 is None:
        return p1
    x1, y1 = p1
    x2, y2 = p2
    if x1 == x2:
        if (y1 + y2) % P == 0:
            return None
        lam = (3 * x1 * x1 + A) * pow(2 * y1, -1, P) % P
    else:
        lam = (y2 - y1) * pow(x2 - x1, -1, P) % P
    x3 = (lam * lam - x1 - x2) % P
    return x3, (lam * (x1 - x3) - y1) % P


def _mul(k: int, point):
    result = None
    while k:
        if k & 1:
            result = _add(result, point)
        point = _add(point, point)
        k >>= 1
    return result


def b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


class SigningKey:
    def __init__(self, d: int | None = None):
        self.d = d if d is not None else secrets.randbelow(N - 1) + 1
        self.public = _mul(self.d, G)

    def jwk(self) -> dict:
        x, y = self.public
        return {"crv": "P-256", "kty": "EC",
                "x": b64url(x.to_bytes(32, "big")), "y": b64url(y.to_bytes(32, "big"))}

    def sign(self, message: bytes) -> bytes:
        """返回 JWS 要的 r||s（各 32 字节）。"""
        e = int.from_bytes(hashlib.sha256(message).digest(), "big")
        while True:
            k = secrets.randbelow(N - 1) + 1
            r = _mul(k, G)[0] % N
            if r == 0:
                continue
            s = pow(k, -1, N) * (e + r * self.d) % N
            if s == 0:
                continue
            return r.to_bytes(32, "big") + s.to_bytes(32, "big")


def jws(payload: dict, header: dict, key: SigningKey) -> str:
    def enc(obj) -> str:
        return b64url(json.dumps(obj, separators=(",", ":"), ensure_ascii=False).encode("utf-8"))

    signing_input = f"{enc(header)}.{enc(payload)}"
    return f"{signing_input}.{b64url(key.sign(signing_input.encode('ascii')))}"
