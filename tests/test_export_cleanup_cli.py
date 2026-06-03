"""E6 #113 §15.4 PR-3: flask export-cleanup CLI のテスト。

期限切れ (expires_at 経過) の ExportJob を storage blob ごと削除し、有効な
ジョブは残す。--dry-run では何も削除しない。
"""

from datetime import datetime, timedelta, timezone

from app.models.export_job import ExportJob
from app.services.storage import get_storage_backend


def _make_job(db, user_id, *, hours, body=b"ARCHIVE"):
    now = datetime.now(timezone.utc)
    job = ExportJob(
        user_id=user_id,
        status="ready",
        storage_key="",
        file_size=len(body),
        created_at=now,
        ready_at=now,
        expires_at=now + timedelta(hours=hours),
        download_count=0,
    )
    db.session.add(job)
    db.session.flush()
    job.storage_key = f"exports/{user_id}/{job.id}.ikexport"
    get_storage_backend().put(job.storage_key, body, "application/octet-stream")
    db.session.commit()
    return job


def test_export_cleanup_deletes_expired_with_blob(db, app, user):
    expired = _make_job(db, user.id, hours=-1)
    fresh = _make_job(db, user.id, hours=24)
    expired_id, fresh_id = expired.id, fresh.id
    expired_key, fresh_key = expired.storage_key, fresh.storage_key
    backend = get_storage_backend()
    assert backend.exists(expired_key)

    result = app.test_cli_runner().invoke(args=["export-cleanup"])
    assert result.exit_code == 0
    assert "deleted ExportJob=1" in result.output
    assert "blob=1" in result.output

    db.session.expire_all()
    # 期限切れは行も blob も削除、有効は残る
    assert db.session.get(ExportJob, expired_id) is None
    assert db.session.get(ExportJob, fresh_id) is not None
    assert backend.exists(expired_key) is False
    assert backend.exists(fresh_key) is True


def test_export_cleanup_dry_run_deletes_nothing(db, app, user):
    expired = _make_job(db, user.id, hours=-1)
    expired_id, expired_key = expired.id, expired.storage_key

    result = app.test_cli_runner().invoke(args=["export-cleanup", "--dry-run"])
    assert result.exit_code == 0
    assert "DRY RUN" in result.output
    assert "1 件" in result.output

    db.session.expire_all()
    assert db.session.get(ExportJob, expired_id) is not None
    assert get_storage_backend().exists(expired_key) is True


def test_export_cleanup_no_expired(db, app, user):
    _make_job(db, user.id, hours=24)
    result = app.test_cli_runner().invoke(args=["export-cleanup"])
    assert result.exit_code == 0
    assert "deleted ExportJob=0" in result.output
    assert ExportJob.query.count() == 1


def test_export_cleanup_missing_blob_still_deletes_row(db, app, user):
    """blob が既に無い (孤立行) でも CLI は行を削除して完走する。"""
    expired = _make_job(db, user.id, hours=-1)
    expired_id = expired.id
    # blob だけ先に消しておく (storage.delete は冪等)
    get_storage_backend().delete(expired.storage_key)

    result = app.test_cli_runner().invoke(args=["export-cleanup"])
    assert result.exit_code == 0
    db.session.expire_all()
    assert db.session.get(ExportJob, expired_id) is None
