CREATE INDEX IF NOT EXISTS idx_logs_access_scope_ts
ON logs_access (tenant, project_id, ts);
