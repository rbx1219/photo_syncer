import os, json, threading, time, queue
from lib.synology import login, list_people, list_selected_people, thumb_photo, download_photo
from lib.google import (
    get_service, get_or_create_album, add_photos_to_album,
    upload_photo_bytes, delete_all_photos_from_album, move_items_to_trash_album,
    GoogleAuthError,
)
from service.google_service import get_photos_upload_to_album
from flask import jsonify
from config.config import Config
from service.batch_service import create_new_batch, update_batch_status
import logging
from dotenv import load_dotenv
import requests
from cachetools import TTLCache
from service.user_service import get_user_info_service
from utils.alert import notify_admin, notify_line
from datetime import datetime, time as dtime

load_dotenv()
logging.basicConfig(filename="error.log", level=logging.ERROR, format="%(asctime)s - %(levelname)s - %(message)s")

DOWNLOAD_DIR = Config.SYNO_DOWNLOAD_DIR
IMG_URL = Config.IMG_URL
ACCOUNT = Config.SYNO_ACCOUNT
PASSWORD = Config.SYNO_PASSWORD
FID = Config.SYNO_FID
TIMEZONE = Config.SYNO_TIMEZONE
NUM_DOWNLOAD_THREADS = 16
NUM_UPLOAD_THREADS = 16

face_cache = TTLCache(maxsize=100, ttl=60)

# 同一本相簿同時只能有一個上傳：每次上傳都會先清空相簿再放新照片，兩個同時跑會互相清掉對方的照片。
# gunicorn 只有 1 個 worker process，所以用 process 內的鎖即可。超過 ALBUM_LOCK_TTL 秒視為卡住、自動失效。
ALBUM_LOCK_TTL = 30 * 60
_album_uploads = {}
_album_uploads_lock = threading.Lock()


def try_claim_album(album_name):
    with _album_uploads_lock:
        started = _album_uploads.get(album_name)
        if started is not None and time.time() - started < ALBUM_LOCK_TTL:
            return False
        _album_uploads[album_name] = time.time()
        return True


def release_album(album_name):
    with _album_uploads_lock:
        _album_uploads.pop(album_name, None)


class UploadError(Exception):
    """上傳流程中「已知原因」的失敗；訊息會直接給使用者看。"""


def background_upload(creds, auth, token, album_name, *args, **kwargs):
    """背景上傳；不論成功、失敗或例外都會釋放相簿鎖。"""
    try:
        _background_upload(creds, auth, token, album_name, *args, **kwargs)
    except Exception as e:
        # 正常的失敗都在 _background_upload 裡處理掉了；會跑到這裡的是沒預期到的錯誤
        logging.exception("❌ 背景上傳發生未預期的錯誤")
        notify_admin(f"❌ photo-syncer 背景上傳未預期錯誤｜相簿「{album_name}」\n{type(e).__name__}: {e}")
    finally:
        release_album(album_name)


def _fail_batch(batch_id, token, album_name, person_id, user_reason, error=None):
    """把批次標成 failed，並同時通知使用者（LINE）與管理員（Telegram）。"""
    update_batch_status(batch_id, "failed")
    notify_line(token, f"❌ 上傳失敗，第 {batch_id} 批次：{user_reason}")
    if isinstance(error, GoogleAuthError):
        return  # lib.google 已經發過 OAuth 告警（含重新授權步驟），不重複發
    detail = f"\n{type(error).__name__}: {error}" if error else ""
    notify_admin(f"❌ photo-syncer 上傳失敗｜第 {batch_id} 批次｜相簿「{album_name}」｜person {person_id}\n{user_reason}{detail}")


