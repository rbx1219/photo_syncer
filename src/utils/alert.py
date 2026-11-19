"""通知工具：給管理員的 Telegram 告警、給使用者的 LINE 通知。

兩者都「盡力而為」：一律有 timeout，失敗只寫 log、絕不拋例外，
避免通知本身失敗把正常流程（例如已經上傳成功的批次）拖成失敗。
"""
import logging
import os
import re
import threading
import time

import requests

NOTIFY_TIMEOUT = 10  # 秒

# 同一個 key 的告警在這段時間內只發一次（例如 OAuth 壞掉時，每個 request 都會失敗）
ALERT_DEDUP_SECONDS = 30 * 60
_last_alert = {}
_last_alert_lock = threading.Lock()

_URL_QUERY = re.compile(r"(https?://[^\s?]+)\?\S*")
_SECRET_PARAM = re.compile(r"(?i)(synotoken|token|access_token|key|sid)=[^&\s'\"]+")
_BEARER = re.compile(r"(?i)bearer\s+[\w.\-~+/]+=*")


def scrub(text):
    """去掉 URL query string、token 類參數與 Bearer token，避免把憑證送進 Telegram。"""
    text = str(text)
    text = _URL_QUERY.sub(r"\1?…", text)
    text = _SECRET_PARAM.sub(r"\1=…", text)
    text = _BEARER.sub("Bearer …", text)
    return text


def _should_send(dedup_key):
    if not dedup_key:
        return True
    now = time.time()
    with _last_alert_lock:
        last = _last_alert.get(dedup_key)
        if last is not None and now - last < ALERT_DEDUP_SECONDS:
            return False
        _last_alert[dedup_key] = now
        return True


def notify_admin(message, silent=False, dedup_key=None):
    """發 Telegram 給管理員（經 rbx-prod/telegram-sender）。沒設定 TG_URL 時只寫 log。"""
    message = scrub(message)[:3500]
    if not _should_send(dedup_key):
        logging.info("告警已在 %ss 內發過，略過：%s", ALERT_DEDUP_SECONDS, dedup_key)
        return
    url = os.getenv("TG_URL")
    if not url:
        logging.warning("TG_URL 未設定，告警只寫 log：%s", message)
        return
    try:
        r = requests.post(url, json={"message": message, "silent": silent}, timeout=NOTIFY_TIMEOUT)
        r.raise_for_status()
    except Exception as e:
        logging.warning("Telegram 告警發送失敗：%s", scrub(e))


def notify_line(token, message):
    """透過 line-reply 的 /notify 通知發起上傳的使用者。回傳是否成功。"""
    from config.config import Config  # 延後 import：Config 在 import 時就要求 SERVER_URL
    try:
        r = requests.post(
            f"{Config.LINE_API_URL}/notify",
            json={"message": message, "token": token},
            timeout=NOTIFY_TIMEOUT,
        )
        r.raise_for_status()
        return True
    except Exception as e:
        logging.warning("LINE 通知失敗：%s", scrub(e))
        return False
