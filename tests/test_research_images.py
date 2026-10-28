from contextlib import contextmanager
import hashlib
import io
import json
from pathlib import Path
from types import SimpleNamespace
from zipfile import ZipFile

from PIL import Image
import pytest

from argosy.services import research_images as images


def packet(tmp_path, monkeypatch, *, owner="test", corrupt=False, missing=False):
    buffer = io.BytesIO()
    Image.new("RGB", (2, 2), "red").save(buffer, format="PNG")
    raw = buffer.getvalue()
    digest = hashlib.sha256(raw).hexdigest()
    archive = tmp_path / "capture.zip"
    with ZipFile(archive, "w") as z:
        z.writestr("../../untrusted.png", raw)  # must never extract this name
        z.writestr("other-post.png", b"not an image")
    row = SimpleNamespace(user_id=owner, deleted_at=None, storage_path=str(archive),
        sha256="bad" if corrupt else hashlib.sha256(archive.read_bytes()).hexdigest())
    @contextmanager
    def session():
        yield SimpleNamespace(get=lambda *args: row)
    monkeypatch.setattr(images, "research_session", session)
    return SimpleNamespace(user_id="test", body="", analysis_json=json.dumps({"capture": {
        "images": 1, "image_sha256": ["f"*64 if missing else digest], "catalog_file_id": 1}}))


def test_hash_bound_images_are_supplied_and_temp_files_removed(tmp_path, monkeypatch):
    item = packet(tmp_path, monkeypatch)
    with images.captured_images(item) as (attachments, manifest):
        assert manifest["count"] == 1 and manifest["status"] == "supplied"
        path = Path(attachments[0].path)
        assert path.is_file() and "untrusted" not in str(path)
    assert not path.exists()
    assert not (tmp_path.parent / "untrusted.png").exists()


@pytest.mark.parametrize("kwargs,match", [({"owner": "other"}, "this user"),
    ({"corrupt": True}, "content hash"), ({"missing": True}, "Not all")])
def test_incomplete_or_wrong_owner_images_fail_explicitly(tmp_path, monkeypatch, kwargs, match):
    item = packet(tmp_path, monkeypatch, **kwargs)
    with pytest.raises(ValueError, match=match), images.captured_images(item):
        pass
