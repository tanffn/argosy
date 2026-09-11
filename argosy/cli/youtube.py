"""``argosy youtube`` - fetch captions or run the read-only research fleet."""

from __future__ import annotations

import asyncio
import json
import sys
from typing import Annotated

import typer

from argosy.agents.errors import AgentRunError, MissingAPIKeyError
from argosy.services.youtube_analysis import (
    analyze_youtube,
    fetch_and_catalog_transcript,
    render_fleet_markdown,
    result_to_dict,
)
from argosy.services.youtube_transcript import TranscriptBackend, YouTubeTranscriptError

app = typer.Typer(no_args_is_help=True)


def _echo_console_safe(value: str, *, err: bool = False) -> None:
    """Print text without failing on a legacy Windows console encoding."""
    stream = sys.stderr if err else sys.stdout
    encoding = getattr(stream, "encoding", None) or "utf-8"
    safe_value = value.encode(encoding, errors="backslashreplace").decode(encoding)
    typer.echo(safe_value, err=err)

LanguageOption = Annotated[
    list[str] | None,
    typer.Option(
        "--language",
        "-l",
        help="Caption language in priority order; repeat for fallbacks (default: en).",
    ),
]


@app.command("fetch")
def fetch_command(
    reference: str = typer.Argument(..., help="YouTube URL or 11-character video ID."),
    language: LanguageOption = None,
    backend: str = typer.Option(
        "auto",
        "--backend",
        help="auto, youtube-transcript-api, or yt-dlp.",
    ),
    user_id: str = typer.Option("ariel", "--user-id"),
    save: bool = typer.Option(
        True, "--save/--no-save", help="Catalog the transcript in Argosy Files."
    ),
    artifact_format: str = typer.Option(
        "markdown",
        "--format",
        help="Catalog/print normalized markdown or the exact yt-dlp vtt file.",
    ),
    stdout: bool = typer.Option(
        False, "--stdout", help="Print the full timestamped transcript."
    ),
    json_output: bool = typer.Option(False, "--json", help="Print machine-readable metadata."),
) -> None:
    """Fetch captions without invoking an LLM."""
    languages = tuple(language or ["en"])
    normalized_format = artifact_format.strip().lower()
    if normalized_format not in {"markdown", "vtt"}:
        raise typer.BadParameter("must be markdown or vtt", param_hint="--format")
    if normalized_format == "vtt" and backend.strip().lower() == "auto":
        backend = "yt-dlp"
    try:
        transcript, cataloged = asyncio.run(
            fetch_and_catalog_transcript(
                reference,
                user_id=user_id,
                languages=languages,
                backend=_backend(backend),
                catalog=save,
                artifact_format=normalized_format,  # type: ignore[arg-type]
            )
        )
    except (YouTubeTranscriptError, ValueError) as exc:
        typer.echo(f"YouTube transcript failed: {exc}", err=True)
        raise typer.Exit(code=2) from exc

    if json_output:
        _echo_console_safe(
            json.dumps(
                {
                    "video_id": transcript.video_id,
                    "url": transcript.url,
                    "title": transcript.title,
                    "channel": transcript.channel,
                    "language": transcript.language,
                    "language_code": transcript.language_code,
                    "is_generated": transcript.is_generated,
                    "backend": transcript.backend,
                    "segments": len(transcript.segments),
                    "file_id": cataloged.id if cataloged else None,
                    "path": cataloged.storage_path if cataloged else None,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
    else:
        typer.echo(
            f"Fetched {len(transcript.segments)} caption segments "
            f"({transcript.language_code}, {transcript.backend})."
        )
        if cataloged:
            typer.echo(f"Catalog file #{cataloged.id}: {cataloged.storage_path}")
        if stdout:
            typer.echo("")
            typer.echo(
                transcript.raw_captions
                if normalized_format == "vtt"
                else transcript.to_markdown()
            )


@app.command("analyze")
def analyze_command(
    reference: str = typer.Argument(..., help="YouTube URL or 11-character video ID."),
    language: LanguageOption = None,
    backend: str = typer.Option(
        "auto",
        "--backend",
        help="auto, youtube-transcript-api, or yt-dlp.",
    ),
    user_id: str = typer.Option("ariel", "--user-id"),
    save: bool = typer.Option(
        True,
        "--save/--no-save",
        help="Catalog transcript, persist fleet telemetry, and save report artifacts.",
    ),
    json_output: bool = typer.Option(False, "--json", help="Print the complete JSON result."),
) -> None:
    """Run claims, skeptic, portfolio, and synthesis agents on the transcript."""
    try:
        result = asyncio.run(
            analyze_youtube(
                reference,
                user_id=user_id,
                languages=tuple(language or ["en"]),
                backend=_backend(backend),
                save=save,
            )
        )
    except (YouTubeTranscriptError, ValueError) as exc:
        typer.echo(f"YouTube transcript failed: {exc}", err=True)
        raise typer.Exit(code=2) from exc
    except MissingAPIKeyError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=3) from exc
    except AgentRunError as exc:
        typer.echo(f"YouTube fleet failed: {exc}", err=True)
        raise typer.Exit(code=4) from exc

    if json_output:
        _echo_console_safe(
            json.dumps(result_to_dict(result), ensure_ascii=False, indent=2)
        )
        return
    _echo_console_safe(render_fleet_markdown(result))
    typer.echo(f"Fleet cost estimate: ${result.cost_usd:.4f}")
    if result.transcript_file:
        typer.echo(
            f"Transcript catalog file #{result.transcript_file.id}: "
            f"{result.transcript_file.storage_path}"
        )
    if result.artifact_path:
        typer.echo(f"Analysis saved: {result.artifact_path}")


def _backend(value: str) -> TranscriptBackend:
    normalized = value.strip().lower()
    if normalized not in {"auto", "youtube-transcript-api", "yt-dlp"}:
        raise typer.BadParameter(
            "must be auto, youtube-transcript-api, or yt-dlp", param_hint="--backend"
        )
    return normalized  # type: ignore[return-value]


__all__ = ["app"]
