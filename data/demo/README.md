# Demo datasets for AI Finance Controller (CrewAI Flow)

Brief-aligned CSV formats:

- `bank.csv` — `transaction_id,date,amount,currency,merchant,reference,description`
- `payment_processor.csv` — same schema
- `ground_truth.csv` — `transaction_id,expected_match_id,expected_status,exception_type`

ERP records are generated in-flow (24 Indian-company stubs).

```bash
python scripts/generate_fc_demo_data.py
```
