from __future__ import annotations
import base64, hashlib, hmac, json, os
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

AAD = b"walden-memory-graph-v1"

def key32(value: str) -> bytes:
    if not value:
        raise ValueError("memory encryption key is empty")
    v=value.strip()
    try:
        b=bytes.fromhex(v)
        if len(b)==32: return b
    except ValueError: pass
    try:
        b=base64.b64decode(v, validate=True)
        if len(b)==32: return b
    except Exception: pass
    return hashlib.sha256(v.encode()).digest()

class Cipher:
    def __init__(self, value: str, enabled: bool=True):
        self.enabled=enabled
        self.key=key32(value) if enabled else b""
    def encrypt(self, raw: bytes) -> bytes:
        if not self.enabled: return raw
        nonce=os.urandom(12)
        ct=AESGCM(self.key).encrypt(nonce, raw, AAD)
        return json.dumps({"v":1,"alg":"AES-256-GCM","nonce":base64.b64encode(nonce).decode(),"ciphertext":base64.b64encode(ct).decode()}, separators=(",",":")).encode()
    def decrypt(self, raw: bytes) -> bytes:
        if not self.enabled: return raw
        env=json.loads(raw)
        return AESGCM(self.key).decrypt(base64.b64decode(env["nonce"]), base64.b64decode(env["ciphertext"]), AAD)

def pseudonym(secret: str, value: str) -> str:
    if not secret:
        raise ValueError("identity_hmac_key is empty")
    return hmac.new(secret.encode(), value.encode(), hashlib.sha256).hexdigest()
