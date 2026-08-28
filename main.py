"""
Kivu Home — MoMo x Shopify Bridge
FastAPI backend that bridges Shopify (electronics landing page) and the
MTN MoMo Collections API (requesttopay flow).

Flow:
1. Customer checks out on Shopify with a phone number cart attribute.
2. Shopify fires an `orders/create` webhook -> POST /webhooks/orders-create
3. We verify the webhook, extract the phone number + amount, and trigger
   a MoMo requesttopay.
4. We poll (or receive a MoMo callback) for payment status.
5. On success, we mark the Shopify order as paid via the Admin API.
"""

import base64
import hashlib
import hmac
import os
import uuid
from datetime import datetime

import httpx
from fastapi import FastAPI, Header, HTTPException, Request
from pydantic import BaseModel

app = FastAPI(title="Kivu Home MoMo x Shopify Bridge")

# ---------------------------------------------------------------------------
# Config (all pulled from environment variables — set these in Render)
# ---------------------------------------------------------------------------
MOMO_SUBSCRIPTION_KEY = os.environ.get("MOMO_SUBSCRIPTION_KEY", "")
MOMO_API_USER = os.environ.get("MOMO_API_USER", "")
MOMO_API_KEY = os.environ.get("MOMO_API_KEY", "")
MOMO_TARGET_ENVIRONMENT = os.environ.get("MOMO_TARGET_ENVIRONMENT", "sandbox")  # "sandbox" or "mtnrwanda" in prod
MOMO_BASE_URL = os.environ.get("MOMO_BASE_URL", "https://sandbox.momodeveloper.mtn.com")

SHOPIFY_STORE_DOMAIN = os.environ.get("SHOPIFY_STORE_DOMAIN", "")  # e.g. kivuhome.myshopify.com
SHOPIFY_ADMIN_API_TOKEN = os.environ.get("SHOPIFY_ADMIN_API_TOKEN", "")
SHOPIFY_WEBHOOK_SECRET = os.environ.get("SHOPIFY_WEBHOOK_SECRET", "")

SHOPIFY_API_VERSION = "2024-10"


# ---------------------------------------------------------------------------
# Health check (used by Render's health check path)
# ---------------------------------------------------------------------------
@app.get("/health")
async def health():
    return {"status": "ok", "time": datetime.utcnow().isoformat()}


# ---------------------------------------------------------------------------
# MoMo helpers
# ---------------------------------------------------------------------------
async def get_momo_access_token() -> str:
    """Exchange API user + API key for a short-lived MoMo access token."""
    url = f"{MOMO_BASE_URL}/collection/token/"
    auth = base64.b64encode(f"{MOMO_API_USER}:{MOMO_API_KEY}".encode()).decode()
    headers = {
        "Authorization": f"Basic {auth}",
        "Ocp-Apim-Subscription-Key": MOMO_SUBSCRIPTION_KEY,
    }
    async with httpx.AsyncClient() as client:
        resp = await client.post(url, headers=headers)
        resp.raise_for_status()
        return resp.json()["access_token"]


async def request_to_pay(phone_number: str, amount: str, currency: str, external_id: str) -> str:
    """Trigger a MoMo Collections requesttopay. Returns the MoMo reference_id."""
    token = await get_momo_access_token()
    reference_id = str(uuid.uuid4())

    url = f"{MOMO_BASE_URL}/collection/v1_0/requesttopay"
    headers = {
        "Authorization": f"Bearer {token}",
        "X-Reference-Id": reference_id,
        "X-Target-Environment": MOMO_TARGET_ENVIRONMENT,
        "Ocp-Apim-Subscription-Key": MOMO_SUBSCRIPTION_KEY,
        "Content-Type": "application/json",
    }
    payload = {
        "amount": amount,
        "currency": currency,
        "externalId": external_id,
        "payer": {"partyIdType": "MSISDN", "partyId": phone_number},
        "payerMessage": "Kivu Home order payment",
        "payeeNote": "Kivu Home order payment",
    }
    async with httpx.AsyncClient() as client:
        resp = await client.post(url, headers=headers, json=payload)
        resp.raise_for_status()
    return reference_id


async def check_payment_status(reference_id: str) -> dict:
    """Poll MoMo for the status of a previously triggered requesttopay."""
    token = await get_momo_access_token()
    url = f"{MOMO_BASE_URL}/collection/v1_0/requesttopay/{reference_id}"
    headers = {
        "Authorization": f"Bearer {token}",
        "X-Target-Environment": MOMO_TARGET_ENVIRONMENT,
        "Ocp-Apim-Subscription-Key": MOMO_SUBSCRIPTION_KEY,
    }
    async with httpx.AsyncClient() as client:
        resp = await client.get(url, headers=headers)
        resp.raise_for_status()
        return resp.json()


# ---------------------------------------------------------------------------
# Shopify helpers
# ---------------------------------------------------------------------------
def verify_shopify_webhook(raw_body: bytes, hmac_header: str) -> bool:
    if not SHOPIFY_WEBHOOK_SECRET or not hmac_header:
        return False
    digest = hmac.new(SHOPIFY_WEBHOOK_SECRET.encode(), raw_body, hashlib.sha256).digest()
    computed_hmac = base64.b64encode(digest).decode()
    return hmac.compare_digest(computed_hmac, hmac_header)


async def mark_order_paid(order_id: int):
    url = f"https://{SHOPIFY_STORE_DOMAIN}/admin/api/{SHOPIFY_API_VERSION}/orders/{order_id}/transactions.json"
    headers = {
        "X-Shopify-Access-Token": SHOPIFY_ADMIN_API_TOKEN,
        "Content-Type": "application/json",
    }
    payload = {"transaction": {"kind": "capture", "status": "success"}}
    async with httpx.AsyncClient() as client:
        resp = await client.post(url, headers=headers, json=payload)
        resp.raise_for_status()


# ---------------------------------------------------------------------------
# Webhook: Shopify orders/create
# ---------------------------------------------------------------------------
@app.post("/webhooks/orders-create")
async def orders_create(request: Request, x_shopify_hmac_sha256: str = Header(None)):
    raw_body = await request.body()

    if not verify_shopify_webhook(raw_body, x_shopify_hmac_sha256):
        raise HTTPException(status_code=401, detail="Invalid webhook signature")

    order = await request.json()

    order_id = order.get("id")
    total_price = order.get("total_price")
    currency = order.get("currency", "RWF")

    # Phone number collected via cart attribute — adjust key to match
    # whatever attribute name is used on the electronics landing page.
    note_attributes = {a["name"]: a["value"] for a in order.get("note_attributes", [])}
    phone_number = note_attributes.get("momo_phone")

    if not phone_number:
        raise HTTPException(status_code=400, detail="No MoMo phone number found on order")

    reference_id = await request_to_pay(
        phone_number=phone_number,
        amount=total_price,
        currency=currency,
        external_id=str(order_id),
    )

    # TODO: persist (order_id -> reference_id) somewhere (DB/table) so a
    # background job or callback can look it up and call mark_order_paid().

    return {"status": "payment_requested", "reference_id": reference_id}


# ---------------------------------------------------------------------------
# Manual/status-check endpoint (useful while callback/polling isn't wired up)
# ---------------------------------------------------------------------------
class StatusCheckRequest(BaseModel):
    reference_id: str
    order_id: int


@app.post("/payments/check-status")
async def payments_check_status(body: StatusCheckRequest):
    status = await check_payment_status(body.reference_id)
    if status.get("status") == "SUCCESSFUL":
        await mark_order_paid(body.order_id)
    return status
