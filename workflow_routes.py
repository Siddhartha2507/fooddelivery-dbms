"""Schema-backed customer, restaurant, delivery, and admin workflows.

PostgreSQL remains the source of structured commerce records. MongoDB stores
password hashes/account-to-record links, audit events, and gateway references.
"""
from decimal import Decimal, InvalidOperation
from functools import wraps
from datetime import datetime
import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import urllib.error
import urllib.request

from flask import Blueprint, current_app, jsonify, render_template, request, session, abort, redirect, url_for, flash

bp = Blueprint("workflows", __name__)


def deps():
    return current_app.extensions["foodflow"]


def required(*roles):
    def decorate(view):
        @wraps(view)
        def wrapped(*args, **kwargs):
            if session.get("role") not in roles:
                if request.path.startswith("/api/"):
                    return jsonify(success=False, error="Access denied"), 403
                abort(403)
            return view(*args, **kwargs)
        return wrapped
    return decorate


def connection():
    return deps()["pg"]()


def actor_id():
    return int(session["target_id"])


def embedding_vector(text):
    try:
        values=deps()["embed_text"](text)
        return "["+",".join(str(float(value)) for value in values)+"]"
    except Exception as exc:
        current_app.logger.warning("Menu embedding refresh unavailable (%s)",type(exc).__name__)
        return None


def audit(action, entity, entity_id=None, metadata=None):
    d = deps()
    try:
        d["audit"].insert_one({"timestamp": datetime.utcnow(), "user_id": session.get("user_id"),
            "role": session.get("role"), "action": action, "entity": entity,
            "entity_id": str(entity_id) if entity_id is not None else None,
            "metadata": metadata or {}})
    except Exception:
        current_app.logger.warning("Audit event unavailable (%s)", action)


def rows(sql, args=()):
    conn = connection()
    try:
        with conn.cursor() as cur:
            cur.execute(sql, args)
            return cur.fetchall()
    finally:
        conn.close()


def query(sql, args=(), one=False):
    conn = connection()
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(sql, args)
                return cur.fetchone() if one else cur.fetchall()
    finally:
        conn.close()


def cart_data():
    cart = session.get("cart", {})
    if not cart:
        return [], Decimal("0.00")
    ids = [int(key) for key in cart if str(key).isdigit()]
    if not ids:
        session["cart"] = {}
        return [], Decimal("0.00")
    items = rows("SELECT m.item_id,m.item_name,m.price,m.availability,m.restaurant_id,r.name "
                 "FROM menu_item m JOIN restaurant r USING(restaurant_id) WHERE m.item_id=ANY(%s)", (ids,))
    output, total = [], Decimal("0.00")
    clean = {}
    for item_id, name, price, available, restaurant_id, restaurant in items:
        qty = max(1, min(50, int(cart.get(str(item_id), 1))))
        clean[str(item_id)] = qty
        line = Decimal(price) * qty
        output.append({"item_id": item_id, "name": name, "price": Decimal(price), "quantity": qty,
                       "available": bool(available), "restaurant_id": restaurant_id,
                       "restaurant": restaurant, "line_total": line})
        total += line
    session["cart"] = clean
    return output, total


def cart_mutate(item_id, quantity):
    item = rows("SELECT availability,restaurant_id FROM menu_item WHERE item_id=%s", (item_id,))
    if not item:
        return "Menu item not found", 404
    if not item[0][0]:
        return "This item is currently unavailable", 409
    cart = session.get("cart", {})
    restaurant_ids = {row["restaurant_id"] for row in cart_data()[0]}
    if restaurant_ids and item[0][1] not in restaurant_ids:
        return "A cart can contain items from one restaurant at a time", 409
    if quantity <= 0:
        cart.pop(str(item_id), None)
    else:
        cart[str(item_id)] = min(quantity, 50)
    session["cart"] = cart
    return None, 200


@bp.get("/customer")
@required("customer")
def customer_home():
    restaurants = rows("SELECT restaurant_id,name,location FROM restaurant ORDER BY name")
    counts=rows("SELECT (SELECT count(*) FROM restaurant),(SELECT count(*) FROM menu_item WHERE availability IS TRUE)")[0]
    items = rows("SELECT m.item_id,m.item_name,m.price,m.availability,r.name,r.restaurant_id "
                 "FROM menu_item m JOIN restaurant r USING(restaurant_id) WHERE m.availability IS TRUE ORDER BY m.item_id LIMIT 8")
    orders = rows("SELECT order_id,status,total_amount FROM orders WHERE customer_id=%s ORDER BY order_date DESC LIMIT 3", (actor_id(),))
    return render_template("customer_home.html", name=session.get("display_name"), restaurants=restaurants, items=items, orders=orders,
        restaurant_count=counts[0],available_food_count=counts[1])


@bp.get("/customer/restaurants")
@required("customer")
def customer_restaurants():
    data = rows("SELECT r.restaurant_id,r.name,r.location,COUNT(m.item_id) FILTER(WHERE m.availability IS TRUE) "
                "FROM restaurant r LEFT JOIN menu_item m USING(restaurant_id) GROUP BY r.restaurant_id ORDER BY r.name")
    return render_template("restaurants.html", restaurants=data)


@bp.get("/customer/menu")
@required("customer")
def customer_menu():
    rid = request.args.get("restaurant_id", type=int)
    if rid:
        restaurant = rows("SELECT restaurant_id,name,location FROM restaurant WHERE restaurant_id=%s", (rid,))
        if not restaurant:
            abort(404)
        items = rows("SELECT item_id,item_name,price,availability FROM menu_item WHERE restaurant_id=%s ORDER BY item_name", (rid,))
        return render_template("menu.html", restaurants=restaurant, items=items, selected=rid)
    restaurants = rows("SELECT restaurant_id,name,location FROM restaurant ORDER BY name")
    return render_template("menu.html", restaurants=restaurants, items=[], selected=None)


