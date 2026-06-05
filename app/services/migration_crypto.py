"""E7 データ移行用のサーバ側 E2EE 暗号プリミティブ (#114)。

v4.0.0 の平文データを一斉移行する際、サーバが一時マスター鍵 (temp-MK) で各行を
暗号化する。生成する暗号文は **クライアント (Web JS / client-py) が復号できる完全
互換フォーマット**でなければならない。本モジュールは client-py の
``iikanji.crypto`` (= Web の ``app/static/js/crypto/record.js``) と同一仕様を
サーバ側に再実装したもの:

- アルゴリズム: AES-256-GCM (IV 12B ランダム / tag 16B)。MK を直接 GCM 鍵に使う
  (HKDF 派生なし)。
- 平文: ``json.dumps(record, separators=(",", ":"), ensure_ascii=False)`` を UTF-8。
- AAD (Option B): ``tableType(ascii) + b"\\x00" + uint64_be(user_id)
  [+ b"\\x00" + uint64_be(id)]*``。je/jel/me は user_id のみ (追加 id なし)。

互換性は client-py の golden-vector テスト (tests/test_crypto.py) と本モジュールの
往復テストで担保する。
"""

from __future__ import annotations

import json
import os
import struct

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

# テーブル種別ごとの追加 ID 個数 (record.js / client-py TABLE_ID_COUNT と一致)。
TABLE_ID_COUNT = {
    "je": 0,      # journal_entries (user_id のみ)
    "jel": 0,     # journal_entry_lines (user_id のみ)
    "me": 0,      # medical_expenses (user_id のみ)
    "bcb": 1,     # balance_cache_blobs (year*100+period)
    "vimg": 1,    # vouchers 画像本体 (voucher_id)
    "vthumb": 1,  # vouchers サムネイル (voucher_id)
    "vmeta": 1,   # vouchers メタ情報 (voucher_id)
    "valog": 1,   # voucher_audit_logs detail (voucher_id)
}


def uint64_be(n: int) -> bytes:
    """非負整数を 8B big-endian にエンコード (record.js uint64BE と一致)。"""
    if not isinstance(n, int):
        raise TypeError("uint64_be expects an int")
    if n < 0 or n > 0xFFFF_FFFF_FFFF_FFFF:
        raise ValueError(f"uint64_be: out of range: {n}")
    return struct.pack(">Q", n)


def build_aad(table_type: str, user_id: int, *ids: int) -> bytes:
    """AAD バイト列を構築 (record.js buildAAD / client-py build_aad と一致)。"""
    if table_type not in TABLE_ID_COUNT:
        raise ValueError(f"build_aad: unsupported tableType: {table_type}")
    expected = TABLE_ID_COUNT[table_type]
    if len(ids) != expected:
        raise ValueError(
            f"build_aad: {table_type} expects {expected} id(s), got {len(ids)}"
        )
    parts = [table_type.encode("ascii"), b"\x00", uint64_be(user_id)]
    for i in ids:
        parts.append(b"\x00")
        parts.append(uint64_be(i))
    return b"".join(parts)


def encrypt_record(mk: bytes, record: dict, aad: bytes) -> tuple[bytes, bytes]:
    """record (dict) を JSON 化 → MK で AES-GCM 暗号化。

    Returns:
        ``(blob, iv)`` — blob = ciphertext + 16B GCM tag, iv = 12B ランダム。
    """
    if len(mk) != 32:
        raise ValueError("mk must be 32 bytes")
    iv = os.urandom(12)
    plaintext = json.dumps(
        record, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")
    blob = AESGCM(mk).encrypt(iv, plaintext, aad)
    return blob, iv


def decrypt_record(mk: bytes, blob: bytes, iv: bytes, aad: bytes) -> dict:
    """blob + iv + aad を AES-GCM 復号 → JSON parse して dict を返す。

    主に検証・テスト用 (本番のサーバは復号しない = E2EE)。
    """
    if len(mk) != 32:
        raise ValueError("mk must be 32 bytes")
    plaintext = AESGCM(mk).decrypt(iv, blob, aad)
    return json.loads(plaintext.decode("utf-8"))


def encrypt_blob(mk: bytes, data: bytes, aad: bytes) -> bytes:
    """生バイト列 (画像/サムネ) を AES-GCM 暗号化し ``iv(12B) || ciphertext || tag``
    を返す (証憑画像のストレージ保存形式。voucher_upload.js _encryptBlob と一致)。

    record (JSON) と異なり IV を別カラムに持たず blob 先頭に inline する。
    """
    if len(mk) != 32:
        raise ValueError("mk must be 32 bytes")
    iv = os.urandom(12)
    return iv + AESGCM(mk).encrypt(iv, data, aad)


def decrypt_blob(mk: bytes, blob: bytes, aad: bytes) -> bytes:
    """``iv(12B) || ciphertext || tag`` を復号して生バイト列を返す (検証用)。"""
    if len(mk) != 32:
        raise ValueError("mk must be 32 bytes")
    return AESGCM(mk).decrypt(blob[:12], blob[12:], aad)
