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
    decrypt_blob,
    decrypt_record,
    encrypt_blob,
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


def test_build_aad_with_extra_id():
    # voucher 系 / bcb は追加 id を 1 個取り、AAD 末尾に NUL+uint64be(id) が付く。
    aad = build_aad("vmeta", 1, 7)
    assert aad == (b"vmeta\x00" + uint64_be(1) + b"\x00" + uint64_be(7))
    assert build_aad("bcb", 42, 202601) == (
        b"bcb\x00" + uint64_be(42) + b"\x00" + uint64_be(202601)
    )


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


def test_uint64_be_type_error():
    with pytest.raises(TypeError):
        uint64_be("42")


def test_decrypt_requires_32b_key():
    with pytest.raises(ValueError):
        decrypt_record(b"short", b"x", b"y", build_aad("je", 1))


# --- record ビルダー (e2ee_data_migration の純粋関数) ---

from datetime import date  # noqa: E402
from decimal import Decimal  # noqa: E402

from app.services.e2ee_data_migration import (  # noqa: E402
    je_record,
    jel_record,
    me_record,
    vmeta_record,
)


def test_je_record_shape_and_nulls():
    assert je_record(date(2026, 2, 15), "摘要", "cashbook", 5) == {
        "v": 1, "date": "2026-02-15", "description": "摘要",
        "source": "cashbook", "fiscal_period": 5,
    }
    # NULL/欠落のデフォルト: date None / description None / source None / fp None
    assert je_record(None, None, None, None) == {
        "v": 1, "date": None, "description": "", "source": "journal",
        "fiscal_period": None,
    }


def test_jel_record_numeric_coercion():
    # Numeric(Decimal) → int、account_code None → ""、None 金額 → 0
    assert jel_record("5010", Decimal("100"), Decimal("0"), "メモ") == {
        "v": 1, "account_code": "5010", "debit_amount": 100,
        "credit_amount": 0, "description": "メモ",
    }
    assert jel_record(None, None, None, None) == {
        "v": 1, "account_code": "", "debit_amount": 0,
        "credit_amount": 0, "description": "",
    }


def test_me_record_provider_type_and_nulls():
    assert me_record(date(2026, 3, 20), "山田", "○○病院", "歯科",
                     "hospital", Decimal("12000"), Decimal("4000")) == {
        "v": 1, "date": "2026-03-20", "patient_name": "山田",
        "hospital_name": "○○病院", "treatment_description": "歯科",
        "provider_type": "hospital", "amount_paid": 12000,
        "insurance_reimbursement": 4000,
    }
    # provider_type の空文字は None へ正規化 (DB nullable)
    me = me_record(None, "", "", "", "", None, 0)
    assert me["provider_type"] is None
    assert me["date"] is None and me["amount_paid"] == 0


def test_record_builder_roundtrip_via_crypto():
    # 実運用と同じ経路: 平文行 → record → encrypt → decrypt が一致する。
    mk = bytes(range(32))
    rec = je_record(date(2026, 1, 2), "テスト", "journal", None)
    aad = build_aad("je", 99)
    blob, iv = encrypt_record(mk, rec, aad)
    assert decrypt_record(mk, blob, iv, aad) == rec


# --- 証憑メタ record / 画像 blob (E7 続き) ---

def test_vmeta_record_shape():
    assert vmeta_record("領収書.jpg", "image/jpeg") == {
        "v": 1, "original_filename": "領収書.jpg", "image_mime": "image/jpeg",
    }
    # NULL は空文字 / octet-stream デフォルトへ
    assert vmeta_record(None, None) == {
        "v": 1, "original_filename": "", "image_mime": "application/octet-stream",
    }


def test_encrypt_blob_roundtrip_and_format():
    # 画像 blob は iv(12B) || ciphertext || tag(16B)。AAD は vimg + user_id + aad_id。
    mk = bytes(range(32))
    aad = build_aad("vimg", 2, 1234567890123)
    data = b"\xff\xd8\xff" + bytes(range(256)) * 4  # 擬似画像
    blob = encrypt_blob(mk, data, aad)
    assert len(blob) == 12 + len(data) + 16  # iv + ct + tag
    assert decrypt_blob(mk, blob, aad) == data


def test_decrypt_blob_wrong_aad_fails():
    mk = bytes(range(32))
    blob = encrypt_blob(mk, b"img", build_aad("vimg", 2, 1))
    with pytest.raises(Exception):
        decrypt_blob(mk, blob, build_aad("vthumb", 2, 1))  # tableType 不一致
    with pytest.raises(Exception):
        decrypt_blob(mk, blob, build_aad("vimg", 2, 2))  # aad_id 不一致


def test_encrypt_blob_requires_32b_key():
    with pytest.raises(ValueError):
        encrypt_blob(b"short", b"x", build_aad("vimg", 1, 1))


def test_decrypt_blob_requires_32b_key():
    with pytest.raises(ValueError):
        decrypt_blob(b"short", b"x" * 30, build_aad("vimg", 1, 1))