def _background_upload(creds, auth, token, album_name, person_id, album_id, num_photos, start_time, batch_id, start_dt=None, end_dt=None):
    try:
        service = get_service(creds)
        google_album_id = get_or_create_album(service, album_name)
    except Exception as e:
        logging.exception("❌ 建立 Google 相簿失敗")
        _fail_batch(batch_id, token, album_name, person_id, "無法建立或取得 Google 相簿", e)
        return

    try:
        # 只更新人臉縮圖；上傳流程沒有 LINE user_id，不推播人臉清單（舊版誤把上傳 token 當 user_id，LINE 回 400）
        update_people_list(auth, user_id=None, notify=False)
    except Exception as e:
        logging.exception("⚠️ 更新人員列表失敗（可略過）")

    try:
        if move_items_to_trash_album(service, google_album_id):
            delete_all_photos_from_album(creds, google_album_id)
        else:
            # 沒成功進『待刪除照片』就不移出相簿，避免照片變成散落在相簿庫的孤兒
            logging.error("⚠️ 舊照片未能移到『待刪除照片』，保留相簿原有內容，本次上傳會與舊照片並存")
    except Exception as e:
        logging.exception("⚠️ 刪除 Google 相簿照片失敗（可能是配額或限速）")

    try:
        update_batch_status(batch_id, "uploading")
        result = run_upload(auth, creds, person_id, album_id, num_photos, google_album_id, token, start_dt, end_dt)
    except UploadError as e:
        logging.error("❌ 第 %s 批次上傳失敗：%s", batch_id, e)
        _fail_batch(batch_id, token, album_name, person_id, str(e), e.__cause__)
        return
    except Exception as e:
        logging.exception("❌ 第 %s 批次上傳過程發生錯誤", batch_id)
        _fail_batch(batch_id, token, album_name, person_id, "上傳過程發生錯誤", e)
        return

    # 成功之後的通知失敗不影響批次狀態（舊版通知失敗會掉進 except，把已上傳成功的批次標成 failed）
    update_batch_status(batch_id, "success")
    uploaded, requested, failed = result["uploaded"], result["requested"], result["failed"]
    elapsed = round(time.time() - start_time)
    logging.info("✅ 第 %s 批次完成：%s/%s 張，耗時 %ss", batch_id, uploaded, requested, elapsed)

    if failed:
        notify_line(token, f"⚠️ 上傳完成，但只成功 {uploaded}/{requested} 張（{len(failed)} 張失敗）")
        notify_admin(
            f"⚠️ photo-syncer 部分上傳失敗｜第 {batch_id} 批次｜相簿「{album_name}」\n"
            f"成功 {uploaded}/{requested} 張，失敗 {len(failed)} 張，例如：{', '.join(failed[:5])}",
            silent=True,
        )
    else:
        notify_line(token, f"✅ 上傳完成，共上傳 {uploaded} 張照片")

def update_people_list(auth, user_id, notify=True):
    if not auth:
        logging.error("Synology 登入失敗，請檢查帳號密碼或網路連線")
        return []

    try:
        people_list = list_selected_people(auth)
        if not people_list:
            logging.error("無法獲取人員列表，請檢查 Synology 服務是否正常")
            return []
        logging.info(f"成功獲取 {len(people_list)} 人員資料")
        result_list = []
        for person in people_list:
            thumb_photo(person['id'], person['additional']['thumbnail']['cache_key'], auth)
            result_list.append({
                "name": person['name'],
                "ID": person['id'],
                "img": f"{IMG_URL}/{person['id']}.jpg"
            })

        if not notify or not user_id:
            pass
        elif 'faces' not in face_cache or not face_cache['faces']:
            requests.post(
                f"{Config.LINE_API_URL}/faces",
                json={
                    "faces": result_list,
                    "user_id": user_id
                }
            )
        elif 'faces' in face_cache:
            requests.post(
                f"{Config.LINE_API_URL}/faces",
                json={
                    "faces": face_cache['faces'],
                    "user_id": user_id
                }
            )
        face_cache['faces'] = result_list
        return face_cache['faces']
    except Exception as e:
        logging.exception("⚠️ 更新人員資料過程中發生錯誤")
        return []

def parse_date_range(start_str, end_str):
    """把 'YYYY-MM-DD' 轉成 datetime；結束日包含當天整天。格式錯誤時拋 ValueError。"""
    start_dt = datetime.fromisoformat(start_str) if start_str else None
    end_dt = None
    if end_str:
        end_dt = datetime.fromisoformat(end_str)
        if len(end_str) == 10:  # 只有日期時，視為當天 23:59:59.999999
            end_dt = datetime.combine(end_dt.date(), dtime.max)
    if start_dt and end_dt and start_dt > end_dt:
        raise ValueError("start_time 不可晚於 end_time")
    return start_dt, end_dt

