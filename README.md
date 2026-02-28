# FraudLens

FraudLens is a production-style Flask web API and dashboard that simulates transaction fraud detection using:
- **Capital One Nessie API** for customer creation and baseline history lookup
- **Google Gemini API** for human-readable fraud explanations
- **SQLite** for local persistence and analytics

It is designed for a hackathon API submission with clean modular structure and REST-first endpoint design.

## Project Structure

```text
fraudlens/
  app.py
  config.py
  models.py
  api/
    routes.py
  web/
    routes.py
  services/
    fraud_engine.py
    gemini_service.py
    nessie_service.py
  templates/
  static/
  README.md
  requirements.txt
```

## Features

- Flask app factory + Blueprint separation (`/api/*` and web dashboard routes)
- SQLite-backed `Customer`, `Transaction`, and idempotency storage
- Rule-based fraud scoring (0-1 normalized score)
- Fraud baselining uses both local transactions and Nessie purchase history
- Gemini-generated fraud explanation with graceful fallback
- API validation, error handling, and proper HTTP status codes
- Pagination on `GET /api/transactions`
- Idempotency support with `Idempotency-Key` for POST endpoints

## Local Demo Mode

By default, `NESSIE_MOCK_MODE=true` so `POST /api/customers` works without external Nessie credentials during local demo/testing.  
Set `NESSIE_MOCK_MODE=false` and provide `NESSIE_API_KEY` to use the live Nessie API.

## Setup

### 1) Create environment and install dependencies

```bash
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
```

### 2) Configure environment variables

Copy `env.example` to `.env` in `fraudlens/` and adjust values:

```env
FLASK_SECRET_KEY=replace-me
NESSIE_MOCK_MODE=true
NESSIE_API_KEY=your_nessie_api_key
GEMINI_API_KEY=your_gemini_api_key

# Optional overrides
# NESSIE_BASE_URL=http://api.nessieisreal.com
# GEMINI_BASE_URL=https://generativelanguage.googleapis.com/v1beta
# GEMINI_MODEL=gemini-2.5-flash
```

### 3) Run the app

```bash
python app.py
```

App will start on `http://127.0.0.1:5000`.

## Dashboard Routes

- `GET /` - list customers
- `GET /simulate` - transaction simulation form
- `POST /simulate` - create and score transaction from form
- `GET /result/<transaction_id>` - display score and risk factors

## API Endpoints

Base path: `/api`

### 1) Create Customer

`POST /api/customers`

Request body:

```json
{
  "first_name": "John",
  "last_name": "Doe"
}
```

Success response (`201`):

```json
{
  "customer": {
    "id": "local-uuid",
    "nessie_customer_id": "remote-nessie-id",
    "first_name": "John",
    "last_name": "Doe",
    "created_at": "2026-02-27T12:00:00+00:00"
  }
}
```

### 2) Create Transaction

`POST /api/transactions`

Request body:

```json
{
  "customer_id": "local-customer-id",
  "amount": 950,
  "merchant": "Best Buy",
  "location": "New York",
  "timestamp": "2026-04-04T02:14:00Z"
}
```

Success response (`201`):

```json
{
  "transaction_id": "txn-uuid",
  "fraud_score": 0.82,
  "risk_level": "HIGH",
  "history_sample_size": 14,
  "nessie_history_available": true
}
```

### 3) Get Fraud Score + AI Explanation

`GET /api/fraud-score/<transaction_id>`

Success response (`200`):

```json
{
  "transaction_id": "txn-uuid",
  "fraud_score": 0.82,
  "risk_level": "HIGH",
  "history_sample_size": 14,
  "nessie_history_available": true,
  "risk_factors": [
    "Amount deviates significantly from average ($120.00).",
    "Transaction occurred during high-risk overnight hours."
  ],
  "ai_explanation": "This transaction is high risk because..."
}
```

Returns `404` if transaction is not found.

### 4) List Customers from Nessie (Remote Only)

`GET /api/customers/remote?limit=100`

Success response (`200`):

```json
{
  "items": [],
  "total": 0
}
```

### 5) Sync Nessie Customers into Local DB

`POST /api/customers/sync`

Request body (optional):

```json
{
  "limit": 100
}
```

Success response (`200`):

```json
{
  "created": 2,
  "updated": 1,
  "unchanged": 4,
  "synced_count": 7,
  "items": []
}
```

### 6) Get Customer Baseline History (Local + Nessie)

`GET /api/customers/<customer_id>/history`

Success response (`200`):

```json
{
  "customer_id": "local-id",
  "nessie_customer_id": "remote-id",
  "total": 11,
  "nessie_history_available": true,
  "items": []
}
```

### 7) List Transactions (Pagination)

`GET /api/transactions?page=1&per_page=10`

Optional query:
- `customer_id=<id>`

Success response (`200`):

```json
{
  "items": [],
  "pagination": {
    "page": 1,
    "per_page": 10,
    "pages": 0,
    "total": 0
  }
}
```

## Idempotency

Use `Idempotency-Key` header on POST requests:
- same key + same payload => returns stored response
- same key + different payload => returns `409 Conflict`

## cURL Examples

### Create Customer

```bash
curl -X POST http://127.0.0.1:5000/api/customers \
  -H "Content-Type: application/json" \
  -H "Idempotency-Key: cust-001" \
  -d "{\"first_name\":\"John\",\"last_name\":\"Doe\"}"
```

### Create Transaction

```bash
curl -X POST http://127.0.0.1:5000/api/transactions \
  -H "Content-Type: application/json" \
  -H "Idempotency-Key: txn-001" \
  -d "{\"customer_id\":\"<LOCAL_CUSTOMER_ID>\",\"amount\":950,\"merchant\":\"Best Buy\",\"location\":\"New York\",\"timestamp\":\"2026-04-04T02:14:00Z\"}"
```

### Get Fraud Score

```bash
curl http://127.0.0.1:5000/api/fraud-score/<TRANSACTION_ID>
```

### List Nessie Customers

```bash
curl "http://127.0.0.1:5000/api/customers/remote?limit=50"
```

### Sync Nessie Customers to Local

```bash
curl -X POST http://127.0.0.1:5000/api/customers/sync \
  -H "Content-Type: application/json" \
  -H "Idempotency-Key: sync-001" \
  -d "{\"limit\":100}"
```

### Get Customer Baseline History

```bash
curl http://127.0.0.1:5000/api/customers/<LOCAL_CUSTOMER_ID>/history
```

### List Transactions

```bash
curl "http://127.0.0.1:5000/api/transactions?page=1&per_page=10"
```

## Fraud Scoring Logic

FraudLens combines weighted factors and normalizes to a score between `0` and `1`:
- amount deviation from customer average
- transaction timestamp anomaly (12am-4am)
- abrupt location change vs prior transaction
- merchant category shift vs usual behavior

Risk levels:
- `LOW` for score `< 0.40`
- `MEDIUM` for score `0.40 - 0.74`
- `HIGH` for score `>= 0.75`

## Error Codes

- `200` success read
- `201` resource created
- `400` invalid input
- `404` resource not found
- `409` idempotency conflict
- `500` internal server error
- `502` upstream API failure

