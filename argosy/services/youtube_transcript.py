"""Fetch and normalize captions for one YouTube video.

The lightweight ``youtube-transcript-api`` path is preferred.  ``yt-dlp`` is
the fallback because YouTube serves captions through several APIs and either
client can temporarily stop working while the other still succeeds.  Both
dependencies are imported lazily so importing Argosy never performs network
work or requires either package in minimal environments.
"""

from __future__ import annotations

import html
import re
import tempfile
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal
from urllib.parse import parse_qs, urlparse

TranscriptBackend = Literal["auto", "youtube-transcript-api", "yt-dlp"]

_VIDEO_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")
_YOUTUBE_HOSTS = {
    "youtube.com",
    "www.youtube.com",
    "m.youtube.com",
    "music.youtube.com",
    "youtube-nocookie.com",
    "www.youtube-nocookie.com",
    "youtu.be",
    "www.youtu.be",
}
_TIMESTAMP_RE = re.compile(
    r"^(?P<start>(?:\d{2}:)?\d{2}:\d{2}[.,]\d{3})\s+-->\s+"
    r"(?P<end>(?:\d{2}:)?\d{2}:\d{2}[.,]\d{3})(?:\s+.*)?$"
)
_TAG_RE = re.compile(r"<[^>]+>")


class YouTubeTranscriptError(RuntimeError):
    """The reference was invalid or no requested captions could be fetched."""


@dataclass(frozen=True)
class TranscriptSegment:
    text: str
    start: float
    duration: float


@dataclass(frozen=True)
class YouTubeTranscript:
    video_id: str
    url: str
    language: str
    language_code: str
    is_generated: bool | None
    backend: str
    segments: tuple[TranscriptSegment, ...]
    title: str | None = None
    channel: str | None = None
    duration_seconds: float | None = None
    raw_captions: str | None = None
    raw_caption_bytes: bytes | None = None

    @property
    def text(self) -> str:
        return "\n".join(segment.text for segment in self.segments).strip()

    def timestamped_text(self) -> str:
        return "\n".join(
            f"[{format_timestamp(segment.start)}] {segment.text}"
            for segment in self.segments
        ).strip()

    def agent_text(self, *, interval_seconds: int = 30) -> str:
        """Compact captions into timestamped paragraphs for an LLM prompt."""
        if not self.segments:
            return ""
        chunks: list[str] = []
        words: list[str] = []
        chunk_start = self.segments[0].start
        next_boundary = chunk_start + interval_seconds
        for segment in self.segments:
            if words and segment.start >= next_boundary:
                chunks.append(f"[{format_timestamp(chunk_start)}] {' '.join(words)}")
                words = []
                chunk_start = segment.start
                next_boundary = chunk_start + interval_seconds
            words.append(segment.text.strip())
        if words:
            chunks.append(f"[{format_timestamp(chunk_start)}] {' '.join(words)}")
        return "\n".join(chunks)

    def to_markdown(self) -> str:
        generated = (
            "unknown" if self.is_generated is None else str(self.is_generated).lower()
        )
        metadata = [
            "# YouTube transcript",
            "",
            f"- Video: {self.title or self.video_id}",
            f"- URL: {self.url}",
            f"- Video ID: {self.video_id}",
            f"- Language: {self.language} ({self.language_code})",
            f"- Automatically generated: {generated}",
            f"- Retrieval backend: {self.backend}",
        ]
        if self.channel:
            metadata.append(f"- Channel: {self.channel}")
        if self.duration_seconds is not None:
            metadata.append(f"- Duration: {format_timestamp(self.duration_seconds)}")
        return "\n".join(metadata) + "\n\n## Transcript\n\n" + self.timestamped_text() + "\n"


def extract_video_id(reference: str) -> str:
    """Return an 11-character video id from an id or supported YouTube URL."""
    candidate = (reference or "").strip()
    if _VIDEO_ID_RE.fullmatch(candidate):
        return candidate

    parsed = urlparse(candidate if "://" in candidate else f"https://{candidate}")
    host = (parsed.hostname or "").lower()
    if host not in _YOUTUBE_HOSTS:
        raise YouTubeTranscriptError(
            "Expected a YouTube URL or an 11-character video ID."
        )

    video_id = ""
    parts = [part for part in parsed.path.split("/") if part]
    if host in {"youtu.be", "www.youtu.be"} and parts:
        video_id = parts[0]
    elif parsed.path.rstrip("/") == "/watch":
        video_id = (parse_qs(parsed.query).get("v") or [""])[0]
    elif len(parts) >= 2 and parts[0] in {"embed", "shorts", "live"}:
        video_id = parts[1]

    if not _VIDEO_ID_RE.fullmatch(video_id):
        raise YouTubeTranscriptError(
            "Could not find a valid video ID in the YouTube URL."
        )
    return video_id


