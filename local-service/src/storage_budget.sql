CREATE TABLE budget_configs (
    version INTEGER PRIMARY KEY,
    body TEXT NOT NULL,
    body_hash TEXT NOT NULL,
    installed_at INTEGER NOT NULL
);
CREATE TABLE budget_state (
    singleton INTEGER PRIMARY KEY CHECK(singleton=1),
    active_version INTEGER REFERENCES budget_configs(version)
);
INSERT INTO budget_state VALUES (1,NULL);
CREATE TABLE budget_reservations (
    task_id TEXT PRIMARY KEY,
    config_version INTEGER NOT NULL REFERENCES budget_configs(version),
    request_hash TEXT NOT NULL,
    symbol TEXT NOT NULL,
    strategies TEXT NOT NULL,
    group_name TEXT NOT NULL,
    industry TEXT NOT NULL,
    qty INTEGER NOT NULL CHECK(qty>0),
    notional TEXT NOT NULL,
    risk TEXT NOT NULL,
    state TEXT NOT NULL CHECK(state IN ('held','released','converted','reconciliation')),
    trade_date TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL
);
