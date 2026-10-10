# FOODFLOW architecture

```text
Browser / Jinja
    └─ Flask session, authorization, CSRF validation
       ├─ PostgreSQL: customer, restaurant, menu_item, orders, order_item, payment, delivery, delivery_person
       │  ├─ pgvector semantic retrieval from existing item embeddings
       │  └─ transactional checkout and status changes
       └─ MongoDB Atlas: existing orders collection, password hashes and role links,
          audit_log, payment_gateway_sessions
```

PostgreSQL is the system of record for structured ordering data. The MongoDB `orders` collection retains the existing cloud document dashboard and is not claimed to mirror PostgreSQL order writes. MongoDB `users` documents store a hashed password, role, and `role_target_id`. For customer/staff sign-in, Flask validates that ID against the corresponding PostgreSQL table before establishing the session. Admin accounts continue using the existing environment-configured demo credential path.

## PostgreSQL data flow

The existing eight-table schema and its foreign-key, uniqueness, and positive-value constraints remain intact. Checkout locks and reloads requested menu rows, validates availability and restaurant grouping, recalculates totals, locks an available delivery-person row, and inserts orders, order_item rows, payment, and delivery in one PostgreSQL transaction. Restaurant and delivery transitions update only rows scoped to the authenticated target ID.

Razorpay gateway IDs and payment IDs are stored in an additive MongoDB `payment_gateway_sessions` document related to the PostgreSQL order/payment IDs. The PostgreSQL payment status and order status remain authoritative. Payment signatures are checked server-side before changing status to Paid. This avoids a PostgreSQL schema change while keeping secret and gateway-session fields out of the browser.

## Semantic search

The existing `/api/semantic-search?q=...` implementation is preserved. The model is lazy-loaded and cached once per process. Query vectors are normalized and compared to existing 384-dimensional vectors using pgvector cosine distance. Search does not regenerate menu embeddings or remove the existing HNSW index. Similarity is a ranking score, not calibrated confidence.

## Health and integrity

`/health` retains its public MongoDB connectivity check. Admin System Health actively pings PostgreSQL and MongoDB, checks pgvector extension availability, and counts populated embeddings. The Integrity Monitor keeps the recorded 4/4 PostgreSQL constraint-test evidence; it does not run destructive probes on each page view and does not describe those results as MongoDB checks.

## Sessions, roles, and CSRF

Flask signed sessions hold role, user ID, and the verified target ID. Cookies are HTTP-only and SameSite=Lax; Secure is controlled by `COOKIE_SECURE`. Authenticated mutation requests require a session CSRF token from a form or `X-CSRFToken` header. Restaurant owners are linked to one restaurant; delivery accounts are linked to one delivery-person record. Every relevant list and mutation filters by that target ID in SQL.

## MongoDB connectivity

The existing `food_ordering_db.orders` documents are preserved. New collections/documents are additive. Cross-database atomic transactions are not assumed: customer registration compensates by removing its Mongo account if PostgreSQL registration fails, and checkout/order/payment/delivery rows are committed together in PostgreSQL. Mongo audit/gateway reference failures do not rewrite the commerce records.
