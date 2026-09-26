CREATE INDEX IF NOT EXISTS idx_revoked_jti_expires_at
ON revoked_jti (expires_at);
