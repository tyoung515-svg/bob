-- A Builder may request its first workspace before a project is attached.
ALTER TABLE builder_modes ALTER COLUMN project_id DROP NOT NULL;
ALTER TABLE builder_approvals ALTER COLUMN project_id DROP NOT NULL;
