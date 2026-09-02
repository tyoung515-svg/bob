"""T1 — dynamic-adaptive MCP tool-server (MS#4). Distinct from core/mcp/ (the publish-Bob
server): the data-driven successor to _select_face — telemetry-first, then chapters + shortcut
profiles over an LKS-indexed tool corpus. Slice 1: telemetry ([B]) + the flat search_tools ([C]).
"""
from core.mcp_tools.telemetry import (  # noqa: F401
    ToolOutcome,
    ToolTrace,
    TraceSink,
    record_tool_trace,
)
from core.mcp_tools.search import (  # noqa: F401
    ToolDescriptor,
    rank_with_confidence,
    search_tools,
)
from core.mcp_tools.chapters import (  # noqa: F401
    Chapter,
    chapter_retrieve,
    coarse_to_fine,
)
from core.mcp_tools.profiles import (  # noqa: F401
    ProfileStatus,
    ShortcutProfile,
    Vote,
    check_schema_drift,
    consensus_ok,
    promote,
    seed_probationary,
)
from core.mcp_tools.reliability import ProfileCache, best_class_for  # noqa: F401
