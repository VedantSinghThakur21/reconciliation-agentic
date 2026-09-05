from __future__ import annotations

SCHEMA_SQL = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS flow_runs (
    id              TEXT PRIMARY KEY,
    user_request    TEXT,
    ar_ap_mode      TEXT,
    status          TEXT NOT NULL,
    stages_json     TEXT,
    summary_json    TEXT,
    final_report    TEXT,
    bank_pdf_path   TEXT,
    started_at      TEXT NOT NULL,
    finished_at     TEXT
);

CREATE TABLE IF NOT EXISTS source_transactions (
    id              TEXT PRIMARY KEY,
    run_id          TEXT NOT NULL,
    source          TEXT NOT NULL,
    transaction_id  TEXT NOT NULL,
    txn_date        TEXT,
    amount          REAL,
    currency        TEXT,
    merchant        TEXT,
    reference       TEXT,
    description     TEXT,
    merchant_norm   TEXT,
    date_norm       TEXT,
    amount_norm     REAL,
    currency_norm   TEXT,
    reference_norm  TEXT,
    raw_json        TEXT,
    created_at      TEXT NOT NULL,
    FOREIGN KEY (run_id) REFERENCES flow_runs(id)
);

CREATE TABLE IF NOT EXISTS match_candidates (
    id              TEXT PRIMARY KEY,
    run_id          TEXT NOT NULL,
    erp_txn_id      TEXT NOT NULL,
    counter_txn_id  TEXT NOT NULL,
    strategy        TEXT NOT NULL,
    amount_score    REAL,
    merchant_score  REAL,
    date_score      REAL,
    reference_score REAL,
    confidence      REAL,
    evidence_json   TEXT,
    created_at      TEXT NOT NULL,
    FOREIGN KEY (run_id) REFERENCES flow_runs(id)
);

CREATE TABLE IF NOT EXISTS exceptions (
    id                  TEXT PRIMARY KEY,
    run_id              TEXT NOT NULL,
    transaction_id      TEXT NOT NULL,
    exception_type      TEXT NOT NULL,
    recommended_action  TEXT,
    confidence          REAL,
    details_json        TEXT,
    created_at          TEXT NOT NULL,
    FOREIGN KEY (run_id) REFERENCES flow_runs(id)
);

CREATE TABLE IF NOT EXISTS human_reviews (
    id              TEXT PRIMARY KEY,
    run_id          TEXT NOT NULL,
    transaction_id  TEXT NOT NULL,
    decision        TEXT NOT NULL,
    reason          TEXT,
    confidence      REAL,
    created_at      TEXT NOT NULL,
    FOREIGN KEY (run_id) REFERENCES flow_runs(id)
);

CREATE TABLE IF NOT EXISTS audit_log (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id          TEXT,
    event_type      TEXT NOT NULL,
    entity_id       TEXT,
    message         TEXT NOT NULL,
    payload_json    TEXT,
    created_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS reconciliation_results (
    id                  TEXT PRIMARY KEY,
    run_id              TEXT NOT NULL,
    transaction_id      TEXT NOT NULL,
    matched_txn_id      TEXT,
    final_status        TEXT NOT NULL,
    confidence          REAL,
    reconciled_amount   REAL,
    strategy            TEXT,
    details_json        TEXT,
    created_at          TEXT NOT NULL,
    FOREIGN KEY (run_id) REFERENCES flow_runs(id)
);

CREATE TABLE IF NOT EXISTS journal_entries (
    id              TEXT PRIMARY KEY,
    run_id          TEXT NOT NULL,
    entry_type      TEXT NOT NULL,
    status          TEXT NOT NULL,
    debit_account   TEXT NOT NULL,
    credit_account  TEXT NOT NULL,
    amount          REAL NOT NULL,
    currency        TEXT NOT NULL,
    transaction_id  TEXT,
    memo            TEXT,
    created_at      TEXT NOT NULL,
    FOREIGN KEY (run_id) REFERENCES flow_runs(id)
);

CREATE TABLE IF NOT EXISTS erp_writeback_log (
    id              TEXT PRIMARY KEY,
    run_id          TEXT NOT NULL,
    journal_id      TEXT NOT NULL,
    status          TEXT NOT NULL,
    message         TEXT,
    created_at      TEXT NOT NULL,
    FOREIGN KEY (run_id) REFERENCES flow_runs(id)
);

CREATE TABLE IF NOT EXISTS evaluation_metrics (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id              TEXT NOT NULL,
    accuracy            REAL,
    precision_score     REAL,
    recall              REAL,
    f1                  REAL,
    match_rate          REAL,
    false_match_rate    REAL,
    tp                  INTEGER,
    fp                  INTEGER,
    fn                  INTEGER,
    tn                  INTEGER,
    details_json        TEXT,
    created_at          TEXT NOT NULL,
    FOREIGN KEY (run_id) REFERENCES flow_runs(id)
);

CREATE INDEX IF NOT EXISTS idx_src_source ON source_transactions(source);
CREATE INDEX IF NOT EXISTS idx_cand_erp ON match_candidates(erp_txn_id);
CREATE INDEX IF NOT EXISTS idx_results_status ON reconciliation_results(final_status);
"""
