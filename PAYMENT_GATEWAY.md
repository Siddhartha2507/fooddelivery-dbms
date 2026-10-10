# Razorpay gateway: test/sandbox only

Razorpay Checkout is optional and disabled unless both `RAZORPAY_KEY_ID` (must begin `rzp_test_`) and `RAZORPAY_KEY_SECRET` are configured. No live key is accepted. The secret is read only by Flask and never included in templates or JavaScript.

## Flow

1. Checkout reloads menu prices and availability and calculates the total in PostgreSQL.
2. PostgreSQL transaction creates the pending food order, order items, pending payment, and assigned delivery.
3. Flask creates a Razorpay TEST order through the server API using Basic authentication.
4. MongoDB stores the Razorpay order ID to PostgreSQL order/payment/customer reference.
5. The browser receives only the test public key, order ID, amount, and customer prefill and opens Checkout.js.
6. On return Flask checks HMAC-SHA256 of `gateway_order_id|gateway_payment_id` using the server-side secret, checks the Mongo mapping and owning customer, and conditionally updates a still-pending PostgreSQL payment to Paid.
7. Duplicate verification does not add or duplicate payment rows. Failed/cancelled checkout marks a still-pending payment/order accordingly and releases the assigned courier.

No card data is received by FOODFLOW. The gateway path could not be verified end-to-end without configured Razorpay TEST credentials and browser checkout access. Do not describe it as live payment processing.

## No credentials configured

The Flask app still starts normally. Razorpay is disabled at checkout and a **Demo Payment (Pending)** option remains available. The demo option does not fabricate a Paid state.

## Environment

```dotenv
RAZORPAY_KEY_ID=rzp_test_your_public_test_key
RAZORPAY_KEY_SECRET=your_test_secret
```

Never add these values to frontend code, documentation, source control, logs, or screenshots. Keep them in a private environment file or secret manager.
