-- Gate Router GR-P3-finish: audit trail for what the Gate auto-clears vs flags.
-- `approved_by` records WHO cleared an approval:
--   NULL     = human-required (the default — a human must still decide / decided)
--   'gate'   = auto-cleared by the scope Gate (non-blocking audit row, status='approved')
-- Idempotent so re-applying / fresh init.sql runs both converge.
ALTER TABLE approvals ADD COLUMN IF NOT EXISTS approved_by TEXT;
