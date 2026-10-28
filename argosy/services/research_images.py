"""Read only this post's hash-bound image evidence from its cataloged archive."""
from contextlib import contextmanager
import hashlib
import io
import json
from pathlib import Path
import re
import tempfile
from zipfile import ZipFile

from PIL import Image

from argosy.services.alpha_capture import MAX_TOTAL, MAX_ASSET, bounded_read
from argosy.services.research_catalog import research_session
from argosy.services.turn_attachments import Attachment
from argosy.state.models import UserFile


@contextmanager
def captured_images(item):
    capture = json.loads(item.analysis_json or "{}").get("capture") or {}
    count = int(capture.get("images") or 0)
    if not count:
        yield [], {"status": "not_applicable", "count": 0}
        return
    hashes = capture.get("image_sha256")
    if hashes is None:
        # Backward compatibility: original import retained exact hashes in body.
        marker = re.search(r"\[Archived image evidence: ([^\]]+); not interpreted\]", item.body)
        hashes = re.findall(r"\b[0-9a-f]{64}\b", marker.group(1)) if marker else []
    if len(hashes) != count or any(not re.fullmatch(r"[0-9a-f]{64}", h) for h in hashes):
        raise ValueError("Post image manifest is missing or does not match captured count")
    with research_session() as session:
        row = session.get(UserFile, capture.get("catalog_file_id"))
        if row is None or row.user_id != item.user_id or row.deleted_at is not None:
            raise ValueError("Captured evidence archive is unavailable for this user")
        raw = bounded_read(Path(row.storage_path), MAX_TOTAL)
        if hashlib.sha256(raw).hexdigest() != row.sha256:
            raise ValueError("Captured evidence archive failed its content hash")
    found = {}
    with ZipFile(io.BytesIO(raw)) as archive:
        members = archive.infolist()
        if len(members) > 205 or sum(m.file_size for m in members) > MAX_TOTAL:
            raise ValueError("Captured evidence archive exceeds extraction bounds")
        for member in members:
            if member.is_dir() or member.file_size > MAX_ASSET:
                continue
            if Path(member.filename).suffix.lower() not in {".png", ".jpg", ".jpeg", ".webp", ".gif"}:
                continue
            data = archive.read(member)
            digest = hashlib.sha256(data).hexdigest()
            if digest in hashes:
                with Image.open(io.BytesIO(data)) as decoded:
                    if decoded.width * decoded.height > 40_000_000:
                        raise ValueError("Captured image exceeds decoded pixel bound")
                    mime = Image.MIME[decoded.format]
                    decoded.verify()
                found[digest] = data, mime
    if set(found) != set(hashes):
        raise ValueError("Not all of this post's image hashes exist in the cataloged archive")
    if sum(len(found[h][0]) for h in hashes) > 4_000_000:
        raise ValueError("Post images exceed the 4 MB review packet; explicit split review required")
    # Never extract archive-controlled paths, scripts or sibling posts.
    with tempfile.TemporaryDirectory(prefix="argosy-research-images-") as directory:
        attachments = []
        for index, digest in enumerate(hashes):
            data, mime = found[digest]
            path = Path(directory) / f"{index}-{digest}.image"
            path.write_bytes(data)
            attachments.append(Attachment(kind="image", path=str(path), mime_type=mime,
                original_name=f"image-{index+1}-{digest[:12]}", size_bytes=len(data)))
        yield attachments, {"status": "supplied", "count": len(attachments),
                            "sha256": hashes, "catalog_file_id": capture["catalog_file_id"]}
