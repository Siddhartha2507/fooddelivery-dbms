# FOODFLOW

**Smart Food Ordering & Delivery for Closed Communities**

FOODFLOW extends the existing Flask project into a controlled ordering platform for campuses, institutions, workplaces, hospitals, and residential communities. PostgreSQL is the primary source for customers, restaurants, menu items, orders, order items, payments, and deliveries. MongoDB Atlas retains the existing order-document collection and provides password-hash account records, role-to-record links, audit events, and Razorpay test-session references. Existing MongoDB orders and PostgreSQL data are preserved.

## Stack and architecture

- Flask, Jinja, HTML, CSS, JavaScript
- PostgreSQL / psycopg2, existing eight relational tables and constraints
- MongoDB Atlas / PyMongo, existing `food_ordering_db.orders` documents
- pgvector cosine search over existing 384-dimensional menu embeddings and HNSW index
- Cached Sentence Transformer `all-MiniLM-L6-v2`
- Optional Razorpay Checkout integration in **TEST/SANDBOX MODE ONLY**

See [ARCHITECTURE.md](ARCHITECTURE.md), [ORDER_FLOW.md](ORDER_FLOW.md), and [PAYMENT_GATEWAY.md](PAYMENT_GATEWAY.md).

## Roles

- **Customer:** self-registration, restaurant/menu browsing, semantic search, session cart, checkout, order history/tracking, profile.
- **Restaurant owner:** admin-provisioned login linked to one PostgreSQL restaurant; scoped orders, menu editing, operational counts.
- **Delivery person:** admin-provisioned login linked to one PostgreSQL delivery-person record; scoped assignments and status updates.
- **Admin:** existing configured demo login; MongoDB operations dashboard, PostgreSQL analytics, integrity, health, audit, and staff account provisioning.

Staff passwords are hashed in MongoDB. Their `role_target_id` maps to an existing PostgreSQL record and is revalidated at sign-in. No relational schema migration is required for that mapping. Commerce mutations are written to PostgreSQL transactions. MongoDB order documents are not claimed to synchronize with new PostgreSQL orders.

## Setup

1. Create a private `.env` with MongoDB and PostgreSQL connection settings. Keep `.env` out of source control.
2. Set a stable, high-entropy `SECRET_KEY`; set `COOKIE_SECURE=true` when served over HTTPS.
3. Install dependencies using `.\venv\Scripts\python.exe -m pip install -r requirements.txt`.
4. Confirm PostgreSQL and MongoDB connectivity. Do not run `generate_menu_embeddings.py` for routine startup; existing embeddings are reused.
5. Start with `.\venv\Scripts\python.exe app.py` and open `http://127.0.0.1:5000`.

Razorpay is optional. For gateway checkout only, configure `RAZORPAY_KEY_ID` beginning with `rzp_test_` and `RAZORPAY_KEY_SECRET` using Razorpay test credentials. When absent, checkout offers a clearly labeled demo payment that remains **Pending**; it never marks an order paid.

## Routes and APIs

- Auth: `/login`, `/register`, `/logout`
- Customer: `/customer`, `/customer/restaurants`, `/customer/menu`, `/customer/search`, `/customer/cart`, `/customer/checkout`, `/customer/orders`, `/customer/orders/<id>`, `/customer/profile`
- Restaurant: `/restaurant`, `/restaurant/orders`, `/restaurant/menu`, `/restaurant/analytics`, `/restaurant/profile`
- Delivery: `/delivery`, `/delivery/active`, `/delivery/history`, `/delivery/profile`
- Admin: `/admin`, `/admin/customers`, `/admin/restaurants`, `/admin/menu`, `/admin/orders`, `/admin/payments`, `/admin/deliveries`, `/admin/analytics`, `/admin/audit`, `/admin/integrity`, `/admin/health`, `/admin/accounts`
- Public existing health endpoint: `/health`
- Existing endpoints retained: `/api/dashboard`, `/api/orders`, `/api/semantic-search?q=...`
- Added APIs: `/api/restaurants`, `/api/restaurants/<id>/menu`, `/api/cart`, `/api/cart/items`, `/api/customer/orders`, `/api/payment/verify`, `/api/payment/failure`, `/api/delivery/assignments`, `/api/admin/analytics`

## Security and data handling

Passwords use Werkzeug password hashes. Role checks run server-side and verify target IDs against PostgreSQL. Forms and mutation APIs use session CSRF tokens. SQL values are parameterized; menu prices and order totals are loaded/recalculated server-side. Session cookies are HTTP-only and SameSite=Lax. Audit metadata excludes passwords and gateway secrets. No card data is stored. Razorpay secret credentials are server-side only; the test public key is sent to Checkout.

The Integrity Monitor retains the recorded 4/4 PostgreSQL constraint-test evidence; it is not represented as a live MongoDB test. The admin health page actively checks PostgreSQL, MongoDB Atlas, pgvector, and embedding counts with sanitized status output.

## Demo flow

See [DEMO_GUIDE.md](DEMO_GUIDE.md). Use the existing configured admin credentials (do not use the sample fallback outside local demos), provision restaurant/delivery accounts against existing IDs, register a customer, then browse and search existing menu data. Checkout creates real PostgreSQL order rows; avoid doing so during a demo unless prepared to keep that demonstration order. Customer demos default to payment pending. Gateway tests require Razorpay test credentials and never process real money.

## Known limits

- Razorpay hosted Checkout requires browser access to Razorpay's Checkout.js. Gateway flow is disabled unless test credentials are configured; local integration cannot be considered verified without a sandbox account.
- A test/demo checkout auto-assigns the first currently available delivery person. No GPS or delivery fee is implemented.
- Semantic ranking depends on existing item descriptions/embeddings. Similarity is a ranking score, not a probability; relevance can vary by query.
- In the current stored embeddings, the query “vegetarian food” ranks “Chicken Biryani” first. This is a known prototype relevance limitation.
- Existing MongoDB order documents remain the original dashboard's document view and are not continuously synchronized with PostgreSQL commerce writes.
- Admin lists are read-only except for staff account provisioning; destructive admin data management is intentionally not exposed.
