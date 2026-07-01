from __future__ import annotations

from core.ledger.session import ledger_slice
from core.ledger.project import read_ledger_at


class LedgerSession:
    """
    Durable-ledger context object (§2.2). Wraps the ledger-slice and blob-read
    primitives from core.ledger. Reconstructs state from the git ledger, never
    from process memory (§2.1).
    """

    def __init__(
        self,
        repo,
        *,
        ledger_dir: str = "ledger",
        events_path: str = "ledger/events.jsonl",
    ) -> None:
        self.repo = repo
        self.ledger_dir = ledger_dir
        self.events_path = events_path

    def slice(self, commit_range: str) -> dict:
        """
        Return the ledger_slice dict for *commit_range*.
        Delegates directly to the locked primitive.
        """
        return ledger_slice(
            self.repo, commit_range, events_path=self.events_path
        )

    def truth_at(self, ref: str = "HEAD") -> dict:
        """
        Return the read_ledger_at dict for *ref*.
        Delegates directly to the locked primitive.
        """
        return read_ledger_at(self.repo, ref, ledger_dir=self.ledger_dir)

    def committed_ids(self, commit_range: str) -> set:
        """
        Durable set of already-committed trajectory ids within *commit_range*.
        Reads only from the ledger (via self.slice), not from process memory.
        """
        return {
            e["id"]
            for e in self.slice(commit_range).get("events", [])
            if isinstance(e, dict) and e.get("id")
        }
