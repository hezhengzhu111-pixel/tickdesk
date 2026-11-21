ALTER TABLE provider_health RENAME TO provider_health_v10;
CREATE TABLE provider_health (
    name TEXT PRIMARY KEY CHECK(name IN ('deepseek','vision','mcp')),
    status TEXT NOT NULL CHECK(status IN ('unknown','online','degraded','offline')),
    checked_at INTEGER NOT NULL,
    error_code TEXT
);
INSERT INTO provider_health SELECT * FROM provider_health_v10;
DROP TABLE provider_health_v10;