def canonical_url(video_id: str) -> str:
    return f"https://www.youtube.com/watch?v={video_id}"


def format_timestamp(seconds: float) -> str:
    total = max(0, int(seconds))
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}"


def fetch_transcript(
    reference: str,
    *,
    languages: Sequence[str] = ("en",),
    backend: TranscriptBackend = "auto",
    api_fetcher: Callable[[str, Sequence[str]], YouTubeTranscript] | None = None,
    ytdlp_fetcher: Callable[[str, Sequence[str]], YouTubeTranscript] | None = None,
) -> YouTubeTranscript:
    """Fetch captions, using yt-dlp only when the preferred client fails."""
    if backend not in {"auto", "youtube-transcript-api", "yt-dlp"}:
        raise YouTubeTranscriptError(f"Unknown transcript backend: {backend!r}")
    video_id = extract_video_id(reference)
    normalized_languages = tuple(
        language.strip() for language in languages if language and language.strip()
    )
    if not normalized_languages:
        raise YouTubeTranscriptError("At least one caption language is required.")

    primary = api_fetcher or _fetch_with_transcript_api
    fallback = ytdlp_fetcher or _fetch_with_ytdlp
    errors: list[str] = []
    if backend in {"auto", "youtube-transcript-api"}:
        try:
            return primary(video_id, normalized_languages)
        except Exception as exc:  # noqa: BLE001 - fallback aggregates client failures
            errors.append(f"youtube-transcript-api: {_short_error(exc)}")
            if backend == "youtube-transcript-api":
                raise YouTubeTranscriptError(errors[-1]) from exc
    if backend in {"auto", "yt-dlp"}:
        try:
            return fallback(video_id, normalized_languages)
        except Exception as exc:  # noqa: BLE001 - normalize third-party errors
            errors.append(f"yt-dlp: {_short_error(exc)}")
            raise YouTubeTranscriptError(
                "Unable to fetch requested captions. " + " | ".join(errors)
            ) from exc
    raise AssertionError("unreachable")


def _fetch_with_transcript_api(
    video_id: str, languages: Sequence[str]
) -> YouTubeTranscript:
    from youtube_transcript_api import YouTubeTranscriptApi

    api = YouTubeTranscriptApi()
    transcript = api.list(video_id).find_transcript(list(languages))
    fetched = transcript.fetch()
    segments = tuple(
        TranscriptSegment(
            text=_clean_caption_text(snippet.text),
            start=float(snippet.start),
            duration=float(snippet.duration),
        )
        for snippet in fetched
        if _clean_caption_text(snippet.text)
    )
    if not segments:
        raise YouTubeTranscriptError("YouTube returned an empty transcript.")
    return YouTubeTranscript(
        video_id=video_id,
        url=canonical_url(video_id),
        language=transcript.language,
        language_code=transcript.language_code,
        is_generated=bool(transcript.is_generated),
        backend="youtube-transcript-api",
        segments=segments,
    )


def _fetch_with_ytdlp(video_id: str, languages: Sequence[str]) -> YouTubeTranscript:
    from yt_dlp import YoutubeDL

    url = canonical_url(video_id)
    with tempfile.TemporaryDirectory(prefix="argosy-youtube-") as temp_name:
        temp_dir = Path(temp_name)
        options = {
            "skip_download": True,
            "writesubtitles": True,
            "writeautomaticsub": True,
            "subtitleslangs": list(languages),
            "subtitlesformat": "vtt/best",
            "outtmpl": str(temp_dir / "%(id)s.%(ext)s"),
            "noplaylist": True,
            "quiet": True,
            "no_warnings": True,
            "noprogress": True,
        }
        with YoutubeDL(options) as downloader:
            info = downloader.extract_info(url, download=True)

        caption_files = sorted(temp_dir.glob(f"{video_id}*.vtt"))
        if not caption_files:
            raise YouTubeTranscriptError(
                f"yt-dlp did not download captions for languages {list(languages)!r}."
            )
        caption_path = _pick_caption_file(caption_files, languages)
        raw_bytes = caption_path.read_bytes()
        raw = raw_bytes.decode("utf-8", errors="replace")
        segments = tuple(_dedupe_rolling_segments(parse_vtt(raw)))
        if not segments:
            raise YouTubeTranscriptError("yt-dlp downloaded a VTT file with no cues.")
        language_code = _language_from_caption_name(caption_path, video_id)
        return YouTubeTranscript(
            video_id=video_id,
            url=url,
            language=language_code,
            language_code=language_code,
            is_generated=_caption_is_generated(info, language_code),
            backend="yt-dlp",
            segments=segments,
            title=str(info.get("title") or "") or None,
            channel=str(info.get("channel") or info.get("uploader") or "") or None,
            duration_seconds=_float_or_none(info.get("duration")),
            raw_captions=raw,
            raw_caption_bytes=raw_bytes,
        )