def handle_upload(request, creds, session):
    try:
        data = request.get_json(force=True)
    except Exception as e:
        logging.error("JSON 解析失敗: %s", str(e))
        return jsonify({"error": "無法解析 JSON，請檢查資料格式"}), 400

    person_id = request.args.get('personID') or data.get('personID')
    if person_id:
        logging.error(f"同步人員 ID: {person_id}")
    else:
        logging.error("請求中未提供 personID")
        return jsonify({"error": "請提供 personID"}), 400
    album_id = data.get("albumID")
    album_name = data.get("albumName") or os.getenv("DEFAULT_ALBUM_NAME", "My New Album")
    num_photos = data.get("numPhotos") or os.getenv("DEFAULT_NUM_PHOTOS", 5)
    token = request.args.get('token') or data.get('token')
    try:
        start_dt, end_dt = parse_date_range(data.get("start_time"), data.get("end_time"))
    except ValueError as e:
        logging.error("日期格式錯誤: %s", e)
        return jsonify({"error": f"日期格式錯誤（需為 YYYY-MM-DD）：{e}"}), 400
    auth = session.get('auth')
    if not auth:
        auth = login(ACCOUNT, PASSWORD, FID, TIMEZONE)
    if not person_id and not album_id:
        return jsonify({"error": "請提供 personID 或 albumID"}), 400

    if not try_claim_album(album_name):
        logging.warning(f"相簿「{album_name}」正在上傳中，拒絕新的上傳")
        return jsonify({"error": f"相簿「{album_name}」正在上傳中，請等上一批完成", "busy": True}), 409

    start_time = time.time()
    new_batch = create_new_batch(auth)
    batch_id = new_batch.id if new_batch else None
    if not new_batch:
        release_album(album_name)
        return jsonify({"error": "目前尚無上傳批次資料"}), 404

    threading.Thread(
        target=background_upload,
        args=(
            creds,
            auth,
            token,
            album_name,
            person_id,
            album_id,
            num_photos,
            start_time,
            batch_id,
        ),
        kwargs={
            "start_dt": start_dt,
            "end_dt": end_dt,
        },
    ).start()

    user_info = get_user_info_service(creds)
    if not user_info or 'name' not in user_info:
        return jsonify({"error": "使用者資訊取得失敗"}), 500

    batch_time = new_batch.created_at

    return jsonify({
        "message": f"同步作業已在背景啟動，使用者:{user_info.get('name')}, 第{new_batch.id}批次, 上傳時間{batch_time}"
    })

def run_upload(auth, creds, person_id, album_id, num_photos, google_album_id, token, start_time=None, end_time=None):
    """下載 → 上傳 → 加進相簿。

    回傳 {"requested", "uploaded", "failed"（檔名清單）, "messages"}；
    整批都沒有成果時拋 UploadError（舊版回傳 uploaded=0，外層會把它當成功）。
    """
    download_queue = queue.Queue()
    photo_queue = queue.Queue()
    token_map = {}
    failed = []
    lock = threading.Lock()

    report = get_photos_upload_to_album(auth, person_id, album_id, num_photos, token, start_time, end_time)
    if not report:
        raise UploadError("無法從 Synology 取得要上傳的照片，請檢查 NAS 是否正常")
    photos = report.get("photos", [])
    if not photos:
        raise UploadError("沒有可上傳的照片")
    logging.info("取得 %s 張照片，準備下載並上傳到 Google 相簿", len(photos))
    for photo in photos:
        download_queue.put(photo)

    def record_failure(filename, stage, error):
        logging.warning("%s %s 失敗：%s", stage, filename, error)
        with lock:
            failed.append(filename)

    def download_worker():
        # download_queue 在啟動前就放滿了，空了就代表沒工作
        while True:
            try:
                p = download_queue.get_nowait()
            except queue.Empty:
                return
            saved_path = os.path.join(DOWNLOAD_DIR, p['filename'])
            try:
                download_photo(auth, p, save_path=saved_path)
                photo_queue.put(p['filename'])
            except Exception as e:
                # 舊版沒接例外：thread 直接死掉、task_done 沒呼叫，download_queue.join() 永遠卡住
                record_failure(p['filename'], "下載", e)
                try:
                    os.remove(saved_path)
                except OSError:
                    pass
            finally:
                download_queue.task_done()

    def upload_worker():
        # 收到 None 才結束（舊版 queue 空 2 秒就結束，下載比較慢時 uploader 會先走光，photo_queue.join() 永遠卡住）
        while True:
            filename = photo_queue.get()
            if filename is None:
                photo_queue.task_done()
                return
            saved_path = os.path.join(DOWNLOAD_DIR, filename)
            try:
                token_filename = upload_photo_bytes(creds, saved_path)
                with lock:
                    token_map[filename] = token_filename
            except Exception as e:
                record_failure(filename, "上傳", e)
            finally:
                # 下載的原圖只是上傳用的暫存：bytes 已送到 Google（之後 batchCreate 只用 upload token），立即刪除
                # （舊版從不刪除，NAS 上累積到 7,651 張 / 19G）
                try:
                    os.remove(saved_path)
                except OSError:
                    pass
                photo_queue.task_done()

    downloaders = [threading.Thread(target=download_worker, daemon=True) for _ in range(NUM_DOWNLOAD_THREADS)]
    uploaders = [threading.Thread(target=upload_worker, daemon=True) for _ in range(NUM_UPLOAD_THREADS)]
    for t in downloaders + uploaders:
        t.start()
    for t in downloaders:
        t.join()
    for _ in uploaders:
        photo_queue.put(None)
    for t in uploaders:
        t.join()

    if not token_map:
        raise UploadError(f"{len(photos)} 張照片全部下載或上傳失敗")

    try:
        add_photos_to_album(creds, google_album_id, token_map)
    except Exception as e:
        raise UploadError("照片已上傳，但加進 Google 相簿失敗") from e

    return {
        "requested": len(photos),
        "uploaded": len(token_map),
        "failed": failed,
        "messages": report.get("messages", []),
    }

