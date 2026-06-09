"""MCP server package.

The package exposes:

* :class:`PipelineAdapter` — a thin, import-safe wrapper around
  :func:`app.services.pipeline.run_pipeline` that validates inputs and shapes
  the response the way MCP tool callers expect.
* :func:`build_server` / :func:`serve` — only available when the optional
  ``mcp`` dependency is installed. The smoke tests in this project exercise
  :class:`PipelineAdapter` directly so MCP remains an optional runtime dep.
"""

from __future__ import annotations

from mcp_server.adapter import (
    AdapterError,
    PipelineAdapter,
    TranscribeArguments,
    TranscribeResponse,
)

__all__ = [
    "AdapterError",
    "PipelineAdapter",
    "TranscribeArguments",
    "TranscribeResponse",
]
