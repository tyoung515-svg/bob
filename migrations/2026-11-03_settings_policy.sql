-- Policy revisions are distinct from signing-key policy_epoch and credential epoch.
CREATE TABLE IF NOT EXISTS owner_policy_versions (
 lane TEXT NOT NULL, owner TEXT NOT NULL, revision BIGINT NOT NULL,
 policy JSONB NOT NULL, created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
 PRIMARY KEY(lane,owner,revision)
);
CREATE TABLE IF NOT EXISTS owner_policy_heads (
 lane TEXT NOT NULL, owner TEXT NOT NULL, revision BIGINT NOT NULL,
 PRIMARY KEY(lane,owner)
);
