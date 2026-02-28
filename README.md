# FraudLens

FraudLens is a production-style Flask web API and dashboard that simulates transaction fraud detection using:
- **Capital One Nessie API** for customer creation and baseline history lookup
- **Google Gemini API** for human-readable fraud explanations
- **SQLite** for local persistence and analytics

It is designed for a hackathon API submission with clean modular structure and REST-first endpoint design.

## 60-Second First Call

1) Start the server:

```bash
python app.py
```

2) Create a customer:

```bash
curl -X POST http://127.0.0.1:5000/api/customers \
  -H "Content-Type: application/json" \
  -d "{\"first_name\":\"Ava\",\"last_name\":\"Shaw\"}"
```

3) Create a v1 fraud check (replace `<CUSTOMER_ID>`):

```bash
curl -X POST http://127.0.0.1:5000/v1/fraud-checks \
  -H "Content-Type: application/json" \
  -H "Idempotency-Key: quickstart-001" \
  -d "{\"customer_id\":\"<CUSTOMER_ID>\",\"amount\":950,\"merchant\":\"Best Buy\",\"location\":\"New York\",\"timestamp\":\"2026-04-04T02:14:00Z\"}"
```

You will receive a `201 Created` response with a persisted `fraud_check` resource.

Interactive docs:
- Swagger UI: `http://127.0.0.1:5000/docs/`
- OpenAPI JSON: `http://127.0.0.1:5000/openapi.json`

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

`GET /api/customers/remote?limit=100&offset=0&newest_first=true`

Success response (`200`):

```json
{
  "items": [],
  "total": 0,
  "limit": 100,
  "offset": 0,
  "newest_first": true,
  "has_more": false
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

### 6) Seed Demo Data in Nessie

`POST /api/nessie/seed-demo-data`

Request body (optional):

```json
{
  "customers": 3,
  "purchases_per_customer": 5,
  "create_local_links": true
}
```

Success response (`201`):

```json
{
  "customers_created": 3,
  "accounts_created": 3,
  "purchases_created": 15,
  "seeded_customers": []
}
```

### 7) Get Customer Baseline History (Local + Nessie)

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

### 8) List Transactions (Pagination)

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
curl "http://127.0.0.1:5000/api/customers/remote?limit=5&offset=0&newest_first=true"
```

### Sync Nessie Customers to Local

```bash
curl -X POST http://127.0.0.1:5000/api/customers/sync \
  -H "Content-Type: application/json" \
  -H "Idempotency-Key: sync-001" \
  -d "{\"limit\":100}"
```

### Seed Nessie Demo Data

```bash
curl -X POST http://127.0.0.1:5000/api/nessie/seed-demo-data \
  -H "Content-Type: application/json" \
  -H "Idempotency-Key: seed-001" \
  -d "{\"customers\":3,\"purchases_per_customer\":5,\"create_local_links\":true}"
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

## v1 Fraud Checks API (Resource-Oriented Surface)

Base path: `/v1`

`/v1/fraud-checks` is Nessie-first:
- Customer identity is resolved against Nessie.
- Baselining uses Nessie purchase history plus prior local fraud checks.
- Local DB stores fraud-check lifecycle state and idempotency records.

### Create Fraud Check

`POST /v1/fraud-checks`

```json
{
  "customer_id": "local-customer-id",
  "amount": 950,
  "merchant": "Best Buy",
  "location": "New York",
  "timestamp": "2026-04-04T02:14:00Z"
}
```

`customer_id` can be either:
- local FraudLens customer UUID, or
- Nessie customer ID (`nessie_customer_id`)

Response: `201 Created`

### Retrieve Fraud Check

`GET /v1/fraud-checks/<fraud_check_id>`

Response: `200 OK`

### List Fraud Checks

`GET /v1/fraud-checks?customer_id=<id>&status=completed&review_status=open&risk_level=HIGH&min_fraud_score=0.2&max_fraud_score=0.9&q=best&page=1&per_page=10`

Response: `200 OK`

### Update Review State

`PATCH /v1/fraud-checks/<fraud_check_id>`

```json
{
  "review_status": "confirmed_fraud"
}
```

Response: `200 OK`

### Delete Fraud Check

`DELETE /v1/fraud-checks/<fraud_check_id>`

Response: `204 No Content`

### Health

`GET /v1/health`

Response: `200 OK`

## Structured Error Format

All v1 errors follow this body:

```json
{
  "error": {
    "type": "invalid_request",
    "message": "amount must be greater than zero."
  }
}
```

Semantics:
- `400 Bad Request` for malformed JSON body.
- `422 Unprocessable Entity` for semantically invalid input.
- `404 Not Found` for missing resources.
- `409 Conflict` for idempotency-key payload mismatches.

## v1 cURL Examples

### POST /v1/fraud-checks

```bash
curl -X POST http://127.0.0.1:5000/v1/fraud-checks \
  -H "Content-Type: application/json" \
  -H "Idempotency-Key: fc-001" \
  -d "{\"customer_id\":\"<CUSTOMER_ID>\",\"amount\":1200,\"merchant\":\"Apple\",\"location\":\"Chicago\",\"timestamp\":\"2026-05-01T01:20:00Z\"}"
