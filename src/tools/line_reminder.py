"""每週提醒：用 LINE push 問使用者要不要更換相簿照片（附「我要上傳照片／不需要上傳／使用自訂參數」快速回覆）。

取代原本 line-reply 容器內的 /app/webhook_service.py（NAS crontab 每週一 09:00 執行）。
訊息與按鈕完全相同；差別：
  - 不把 Channel Access Token、完整 user ID 印進 log（舊版會把 Authorization header 整個印出來）
  - 不寫 sessions/<user_id>.json：舊版寫入 {"last_action": "asked_album_change"}，但 line-reply 沒有任何地方讀取這個值
  - 任何一位使用者發送失敗就以非 0 結束（搭配 run_tool.py 發 Telegram 通知）

環境變數：
  LINE_CHANNEL_ACCESS_TOKEN   必填
  LINE_REMINDER_USER_IDS      收件人，以換行或逗號分隔（等同 line-reply 的 user_ids.txt）
  LINE_REMINDER_USER_IDS_FILE 或改用檔案路徑
  LINE_PUSH_URL               預設 https://api.line.me/v2/bot/message/push
  LINE_REMINDER_MESSAGE       預設為原本的提醒文字
  LINE_REMINDER_PRESETS       一鍵上傳按鈕（JSON），見 load_presets()
  DRY_RUN=1                   只印出要送的內容，不實際發送
"""
import json
import logging
import os
import re
import sys

import requests

DEFAULT_MESSAGE = "已經到了同步相簿的時間了喔，請問可有需要更換相簿照片"
DEFAULT_PUSH_URL = "https://api.line.me/v2/bot/message/push"
QUICK_REPLY_LABELS = ["我要上傳照片", "不需要上傳", "使用自訂參數"]

log = logging.getLogger("line_reminder")


def mask(user_id):
    return f"{user_id[:4]}…{user_id[-3:]}" if len(user_id) > 8 else "…"


def load_user_ids():
    raw = os.getenv("LINE_REMINDER_USER_IDS", "")
    path = os.getenv("LINE_REMINDER_USER_IDS_FILE")
    if path and os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            raw += "\n" + f.read()
    ids = [u.strip() for u in re.split(r"[\s,]+", raw) if u.strip()]
    return list(dict.fromkeys(ids))  # 去重、保留順序


MAX_LABEL_LEN = 20   # LINE quick reply 按鈕文字上限
MAX_ITEMS = 13       # LINE quick reply 按鈕數上限


def load_presets():
    """LINE_REMINDER_PRESETS：一鍵上傳按鈕，JSON 陣列，例如
    [{"label": "Nest Hub 換成米 200張", "person_id": 19365, "count": 200, "album": "Google Nest Hub"}]
    按下後送出「一鍵上傳 <person_id> <count> <album>」，由 line-reply 直接開始上傳。
    """
    raw = os.getenv("LINE_REMINDER_PRESETS", "").strip()
    if not raw:
        return []
    presets = json.loads(raw)
    items = []
    for p in presets:
        label = str(p["label"])
        person_id, count, album = int(p["person_id"]), int(p["count"]), str(p["album"]).strip()
        if len(label) > MAX_LABEL_LEN:
            raise ValueError(f"按鈕文字超過 {MAX_LABEL_LEN} 字：{label}")
        if not (1 <= count <= 500) or not album:
            raise ValueError(f"預設參數不正確：{p}")
        items.append((label, f"一鍵上傳 {person_id} {count} {album}"))
    return items


def build_payload(user_id, message, presets=()):
    buttons = [(label, label) for label in QUICK_REPLY_LABELS] + list(presets)
    if len(buttons) > MAX_ITEMS:
        raise ValueError(f"按鈕超過 {MAX_ITEMS} 個")
    return {
        "to": user_id,
        "messages": [{
            "type": "text",
            "text": message,
            "quickReply": {
                "items": [
                    {"type": "action", "action": {"type": "message", "label": label, "text": text}}
                    for label, text in buttons
                ]
            },
        }],
    }


def main():
    if not logging.getLogger().handlers:
        logging.basicConfig(stream=sys.stdout, level=logging.INFO)
    token = os.getenv("LINE_CHANNEL_ACCESS_TOKEN")
    push_url = os.getenv("LINE_PUSH_URL") or DEFAULT_PUSH_URL
    message = os.getenv("LINE_REMINDER_MESSAGE") or DEFAULT_MESSAGE
    dry_run = os.getenv("DRY_RUN") == "1"
    user_ids = load_user_ids()
    presets = load_presets()  # 設定錯誤時直接失敗（run_tool.py 會發 Telegram）

    if not token and not dry_run:
        sys.exit("LINE_CHANNEL_ACCESS_TOKEN 未設定")
    if not user_ids:
        sys.exit("沒有收件人（LINE_REMINDER_USER_IDS / LINE_REMINDER_USER_IDS_FILE）")

    headers = {"Content-Type": "application/json", "Authorization": f"Bearer {token}"}
    failed = []
    for uid in user_ids:
        payload = build_payload(uid, message, presets)
        if dry_run:
            shown = dict(payload, to=mask(uid))
            log.info("[DRY_RUN] %s", json.dumps(shown, ensure_ascii=False))
            continue
        try:
            r = requests.post(push_url, headers=headers, json=payload, timeout=15)
            ok = r.status_code == 200
            # LINE 的錯誤訊息不含 token，可以記錄
            log.info("push %s → %s %s", mask(uid), r.status_code, "" if ok else r.text[:300])
        except requests.RequestException as e:
            ok = False
            log.error("push %s 失敗：%s", mask(uid), e)
        if not ok:
            failed.append(uid)

    log.info("完成：%d 位收件人，失敗 %d 位", len(user_ids), len(failed))
    if failed:
        sys.exit(1)


if __name__ == "__main__":
    main()
