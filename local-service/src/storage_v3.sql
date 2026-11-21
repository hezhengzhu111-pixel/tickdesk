CREATE TABLE domain_state (
    singleton INTEGER PRIMARY KEY CHECK(singleton=1),
    paused INTEGER NOT NULL DEFAULT 0,
    control_version INTEGER NOT NULL DEFAULT 0,
    profile TEXT NOT NULL DEFAULT 'conservative',
    settings TEXT NOT NULL DEFAULT '{"settingsRevision":0,"reminderModes":{"critical":"full_screen","warn":"sound","info":"visual"},"nightStart":"23:00","nightEnd":"07:00","criticalAtNight":false,"pushEnabled":false}',
    position_revision INTEGER NOT NULL DEFAULT 0
);
INSERT INTO domain_state(singleton) VALUES (1);
CREATE TABLE objects (
    collection TEXT NOT NULL,
    object_id TEXT NOT NULL,
    body TEXT NOT NULL,
    PRIMARY KEY(collection,object_id)
);
CREATE TABLE snapshot_pages (
    event_id TEXT PRIMARY KEY,
    pair_id TEXT NOT NULL,
    epoch TEXT NOT NULL,
    snapshot_id TEXT NOT NULL,
    cursor TEXT UNIQUE,
    expires_at INTEGER NOT NULL,
    body TEXT NOT NULL
);
CREATE TABLE command_events (ack_id TEXT NOT NULL, event_id TEXT NOT NULL, body TEXT NOT NULL, PRIMARY KEY(ack_id, event_id));
CREATE TABLE read_cursors (
    cursor TEXT PRIMARY KEY,
    pair_id TEXT NOT NULL,
    epoch TEXT NOT NULL,
    query TEXT NOT NULL,
    expires_at INTEGER NOT NULL,
    body TEXT NOT NULL
);
CREATE TABLE task_actions (
    action_id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL,
    at INTEGER NOT NULL,
    body TEXT NOT NULL
);
CREATE INDEX actions_by_task ON task_actions(task_id,at DESC,action_id);
CREATE TABLE diagnostics (id INTEGER PRIMARY KEY, at INTEGER NOT NULL, level TEXT NOT NULL, code TEXT NOT NULL, text TEXT NOT NULL);
CREATE TABLE device_registration (singleton INTEGER PRIMARY KEY CHECK(singleton=1), device_id TEXT NOT NULL, reg_id TEXT NOT NULL);
