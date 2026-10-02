"""
安全工具模块：JWT、密码哈希、API Key 加密
"""

import hashlib
import hmac
import json
import time
import base64
import secrets
from typing import Optional
from app.core.config import settings
from app.core.logging import get_logger

log = get_logger("security")


def hash_password(password: str) -> str:
    """密码哈希（SHA-256 + salt）"""
    salt = secrets.token_hex(16)
    hashed = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 100000)
    log.debug("密码哈希生成完成")
    return f"{salt}:{hashed.hex()}"


def verify_password(password: str, hashed: str) -> bool:
    """验证密码"""
    try:
        salt, hash_hex = hashed.split(":")
        expected = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 100000)
        result = hmac.compare_digest(expected.hex(), hash_hex)
        log.debug(f"密码验证 | result={'通过' if result else '失败'}")
        return result
    except Exception as e:
        log.error(f"密码验证异常 | error={str(e)}")
        return False


def create_jwt_token(payload: dict, expire_hours: Optional[int] = None) -> str:
    """创建简单 JWT token"""
    if expire_hours is None:
        expire_hours = settings.security.jwt_expire_hours
    payload = dict(payload)  # 不原地修改调用方的 dict
    payload["exp"] = int(time.time()) + expire_hours * 3600
    payload["iat"] = int(time.time())

    header = base64.urlsafe_b64encode(json.dumps({"alg": "HS256", "typ": "JWT"}).encode()).decode().rstrip("=")
    body = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")
    signature_input = f"{header}.{body}"
    sig = hmac.new(settings.security.jwt_secret.encode(), signature_input.encode(), hashlib.sha256).digest()
    signature = base64.urlsafe_b64encode(sig).decode().rstrip("=")

    log.debug(f"JWT Token 生成 | user_id={payload.get('user_id')} | expire_hours={expire_hours}")
    return f"{header}.{body}.{signature}"


def decode_jwt_token(token: str) -> Optional[dict]:
    """解码并验证 JWT token"""
    try:
        parts = token.split(".")
        if len(parts) != 3:
            log.debug("JWT 解码失败: token 格式错误")
            return None

        header_b, body_b, sig_b = parts
        signature_input = f"{header_b}.{body_b}"
        expected_sig = hmac.new(
            settings.security.jwt_secret.encode(),
            signature_input.encode(),
            hashlib.sha256
        ).digest()
        expected = base64.urlsafe_b64encode(expected_sig).decode().rstrip("=")

        if not hmac.compare_digest(expected, sig_b):
            log.debug("JWT 解码失败: 签名不匹配")
            return None

        # 补齐 base64 padding
        padding = 4 - len(body_b) % 4
        if padding != 4:
            body_b += "=" * padding

        payload = json.loads(base64.urlsafe_b64decode(body_b))

        if payload.get("exp", 0) < time.time():
            log.debug(f"JWT 解码失败: token 已过期 | user_id={payload.get('user_id')}")
            return None

        log.debug(f"JWT 解码成功 | user_id={payload.get('user_id')}")
        return payload
    except Exception as e:
        log.debug(f"JWT 解码异常 | error={str(e)}")
        return None


def generate_api_key() -> str:
    """生成随机 API Key"""
    key = f"pfk_{secrets.token_urlsafe(32)}"
    log.debug(f"API Key 生成 | key_prefix={key[:15]}...")
    return key


def encrypt_value(value: str) -> str:
    """简单加密（用于数据源密码等敏感信息存储）"""
    key = settings.security.encryption_key.encode()[:32].ljust(32, b'\0')
    # v2.20: 密钥流长度按「字节数」而不是「字符数」算。原来含中文等多字节字符的密码
    # 字节数 > 字符数，zip 按短的截断，密文被截掉一截，保存后再也解不出正确密码。
    # 纯 ASCII 密码两者相等，加密结果与原来完全一致，已存数据不受影响。
    raw = value.encode()
    encrypted = bytes(a ^ b for a, b in zip(raw, (key * ((len(raw) // 32) + 1))[:len(raw)]))
    log.debug("敏感值加密完成")
    return base64.urlsafe_b64encode(encrypted).decode()


def decrypt_value(encrypted: str) -> str:
    """解密"""
    key = settings.security.encryption_key.encode()[:32].ljust(32, b'\0')
    data = base64.urlsafe_b64decode(encrypted)
    decrypted = bytes(a ^ b for a, b in zip(data, (key * ((len(data) // 32) + 1))[:len(data)]))
    log.debug("敏感值解密完成")
    return decrypted.decode()
