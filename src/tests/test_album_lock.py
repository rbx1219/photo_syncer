import threading
import time
from unittest.mock import patch

import service.upload_service as us


def setup_function():
    us._album_uploads.clear()


def test_second_claim_same_album_is_rejected_until_released():
    assert us.try_claim_album("Google Nest Hub")
    assert not us.try_claim_album("Google Nest Hub")
    assert us.try_claim_album("天穗")            # 不同相簿互不影響
    us.release_album("Google Nest Hub")
    assert us.try_claim_album("Google Nest Hub")


def test_stale_lock_expires():
    us._album_uploads["A"] = time.time() - us.ALBUM_LOCK_TTL - 1
    assert us.try_claim_album("A")


def test_background_upload_always_releases_even_on_exception():
    assert us.try_claim_album("A")
    with patch.object(us, "_background_upload", side_effect=RuntimeError("boom")):
        try:
            us.background_upload("creds", "auth", "tok", "A", 1, None, 5, 0, 1)
        except RuntimeError:
            pass
    assert us.try_claim_album("A")


def test_only_one_of_many_concurrent_claims_wins():
    wins = []
    threads = [threading.Thread(target=lambda: wins.append(us.try_claim_album("X"))) for _ in range(20)]
    [t.start() for t in threads]; [t.join() for t in threads]
    assert wins.count(True) == 1
