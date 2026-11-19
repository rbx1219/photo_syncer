"""LINE 照片挑選器用的 API（line-reply 呼叫）：列出相簿目前的照片、把不要的照片移出並加入黑名單。

GET  /picker/photos?album=<相簿名>&offset=0&limit=10
     → {"album", "total", "offset", "items": [{"media_id", "filename", "thumb_url", "photo_id", "time"}]}
       thumb_url 是 Google 提供的縮圖（約 60 分鐘有效）
POST /picker/exclude  {"album": <相簿名>, "media_id": <Google media item id>}
     → 先把照片加入「待刪除照片」（避免變成相簿庫裡的孤兒），再移出該相簿；
       能對應到 Synology 照片時加入 photo_blacklist，之後隨機挑選不會再選到
"""
import json
import logging

from flask import Blueprint, jsonify, request

from lib.google import (ALBUM_BATCH_SIZE, API_BASE_URL, TRASH_ALBUM_NAME, authenticate, get_or_create_album,
                        get_service, list_media_items_in_album)
from models.database import SessionLocal
from models.exist_person import ExistPerson
from models.photo import Photo
from models.photo_blacklist import PhotoBlacklist

picker_bp = Blueprint("picker", __name__)
MAX_LIMIT = 10  # LINE carousel 一次最多 12 張，留 2 格餘裕


def _find_album_id(service, title):
    token = None
    while True:
        r = service.albums().list(pageSize=50, pageToken=token, fields="albums(id,title),nextPageToken").execute()
        for a in r.get("albums", []):
            if a.get("title") == title:
                return a["id"]
        token = r.get("nextPageToken")
        if not token:
            return None


def _photo_ids_by_filename(db, filenames):
    """Google 上的檔名就是上傳時的 Synology 檔名；優先對應目前已上傳（exist_person_photos）的那張，避免同名照片對錯。"""
    if not filenames:
        return {}
    rows = (db.query(Photo.filename, Photo.item_id, ExistPerson.id)
            .outerjoin(ExistPerson, ExistPerson.photo_id == Photo.item_id)
            .filter(Photo.filename.in_(filenames)).all())
    best = {}
    for filename, item_id, exist_id in rows:
        if filename not in best or exist_id is not None:
            best[filename] = item_id
    return best


def _album_request(service, album_id, action, media_ids):
    resp, content = service._http.request(
        uri=f"{API_BASE_URL}albums/{album_id}:{action}", method="POST",
        body=json.dumps({"mediaItemIds": media_ids}), headers={"Content-Type": "application/json"})
    if resp.status != 200:
        raise RuntimeError(f"{action} 失敗：{resp.status} {content.decode()[:200]}")


@picker_bp.route("/picker/photos", methods=["GET"])
def picker_photos():
    album = request.args.get("album", "Google Nest Hub")
    offset = max(int(request.args.get("offset", 0)), 0)
    limit = min(max(int(request.args.get("limit", MAX_LIMIT)), 1), MAX_LIMIT)
    service = get_service(authenticate())
    album_id = _find_album_id(service, album)
    if not album_id:
        return jsonify({"error": f"找不到相簿「{album}」"}), 404
    items = list_media_items_in_album(service, album_id)
    page = items[offset:offset + limit]
    db = SessionLocal()
    try:
        ids = _photo_ids_by_filename(db, [m.get("filename") for m in page])
    finally:
        db.close()
    return jsonify({
        "album": album, "total": len(items), "offset": offset,
        "items": [{
            "media_id": m["id"],
            "filename": m.get("filename"),
            "thumb_url": f"{m['baseUrl']}=w600-h600-c",
            "photo_id": ids.get(m.get("filename")),
            "time": (m.get("mediaMetadata") or {}).get("creationTime"),
        } for m in page],
    })


@picker_bp.route("/picker/exclude", methods=["POST"])
def picker_exclude():
    data = request.get_json(force=True) or {}
    album, media_id = data.get("album", "Google Nest Hub"), data.get("media_id")
    if not media_id:
        return jsonify({"error": "缺少 media_id"}), 400
    service = get_service(authenticate())
    album_id = _find_album_id(service, album)
    if not album_id:
        return jsonify({"error": f"找不到相簿「{album}」"}), 404
    item = service.mediaItems().get(mediaItemId=media_id).execute()
    filename = item.get("filename")

    # 先進「待刪除照片」再移出相簿（順序不能反，否則照片會變成不在任何相簿的孤兒）
    trash_id = get_or_create_album(service, TRASH_ALBUM_NAME)
    _album_request(service, trash_id, "batchAddMediaItems", [media_id])
    _album_request(service, album_id, "batchRemoveMediaItems", [media_id])

    db = SessionLocal()
    try:
        photo_id = _photo_ids_by_filename(db, [filename]).get(filename)
        blacklisted = False
        if photo_id is not None:
            if not db.query(PhotoBlacklist).filter_by(photo_id=photo_id).first():
                db.add(PhotoBlacklist(photo_id=photo_id, reason="line-picker"))
            db.query(ExistPerson).filter(ExistPerson.photo_id == photo_id).delete(synchronize_session=False)
            db.commit()
            blacklisted = True
    finally:
        db.close()
    logging.info(f"[picker] 移出 {album}：{filename} photo_id={photo_id} blacklisted={blacklisted}")
    return jsonify({"status": "ok", "filename": filename, "photo_id": photo_id, "blacklisted": blacklisted})