@bp.get("/customer/search")
@required("customer")
def customer_search():
    return render_template("customer.html", name=session.get("display_name", "there"), search=True, query=request.args.get("q", "")[:300])


@bp.get("/customer/cart")
@required("customer")
def customer_cart():
    items, total = cart_data()
    return render_template("cart.html", items=items, total=total)


@bp.route("/customer/checkout", methods=["GET", "POST"])
@required("customer")
def customer_checkout():
    items, total = cart_data()
    profile = rows("SELECT name,email,phone,address FROM customer WHERE customer_id=%s", (actor_id(),))
    if not profile:
        abort(403)
    gateway_configured = bool(os.getenv("RAZORPAY_KEY_ID", "").startswith("rzp_test_") and os.getenv("RAZORPAY_KEY_SECRET"))
    if request.method == "GET":
        return render_template("checkout.html", items=items, total=total, profile=profile[0], gateway_configured=gateway_configured)
    if not items or not all(item["available"] for item in items):
        flash("Your cart is empty or includes an unavailable item.", "error")
        return redirect(url_for("workflows.customer_cart"))
    if len({item["restaurant_id"] for item in items}) != 1:
        flash("Choose items from one restaurant per order.", "error")
        return redirect(url_for("workflows.customer_cart"))
    payment_choice = request.form.get("payment_method", "demo")
    if payment_choice == "razorpay" and not gateway_configured:
        flash("Razorpay TEST MODE is not configured. Choose demo pending payment or configure test credentials.", "error")
        return redirect(url_for("workflows.customer_checkout"))
    # Refresh all items and totals inside the transaction; never trust submitted prices.
    conn = connection()
    try:
        with conn:
            with conn.cursor() as cur:
                ids = [item["item_id"] for item in items]
                cur.execute("SELECT item_id,price,availability,restaurant_id FROM menu_item WHERE item_id=ANY(%s) FOR UPDATE", (ids,))
                fresh = {row[0]: row[1:] for row in cur.fetchall()}
                if len(fresh) != len(items) or any(not fresh[i["item_id"]][1] for i in items):
                    raise ValueError("One or more menu items became unavailable")
                restaurant_id = next(iter({fresh[i["item_id"]][2] for i in items}))
                total = sum((Decimal(fresh[i["item_id"]][0]) * i["quantity"] for i in items), Decimal("0.00"))
                cur.execute("SELECT delivery_person_id FROM delivery_person WHERE availability IS TRUE ORDER BY delivery_person_id FOR UPDATE SKIP LOCKED LIMIT 1")
                courier = cur.fetchone()
                if not courier:
                    raise ValueError("No delivery person is currently available")
                status = "Pending Payment" if payment_choice == "razorpay" else "Placed"
                cur.execute("INSERT INTO orders(customer_id,restaurant_id,status,total_amount) VALUES(%s,%s,%s,%s) RETURNING order_id",
                            (actor_id(), restaurant_id, status, total))
                order_id = cur.fetchone()[0]
                for item in items:
                    cur.execute("INSERT INTO order_item(order_id,item_id,quantity,unit_price) VALUES(%s,%s,%s,%s)",
                                (order_id, item["item_id"], item["quantity"], fresh[item["item_id"]][0]))
                method = "Razorpay Test" if payment_choice == "razorpay" else "Demo Payment (Pending)"
                cur.execute("INSERT INTO payment(order_id,payment_method,payment_status,amount) VALUES(%s,%s,'Pending',%s) RETURNING payment_id",
                            (order_id, method, total))
                payment_id = cur.fetchone()[0]
                cur.execute("INSERT INTO delivery(order_id,delivery_person_id,delivery_status) VALUES(%s,%s,'Assigned')",
                            (order_id, courier[0]))
                cur.execute("UPDATE delivery_person SET availability=FALSE WHERE delivery_person_id=%s", (courier[0],))
        session.pop("cart", None)
        audit("order_created", "order", order_id, {"payment_method": method})
    except ValueError as exc:
        conn.rollback()
        flash(str(exc), "error")
        return redirect(url_for("workflows.customer_cart"))
    except Exception:
        conn.rollback()
        current_app.logger.exception("Checkout transaction failed")
        flash("We couldn't create your order. Your cart is still available; please retry.", "error")
        return redirect(url_for("workflows.customer_checkout"))
    finally:
        conn.close()
    if payment_choice == "razorpay":
        try:
            gw_order = razorpay_create_order(total, order_id)
            deps()["gateway_sessions"].insert_one({"gateway_order_id": gw_order["id"], "order_id": order_id,
                "payment_id": payment_id, "customer_id": actor_id(), "created_at": datetime.utcnow()})
            return render_template("payment.html", order_id=order_id, amount=total, amount_paise=int(Decimal(total)*100), gateway_order=gw_order["id"],
                public_key=os.getenv("RAZORPAY_KEY_ID"), customer=profile[0])
        except Exception:
            mark_payment(order_id, "Failed", "Cancelled")
            flash("Razorpay test checkout could not be started. No payment was taken.", "error")
            return redirect(url_for("workflows.customer_orders"))
    return redirect(url_for("workflows.customer_order_detail", order_id=order_id))


