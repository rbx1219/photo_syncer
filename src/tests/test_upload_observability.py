"""背景上傳的失敗處理與告警（fix/upload-observability）。全部 mock，不打外部服務。"""
import threading
import time
from unittest.mock import MagicMock, patch

import pytest

import lib.google as g
import service.upload_service as us
import utils.alert as alert


PHOTOS = [{"id": i, "filename": f"p{i}.jpg"} for i in range(6)]


def run_with_deadline(fn, seconds=20):
    """在 thread 裡跑 fn；超時代表卡死（舊版的問題），直接讓測試失敗。"""
    out = {}

    def target():
        try:
            out["result"] = fn()
        except BaseException as e:  # noqa: BLE001 - 交給呼叫端判斷
            out["error"] = e

    t = threading.Thread(target=target, daemon=True)
    t.start()
    t.join(seconds)
    assert not t.is_alive(), "run_upload 卡住了"
    if "error" in out:
        raise out["error"]
    return out["result"]


@pytest.fixture
def upload_env(tmp_path):
    with patch.object(us, "DOWNLOAD_DIR", str(tmp_path)), \
         patch.object(us, "get_photos_upload_to_album", return_value={"photos": PHOTOS, "messages": []}), \
         patch.object(us, "download_photo") as dl, \
         patch.object(us, "upload_photo_bytes", side_effect=lambda creds, path: "tok-" + path) as up, \
         patch.object(us, "add_photos_to_album") as add:
        yield dl, up, add


def call_run_upload():
    return us.run_upload("auth", "creds", 1, None, len(PHOTOS), "galbum", "tok")


# ---------- run_upload ----------

def test_download_failure_does_not_hang_and_is_counted(upload_env):
    dl, _, _ = upload_env
    dl.side_effect = lambda auth, p, save_path: (_ for _ in ()).throw(RuntimeError("NAS 500")) if p["id"] == 2 else None
    result = run_with_deadline(call_run_upload)
    assert result["requested"] == 6
    assert result["uploaded"] == 5
    assert result["failed"] == ["p2.jpg"]


def test_slow_download_does_not_strand_uploaders(upload_env):
    # 舊版 uploader 在 queue 空 2 秒後就結束；下載比較慢時 photo_queue.join() 會永遠卡住
    dl, _, _ = upload_env
    dl.side_effect = lambda auth, p, save_path: time.sleep(2.5)
    result = run_with_deadline(call_run_upload)
    assert result["uploaded"] == 6 and result["failed"] == []


def test_upload_failure_is_counted(upload_env):
    _, up, _ = upload_env
    def flaky(creds, path):
        if path.endswith("p0.jpg"):
            raise RuntimeError("Upload failed: 429")
        return "t"
    up.side_effect = flaky
    result = run_with_deadline(call_run_upload)
    assert result["uploaded"] == 5 and result["failed"] == ["p0.jpg"]


def test_everything_failing_raises(upload_env):
    dl, _, _ = upload_env
    dl.side_effect = RuntimeError("NAS down")
    with pytest.raises(us.UploadError):
        run_with_deadline(call_run_upload)


def test_add_to_album_failure_raises_instead_of_reporting_zero(upload_env):
    _, _, add = upload_env
    add.side_effect = RuntimeError("Failed to add media items: 500")
    with pytest.raises(us.UploadError) as ei:
        run_with_deadline(call_run_upload)
    assert isinstance(ei.value.__cause__, RuntimeError)
    assert "500" not in str(ei.value)  # 給使用者的訊息不夾帶 API 原始錯誤


def test_no_photos_raises(upload_env):
    with patch.object(us, "get_photos_upload_to_album", return_value={"photos": [], "messages": []}):
        with pytest.raises(us.UploadError):
            call_run_upload()


# ---------- _background_upload：狀態與通知 ----------

@pytest.fixture
def bg_env():
    with patch.object(us, "get_service", return_value=MagicMock()), \
         patch.object(us, "get_or_create_album", return_value="galbum"), \
         patch.object(us, "update_people_list"), \
         patch.object(us, "move_items_to_trash_album", return_value=False), \
         patch.object(us, "update_batch_status") as status, \
         patch.object(us, "notify_line", return_value=True) as line, \
         patch.object(us, "notify_admin") as admin:
        yield status, line, admin


