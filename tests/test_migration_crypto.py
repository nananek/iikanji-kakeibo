"""E7 サーバ側暗号 (migration_crypto) の互換性テスト (#114)。

golden vector は client-py/tests/test_crypto.py と共有 (元は Web JS が生成)。
サーバ実装が JS / client-py とバイト一致することを担保する。
"""
import base64
import json

import pytest
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from app.services.migration_crypto import (
    build_aad,
    decrypt_record,
    encrypt_record,
    uint64_be,
)

GOLDEN_MK = bytes.fromhex(
    "030a11181f262d343b424950575e656c737a81888f969da4abb2b9c0c7ced5dc"
)
GOLDEN_USER_ID = 42
GOLDEN_AAD_JE_HEX = "6a6500000000000000002a"
GOLDEN_AAD_ME_HEX = "6d6500000000000000002a"
GOLDEN_REC_IV = base64.b64decode("yMnKy8zNzs/Q0dLT")
GOLDEN_REC_BLOB = base64.b64decode(
    "VfR2bWXZVhf6yQgyJTXo8aLBBbmQ8pfXlOtEW/EmJQp7Jy1V4ThM88rjKvHlgznLkWCEdEss"
    "LF26z2QvLneBHiyLff3BIdwJvg9s+dnXMVlZXsobo43qewv66TYDpo5chSpnjTd5FmD9XVTztU2K"
)
GOLDEN_RECORD = {
    "v": 1,
    "date": "2026-02-15",
    "description": "テスト摘要",
    "source": "api",
    "fiscal_period": None,
}


def test_uint64_be():
    assert uint64_be(42) == bytes.fromhex("000000000000002a")
    with pytest.raises(ValueError):
        uint64_be(-1)


def test_build_aad_je_me_golden():
    # JS/client-py と同一の AAD バイト列 (Option B: user_id のみ)。
    assert build_aad("je", GOLDEN_USER_ID).hex() == GOLDEN_AAD_JE_HEX
    assert build_aad("me", GOLDEN_USER_ID).hex() == GOLDEN_AAD_ME_HEX
    assert build_aad("jel", GOLDEN_USER_ID).hex() == "6a656c00000000000000002a"


def test_build_aad_id_count_validation():
    with pytest.raises(ValueError):
        build_aad("je", 1, 999)  # je は追加 id を取らない
    with pytest.raises(ValueError):
        build_aad("vmeta", 1)  # voucher 系は id 1 個必須
    with pytest.raises(ValueError):
        build_aad("unknown", 1)


def test_encrypt_byte_identical_to_js_golden():
    # 同じ MK / IV / AAD / record で JS と完全に同じ暗号文を生成する。
    aad = build_aad("je", GOLDEN_USER_ID)
    pt = json.dumps(
        GOLDEN_RECORD, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")
    ct = AESGCM(GOLDEN_MK).encrypt(GOLDEN_REC_IV, pt, aad)
    assert ct == GOLDEN_REC_BLOB


def test_decrypt_js_golden():
    aad = build_aad("je", GOLDEN_USER_ID)
    body = decrypt_record(GOLDEN_MK, GOLDEN_REC_BLOB, GOLDEN_REC_IV, aad)
    assert body == GOLDEN_RECORD


def test_encrypt_record_roundtrip():
    mk = bytes(range(32))
    rec = {"v": 1, "account_code": "5010", "debit_amount": 100,
           "credit_amount": 0, "description": "テスト"}
    aad = build_aad("jel", 7)
    blob, iv = encrypt_record(mk, rec, aad)
    assert len(iv) == 12
    assert decrypt_record(mk, blob, iv, aad) == rec


def test_decrypt_wrong_aad_fails():
    mk = bytes(range(32))
    blob, iv = encrypt_record(mk, {"v": 1, "x": 1}, build_aad("je", 1))
    with pytest.raises(Exception):
        decrypt_record(mk, blob, iv, build_aad("je", 2))  # user_id 不一致


def test_encrypt_requires_32b_key():
    with pytest.raises(ValueError):
        encrypt_record(b"short", {"v": 1}, build_aad("je", 1))
