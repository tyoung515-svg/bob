from __future__ import annotations


class MemoryError(Exception):
    """Base exception for all memory module errors."""


class MemoryConfigError(MemoryError):
    def __init__(self, detail: str) -> None:
        self.detail = detail
        super().__init__(detail)


class HashingError(MemoryError):
    def __init__(self, detail: str) -> None:
        self.detail = detail
        super().__init__(detail)


class HashAllowlistMissing(HashingError):
    def __init__(self, generation_method: str) -> None:
        self.generation_method = generation_method
        super().__init__(
            f"no allowlist registered for generation_method={generation_method!r}"
        )


class L0AppendFailed(MemoryError):
    def __init__(self, event_id: str, detail: str) -> None:
        self.event_id = event_id
        self.detail = detail
        super().__init__(f"L0 append failed for event={event_id!r}: {detail}")


class L1ValidationFailed(MemoryError):
    def __init__(self, fact_id: str, schema_errors: list[str]) -> None:
        self.fact_id = fact_id
        self.schema_errors = schema_errors
        super().__init__(
            f"L1 validation failed for fact={fact_id!r}: {'; '.join(schema_errors)}"
        )


class SpliceFailed(MemoryError):
    def __init__(self, section_id: str, detail: str) -> None:
        self.section_id = section_id
        self.detail = detail
        super().__init__(f"splice failed for section={section_id!r}: {detail}")


class RenderFailed(MemoryError):
    def __init__(self, section_id: str, detail: str) -> None:
        self.section_id = section_id
        self.detail = detail
        super().__init__(f"render failed for section={section_id!r}: {detail}")


class EmbedderUnavailable(MemoryError):
    def __init__(self, endpoint: str, detail: str) -> None:
        self.endpoint = endpoint
        self.detail = detail
        super().__init__(f"embedder unavailable at {endpoint!r}: {detail}")


class RetrievalProviderError(MemoryError):
    def __init__(self, provider_id: str, detail: str) -> None:
        self.provider_id = provider_id
        self.detail = detail
        super().__init__(f"retrieval provider {provider_id!r}: {detail}")


class SlotDeferred(MemoryError):
    def __init__(self, slot_name: str) -> None:
        self.slot_name = slot_name
        super().__init__(
            f"slot {slot_name!r} is declared but not active in Phase 1"
        )


class SlotMisconfigured(MemoryError):
    def __init__(self, slot_name: str, detail: str) -> None:
        self.slot_name = slot_name
        self.detail = detail
        super().__init__(f"slot {slot_name!r} misconfigured: {detail}")


class HopBudgetExceeded(MemoryError):
    def __init__(self, requested: int, budget: int) -> None:
        self.requested = requested
        self.budget = budget
        super().__init__(f"hop budget exceeded: requested {requested}, max {budget}")


class ACLViolation(MemoryError):
    def __init__(self, resource: str, detail: str) -> None:
        self.resource = resource
        self.detail = detail
        super().__init__(f"ACL violation for {resource!r}: {detail}")


class TruthMaintenanceError(MemoryError):
    """Truth-maintenance pipeline failed to update or validate confidence."""


class SchemaEvolutionError(MemoryError):
    """Schema upcaster failed or is incomplete (partial function)."""


class AttestationError(MemoryError):
    """Attestation envelope validation failed."""
