from __future__ import annotations

SCHEMA_SQL = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS runs (
    id              TEXT PRIMARY KEY,
    started_at      TEXT NOT NULL,
    finished_at     TEXT,
    status          TEXT NOT NULL DEFAULT 'running',
    mode            TEXT NOT NULL DEFAULT 'demo',
    use_ai          INTEGER NOT NULL DEFAULT 0,
    summary_json    TEXT,
    stages_json     TEXT
);

CREATE TABLE IF NOT EXISTS transactions (
    id              TEXT PRIMARY KEY,
    run_id          TEXT NOT NULL,
    source          TEXT NOT NULL,
    kind            TEXT NOT NULL,
    external_id     TEXT NOT NULL,
    party_name      TEXT,
    amount          REAL NOT NULL,
    currency        TEXT NOT NULL DEFAULT 'INR',
    txn_date        TEXT,
    reference       TEXT,
    raw_json        TEXT,
    FOREIGN KEY (run_id) REFERENCES runs(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS results (
    id              TEXT PRIMARY KEY,
    run_id          TEXT NOT NULL,
    payment_id      TEXT,
    invoice_id      TEXT,
    status          TEXT NOT NULL,
    confidence      REAL NOT NULL DEFAULT 0,
    tier            TEXT,
    amount_applied  REAL NOT NULL DEFAULT 0,
    discrepancy     REAL NOT NULL DEFAULT 0,
    treatment       TEXT,
    reasoning       TEXT,
    source          TEXT,
    review_reason   TEXT,
    FOREIGN KEY (run_id) REFERENCES runs(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS exceptions (
    id              TEXT PRIMARY KEY,
    run_id          TEXT NOT NULL,
    payment_id      TEXT NOT NULL,
    invoice_id      TEXT,
    confidence      REAL NOT NULL DEFAULT 0,
    reason          TEXT,
    reasoning       TEXT,
    amount_applied  REAL NOT NULL DEFAULT 0,
    status          TEXT NOT NULL DEFAULT 'pending',
    created_at      TEXT NOT NULL,
    decided_at      TEXT,
    decided_by      TEXT,
    FOREIGN KEY (run_id) REFERENCES runs(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS metrics (
    run_id          TEXT PRIMARY KEY,
    accuracy        REAL,
    precision_score REAL,
    recall          REAL,
    f1              REAL,
    match_rate      REAL,
    auto_reconciled INTEGER,
    pending_review  INTEGER,
    total_payments  INTEGER,
    unreconciled_amount REAL,
    details_json    TEXT,
    FOREIGN KEY (run_id) REFERENCES runs(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS audit_log (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id          TEXT,
    event_type      TEXT NOT NULL,
    entity_type     TEXT,
    entity_id       TEXT,
    message         TEXT NOT NULL,
    payload_json    TEXT,
    created_at      TEXT NOT NULL,
    FOREIGN KEY (run_id) REFERENCES runs(id) ON DELETE SET NULL
);

CREATE INDEX IF NOT EXISTS idx_tx_run ON transactions(run_id);
CREATE INDEX IF NOT EXISTS idx_results_run ON results(run_id);
CREATE INDEX IF NOT EXISTS idx_exceptions_run ON exceptions(run_id);
CREATE INDEX IF NOT EXISTS idx_audit_run ON audit_log(run_id);
"""