def razorpay_create_order(amount, order_id):
    key = os.getenv("RAZORPAY_KEY_ID", "")
    secret = os.getenv("RAZORPAY_KEY_SECRET", "")
    if not key.startswith("rzp_test_") or not secret:
        raise RuntimeError("Razorpay TEST MODE is not configured")
    token = base64.b64encode(f"{key}:{secret}".encode()).decode()
    body = json.dumps({"amount": int(Decimal(amount) * 100), "currency": "INR", "receipt": f"foodflow-{order_id}"}).encode()
    req = urllib.request.Request("https://api.razorpay.com/v1/orders", data=body,
        headers={"Authorization": "Basic " + token, "Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=12) as response:
        payload = json.loads(response.read().decode())
    if not payload.get("id"):
        raise RuntimeError("Gateway order was not returned")
    return payload


def mark_payment(order_id, payment_status, order_status):
    conn = connection()
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute("UPDATE payment SET payment_status=%s WHERE order_id=%s AND payment_status='Pending'", (payment_status, order_id))
                changed=cur.rowcount
                if changed:
                    cur.execute("UPDATE orders SET status=%s WHERE order_id=%s AND status IN ('Pending Payment','Placed')", (order_status, order_id))
                if changed and order_status == "Cancelled":
                    cur.execute("UPDATE delivery_person SET availability=TRUE WHERE delivery_person_id=(SELECT delivery_person_id FROM delivery WHERE order_id=%s)", (order_id,))
                    cur.execute("UPDATE delivery SET delivery_status='Cancelled' WHERE order_id=%s", (order_id,))
    finally:
        conn.close()


@bp.post("/api/payment/verify")
@required("customer")
def payment_verify():
    body = request.get_json(silent=True) or {}
    try: order_id = int(body.get("foodflow_order_id", 0))
    except (TypeError,ValueError): order_id = 0
    gateway_order = str(body.get("razorpay_order_id", ""))
    gateway_payment = str(body.get("razorpay_payment_id", ""))
    signature = str(body.get("razorpay_signature", ""))
    record = deps()["gateway_sessions"].find_one({"gateway_order_id": gateway_order, "customer_id": actor_id()})
    if not record or record.get("order_id") != order_id:
        return jsonify(success=False, error="Payment session not found"), 404
    secret = os.getenv("RAZORPAY_KEY_SECRET", "")
    expected = hmac.new(secret.encode(), f"{gateway_order}|{gateway_payment}".encode(), hashlib.sha256).hexdigest()
    if not secret or not hmac.compare_digest(expected, signature):
        mark_payment(order_id, "Failed", "Cancelled")
        return jsonify(success=False, error="Payment signature verification failed"), 400
    conn = connection()
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute("SELECT p.payment_status,o.customer_id FROM payment p JOIN orders o USING(order_id) WHERE p.order_id=%s FOR UPDATE", (order_id,))
                state = cur.fetchone()
                if not state or state[1] != actor_id():
                    return jsonify(success=False, error="Order not found"), 404
                if state[0] == "Paid":
                    return jsonify(success=record.get("gateway_payment_id") == gateway_payment, order_id=order_id), 200 if record.get("gateway_payment_id") == gateway_payment else 409
                if state[0] != "Pending":
                    return jsonify(success=False, error="Payment is no longer pending"), 409
                cur.execute("UPDATE payment SET payment_status='Paid' WHERE order_id=%s", (order_id,))
                cur.execute("UPDATE orders SET status='Placed' WHERE order_id=%s", (order_id,))
        deps()["gateway_sessions"].update_one({"_id": record["_id"]}, {"$set": {"gateway_payment_id": gateway_payment, "verified_at": datetime.utcnow()}})
        audit("payment_verified", "order", order_id)
        return jsonify(success=True, data={"order_id": order_id}), 200
    finally:
        conn.close()


@bp.post("/api/payment/create")
@required("customer")
def payment_create():
    if not (os.getenv("RAZORPAY_KEY_ID","").startswith("rzp_test_") and os.getenv("RAZORPAY_KEY_SECRET")):
        return jsonify(success=False,error="Razorpay TEST MODE is not configured"),503
    data=request.get_json(silent=True) or {}
    try: order_id=int(data.get("order_id",0))
    except (TypeError,ValueError): return jsonify(success=False,error="Invalid order ID"),400
    payment=rows("SELECT o.total_amount,p.payment_id,p.payment_status,o.status FROM orders o JOIN payment p USING(order_id) WHERE o.order_id=%s AND o.customer_id=%s AND p.payment_method='Razorpay Test'",(order_id,actor_id()))
    if not payment: return jsonify(success=False,error="Order not found"),404
    amount,payment_id,payment_status,order_status=payment[0]
    if payment_status!="Pending" or order_status!="Pending Payment":
        return jsonify(success=False,error="Order payment is no longer pending"),409
    prior=deps()["gateway_sessions"].find_one({"order_id":order_id,"customer_id":actor_id()})
    if prior: return jsonify(success=True,data={"gateway_order_id":prior["gateway_order_id"],"amount":float(amount),"public_key":os.getenv("RAZORPAY_KEY_ID")}),200
    try:
        gateway=razorpay_create_order(amount,order_id)
        deps()["gateway_sessions"].create_index("gateway_order_id",unique=True)
        deps()["gateway_sessions"].insert_one({"gateway_order_id":gateway["id"],"order_id":order_id,"payment_id":payment_id,"customer_id":actor_id(),"created_at":datetime.utcnow()})
        return jsonify(success=True,data={"gateway_order_id":gateway["id"],"amount":float(amount),"public_key":os.getenv("RAZORPAY_KEY_ID")}),201
    except Exception:
        current_app.logger.warning("Razorpay test order creation failed")
        return jsonify(success=False,error="Razorpay TEST MODE could not create a payment session"),503


@bp.get("/api/orders/<int:order_id>")
@required("customer","admin")
def api_order_detail(order_id):
    if session.get("role")=="customer":
        order=rows("SELECT order_id,order_date,status,total_amount FROM orders WHERE order_id=%s AND customer_id=%s",(order_id,actor_id()))
    else:
        order=rows("SELECT order_id,order_date,status,total_amount FROM orders WHERE order_id=%s",(order_id,))
    if not order: return jsonify(success=False,error="Order not found"),404
    items=rows("SELECT m.item_id,m.item_name,oi.quantity,oi.unit_price FROM order_item oi JOIN menu_item m USING(item_id) WHERE oi.order_id=%s",(order_id,))
    return jsonify(success=True,data={"order_id":order[0][0],"order_date":order[0][1].isoformat() if order[0][1] else None,"status":order[0][2],"total":float(order[0][3]),"items":[{"item_id":r[0],"name":r[1],"quantity":r[2],"unit_price":float(r[3])} for r in items]})


@bp.post("/api/payment/failure")
@required("customer")
def payment_failure():
    body = request.get_json(silent=True) or {}
    gateway_order = str(body.get("razorpay_order_id", ""))
    record = deps()["gateway_sessions"].find_one({"gateway_order_id": gateway_order, "customer_id": actor_id()})
    if not record:
        return jsonify(success=False, error="Payment session not found"), 404
    mark_payment(record["order_id"], "Failed", "Cancelled")
    audit("payment_failed", "order", record["order_id"])
    return jsonify(success=True), 200


@bp.get("/customer/orders")
@required("customer")
def customer_orders():
    orders = rows("SELECT o.order_id,o.order_date,o.status,o.total_amount,p.payment_status,r.name,d.delivery_status "
                  "FROM orders o JOIN restaurant r USING(restaurant_id) LEFT JOIN payment p USING(order_id) "
                  "LEFT JOIN delivery d USING(order_id) WHERE o.customer_id=%s ORDER BY o.order_date DESC", (actor_id(),))
    return render_template("orders.html", title="Your orders", orders=orders)


@bp.get("/customer/orders/<int:order_id>")
@required("customer")
def customer_order_detail(order_id):
    order = rows("SELECT o.order_id,o.order_date,o.status,o.total_amount,p.payment_status,p.payment_method,r.name,d.delivery_status "
                  "FROM orders o JOIN restaurant r USING(restaurant_id) LEFT JOIN payment p USING(order_id) LEFT JOIN delivery d USING(order_id) "
                  "WHERE o.order_id=%s AND o.customer_id=%s", (order_id, actor_id()))
    if not order:
        abort(404)
    items = rows("SELECT m.item_name,oi.quantity,oi.unit_price FROM order_item oi JOIN menu_item m USING(item_id) WHERE oi.order_id=%s", (order_id,))
    return render_template("order_detail.html", order=order[0], items=items)


@bp.post("/customer/orders/<int:order_id>/cancel")
@required("customer")
def customer_cancel_order(order_id):
    conn=connection()
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute("SELECT o.status,p.payment_status,d.delivery_person_id FROM orders o JOIN payment p USING(order_id) LEFT JOIN delivery d USING(order_id) WHERE o.order_id=%s AND o.customer_id=%s FOR UPDATE OF o,p",(order_id,actor_id()))
                rec=cur.fetchone()
                if not rec: abort(404)
                if rec[0]!="Placed" or rec[1]!="Pending": abort(400)
                cur.execute("UPDATE orders SET status='Cancelled' WHERE order_id=%s",(order_id,))
                cur.execute("UPDATE payment SET payment_status='Cancelled' WHERE order_id=%s",(order_id,))
                cur.execute("UPDATE delivery SET delivery_status='Cancelled' WHERE order_id=%s",(order_id,))
                if rec[2]: cur.execute("UPDATE delivery_person SET availability=TRUE WHERE delivery_person_id=%s",(rec[2],))
        audit("order_cancelled","order",order_id)
        flash("Order cancelled.","success")
    finally: conn.close()
    return redirect(url_for("workflows.customer_orders"))


@bp.route("/customer/profile", methods=["GET", "POST"])
@required("customer")
def customer_profile():
    if request.method == "POST":
        name=request.form.get("name","").strip()[:100]; phone=request.form.get("phone","").strip()[:20]; address=request.form.get("address","").strip()[:500]
        if not name or len(phone)<7 or not address:
            flash("Name, phone, and address are required.", "error")
        else:
            conn=connection()
            try:
                with conn:
                    with conn.cursor() as cur: cur.execute("UPDATE customer SET name=%s,phone=%s,address=%s WHERE customer_id=%s",(name,phone,address,actor_id()))
                deps()["users"].update_one({"email":session["email"]},{"$set":{"name":name,"phone":phone,"address":address}})
                session["display_name"]=name; flash("Profile updated.","success")
            except Exception: flash("Phone number may already be in use.","error")
            finally: conn.close()
        return redirect(url_for("workflows.customer_profile"))
    profile=rows("SELECT name,email,phone,address FROM customer WHERE customer_id=%s",(actor_id(),))
    return render_template("profile.html",profile=profile[0] if profile else None)


@bp.route("/api/cart", methods=["GET", "DELETE"])
@required("customer")
def api_cart():
    if request.method == "DELETE":
        session.pop("cart",None)
        return jsonify(success=True,data={"items":[],"subtotal":0}),200
    items,total=cart_data()
    return jsonify(success=True,data={"items":[{**i,"price":float(i["price"]),"line_total":float(i["line_total"])} for i in items],"subtotal":float(total)}),200


@bp.post("/api/cart/items")
@required("customer")
def api_cart_add():
    data=request.get_json(silent=True) or request.form
    try: item_id=int(data.get("item_id")); quantity=int(data.get("quantity",1))
    except (TypeError,ValueError): return jsonify(success=False,error="Invalid item or quantity"),400
    if quantity<1 or quantity>50: return jsonify(success=False,error="Quantity must be between 1 and 50"),400
    error,status=cart_mutate(item_id,quantity)
    if error: return jsonify(success=False,error=error),status
    return jsonify(success=True,data={"cart_count":sum(session.get("cart",{}).values())}),201


@bp.patch("/api/cart/items/<int:item_id>")
@required("customer")
def api_cart_update(item_id):
    data=request.get_json(silent=True) or {}
    try: quantity=int(data.get("quantity"))
    except (TypeError,ValueError): return jsonify(success=False,error="Invalid quantity"),400
    if quantity<0 or quantity>50: return jsonify(success=False,error="Quantity must be between 0 and 50"),400
    error,status=cart_mutate(item_id,quantity)
    return (jsonify(success=False,error=error),status) if error else (jsonify(success=True),200)


@bp.delete("/api/cart/items/<int:item_id>")
@required("customer")
def api_cart_remove(item_id):
    cart=session.get("cart",{}); cart.pop(str(item_id),None); session["cart"]=cart
    return jsonify(success=True),200


@bp.get("/restaurant")
@required("restaurant_owner")
def restaurant_home():
    rid=actor_id()
    counts=rows("SELECT count(*) FILTER(WHERE order_date::date=CURRENT_DATE),count(*) FILTER(WHERE status='Placed'),count(*) FILTER(WHERE status='Preparing'),count(*) FILTER(WHERE status='Delivered') FROM orders WHERE restaurant_id=%s",(rid,))[0]
    revenue=rows("SELECT COALESCE(sum(o.total_amount),0) FROM orders o JOIN payment p USING(order_id) WHERE o.restaurant_id=%s AND o.status='Delivered' AND p.payment_status='Paid'",(rid,))[0][0]
    return render_template("role_dashboard.html",role="Restaurant",title="Restaurant workspace",stats=[("Today's orders",counts[0]),("Pending",counts[1]),("Preparing",counts[2]),("Completed",counts[3]),("Paid delivered sales",revenue)],rows=[],links=[("Orders","/restaurant/orders"),("Menu","/restaurant/menu"),("Analytics","/restaurant/analytics"),("Profile","/restaurant/profile")])


@bp.get("/restaurant/orders")
@required("restaurant_owner")
def restaurant_orders():
    orders=rows("SELECT o.order_id,o.order_date,o.status,o.total_amount,c.name,p.payment_status FROM orders o JOIN customer c USING(customer_id) LEFT JOIN payment p USING(order_id) WHERE o.restaurant_id=%s ORDER BY o.order_date DESC",(actor_id(),))
    return render_template("orders.html",title="Restaurant orders",orders=orders,role="restaurant")


@bp.post("/restaurant/orders/<int:order_id>/status")
@bp.post("/api/restaurant/orders/<int:order_id>/status")
@required("restaurant_owner")
def restaurant_order_status(order_id):
    data=request.get_json(silent=True) or request.form
    target=data.get("status")
    allowed={"Placed":"Preparing","Preparing":"Ready for Pickup"}
    conn=connection()
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute("SELECT status FROM orders WHERE order_id=%s AND restaurant_id=%s FOR UPDATE",(order_id,actor_id()))
                order=cur.fetchone()
                if not order: abort(404)
                if allowed.get(order[0])!=target: abort(400)
                cur.execute("UPDATE orders SET status=%s WHERE order_id=%s",(target,order_id))
        audit("order_status_changed","order",order_id,{"status":target})
        flash("Order status updated.","success")
    finally: conn.close()
    return (jsonify(success=True),200) if request.is_json else redirect(url_for("workflows.restaurant_orders"))


@bp.route("/restaurant/menu",methods=["GET","POST"])
@required("restaurant_owner")
def restaurant_menu():
    rid=actor_id()
    if request.method=="POST":
        name=request.form.get("item_name","").strip()[:120]
        try: price=Decimal(request.form.get("price",""))
        except (InvalidOperation,TypeError,ValueError): price=Decimal("0")
        if not name or not price.is_finite() or price<=0 or price>Decimal("1000000"): flash("Enter an item name and a price between ₹0.01 and ₹1,000,000.","error")
        else:
            restaurant_name=rows("SELECT name FROM restaurant WHERE restaurant_id=%s",(rid,))[0][0]
            vector=embedding_vector(f"Food: {name}. Restaurant: {restaurant_name}")
            conn=connection()
            try:
                with conn:
                    with conn.cursor() as cur:
                        if vector:
                            cur.execute("INSERT INTO menu_item(restaurant_id,item_name,price,availability,embedding) VALUES(%s,%s,%s,%s,%s::vector)",(rid,name,price,request.form.get("availability")=="on",vector))
                        else:
                            cur.execute("INSERT INTO menu_item(restaurant_id,item_name,price,availability) VALUES(%s,%s,%s,%s)",(rid,name,price,request.form.get("availability")=="on"))
            finally: conn.close()
            audit("menu_item_created","restaurant",rid,{"item_name":name})
            flash("Menu item added.","success")
        return redirect(url_for("workflows.restaurant_menu"))
    items=rows("SELECT item_id,item_name,price,availability FROM menu_item WHERE restaurant_id=%s ORDER BY item_name",(rid,))
    return render_template("restaurant_menu.html",items=items)


@bp.post("/restaurant/menu/<int:item_id>")
@required("restaurant_owner")
def restaurant_menu_update(item_id):
    name=request.form.get("item_name","").strip()[:120]
    try: price=Decimal(request.form.get("price",""))
    except (InvalidOperation,TypeError,ValueError): price=Decimal("0")
    available=request.form.get("availability")=="on"
    if not name or not price.is_finite() or price<=0 or price>Decimal("1000000"): abort(400)
    conn=connection()
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute("SELECT item_name FROM menu_item WHERE item_id=%s AND restaurant_id=%s FOR UPDATE",(item_id,actor_id()))
                current=cur.fetchone()
                if not current: abort(404)
                if current[0] != name:
                    cur.execute("SELECT name FROM restaurant WHERE restaurant_id=%s",(actor_id(),))
                    restaurant_label=cur.fetchone()[0]
                    vector=embedding_vector(f"Food: {name}. Restaurant: {restaurant_label}")
                    if vector:
                        cur.execute("UPDATE menu_item SET item_name=%s,price=%s,availability=%s,embedding=%s::vector WHERE item_id=%s AND restaurant_id=%s",(name,price,available,vector,item_id,actor_id()))
                    else:
                        cur.execute("UPDATE menu_item SET item_name=%s,price=%s,availability=%s WHERE item_id=%s AND restaurant_id=%s",(name,price,available,item_id,actor_id()))
                else:
                    cur.execute("UPDATE menu_item SET price=%s,availability=%s WHERE item_id=%s AND restaurant_id=%s",(price,available,item_id,actor_id()))
                if cur.rowcount!=1: abort(404)
        audit("menu_item_updated","menu_item",item_id)
    finally: conn.close()
    return redirect(url_for("workflows.restaurant_menu"))


@bp.get("/restaurant/menu/add")
@required("restaurant_owner")
def restaurant_menu_add_page():
    return redirect(url_for("workflows.restaurant_menu"))


@bp.get("/restaurant/menu/edit/<int:item_id>")
@required("restaurant_owner")
def restaurant_menu_edit_page(item_id):
    own=rows("SELECT 1 FROM menu_item WHERE item_id=%s AND restaurant_id=%s",(item_id,actor_id()))
    if not own: abort(404)
    return redirect(url_for("workflows.restaurant_menu"))


@bp.get("/restaurant/orders/<int:order_id>")
@required("restaurant_owner")
def restaurant_order_detail(order_id):
    order=rows("SELECT o.order_id,o.order_date,o.status,o.total_amount,r.name,p.payment_status,p.payment_method,d.delivery_status FROM orders o JOIN restaurant r USING(restaurant_id) LEFT JOIN payment p USING(order_id) LEFT JOIN delivery d USING(order_id) WHERE o.order_id=%s AND o.restaurant_id=%s",(order_id,actor_id()))
    if not order: abort(404)
    items=rows("SELECT m.item_name,oi.quantity,oi.unit_price FROM order_item oi JOIN menu_item m USING(item_id) WHERE oi.order_id=%s",(order_id,))
    return render_template("order_detail.html",order=order[0],items=items)


@bp.get("/delivery/order/<int:order_id>")
@required("delivery_person")
def delivery_order_detail(order_id):
    data=rows("SELECT o.order_id,o.order_date,o.status,o.total_amount,r.name,p.payment_status,p.payment_method,d.delivery_status FROM orders o JOIN restaurant r USING(restaurant_id) LEFT JOIN payment p USING(order_id) JOIN delivery d USING(order_id) WHERE o.order_id=%s AND d.delivery_person_id=%s",(order_id,actor_id()))
    if not data: abort(404)
    items=rows("SELECT m.item_name,oi.quantity,oi.unit_price FROM order_item oi JOIN menu_item m USING(item_id) WHERE oi.order_id=%s",(order_id,))
    return render_template("order_detail.html",order=data[0],items=items,delivery=True)


@bp.get("/restaurant/analytics")
@required("restaurant_owner")
def restaurant_analytics():
    counts=rows("SELECT m.item_name,COALESCE(sum(oi.quantity),0) FROM menu_item m LEFT JOIN order_item oi USING(item_id) LEFT JOIN orders o ON o.order_id=oi.order_id AND o.restaurant_id=m.restaurant_id WHERE m.restaurant_id=%s GROUP BY m.item_id ORDER BY 2 DESC",(actor_id(),))
    return render_template("role_dashboard.html",role="Restaurant",title="Restaurant analytics",stats=[],rows=counts,links=[("Dashboard","/restaurant"),("Orders","/restaurant/orders"),("Menu","/restaurant/menu")])


@bp.get("/restaurant/profile")
@required("restaurant_owner")
def restaurant_profile():
    profile=rows("SELECT name,location,contact FROM restaurant WHERE restaurant_id=%s",(actor_id(),))
    if not profile: abort(404)
    return render_template("role_dashboard.html",role="Restaurant",title="Restaurant profile",stats=[],rows=profile,links=[("Dashboard","/restaurant"),("Orders","/restaurant/orders"),("Menu","/restaurant/menu")])


@bp.get("/delivery")
@required("delivery_person")
def delivery_home():
    data=rows("SELECT d.delivery_id,d.order_id,d.delivery_status,o.status,o.total_amount,r.name,c.address,c.name FROM delivery d JOIN orders o USING(order_id) JOIN restaurant r USING(restaurant_id) JOIN customer c USING(customer_id) WHERE d.delivery_person_id=%s ORDER BY d.delivery_date DESC NULLS LAST,d.delivery_id DESC",(actor_id(),))
    counts=rows("SELECT count(*) FILTER(WHERE delivery_status IN ('Assigned','Out for Delivery')),count(*) FILTER(WHERE delivery_status='Delivered') FROM delivery WHERE delivery_person_id=%s",(actor_id(),))[0]
    return render_template("role_dashboard.html",role="Delivery",title="Delivery status tracking",stats=[("Active deliveries",counts[0]),("Completed deliveries",counts[1])],rows=data,links=[("Active","/delivery/active"),("History","/delivery/history"),("Profile","/delivery/profile")])


@bp.get("/delivery/active")
@required("delivery_person")
def delivery_active():
    data=rows("SELECT d.delivery_id,d.order_id,d.delivery_status,o.status,o.total_amount,r.name,c.address,c.name FROM delivery d JOIN orders o USING(order_id) JOIN restaurant r USING(restaurant_id) JOIN customer c USING(customer_id) WHERE d.delivery_person_id=%s AND d.delivery_status IN ('Assigned','Out for Delivery') ORDER BY d.delivery_id",(actor_id(),))
    return render_template("role_dashboard.html",role="Delivery",title="Assigned deliveries",stats=[],rows=data,links=[("Dashboard","/delivery")])


@bp.get("/delivery/history")
@required("delivery_person")
def delivery_history():
    data=rows("SELECT d.delivery_id,d.order_id,d.delivery_status,o.status,o.total_amount,r.name,c.address,c.name FROM delivery d JOIN orders o USING(order_id) JOIN restaurant r USING(restaurant_id) JOIN customer c USING(customer_id) WHERE d.delivery_person_id=%s AND d.delivery_status='Delivered' ORDER BY d.delivery_id DESC",(actor_id(),))
    return render_template("role_dashboard.html",role="Delivery",title="Delivery history",stats=[],rows=data,links=[("Dashboard","/delivery")])


@bp.post("/delivery/<int:delivery_id>/status")
@bp.post("/api/delivery/<int:delivery_id>/status")
@required("delivery_person")
def delivery_status(delivery_id):
    data=request.get_json(silent=True) or request.form
    target=data.get("status")
    conn=connection()
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute("SELECT d.delivery_status,d.order_id,o.status FROM delivery d JOIN orders o USING(order_id) WHERE d.delivery_id=%s AND d.delivery_person_id=%s FOR UPDATE OF d,o",(delivery_id,actor_id()))
                rec=cur.fetchone()
                if not rec: abort(404)
                valid={"Assigned":("Out for Delivery","Ready for Pickup"),"Out for Delivery":("Delivered","Out for Delivery")}
                expected=valid.get(rec[0])
                if not expected or (target,expected[1])!=(target,rec[2]) or target!=expected[0]: abort(400)
                cur.execute("UPDATE delivery SET delivery_status=%s,delivery_date=CASE WHEN %s='Delivered' THEN CURRENT_TIMESTAMP ELSE delivery_date END WHERE delivery_id=%s",(target,target,delivery_id))
                cur.execute("UPDATE orders SET status=%s WHERE order_id=%s",(target,rec[1]))
                if target=="Delivered": cur.execute("UPDATE delivery_person SET availability=TRUE WHERE delivery_person_id=%s",(actor_id(),))
        audit("delivery_status_changed","delivery",delivery_id,{"status":target})
        flash("Delivery status updated.","success")
    finally: conn.close()
    return (jsonify(success=True),200) if request.is_json else redirect(url_for("workflows.delivery_home"))


@bp.get("/delivery/profile")
@required("delivery_person")
def delivery_profile():
    data=rows("SELECT name,phone,availability FROM delivery_person WHERE delivery_person_id=%s",(actor_id(),))
    if not data: abort(404)
    return render_template("role_dashboard.html",role="Delivery",title="Delivery profile",stats=[],rows=data,links=[("Dashboard","/delivery")])


@bp.get("/admin/analytics")
@required("admin")
def admin_analytics():
    totals=rows("SELECT (SELECT count(*) FROM customer),(SELECT count(*) FROM restaurant),(SELECT count(*) FROM menu_item),(SELECT count(*) FROM orders),(SELECT COALESCE(sum(total_amount),0) FROM orders WHERE status='Delivered'),(SELECT COALESCE(sum(total_amount),0) FROM orders)")[0]
    statuses=rows("SELECT status,count(*) FROM orders GROUP BY status ORDER BY status")
    payments=rows("SELECT payment_status,count(*) FROM payment GROUP BY payment_status ORDER BY payment_status")
    deliveries=rows("SELECT delivery_status,count(*) FROM delivery GROUP BY delivery_status ORDER BY delivery_status")
    restaurants=rows("SELECT r.name,count(o.order_id),COALESCE(sum(o.total_amount) FILTER(WHERE o.status='Delivered'),0) FROM restaurant r LEFT JOIN orders o USING(restaurant_id) GROUP BY r.restaurant_id ORDER BY r.name")
    return render_template("analytics.html",totals=totals,statuses=statuses,payments=payments,deliveries=deliveries,restaurants=restaurants)


@bp.get("/api/admin/analytics")
@required("admin")
def api_admin_analytics():
    totals=rows("SELECT (SELECT count(*) FROM customer),(SELECT count(*) FROM restaurant),(SELECT count(*) FROM menu_item),(SELECT count(*) FROM orders),(SELECT COALESCE(sum(total_amount),0) FROM orders WHERE status='Delivered'),(SELECT COALESCE(sum(total_amount),0) FROM orders),(SELECT count(*) FROM payment WHERE payment_status='Paid'),(SELECT count(*) FROM payment WHERE payment_status='Pending'),(SELECT count(*) FROM payment WHERE payment_status='Failed')")[0]
    statuses=rows("SELECT status,count(*) FROM orders GROUP BY status ORDER BY status")
    return jsonify(success=True,data={"totals":[int(v) if isinstance(v,int) else float(v) for v in totals],"orders_by_status":statuses})


@bp.get("/api/restaurants")
@required("customer","admin")
def api_restaurants():
    data=rows("SELECT restaurant_id,name,location FROM restaurant ORDER BY name")
    return jsonify(success=True,data=[{"restaurant_id":r[0],"name":r[1],"location":r[2]} for r in data])


@bp.get("/api/restaurants/<int:restaurant_id>/menu")
@required("customer","admin")
def api_restaurant_menu(restaurant_id):
    data=rows("SELECT item_id,item_name,price,availability FROM menu_item WHERE restaurant_id=%s ORDER BY item_name",(restaurant_id,))
    return jsonify(success=True,data=[{"item_id":r[0],"item_name":r[1],"price":float(r[2]),"availability":bool(r[3])} for r in data])


@bp.get("/api/customer/orders")
@required("customer")
def api_customer_orders():
    data=rows("SELECT order_id,order_date,status,total_amount FROM orders WHERE customer_id=%s ORDER BY order_date DESC",(actor_id(),))
    return jsonify(success=True,data=[{"order_id":r[0],"order_date":r[1].isoformat() if r[1] else None,"status":r[2],"total":float(r[3])} for r in data])


@bp.get("/api/delivery/assignments")
@required("delivery_person")
def api_delivery_assignments():
    data=rows("SELECT delivery_id,order_id,delivery_status FROM delivery WHERE delivery_person_id=%s ORDER BY delivery_id DESC",(actor_id(),))
    return jsonify(success=True,data=[{"delivery_id":r[0],"order_id":r[1],"status":r[2]} for r in data])


@bp.get("/admin/integrity")
@required("admin")
def admin_integrity_alias():
    return redirect(url_for("integrity_monitor"))


@bp.get("/admin/<section>")
@required("admin")
def admin_section(section):
    configs={"customers":("Customers","SELECT customer_id,name,email,phone,address FROM customer ORDER BY customer_id"),
        "restaurants":("Restaurants","SELECT restaurant_id,name,location,contact FROM restaurant ORDER BY restaurant_id"),
        "menu":("Menu items","SELECT m.item_id,m.item_name,m.price,m.availability,r.name FROM menu_item m JOIN restaurant r USING(restaurant_id) ORDER BY m.item_id"),
        "orders":("Orders","SELECT o.order_id,o.order_date,o.status,o.total_amount,c.name,r.name FROM orders o JOIN customer c USING(customer_id) JOIN restaurant r USING(restaurant_id) ORDER BY o.order_date DESC"),
        "payments":("Payments","SELECT payment_id,order_id,payment_method,payment_status,amount,payment_date FROM payment ORDER BY payment_id DESC"),
        "deliveries":("Deliveries","SELECT d.delivery_id,d.order_id,d.delivery_status,dp.name FROM delivery d JOIN delivery_person dp USING(delivery_person_id) ORDER BY d.delivery_id DESC")}
    if section not in configs: abort(404)
    title,sql=configs[section]
    data=rows(sql)
    return render_template("admin_table.html",title=title,section=section,rows=data)


@bp.route("/admin/accounts",methods=["GET","POST"])
@required("admin")
def admin_accounts():
    if request.method=="POST":
        name=request.form.get("name","").strip()[:100]; email=request.form.get("email","").strip().lower()[:254]; password=request.form.get("password",""); role=request.form.get("role")
        target_field="restaurant_target" if role=="restaurant_owner" else "delivery_target"
        try: target_id=int(request.form.get(target_field,""))
        except ValueError: target_id=0
        target={"restaurant_owner":("restaurant","restaurant_id"),"delivery_person":("delivery_person","delivery_person_id")}.get(role)
        if not name or not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+",email) or not target or target_id<1 or len(password)<10 or len(password)>1024: flash("Check the role, linked record, email, name, and password length.","error")
        else:
            linked=rows(f"SELECT 1 FROM {target[0]} WHERE {target[1]}=%s",(target_id,))
            if not linked: flash("The selected database record does not exist.","error")
            else:
                try:
                    deps()["users"].create_index("email",unique=True)
                    deps()["users"].create_index([("role",1),("role_target_id",1)],unique=True,
                        partialFilterExpression={"role_target_id":{"$exists":True}})
                    if deps()["users"].find_one({"role":role,"role_target_id":target_id}):
                        flash("That restaurant or delivery record already has a linked account.","error")
                        return redirect(url_for("workflows.admin_accounts"))
                    deps()["users"].insert_one({"email":email,"name":name,"role":role,"role_target_id":target_id,"password_hash":deps()["hash_password"](password),"created_at":datetime.utcnow()})
                    audit("staff_account_created","user",email,{"role":role,"target_id":target_id})
                    flash("Staff account created.","success")
                except Exception: flash("Email already exists or account storage is unavailable.","error")
        return redirect(url_for("workflows.admin_accounts"))
    restaurants=rows("SELECT restaurant_id,name FROM restaurant ORDER BY name")
    delivery=rows("SELECT delivery_person_id,name FROM delivery_person ORDER BY name")
    return render_template("admin_accounts.html",restaurants=restaurants,delivery_people=delivery)


@bp.get("/admin/audit")
@required("admin")
def audit_page():
    events=list(deps()["audit"].find({}, {"_id":0}).sort("timestamp",-1).limit(200))
    return render_template("audit.html",events=events)


@bp.get("/admin/health")
@bp.get("/system-health")
@required("admin")
def system_health():
    state={"Flask API":"CONNECTED","PostgreSQL":"UNAVAILABLE","MongoDB Atlas":"UNAVAILABLE","pgvector":"UNAVAILABLE","pgvector version":"—","Embeddings":"0 / 0","Razorpay TEST MODE":"CONFIGURED" if os.getenv("RAZORPAY_KEY_ID","").startswith("rzp_test_") and os.getenv("RAZORPAY_KEY_SECRET") else "NOT CONFIGURED"}
    conn=None
    try:
        conn=connection()
        with conn.cursor() as cur:
            cur.execute("SELECT extversion FROM pg_extension WHERE extname='vector'")
            vector=cur.fetchone()
            state["pgvector"]="CONNECTED" if vector else "UNAVAILABLE"
            state["pgvector version"]=vector[0] if vector else "—"
            cur.execute("SELECT count(*) FILTER(WHERE embedding IS NOT NULL),count(*) FROM menu_item")
            a,b=cur.fetchone(); state["Embeddings"]=f"{a} / {b}"
        state["PostgreSQL"]="CONNECTED"
    except Exception: pass
    finally:
        if conn: conn.close()
    try: deps()["mongo_client"].admin.command("ping"); state["MongoDB Atlas"]="CONNECTED"
    except Exception: pass
    return render_template("health.html",state=state)


def register_workflows(app):
    app.extensions["foodflow"]={"pg":app.config["FOODFLOW_PG"],"users":app.config["FOODFLOW_USERS"],
        "audit":app.config["FOODFLOW_AUDIT"],"gateway_sessions":app.config["FOODFLOW_GATEWAY_SESSIONS"],
        "mongo_client":app.config["FOODFLOW_MONGO_CLIENT"],"hash_password":app.config["FOODFLOW_HASH_PASSWORD"],
        "embed_text":app.config["FOODFLOW_EMBED_TEXT"]}
    app.register_blueprint(bp)
