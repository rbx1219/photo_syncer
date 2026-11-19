import importlib

import pytest
from flask import Flask


@pytest.fixture
def client(tmp_path, monkeypatch):
    (tmp_path / "13247.jpg").write_bytes(b"\xff\xd8jpeg")
    (tmp_path / "secret.txt").write_text("no")
    monkeypatch.setenv("FACE_IMAGE_DIR", str(tmp_path))
    import controllers.image_controller as ic
    importlib.reload(ic)
    app = Flask(__name__)
    app.register_blueprint(ic.image_bp)
    return app.test_client()


def test_serves_image(client):
    r = client.get("/images/13247.jpg")
    assert r.status_code == 200 and r.data == b"\xff\xd8jpeg"


def test_missing_and_non_image_are_404(client):
    assert client.get("/images/nope.jpg").status_code == 404
    assert client.get("/images/secret.txt").status_code == 404
    assert client.get("/images/../app.py").status_code == 404
    assert client.get("/images/sub/13247.jpg").status_code == 404
