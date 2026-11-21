"""Bounded, isolated rendering of uploaded PDFs for a model with image inputs."""
from __future__ import annotations

import math
import sys
from pathlib import Path

MAX_IMAGES = 32
MAX_RENDER_BYTES = 32 * 1024 * 1024
MAX_IMAGE_PIXELS = 16_000_000


def validate_images(paths: list[Path]) -> None:
    from PIL import Image

    for path in paths:
        with Image.open(path) as image:
            if (image.format not in {"PNG", "JPEG", "WEBP", "GIF"}
                    or image.width * image.height > MAX_IMAGE_PIXELS
                    or getattr(image, "n_frames", 1) != 1):
                raise ValueError("Unsupported image")
            image.verify()
        with Image.open(path) as image:
            image.load()


def render(pdf: Path, directory: Path, page_limit: int, byte_limit: int) -> None:
    import pypdfium2 as pdfium

    with pdfium.PdfDocument(pdf) as document:
        if not 0 < len(document) <= min(page_limit, MAX_IMAGES):
            raise ValueError("Document exceeds page limit")
        document.init_forms()
        total = 0
        for number, page in enumerate(document, start=1):
            try:
                width, height = page.get_size()
                if not all(math.isfinite(size) and 0 < size <= 3200 for size in (width, height)):
                    raise ValueError("Unsupported PDF page dimensions")
                scale = min(2.25, 3200 / max(width, height))
                bitmap = page.render(scale=scale, draw_annots=True)
                try:
                    output = directory / f"page-{number:04}.jpg"
                    with bitmap.to_pil() as image:
                        image.convert("RGB").save(output, "JPEG", quality=94)
                    total += output.stat().st_size
                    if total > min(byte_limit, MAX_RENDER_BYTES):
                        raise ValueError("Rendered document exceeds attachment budget")
                finally:
                    bitmap.close()
            finally:
                page.close()


if __name__ == "__main__":
    from argosy.services.document_extract_worker import limit_memory

    limit_memory()
    if sys.argv[1] == "--images":
        validate_images([Path(value) for value in sys.argv[2:]])
    else:
        render(Path(sys.argv[1]), Path(sys.argv[2]), int(sys.argv[3]), int(sys.argv[4]))
