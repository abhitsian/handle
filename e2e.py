#!/usr/bin/env python3
"""End-to-end encryption for the Handle relay.

The relay (a Cloudflare Worker) carries tab lists, page text and screenshots
between this Mac and a linked browser. It must never be able to read them, so
each message is sealed here and opened in the extension (and the reverse) with
a key that only the two ends hold: it is in the pairing code, never sent to
the Worker.

Construction: AES-256-CTR, then HMAC-SHA256 over iv||ciphertext
(encrypt-then-MAC). Both are native on each side: WebCrypto in the extension;
here, stdlib hmac plus the `openssl` binary for AES, since this Python has no
AES and pip is broken on it. Separate encryption and MAC keys are derived from
the shared key with HMAC.

The IV's low 8 bytes are zero: WebCrypto's AES-CTR (length 64) and OpenSSL's
128-bit counter only agree while the low 64 bits don't wrap, and starting
them at zero guarantees that for any message this size.

Wire format: base64url(iv[16] || ciphertext || tag[32]).
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import os
import subprocess

OPENSSL = next((p for p in ("/opt/homebrew/bin/openssl", "/usr/bin/openssl")
                if os.path.exists(p)), "openssl")


class BadMessage(Exception):
    pass


def b64u(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).decode().rstrip("=")


def unb64u(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def _keys(key: bytes) -> tuple[bytes, bytes]:
    return (hmac.new(key, b"handle-enc", hashlib.sha256).digest(),
            hmac.new(key, b"handle-mac", hashlib.sha256).digest())


def _ctr(enc_key: bytes, iv: bytes, data: bytes) -> bytes:
    r = subprocess.run([OPENSSL, "enc", "-aes-256-ctr", "-nosalt",
                        "-K", enc_key.hex(), "-iv", iv.hex()],
                       input=data, capture_output=True, timeout=30)
    if r.returncode != 0:
        raise RuntimeError("openssl: " + r.stderr.decode(errors="replace")[:200])
    return r.stdout


def seal(key: bytes, plaintext: bytes) -> str:
    ek, mk = _keys(key)
    iv = os.urandom(8) + b"\x00" * 8
    ct = _ctr(ek, iv, plaintext)
    tag = hmac.new(mk, iv + ct, hashlib.sha256).digest()
    return b64u(iv + ct + tag)


def open_(key: bytes, sealed: str) -> bytes:
    ek, mk = _keys(key)
    try:
        raw = unb64u(sealed)
    except Exception as exc:
        raise BadMessage("not base64") from exc
    if len(raw) < 48:
        raise BadMessage("too short")
    iv, ct, tag = raw[:16], raw[16:-32], raw[-32:]
    if not hmac.compare_digest(tag, hmac.new(mk, iv + ct, hashlib.sha256).digest()):
        raise BadMessage("bad tag")
    return _ctr(ek, iv, ct)
