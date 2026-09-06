# Demo datasets

Two generators write **separate** files so they do not overwrite each other.

## Finance Controller eval (`scripts/generate_fc_demo_data.py`)

Brief-aligned CSV formats used by `run_finance_flow.py`:

- `bank.csv` — `transaction_id,date,amount,currency,merchant,reference,description`
- `payment_processor.csv` — same schema
- `ground_truth.csv` — `transaction_id,expected_match_id,expected_status,exception_type`

ERP records are generated in-flow (`DEMO_ERP_COUNT` Indian-company stubs; 50+ labeled eval batch).

```bash
python scripts/generate_fc_demo_data.py
python scripts/run_finance_flow.py
```

## Classic CLI / orchestrator (`scripts/generate_demo_data.py`)

QuickBooks-style feeds for `run_pipeline.py` and `FinanceControllerOrchestrator` (50+ payments):

- `quickbooks_invoices.csv`
- `bank_transactions.csv`
- `payment_processor_cli.csv` (CLI schema — not the FC `payment_processor.csv`)
- `ground_truth_cli.csv` (CLI schema — not the FC `ground_truth.csv`)

```bash
python scripts/generate_demo_data.py
python scripts/run_pipeline.py \
  --invoices data/demo/quickbooks_invoices.csv \
  --payments data/demo/bank_transactions.csv
```
