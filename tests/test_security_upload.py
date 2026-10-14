"""証憑画像アップロードの Content-Type 偽装対策テスト。

クライアント申告の Content-Type だけでなく、マジックバイトで実際の
画像形式を検証し、非画像・不一致を拒否する。
"""

import io

from PIL import Image

from app.models.voucher import Voucher
from app.services.storage import sniff_image_mime
from tests.conftest import make_journal


def _png_bytes() -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (4, 4), (255, 0, 0)).save(buf, format="PNG")
    return buf.getvalue()


class TestSniffImageMime:
    def test_png(self):
        assert sniff_image_mime(b"\x89PNG\r\n\x1a\n" + b"rest") == "image/png"

    def test_jpeg(self):
        assert sniff_image_mime(b"\xff\xd8\xff\xe0" + b"rest") == "image/jpeg"

    def test_gif(self):
        assert sniff_image_mime(b"GIF89a" + b"rest") == "image/gif"

    def test_webp(self):
        assert sniff_image_mime(b"RIFF\x00\x00\x00\x00WEBP") == "image/webp"

    def test_non_image(self):
        assert sniff_image_mime(b"<script>alert(1)</script>") is None
        assert sniff_image_mime(b"") is None


class TestUploadContentTypeSpoof:
    def test_spoofed_png_rejected(self, logged_in_client, db, user, accounts):
        """HTML を image/png と偽って添付できないこと。"""
        entry = make_journal(db, user.id, "5010", "1010", 1000)
        resp = logged_in_client.post(
            f"/vouchers/attach/{entry.id}",
            data={"image": (io.BytesIO(b"<script>alert(1)</script>"),
                            "x.png", "image/png")},
            content_type="multipart/form-data",
        )
        assert resp.status_code == 400
        assert Voucher.query.filter_by(user_id=user.id).count() == 0

    def test_mismatched_mime_rejected(self, logged_in_client, db, user, accounts):
        """PNG の実体を image/jpeg と偽っても拒否されること。"""
        entry = make_journal(db, user.id, "5010", "1010", 1000)
        resp = logged_in_client.post(
            f"/vouchers/attach/{entry.id}",
            data={"image": (io.BytesIO(_png_bytes()), "x.jpg", "image/jpeg")},
            content_type="multipart/form-data",
        )
        assert resp.status_code == 400
        assert Voucher.query.filter_by(user_id=user.id).count() == 0

    def test_valid_png_accepted(self, logged_in_client, db, user, accounts):
        """正しい PNG は受け付けられること。"""
        entry = make_journal(db, user.id, "5010", "1010", 1000)
        resp = logged_in_client.post(
            f"/vouchers/attach/{entry.id}",
            data={"image": (io.BytesIO(_png_bytes()), "x.png", "image/png")},
            content_type="multipart/form-data",
        )
        assert resp.status_code == 200
        assert Voucher.query.filter_by(user_id=user.id).count() == 1
