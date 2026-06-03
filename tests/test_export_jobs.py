"""E6 #113 §15.4 PR-2: サーバ保存版エクスポート (export_jobs) のテスト。

POST /api/v1/export/jobs (アップロード + メール) / GET 一覧 / GET download。
復号はクライアント側 (パスフレーズ) なのでサーバは暗号文 blob を預かるだけ。
"""

from datetime import datetime, timedelta, timezone

import pytest

from app.models.export_job import ExportJob, EXPORT_TTL
from app.models.user import User
from app.services.storage import get_storage_backend


def _make_job(db, user_id, *, hours=24, status="ready", download_count=0,
              body=b"ENCRYPTED-ARCHIVE"):
    """storage blob 付きの ExportJob を作る。"""
    now = datetime.now(timezone.utc)
    job = ExportJob(
        user_id=user_id,
        status=status,
        storage_key="",
        file_size=len(body),
        created_at=now,
        ready_at=now,
        expires_at=now + timedelta(hours=hours),
        download_count=download_count,
    )
    db.session.add(job)
    db.session.flush()
    job.storage_key = f"exports/{user_id}/{job.id}.ikexport"
    get_storage_backend().put(job.storage_key, body, "application/octet-stream")
    db.session.commit()
    return job


def _second_user(db):
    u = User(username="other", email="other@example.com", user_type="personal")
    u.set_password("password123")
    db.session.add(u)
    db.session.commit()
    return u


class TestExportJobModel:
    def test_default_expires_at(self, db, user):
        # SQLite は読み戻し時に tzinfo を落とすため naive で比較する
        # (実 Postgres では timezone-aware で保持される)。
        before = datetime.now(timezone.utc).replace(tzinfo=None)
        job = ExportJob(user_id=user.id, status="ready", storage_key="k",
                        file_size=1)
        db.session.add(job)
        db.session.commit()
        exp = job.expires_at
        if exp.tzinfo is not None:
            exp = exp.replace(tzinfo=None)
        # _default_expires_at = now + EXPORT_TTL (24h)
        delta = exp - before
        assert EXPORT_TTL - timedelta(minutes=1) <= delta <= EXPORT_TTL + timedelta(minutes=1)

    def test_is_expired(self, db, user):
        past = datetime.now(timezone.utc) - timedelta(hours=1)
        job = ExportJob(user_id=user.id, status="ready", storage_key="k",
                        file_size=1, expires_at=past)
        assert job.is_expired() is True
        future = datetime.now(timezone.utc) + timedelta(hours=1)
        job2 = ExportJob(user_id=user.id, status="ready", storage_key="k",
                         file_size=1, expires_at=future)
        assert job2.is_expired() is False


class TestExportJobCreate:
    """POST /api/v1/export/jobs"""

    def test_unauthenticated_401_or_redirect(self, client):
        resp = client.post("/api/v1/export/jobs", data=b"x",
                           content_type="application/octet-stream")
        assert resp.status_code in (401, 302)

    def test_create_stores_blob_and_sends_email(self, db, logged_in_client,
                                                user, monkeypatch):
        sent = {}

        def fake_send(to, template, ctx, **kw):
            sent["to"] = to
            sent["template"] = template
            sent["ctx"] = ctx

        monkeypatch.setattr("app.views.api.send_email", fake_send)

        body = b"ENCRYPTED-ZIP-BYTES"
        resp = logged_in_client.post("/api/v1/export/jobs", data=body,
                                     content_type="application/octet-stream")
        assert resp.status_code == 201
        data = resp.get_json()
        assert data["ok"] is True
        job_id = data["job_id"]

        job = db.session.get(ExportJob, job_id)
        assert job is not None
        assert job.user_id == user.id
        assert job.status == "ready"
        assert job.file_size == len(body)
        # storage に暗号文 blob が保存されている
        assert get_storage_backend().get(job.storage_key) == body
        # メールが送られた
        assert sent["to"] == user.email
        assert sent["template"] == "export_ready"
        assert "url" in sent["ctx"] and "expires_at" in sent["ctx"]

    def test_empty_body_400(self, db, logged_in_client, monkeypatch):
        monkeypatch.setattr("app.views.api.send_email", lambda *a, **k: None)
        resp = logged_in_client.post("/api/v1/export/jobs", data=b"",
                                     content_type="application/octet-stream")
        assert resp.status_code == 400

    def test_oversize_413(self, app, db, logged_in_client, monkeypatch):
        monkeypatch.setattr("app.views.api.send_email", lambda *a, **k: None)
        monkeypatch.setitem(app.config, "EXPORT_MAX_UPLOAD_BYTES", 10)
        resp = logged_in_client.post("/api/v1/export/jobs", data=b"x" * 20,
                                     content_type="application/octet-stream")
        assert resp.status_code == 413


class TestExportJobsList:
    """GET /api/v1/export/jobs"""

    def test_lists_only_own_jobs(self, db, logged_in_client, user):
        other = _second_user(db)
        mine = _make_job(db, user.id)
        _make_job(db, other.id)  # 他人のジョブ (出てはいけない)

        resp = logged_in_client.get("/api/v1/export/jobs")
        assert resp.status_code == 200
        jobs = resp.get_json()["jobs"]
        ids = [j["id"] for j in jobs]
        assert mine.id in ids
        assert len(jobs) == 1  # 他人の分は含まれない

    def test_expired_shown_as_expired(self, db, logged_in_client, user):
        _make_job(db, user.id, hours=-1)  # 期限切れ
        resp = logged_in_client.get("/api/v1/export/jobs")
        jobs = resp.get_json()["jobs"]
        assert jobs[0]["status"] == "expired"


class TestExportJobDownload:
    """GET /api/v1/export/jobs/<id>/download"""

    def test_owner_downloads_blob(self, db, logged_in_client, user):
        body = b"CIPHERTEXT-123"
        job = _make_job(db, user.id, body=body)
        resp = logged_in_client.get(f"/api/v1/export/jobs/{job.id}/download")
        assert resp.status_code == 200
        assert resp.data == body
        assert "attachment" in resp.headers.get("Content-Disposition", "")
        # download_count がインクリメントされる
        db.session.refresh(job)
        assert job.download_count == 1

    def test_other_users_job_404(self, db, logged_in_client, user):
        other = _second_user(db)
        job = _make_job(db, other.id)
        resp = logged_in_client.get(f"/api/v1/export/jobs/{job.id}/download")
        assert resp.status_code == 404

    def test_missing_job_404(self, db, logged_in_client):
        resp = logged_in_client.get("/api/v1/export/jobs/999999/download")
        assert resp.status_code == 404

    def test_expired_410(self, db, logged_in_client, user):
        job = _make_job(db, user.id, hours=-1)
        resp = logged_in_client.get(f"/api/v1/export/jobs/{job.id}/download")
        assert resp.status_code == 410

    def test_download_limit_410(self, app, db, logged_in_client, user):
        max_dl = app.config.get("EXPORT_MAX_DOWNLOADS", 3)
        job = _make_job(db, user.id, download_count=max_dl)
        resp = logged_in_client.get(f"/api/v1/export/jobs/{job.id}/download")
        assert resp.status_code == 410