def parse_vtt(raw: str) -> list[TranscriptSegment]:
    """Parse the cue text needed for analysis without adding another dependency."""
    lines = raw.replace("\ufeff", "").replace("\r\n", "\n").split("\n")
    segments: list[TranscriptSegment] = []
    index = 0
    while index < len(lines):
        match = _TIMESTAMP_RE.match(lines[index].strip())
        if match is None:
            index += 1
            continue
        start = _parse_vtt_timestamp(match.group("start"))
        end = _parse_vtt_timestamp(match.group("end"))
        index += 1
        cue_lines: list[str] = []
        while index < len(lines) and lines[index].strip():
            cue_lines.append(lines[index].strip())
            index += 1
        text = _clean_caption_text(" ".join(cue_lines))
        if text:
            segments.append(
                TranscriptSegment(text=text, start=start, duration=max(0.0, end - start))
            )
    return segments


def _parse_vtt_timestamp(value: str) -> float:
    fields = value.replace(",", ".").split(":")
    if len(fields) == 2:
        hours = "0"
        minutes, seconds = fields
    elif len(fields) == 3:
        hours, minutes, seconds = fields
    else:
        raise ValueError(f"Invalid VTT timestamp: {value!r}")
    return int(hours) * 3600 + int(minutes) * 60 + float(seconds)


def _clean_caption_text(value: str) -> str:
    without_tags = _TAG_RE.sub("", value)
    return " ".join(html.unescape(without_tags).split())


def _pick_caption_file(files: Sequence[Path], languages: Sequence[str]) -> Path:
    for language in languages:
        marker = f".{language}."
        for path in files:
            if marker.lower() in path.name.lower():
                return path
    return files[0]


def _dedupe_rolling_segments(
    segments: Sequence[TranscriptSegment],
) -> list[TranscriptSegment]:
    """Remove the rolling-word repetition in YouTube auto-caption VTT cues."""
    cleaned: list[TranscriptSegment] = []
    previous_words: list[str] = []
    for segment in segments:
        current_words = segment.text.split()
        if not current_words:
            continue
        emit_words = current_words
        if previous_words == current_words:
            emit_words = []
        elif (
            len(current_words) <= len(previous_words)
            and previous_words[: len(current_words)] == current_words
        ):
            emit_words = []
        elif (
            len(previous_words) <= len(current_words)
            and current_words[: len(previous_words)] == previous_words
        ):
            emit_words = current_words[len(previous_words) :]
        else:
            max_overlap = min(len(previous_words), len(current_words))
            overlap = 0
            for size in range(max_overlap, 1, -1):
                if previous_words[-size:] == current_words[:size]:
                    overlap = size
                    break
            if overlap:
                emit_words = current_words[overlap:]
        if emit_words:
            cleaned.append(
                TranscriptSegment(
                    text=" ".join(emit_words),
                    start=segment.start,
                    duration=segment.duration,
                )
            )
        previous_words = current_words
    return cleaned


def _language_from_caption_name(path: Path, video_id: str) -> str:
    name = path.name
    prefix = f"{video_id}."
    if name.startswith(prefix) and name.endswith(".vtt"):
        return name[len(prefix) : -4]
    return "unknown"


def _caption_is_generated(info: dict, language_code: str) -> bool | None:
    subtitles = info.get("subtitles") or {}
    automatic = info.get("automatic_captions") or {}
    if language_code in subtitles:
        return False
    if language_code in automatic:
        return True
    return None


def _float_or_none(value: object) -> float | None:
    try:
        return float(str(value)) if value is not None else None
    except (TypeError, ValueError):
        return None


def _short_error(exc: Exception) -> str:
    text = " ".join(str(exc).split())
    return (text or type(exc).__name__)[:500]


__all__ = [
    "TranscriptBackend",
    "TranscriptSegment",
    "YouTubeTranscript",
    "YouTubeTranscriptError",
    "canonical_url",
    "extract_video_id",
    "fetch_transcript",
    "format_timestamp",
    "parse_vtt",
]
