# FOODFLOW order flow

## Customer checkout

Customer adds available items to a session cart. The cart supports a single restaurant per order. At checkout, Flask reloads all item rows under PostgreSQL row locks, confirms availability and restaurant consistency, recalculates each line from the database price, locks an available delivery-person record, and then inserts `orders`, `order_item`, `payment`, and `delivery` within one transaction. Any SQL error rolls back the complete transaction. Browser totals are informational only.

The configured delivery charge is currently ₹0.00 because no delivery-fee setting exists in the project. Checkout auto-assigns the first available delivery person. If none is available, it does not create an order.

Demo checkout sets the PostgreSQL payment status to Pending and never reports payment as Paid. Razorpay TEST checkout creates a Pending Payment order and changes it to Placed/Paid only after server-side signature verification. Failed or cancelled gateway payment is not treated as successful.

## Status transitions

```text
Placed → Preparing → Ready for Pickup → Out for Delivery → Delivered
```

Restaurant owner can change Placed to Preparing and Preparing to Ready for Pickup, scoped to the linked restaurant. The assigned delivery person can change Assigned to Out for Delivery only after the order is Ready for Pickup, then Out for Delivery to Delivered. Delivery completion updates the order and makes the delivery person available again. A customer may cancel only a Placed order whose payment remains Pending. All other transitions are rejected server-side.

## Data ownership and access

- Customer history/detail queries include the authenticated customer ID.
- Restaurant order/menu queries and updates include the authenticated restaurant ID.
- Delivery reads/status updates include the authenticated delivery-person ID.
- PostgreSQL is the source of truth for commerce. Existing MongoDB order documents remain intact and are not claimed to sync with PostgreSQL orders.
- Audit activity is stored in MongoDB without passwords, card details, or gateway secrets.
