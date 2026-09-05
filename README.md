# GST Invoice Matching Agent

GST Invoice Matching Agent is a deterministic reconciliation tool that compares a company's **GSTR-2B** purchase data against its **purchase ledger**, classifies every discrepancy, and calculates how much input tax credit (ITC) is at risk.

It was built for the **Razorpay AI Buildathon - Track 4: AI Finance Controller**.

## Why this exists

GSTR-2B vs purchase-register mismatches are a recurring finance-ops problem for Indian businesses. Typical issues include late supplier filing, invoice-number typos, amount drift, duplicate booking, or invoices that were never recorded. The goal here is to make that review process deterministic, auditable, and easy to explain.

## What it does

1. Ingests two CSVs:
   - `gstr2b.csv`
   - `purchase_ledger.csv`
2. Matches records using layered rules:
   - exact GSTIN + invoice-number matching
   - amount-tolerance checks for small rounding differences
   - fuzzy invoice-number matching for likely typos
   - duplicate detection in the ledger
   - optional credit/debit-note netting through `document_type` and `reference_invoice`
   - cross-period classification when the same invoice appears in different months
3. Classifies every row into one of these statuses:
   - `MATCHED`
   - `AMOUNT_MISMATCH`
   - `DATE_MISMATCH`
   - `CROSS_PERIOD_MATCH`
   - `FUZZY_MATCH`
   - `DUPLICATE_IN_LEDGER`
   - `MISSING_IN_LEDGER`
   - `MISSING_IN_2B`
   - `INVALID_GSTIN`
   - `INVALID_DATA`
4. Persists each run in SQLite by default and exposes a patchable exception-resolution flow.
5. Optionally asks Gemini for a bounded narrative/review layer when `GEMINI_API_KEY` is set.

This is intentionally a rules-first matcher, not a black-box LLM workflow. Finance decisions here need to be deterministic and audit-friendly.

## Project structure

```text
gst-invoice-matcher/
|-- backend/
|   |-- app/
|   |   |-- main.py        # FastAPI app, upload/demo endpoints, export, run persistence
|   |   |-- matcher.py     # reconciliation engine
|   |   |-- data_gen.py    # synthetic data generator
|   |   |-- persistence.py # SQLite/PostgreSQL-backed run storage
|   |   |-- review.py      # optional LLM review for ambiguous cases
|   |   `-- __init__.py
|   |-- data/              # generated CSVs live here
|   `-- requirements.txt
|-- frontend/
|   `-- index.html         # single-file dashboard UI
|-- scripts/
|   |-- evaluate.py        # compares matcher output to synthetic ground truth
|   |-- demo.sh
|   `-- demo.bat
|-- tests/
|   |-- test_matcher.py
|   |-- test_review.py
|   `-- test_persistence.py
`-- README.md
```

## Data format

Uploaded CSVs must include these required columns:

```text
gstin, supplier_name, invoice_number, invoice_date, taxable_value, tax_amount, total_value
```

Optional columns:

```text
document_type, reference_invoice
```

The backend accepts case-insensitive headers, trims whitespace, and cleans currency-formatted amount values. Rows with missing required fields or invalid amounts are skipped and reported in `data_quality_warnings`.

Example input:

```csv
gstin,supplier_name,invoice_number,invoice_date,taxable_value,tax_amount,total_value,document_type,reference_invoice
27AABCU9603R1ZM,Acme Supplies,INV-1001,2024-04-15,10000.00,1800.00,11800.00,Invoice,
27AABCU9603R1ZM,Acme Supplies,CN-1001,2024-04-20,-1000.00,-180.00,-1180.00,Credit Note,INV-1001
```

`document_type` and `reference_invoice` are optional and support credit/debit-note netting against an original invoice.

## Running it locally

```bash
cd gst-invoice-matcher
pip install -r backend/requirements.txt
python backend/app/data_gen.py
cd backend
uvicorn app.main:app --reload --port 8000
```

All runtime dependencies are pinned to exact versions in `backend/requirements.txt`. If `pip install` fails in a restricted or offline environment, the failure is due to package-index/network access; retry with a normal internet connection.

