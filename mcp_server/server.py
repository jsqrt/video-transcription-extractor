"""MCP server exposing the ``transcribe_media`` tool over stdio.

Import is deferred so the rest of the package (and the smoke tests) can be
used without the optional ``mcp`` dependency. Install it with::

    pip install "mcp>=1.0.0"

Run the server::

    python -m mcp_server
"""

from __future__ import annotations

import asyncio
from typing import Any, Optional

from mcp_server.adapter import (
    AdapterError,
    PipelineAdapter,
    TranscribeArguments,
    TranscribeResponse,
)


SERVER_NAME = "video-transcription-extractor"
SERVER_INSTRUCTIONS = (
    "Transcribe local audio or video files with a Whisper + optional Ollama "
    "pipeline. The 'transcribe_media' tool returns on-disk paths to two "
    "artefacts: transcript_path (<stem>.transcription.md — cleaned, with chapters) "
    "and summary_path (<stem>.summary.md — overview + key facts + intents "
    "+ per-chapter). Either can be disabled via write_clean=false or "
    "summary_mode='none'. Progress is reported via MCP progress "
    "notifications when the client supplies a progress token."
)


def build_server(adapter: Optional[PipelineAdapter] = None):
    """Construct a ``FastMCP`` server with the ``transcribe_media`` tool.

    Returns the server object. Callers are expected to invoke ``.run()`` on
    it. Factored out of ``serve()`` so it can be exercised from automated
    tests that monkey-patch the adapter.
    """

    try:
        from mcp.server.fastmcp import Context, FastMCP
    except ImportError as exc:  # pragma: no cover - depends on env
        raise RuntimeError(
            "The 'mcp' Python SDK is required to run the MCP server. "
            "Install it with: pip install 'mcp>=1.0.0'"
        ) from exc

    adapter = adapter or PipelineAdapter()
    server = FastMCP(name=SERVER_NAME, instructions=SERVER_INSTRUCTIONS)

    @server.tool(
        name="transcribe_media",
        description=(
            "Transcribe a local audio or video file. Produces up to two "
            "files: <stem>.transcription.md (cleaned with chapters) and "
            "<stem>.summary.md (when summary_mode is 'ollama' or "
            "'extractive'). file_path must be absolute and point to a "
            "supported format."
        ),
    )
    async def transcribe_media(
        file_path: str,
        output_dir: Optional[str] = None,
        summary_mode: str = "ollama",
        chapters: bool = True,
        language: Optional[str] = None,
        profile: str = "best",
        model: Optional[str] = None,
        title_style: str = "keywords",
        timeout_sec: int = 0,
        clean_mode: str = "rule-based",
        write_clean: bool = True,
        ctx: Context = None,  # type: ignore[assignment]
    ) -> dict[str, Any]:
        args = TranscribeArguments(
            file_path=file_path,
            output_dir=output_dir,
            summary_mode=summary_mode,  # type: ignore[arg-type]
            chapters=chapters,
            language=language,
            profile=profile,
            model=model,
            title_style=title_style,
            timeout_sec=timeout_sec,
            clean_mode=clean_mode,  # type: ignore[arg-type]
            write_clean=write_clean,
        )

        loop = asyncio.get_running_loop()
        last_reported = [-1.0]

        def _log(msg: str) -> None:
            if ctx is None:
                return
            # Schedule on the main loop; ctx.info is a coroutine.
            asyncio.run_coroutine_threadsafe(ctx.info(msg), loop)

        def _progress(fraction: float) -> None:
            if ctx is None:
                return
            # De-duplicate and avoid flooding the client — only forward full
            # percentage-point changes.
            current = round(max(0.0, min(1.0, fraction)) * 100)
            if current <= last_reported[0]:
                return
            last_reported[0] = current
            asyncio.run_coroutine_threadsafe(
                ctx.report_progress(current, 100), loop
            )

        # Wire the adapter's logger through ctx.info so the MCP client gets
        # the same high-level breadcrumbs the CLI prints.
        adapter_with_logger = _rebind_logger(adapter, _log)

        try:
            response: TranscribeResponse = await asyncio.to_thread(
                adapter_with_logger.transcribe, args, _progress
            )
        except AdapterError as exc:
            # Return a structured error payload instead of raising so the
            # client can inspect ``error.code`` without parsing exception text.
            return {"ok": False, "error": exc.as_dict()}

        return {"ok": True, "result": response.as_dict()}

    return server


def _rebind_logger(
    adapter: PipelineAdapter, logger_fn
) -> PipelineAdapter:
    """Return a shallow copy of the adapter with its logger replaced."""
    # The adapter stores private attributes; the safest way to swap just the
    # logger is to construct a new adapter that reuses the existing factories.
    return PipelineAdapter(
        pipeline_fn=adapter._pipeline_fn,  # noqa: SLF001
        extractor_factory=adapter._extractor_factory,  # noqa: SLF001
        transcriber_factory=adapter._transcriber_factory,  # noqa: SLF001
        summarizer_factory=adapter._summarizer_factory,  # noqa: SLF001
        clean_writer_factory=adapter._clean_writer_factory,  # noqa: SLF001
        summary_writer_factory=adapter._summary_writer_factory,  # noqa: SLF001
        allowed_extensions=adapter._allowed_extensions,  # noqa: SLF001
        logger_fn=logger_fn,
    )


def serve() -> None:
    """Entry point used by ``python -m mcp_server``."""
    server = build_server()
    server.run()
