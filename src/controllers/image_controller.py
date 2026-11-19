"""對外提供人臉縮圖：GET /images/<檔名>（LINE 的人臉選單、預設圖 grey.jpg 用）。

檔案存在 NAS（k3s 中以 NFS 掛在 FACE_IMAGE_DIR，預設 /app/face_image），由 lib/synology.thumb_photo 寫入。
取代原本 NAS nginx 的 /images/（2026-11-19 起 .163 nginx-proxy 直接把 /images/ 導到 photo-syncer）。
只提供目錄內的圖片檔，不列目錄。
"""
import os

from flask import Blueprint, abort, send_from_directory

image_bp = Blueprint("image", __name__)
FACE_IMAGE_DIR = os.getenv("FACE_IMAGE_DIR", "/app/face_image")
ALLOWED_EXT = {".jpg", ".jpeg", ".png"}


@image_bp.route("/images/<path:filename>", methods=["GET"])
def face_image(filename):
    # 只允許單層檔名與圖片副檔名；send_from_directory 也會擋掉 ../ 之類的路徑
    if "/" in filename or os.path.splitext(filename)[1].lower() not in ALLOWED_EXT:
        abort(404)
    return send_from_directory(FACE_IMAGE_DIR, filename, max_age=3600)