On Windows, run `scripts\demo.bat` from the repository root.

On Unix-like systems, run `./scripts/demo.sh`.

Both demo scripts install dependencies, regenerate the synthetic CSVs, start the server, and open the dashboard.

Then open http://localhost:8000 and either:

1. Click **Run demo data** to reconcile the bundled synthetic dataset.
2. Upload your own two CSVs and click **Reconcile uploaded files**.

## API endpoints

- `GET /api/health` - liveness check
- `GET /api/demo-data` - regenerate the bundled synthetic data
- `POST /api/reconcile/demo` - reconcile the bundled demo CSVs
- `POST /api/reconcile` - reconcile uploaded CSVs
- `GET /api/runs` - list saved reconciliation runs
- `GET /api/runs/{id}` - fetch a persisted run with resolution metadata
- `PATCH /api/runs/{run_id}/exceptions/{exception_id}` - mark an exception `OPEN`, `RESOLVED`, or `IGNORED`
- `GET /api/reconcile/demo/export` - download an Excel exception report
- `POST /api/reconcile/demo/narrate` - optional Gemini summary for a report body

## Tests and evaluation

Run these commands from inside the project virtual environment (`.venv`) — using the system Python directly will fail with `No module named pytest` since dependencies are installed only in `.venv`.

```bash
pip install pytest
python -m pytest tests -v
python scripts/evaluate.py
```

The evaluation script compares matcher output against `backend/data/ground_truth.csv`, which is generated alongside the demo CSVs.

The test suite currently contains 21 tests; the latest run passed all 21.

On the checked-in synthetic dataset, the latest local run produced:

| Metric | Result |
| --- | ---: |
| Macro precision | 1.000 |
| Macro recall | 1.000 |
| Macro F1 | 1.000 |
| Weighted F1 | 1.000 |
| `DATE_MISMATCH` recall | 1.000 |
| `FUZZY_MATCH` recall | 1.000 |

These are deterministic matcher results for the generated scenarios, not a claim about production data. Regenerate the demo files with `python backend/app/data_gen.py` and rerun `python scripts/evaluate.py` to reproduce the measurement for a new batch.

## Optional review layer

The deterministic matcher always sets the authoritative `status`. The optional review layer never overrides it; it adds an `llm_recommendation` object with one of `ACCEPT`, `REJECT`, or `NEEDS_HUMAN`, plus a short reason.

Review is requested only for ambiguous results:

- `FUZZY_MATCH` with confidence below `0.90`
- `AMOUNT_MISMATCH` where taxable-value drift is between `1%` and `3%`

Set both `ENABLE_LLM_REVIEW=true` and `GEMINI_API_KEY` to call Gemini. Without configuration, or if the response is invalid or fails, the recommendation is `NEEDS_HUMAN` and the deterministic result is retained.

## Synthetic dataset

`backend/app/data_gen.py` generates a reproducible synthetic batch of roughly 60 base invoices and injects a realistic mix of scenarios:

- clean matches
- amount mismatches
- missing-in-ledger cases
- missing-in-2B cases
- duplicate ledger bookings
- invoice-number typos
- date mismatches

The exact row counts in the generated CSVs vary because some scenarios add extra ledger rows, while others remove rows from GSTR-2B.

## Notes

- `DATABASE_URL` can be set to use PostgreSQL instead of the default SQLite file `reconciliation.db`.
- `GEMINI_API_KEY` is used for optional Gemini narration and review; ambiguous-case review also requires `ENABLE_LLM_REVIEW=true`.
- The dashboard UI is a single static HTML file in `frontend/index.html`.

## Buildathon fit

- Solves one finance-ops loop end to end: GSTR-2B vs purchase-ledger reconciliation
- Works on a batch large enough to surface real exception patterns
- Produces a full exception list with plain-language reasons
- Persists runs so resolution history is retained
<img width="2720" height="2304" alt="gst_matcher_architecture" src="https://github.com/user-attachments/assets/30712b58-5733-4e88-a87d-81d76c672a9b" />

