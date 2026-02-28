"""画像配信ヘルパー

3つの画像エンドポイント（ドラフト画像・証憑画像・API証憑画像）から
共通で呼び出される配信ロジック。

- LocalStorageBackend: send_file() で配信（ストリーミング、Range対応）
- S3StorageBackend: presigned URL にリダイレクト
- ETag (file_hash) による条件付きリクエスト (304 Not Modified)
- Cache-Control: private, max-age=31536000, immutable
- ?size=thumb でサムネイル配信（存在しなければ元画像にフォールバック）
"""

import logging

from flask import Response, redirect, request, send_file

from app.services.storage import (
    LocalStorageBackend,
    S3StorageBackend,
    get_storage_backend,
    make_thumbnail_key,
)

logger = logging.getLogger(__name__)

_CACHE_CONTROL = "private, max-age=31536000, immutable"


def serve_image(
    image_key: str,
    image_mime: str,
    file_hash: str | None = None,
) -> Response:
    """画像を最適な方法で配信する。

    Args:
        image_key: ストレージキー (例: "vouchers/1/42.jpg")
        image_mime: MIMEタイプ (例: "image/jpeg")
        file_hash: SHA-256ハッシュ (ETag用、Noneなら条件付きリクエスト無効)

    Returns:
        Flask Response

    Raises:
        FileNotFoundError: 画像が存在しない場合
    """
    backend = get_storage_backend()

    # ?size=thumb の判定
    serving_key = image_key
    serving_mime = image_mime
    if request.args.get("size") == "thumb":
        thumb_key = make_thumbnail_key(image_key)
        if backend.exists(thumb_key):
            serving_key = thumb_key
            serving_mime = "image/jpeg"

    # ETag 条件付きリクエスト (304 Not Modified)
    if file_hash:
        etag = f'"{file_hash}"'
        if_none_match = request.headers.get("If-None-Match")
        if if_none_match and if_none_match.strip() == etag:
            return Response(status=304, headers={
                "ETag": etag,
                "Cache-Control": _CACHE_CONTROL,
            })

    if isinstance(backend, LocalStorageBackend):
        return _serve_local(backend, serving_key, serving_mime, file_hash)
    elif isinstance(backend, S3StorageBackend):
        return _serve_s3(backend, serving_key, serving_mime, file_hash)
    else:
        return _serve_generic(backend, serving_key, serving_mime, file_hash)


def _serve_local(backend, key, mime, file_hash):
    """ローカルバックエンド: send_file() で配信。"""
    path = backend.full_path(key)
    if not path.exists():
        raise FileNotFoundError(f"Storage key not found: {key}")

    response = send_file(path, mimetype=mime, conditional=True)
    response.headers["Cache-Control"] = _CACHE_CONTROL
    if file_hash:
        response.headers["ETag"] = f'"{file_hash}"'
    return response


def _serve_s3(backend, key, mime, file_hash):
    """S3バックエンド: presigned URL にリダイレクト。"""
    try:
        url = backend.generate_presigned_url(key)
    except Exception:
        return _serve_generic(backend, key, mime, file_hash)

    response = redirect(url, code=302)
    response.headers["Cache-Control"] = "private, max-age=3600"
    return response


def _serve_generic(backend, key, mime, file_hash):
    """汎用フォールバック: バイト列を直接返す。"""
    image_data = backend.get(key)
    headers = {"Cache-Control": _CACHE_CONTROL}
    if file_hash:
        headers["ETag"] = f'"{file_hash}"'
    return Response(image_data, mimetype=mime, headers=headers)
