CREATE TABLE recovery_records (
    session_id TEXT NOT NULL,
    record_id TEXT NOT NULL,
    source TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    part_count INTEGER NOT NULL,
    evidence_epoch TEXT NOT NULL,
    captured_at INTEGER NOT NULL,
    observed_seq INTEGER,
    status TEXT NOT NULL DEFAULT 'receiving' CHECK(status IN ('receiving','verified','invalid')),
    body TEXT,
    PRIMARY KEY(session_id,record_id)
);
CREATE TABLE recovery_parts (
    session_id TEXT NOT NULL,
    record_id TEXT NOT NULL,
    part_index INTEGER NOT NULL,
    bytes BLOB NOT NULL,
    PRIMARY KEY(session_id,record_id,part_index),
    FOREIGN KEY(session_id,record_id) REFERENCES recovery_records(session_id,record_id)
);
CREATE TABLE recovery_items (
    session_id TEXT NOT NULL,
    item_id TEXT NOT NULL,
    subject TEXT NOT NULL,
    body TEXT NOT NULL,
    decision TEXT,
    PRIMARY KEY(session_id,item_id),
    UNIQUE(session_id,subject)
);
CREATE TABLE recovery_seals (
    session_id TEXT PRIMARY KEY,
    reviewed_seq INTEGER NOT NULL,
    record_count INTEGER NOT NULL,
    manifest_hash TEXT NOT NULL
);
CREATE TABLE recovery_completed (
    session_id TEXT PRIMARY KEY,
    original_epoch TEXT NOT NULL,
    server_epoch TEXT NOT NULL,
    reviewed_seq INTEGER NOT NULL,
    completed_at INTEGER NOT NULL
);
