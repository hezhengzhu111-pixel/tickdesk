CREATE TABLE recovery_original_reviews (
    session_id TEXT NOT NULL,
    file_id TEXT NOT NULL REFERENCES files(file_id),
    decision TEXT CHECK(decision IN ('keep','missing','deleted')),
    decided_at INTEGER,
    PRIMARY KEY(session_id,file_id)
);
CREATE TABLE kernel_probe (
    singleton INTEGER PRIMARY KEY CHECK(singleton=1),
    checked_at INTEGER NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('offline','auth_required','degraded','available')),
    version TEXT,
    usable_capabilities TEXT NOT NULL,
    strategy_ids TEXT NOT NULL,
    strategy_load_errors INTEGER NOT NULL
);