```

### GET /v1/fraud-checks/:id

```bash
curl http://127.0.0.1:5000/v1/fraud-checks/<FRAUD_CHECK_ID>
```

### GET /v1/fraud-checks

```bash
curl "http://127.0.0.1:5000/v1/fraud-checks?risk_level=HIGH&page=1&per_page=10"
```

### GET /v1/health

```bash
curl http://127.0.0.1:5000/v1/health
```

## Tech Stack

- Flask 3 + Blueprints for API organization
- Flask-SQLAlchemy + SQLite for fraud-check lifecycle and idempotency state
- Capital One Nessie API for customer and purchase-history data
- Google Gemini API for human-readable fraud explanations
- Flasgger-powered Swagger docs at `/docs/`

## HackIllinois Requirement Mapping

- Build an API with valuable action/data: **met**
  - Creates, scores, retrieves, updates, and deletes fraud checks.
- Queryable over HTTP on localhost: **met**
  - App runs at `http://127.0.0.1:5000`.
- Usable via cURL/Postman: **met**
  - cURL examples provided for core endpoints.
- Documentation and usage examples in README: **met**
  - Includes quickstart, endpoint docs, examples, and errors.
- Returns expected 2xx for valid input: **met**
  - `200/201` and `204` are returned across successful flows.
- Informative errors and edge-case handling: **met**
  - Structured errors and status codes (`400/422/404/409/502`).
- Beyond GET + stateful behavior: **met**
  - Uses `POST`, `PATCH`, `DELETE`; stores fraud-check lifecycle state.
- Pagination/filtering/search where appropriate: **met**
  - `GET /v1/fraud-checks` supports page/per_page, filters, and `q` search.
- Bonus: publicly accessible API: **pending**
  - Deploy to Render/Railway for a public URL.
- Bonus: hosted docs page: **partially met**
  - Hosted locally at `/docs/`; public once deployed.

## Devpost Submission Template

### Project Name
FraudLens

### Elevator Pitch
FraudLens is a Nessie-first fraud detection API that turns transaction events into explainable fraud decisions with a clean, developer-friendly interface.

### What It Does
- Accepts fraud-check creation requests via REST
- Pulls customer history from Capital One Nessie
- Scores fraud risk with rule-based logic
- Generates AI explanation with Gemini
- Exposes full fraud-check lifecycle endpoints

### Why It’s Useful
Developers can plug FraudLens into fintech workflows to get deterministic scoring plus readable explanations while keeping API behavior predictable and idempotent.

### Key Endpoints
- `POST /v1/fraud-checks`
- `GET /v1/fraud-checks/{id}`
- `GET /v1/fraud-checks`
- `PATCH /v1/fraud-checks/{id}`
- `DELETE /v1/fraud-checks/{id}`
- `GET /api/customers/remote`
- `GET /v1/health`

### Built With
Flask, Flask-SQLAlchemy, SQLite, Capital One Nessie API, Google Gemini API, Flasgger (Swagger).

### Demo Notes
Use `/docs/` for interactive execution, or run cURL commands from this README.

### PATCH /v1/fraud-checks/:id

```bash
curl -X PATCH http://127.0.0.1:5000/v1/fraud-checks/<FRAUD_CHECK_ID> \
  -H "Content-Type: application/json" \
  -d "{\"review_status\":\"dismissed\"}"
```

### DELETE /v1/fraud-checks/:id

```bash
curl -X DELETE http://127.0.0.1:5000/v1/fraud-checks/<FRAUD_CHECK_ID>
```

