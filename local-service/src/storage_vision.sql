CREATE TABLE vision_notice (
    singleton INTEGER PRIMARY KEY CHECK(singleton=1),
    version TEXT NOT NULL,
    consented_at INTEGER
);
INSERT INTO vision_notice VALUES (1,'vision-v2',NULL);
CREATE TABLE vision_jobs (
    import_id TEXT PRIMARY KEY,
    file_id TEXT NOT NULL REFERENCES files(file_id),
    pair_id TEXT NOT NULL,
    epoch TEXT NOT NULL,
    fence INTEGER NOT NULL,
    deadline INTEGER NOT NULL,
    state TEXT NOT NULL CHECK(state IN ('queued','running','finished','failed','cancelled'))
);
CREATE INDEX vision_file_leases ON vision_jobs(file_id,deadline) WHERE state IN ('queued','running');
CREATE TABLE import_commits (
    import_id TEXT PRIMARY KEY REFERENCES vision_jobs(import_id),
    import_version INTEGER NOT NULL,
    diff_hash TEXT NOT NULL,
    result TEXT NOT NULL
);
CREATE TRIGGER import_file_state AFTER UPDATE OF state ON files WHEN OLD.state<>NEW.state BEGIN
    UPDATE objects SET body=json_set(body,'$.fileStatus',NEW.state,
        '$.importVersion',json_extract(body,'$.importVersion')+1)
    WHERE collection='imports' AND json_extract(body,'$.fileId')=NEW.file_id;
END;
