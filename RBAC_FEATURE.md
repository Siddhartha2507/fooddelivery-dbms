# FOODFLOW role-based access

| Role | Account source | PostgreSQL target | Protected workflows |
| --- | --- | --- | --- |
| Customer | Self-registered MongoDB `users` document with Werkzeug password hash | `customer_id` | Restaurants, menu, cart, checkout, own order history/detail, own profile |
| Restaurant owner | Admin-provisioned MongoDB account | `restaurant_id` | Own orders, own menu items, restaurant analytics/profile |
| Delivery person | Admin-provisioned MongoDB account | `delivery_person_id` | Own assignments, status progression, history/profile |
| Admin | Existing environment-configured demo credential | Admin role | Operational dashboard, all admin views, analytics, audit, health, staff provisioning |

The `role_target_id` is server-side account metadata. At sign-in Flask verifies it exists in the correct PostgreSQL table. A customer cannot choose their own role or target. Restaurant/delivery route SQL includes the session target in each lookup and update. Customer order detail also filters by the authenticated customer ID. Missing records return 404; wrong roles return 403; unauthenticated APIs return 401 JSON.

Passwords are hashed with Werkzeug. The auth/account layer is stored in MongoDB as an extension; relational commerce data stays in PostgreSQL. Customer sign-up inserts the PostgreSQL customer record and a Mongo account record, compensating the Mongo insert if the SQL transaction fails. Admin provisions staff against existing restaurant/delivery-person rows at `/admin/accounts`.

POST/PATCH/DELETE requests require a session CSRF token, supplied as a hidden `csrf_token` form value or `X-CSRFToken` header. Session cookies are HTTP-only and SameSite=Lax. Set `COOKIE_SECURE=true` over HTTPS and configure a stable strong `SECRET_KEY` for deployments.

Admin demo credentials remain the existing `DEMO_USERNAME` / `DEMO_PASSWORD` compatibility settings. Change their local fallback before sharing the app. Staff credentials are never written to logs or sent to client-side JavaScript.
