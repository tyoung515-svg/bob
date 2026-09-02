"""W3 mechanical memory writer.

Only T0 (``project_verbatim``) has an implementation.  T1/T2/T3 are explicit,
default-off contracts in :mod:`core.memory.writer.tasks`.
"""

from core.memory.writer.ledger import CompletionLedger
from core.memory.writer.tasks import ProjectResult, ProjectVerbatimTask

__all__ = ["CompletionLedger", "ProjectResult", "ProjectVerbatimTask"]
