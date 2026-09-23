"""Bounded subprocess parser for public research bytes (no network access code)."""
from __future__ import annotations

import io
import json
import os
import sys

MAX_BYTES = 8_000_000
MAX_TEXT = 20_000
MAX_PAGES = 20
MEMORY_BYTES = 512 * 1024 * 1024
_job_handle = None


def limit_memory():
    """Fail closed if the OS cannot establish the parser's memory ceiling."""
    global _job_handle
    if os.name != "nt":
        import resource
        resource.setrlimit(resource.RLIMIT_AS, (MEMORY_BYTES, MEMORY_BYTES))
        resource.setrlimit(resource.RLIMIT_CPU, (20, 20))
        return
    import ctypes
    from ctypes import wintypes

    class Basic(ctypes.Structure):
        _fields_ = [("process_time", ctypes.c_longlong), ("job_time", ctypes.c_longlong),
                    ("flags", wintypes.DWORD), ("min_ws", ctypes.c_size_t),
                    ("max_ws", ctypes.c_size_t), ("active", wintypes.DWORD),
                    ("affinity", ctypes.c_size_t), ("priority", wintypes.DWORD),
                    ("scheduling", wintypes.DWORD)]

    class IO(ctypes.Structure):
        _fields_ = [(name, ctypes.c_ulonglong) for name in
                    ("read_ops", "write_ops", "other_ops", "read_bytes", "write_bytes", "other_bytes")]

    class Extended(ctypes.Structure):
        _fields_ = [("basic", Basic), ("io", IO), ("process_memory", ctypes.c_size_t),
                    ("job_memory", ctypes.c_size_t), ("peak_process", ctypes.c_size_t),
                    ("peak_job", ctypes.c_size_t)]

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
    kernel.CreateJobObjectW.restype = wintypes.HANDLE
    kernel.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
    kernel.SetInformationJobObject.restype = wintypes.BOOL
    kernel.GetCurrentProcess.restype = wintypes.HANDLE
    kernel.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    kernel.AssignProcessToJobObject.restype = wintypes.BOOL
    _job_handle = kernel.CreateJobObjectW(None, None)
    limits = Extended()
    limits.basic.flags = 0x100 | 0x2000  # process memory + kill on job close
    limits.process_memory = MEMORY_BYTES
    if (not _job_handle or not kernel.SetInformationJobObject(
            _job_handle, 9, ctypes.byref(limits), ctypes.sizeof(limits))
            or not kernel.AssignProcessToJobObject(_job_handle, kernel.GetCurrentProcess())):
        raise OSError(ctypes.get_last_error(), "Cannot isolate document parser memory")


def extract(raw: bytes, content_type: str) -> dict:
    from argosy.services.domain_sources import extract_text

    if len(raw) > MAX_BYTES:
        raise ValueError("Document exceeds byte budget")
    pages, more_pages = [], False
    if raw.startswith(b"%PDF-"):
        from pypdf import PdfReader
        reader = PdfReader(io.BytesIO(raw))
        page_count = len(reader.pages)
        pages = list(range(1, min(MAX_PAGES, page_count) + 1))
        more_pages = page_count > MAX_PAGES
        parts = []
        used = 0
        has_text = False
        for number in pages:
            text = reader.pages[number - 1].extract_text() or ""
            has_text = has_text or bool(text.strip())
            part = f"[PDF page {number}]\n{text}"
            parts.append(part[:MAX_TEXT + 1 - used])
            used += len(parts[-1])
            if used > MAX_TEXT:
                pages = list(range(1, number + 1))
                more_pages = number < page_count
                break
        text = "\n".join(parts)
        if not has_text:
            raise ValueError("No extractable PDF text; OCR needed")
    else:
        text = extract_text(raw, content_type)
    if not text.strip():
        raise ValueError("Document has no extractable text")
    return {"text": text[:MAX_TEXT], "inspected_pdf_pages": pages,
            "more_pdf_pages": more_pages, "text_truncated": len(text) > MAX_TEXT}


def main():
    try:
        limit_memory()
        result = extract(sys.stdin.buffer.read(MAX_BYTES + 1), sys.argv[1])
    except Exception as exc:
        result = {"error": f"{type(exc).__name__}: {exc}"[:500]}
    encoded = json.dumps(result, ensure_ascii=True).encode("ascii")
    if len(encoded) > 120_000 and "text" in result:
        # JSON escapes astral/non-ASCII text; bound bytes as well as characters.
        text, lower, upper = result["text"], 0, len(result["text"])
        result["text_truncated"] = True
        while lower < upper:
            middle = (lower + upper + 1) // 2
            candidate = {**result, "text": text[:middle]}
            if len(json.dumps(candidate, ensure_ascii=True).encode("ascii")) <= 120_000:
                lower = middle
            else:
                upper = middle - 1
        result["text"] = text[:lower]
        encoded = json.dumps(result, ensure_ascii=True).encode("ascii")
    sys.stdout.buffer.write(encoded)


if __name__ == "__main__":
    main()
