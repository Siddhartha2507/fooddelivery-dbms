# FOODFLOW demo guide

## Start

Use the project venv: `.\venv\Scripts\python.exe app.py`. Visit `http://127.0.0.1:5000`. Configure MongoDB/PostgreSQL in your private `.env`. Never show `.env` values in a recording. Admin uses the existing configured demo login; do not rely on sample fallback credentials outside a local demonstration.

## Admin

Open `/admin/analytics`, `/admin/customers`, `/admin/restaurants`, `/admin/orders`, `/admin/payments`, `/admin/deliveries`, `/admin/audit`, `/admin/integrity`, and `/admin/health`. Analytics reads actual PostgreSQL counts and sales. The integrity page displays recorded PostgreSQL constraint-test evidence, not live MongoDB tests. Use `/admin/accounts` to create restaurant-owner and delivery accounts linked to existing IDs; do not create staff with unrelated IDs.

## Customer

Register with a unique email, phone, and delivery address at `/register`. Registration creates a PostgreSQL customer record and a hashed-password MongoDB account. Browse `/customer/restaurants`, choose a menu, add items to the session cart, and check out. Totals and availability are read again server-side. The default demo-payment option creates a real PostgreSQL order but leaves payment **Pending**. This persists an order; avoid checkout during a read-only presentation or use a prepared demo account/order.

## Restaurant and delivery

Sign in with an admin-provisioned owner account. The restaurant owner sees only their mapped restaurant's orders/menu. Orders progress `Placed → Preparing → Ready for Pickup`. A mapped delivery person sees only their assignments, then progresses `Assigned → Out for Delivery → Delivered`. This is status-based delivery tracking; no GPS is implemented.

## Razorpay

Razorpay is **TEST/SANDBOX MODE ONLY**. Configure `RAZORPAY_KEY_ID` starting with `rzp_test_` and `RAZORPAY_KEY_SECRET` only when sandbox testing is intended. The server verifies the checkout signature. If credentials are absent, Razorpay is disabled and demo payment stays pending. Never use live credentials or present a pending demo order as paid.

## Semantic search

Try `spicy chicken`, `biryani`, and `vegetarian food` on Smart Search. Search uses existing PostgreSQL vectors and can return imperfect semantic matches. In the current data, `vegetarian food` ranks Chicken Biryani first; this is a prototype limitation, not a reliable dietary filter. It is not a complete recommendation engine. Routine search does not regenerate embeddings.