def run_bg():
    us._background_upload("creds", "auth", "tok", "Album", 7, None, 6, time.time(), 42)


def final_status(status_mock):
    return status_mock.call_args_list[-1].args


def test_success_stays_success_even_if_line_notify_fails(bg_env):
    status, line, admin = bg_env
    line.return_value = False
    with patch.object(us, "run_upload", return_value={"requested": 6, "uploaded": 6, "failed": [], "messages": []}):
        run_bg()
    assert final_status(status) == (42, "success")
    admin.assert_not_called()


def test_partial_failure_is_success_with_warning(bg_env):
    status, line, admin = bg_env
    with patch.object(us, "run_upload", return_value={"requested": 6, "uploaded": 4, "failed": ["a", "b"], "messages": []}):
        run_bg()
    assert final_status(status) == (42, "success")
    assert "4/6" in line.call_args.args[1]
    assert admin.call_args.kwargs.get("silent") is True


def test_upload_error_marks_failed_and_alerts_admin(bg_env):
    status, line, admin = bg_env
    with patch.object(us, "run_upload", side_effect=us.UploadError("沒有可上傳的照片")):
        run_bg()
    assert final_status(status) == (42, "failed")
    assert "沒有可上傳的照片" in line.call_args.args[1]
    assert "第 42 批次" in admin.call_args.args[0]


def test_unexpected_error_marks_failed_and_alerts_admin(bg_env):
    status, _, admin = bg_env
    with patch.object(us, "run_upload", side_effect=KeyError("boom")):
        run_bg()
    assert final_status(status) == (42, "failed")
    assert "KeyError" in admin.call_args.args[0]


def test_oauth_failure_does_not_double_alert(bg_env):
    status, line, admin = bg_env
    with patch.object(us, "get_service", side_effect=g.GoogleAuthError("invalid_grant")):
        run_bg()
    assert final_status(status) == (42, "failed")
    line.assert_called_once()
    admin.assert_not_called()  # lib.google 自己會發（含重新授權步驟）


# ---------- lib.google.authenticate ----------

def test_missing_token_alerts_instead_of_waiting_for_browser(tmp_path):
    with patch.object(g, "TOKEN_PATH", str(tmp_path / "nope.pickle")), \
         patch.object(g, "InstalledAppFlow") as flow, \
         patch.object(g, "notify_admin") as admin:
        with pytest.raises(g.GoogleAuthError):
            g.authenticate()
    flow.from_client_secrets_file.assert_not_called()
    assert admin.call_args.kwargs["dedup_key"] == "google-oauth"
    assert "get_cred.py" in admin.call_args.args[0]


def test_refresh_failure_raises_auth_error():
    creds = MagicMock(expired=True, refresh_token="r")
    creds.refresh.side_effect = RuntimeError("invalid_grant")
    with patch.object(g, "notify_admin") as admin:
        with pytest.raises(g.GoogleAuthError):
            g.get_service(creds)
    admin.assert_called_once()


# ---------- utils.alert ----------

def test_scrub_removes_tokens_and_query_strings():
    raw = ("500 Server Error for url: https://nas:5001/webapi/entry.cgi/a.jpg?SynoToken=SECRET123&x=1 "
           "Authorization: Bearer ya29.ABC-def token=XYZ")
    out = alert.scrub(raw)
    for secret in ("SECRET123", "ya29.ABC-def", "XYZ"):
        assert secret not in out
    assert "https://nas:5001/webapi/entry.cgi/a.jpg" in out


def test_notify_admin_dedups_and_never_raises(monkeypatch):
    alert._last_alert.clear()
    monkeypatch.setenv("TG_URL", "http://tg.invalid/send")
    with patch.object(alert.requests, "post", side_effect=RuntimeError("down")) as post:
        alert.notify_admin("x", dedup_key="k")
        alert.notify_admin("x", dedup_key="k")
    assert post.call_count == 1
