# Kivu Home — MoMo x Shopify Bridge

FastAPI backend that bridges Kivu Home's Shopify store (electronics landing
page) and the MTN MoMo Collections API, using the `requesttopay` flow.

## How it works

1. Customer checks out on Shopify; their MoMo phone number is captured via
   a cart attribute (key: `momo_phone`).
2. Shopify fires an `orders/create` webhook to `POST /webhooks/orders-create`.
3. The backend verifies the webhook signature, then calls MTN MoMo's
   `requesttopay` to charge the customer.
4. `POST /payments/check-status` can be polled (or wired to a MoMo callback
   later) to confirm payment and mark the Shopify order as paid via the
   Admin API.

## Local development

```bash
python -m venv venv
source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # fill in real values
uvicorn main:app --reload
```

Health check: `GET /health`

## Environment variables

See `.env.example` for the full list. Set these as environment variables in
the Render dashboard — never commit a real `.env` file.

## Deployment (Render)

- **Build command:** `pip install -r requirements.txt`
- **Start command:** `uvicorn main:app --host 0.0.0.0 --port $PORT`
- **Health check path:** `/health`
