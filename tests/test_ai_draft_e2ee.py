"""E5 (#111) AI 下書き画像の E2EE 化 — モデル / スキーマのテスト。

PR-1 (059 マイグレ + AIDraft モデル E2EE 列) のカバレッジ。
後続 PR (2 段階 upload / クライアント暗号化 / 証憑移行) で拡充する。
"""

import pytest
from sqlalchemy.exc import IntegrityError

from app.extensions import db
from app.models.ai_draft import AIDraft


def _make_draft(user_id, *, aad_id=None, **kwargs):
    defaults = dict(
        user_id=user_id,
        image_key="ai/1/x.bin",
        image_mime="application/octet-stream",
        status="pending",
        suggestions_json="[]",
    )
    defaults.update(kwargs)
    return AIDraft(aad_id=aad_id, **defaults)


class TestAIDraftE2EEColumns:
    def test_e2ee_columns_persist(self, db, user):
        # E2EE 列 (encrypted_meta_blob / meta_iv / file_hash_plain /
        # thumbnail_key / aad_id) が永続化される。
        draft = _make_draft(
            user.id,
            aad_id=123456789,
            encrypted_meta_blob=b"\x01\x02\x03",
            meta_iv=b"\x00" * 12,
            file_hash_plain="a" * 64,
            thumbnail_key="ai/1/x_thumb.bin",
        )
        db.session.add(draft)
        db.session.commit()

        fetched = AIDraft.query.get(draft.id)
        assert fetched.aad_id == 123456789
        assert fetched.encrypted_meta_blob == b"\x01\x02\x03"
        assert fetched.meta_iv == b"\x00" * 12
        assert fetched.file_hash_plain == "a" * 64
        assert fetched.thumbnail_key == "ai/1/x_thumb.bin"

    def test_aad_id_unique_per_user(self, db, user):
        # 同一 user で同じ aad_id は UNIQUE(user_id, aad_id) で弾かれる
        # (下書き間 ciphertext swap の検知能力)。
        db.session.add(_make_draft(user.id, aad_id=42))
        db.session.commit()

        db.session.add(_make_draft(user.id, aad_id=42))
        with pytest.raises(IntegrityError):
            db.session.commit()
        db.session.rollback()

    def test_null_aad_id_rows_coexist(self, db, user):
        # レガシー平文下書き (aad_id NULL) は複数併存できる
        # (NULL は UNIQUE 上 distinct 扱い)。
        db.session.add(_make_draft(user.id, aad_id=None))
        db.session.add(_make_draft(user.id, aad_id=None))
        db.session.commit()  # 例外が出ないこと

        assert AIDraft.query.filter_by(
            user_id=user.id, aad_id=None
        ).count() == 2
