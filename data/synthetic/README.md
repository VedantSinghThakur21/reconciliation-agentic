# Synthetic fixtures (safe demo data only — never real customer files)

- `sample_invoices.csv` / `sample_payments.csv` — tiny StrictMatcher smoke set
- `bank_statements/` — text-layer PDFs for PDF ingestion tests

```bash
python scripts/generate_synthetic_bank_pdfs.py
```
