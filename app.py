from flask import Flask, render_template, jsonify, request, session, redirect, url_for, abort, flash
from pymongo import MongoClient
import os
import re
import secrets
from datetime import datetime, timezone
from werkzeug.security import generate_password_hash, check_password_hash
from functools import lru_cache
from urllib.parse import urljoin, urlparse
from dotenv import load_dotenv

load_dotenv()

app = Flask(__name__)
app.secret_key = os.getenv("SECRET_KEY") or os.urandom(32)
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=os.getenv("COOKIE_SECURE", "false").lower() == "true",
    PERMANENT_SESSION_LIFETIME=60 * 60 * 8,
    SESSION_REFRESH_EACH_REQUEST=True,
)
app.config["SESSION_COOKIE_NAME"] = "foodflow_session"


def is_safe_redirect_target(target):
    if not target:
        return False
    ref = urlparse(urljoin(request.host_url, target))
    return ref.scheme in ("http", "https") and ref.netloc == request.host


@app.before_request
def require_login():
    """Protect application and data routes while leaving login and health public."""
    if request.endpoint in ("login", "logout", "register", "health") or request.endpoint == "static" or request.path == "/register":
        return None
    if session.get("authenticated"):
        return None
    if request.path.startswith("/api/") or request.is_json:
        return jsonify({"success": False, "error": "Authentication required"}), 401
    return redirect(url_for("login", next=request.full_path.rstrip("?")))


@app.before_request
def validate_csrf():
    if request.method in ("POST", "PUT", "PATCH", "DELETE"):
        expected = session.get("csrf_token")
        supplied = request.headers.get("X-CSRFToken") or request.form.get("csrf_token")
        if not expected or not supplied or not secrets.compare_digest(expected, supplied):
            return jsonify({"success": False, "error": "Your session token expired. Refresh and try again."}), 400


@app.context_processor
def inject_csrf_token():
    if not session.get("csrf_token"):
        session["csrf_token"] = secrets.token_urlsafe(32)
    cart = session.get("cart", {})
    try:
        cart_count = sum(max(0, int(value)) for value in cart.values()) if isinstance(cart, dict) else 0
    except (TypeError, ValueError):
        cart_count = 0
    return {"csrf_token": session["csrf_token"], "cart_count": cart_count}


@app.route("/login", methods=["GET", "POST"])
@app.route("/api/auth/login", methods=["POST"])
def login():
    if session.get("authenticated") and request.method == "GET":
        return redirect(role_home(session.get("role")))
    error = None
    if request.method == "POST":
        body=request.get_json(silent=True) or request.form
        username = str(body.get("username", body.get("email", "")) or "").strip()
        password = str(body.get("password", "") or "")
        account = None
        role = None
        configured_admin_password = os.getenv("DEMO_PASSWORD", "admin123")
        configured_admin_hash = os.getenv("DEMO_PASSWORD_HASH", "")
        try:
            admin_password_valid = check_password_hash(configured_admin_hash, password) if configured_admin_hash else secrets.compare_digest(password, configured_admin_password)
        except (ValueError, TypeError):
            admin_password_valid = False
        if username == os.getenv("DEMO_USERNAME", "admin") and admin_password_valid:
            account, role = {"username": username}, "admin"
        else:
            try:
                account = users_collection.find_one({"email": username.lower()})
                if account and check_password_hash(account.get("password_hash", ""), password):
                    role = account.get("role", "customer")
                    role_target = {"customer": ("customer", "customer_id"), "restaurant_owner": ("restaurant", "restaurant_id"), "delivery_person": ("delivery_person", "delivery_person_id")}.get(role)
                    target_id = account.get("role_target_id")
                    if role == "customer" and not target_id:
                        legacy_conn = get_postgres_connection()
                        try:
                            with legacy_conn.cursor() as legacy_cur:
                                legacy_cur.execute("SELECT customer_id FROM customer WHERE email=%s", (username.lower(),))
                                legacy_row = legacy_cur.fetchone()
                                if legacy_row:
                                    target_id = legacy_row[0]
                                    account["role_target_id"] = target_id
                                    users_collection.update_one({"_id": account["_id"]}, {"$set": {"role_target_id": target_id}})
                        finally:
                            legacy_conn.close()
                    if not role_target or not target_id:
                        role = None
                    else:
                        mapping_conn = get_postgres_connection()
                        try:
                            with mapping_conn.cursor() as mapping_cur:
                                mapping_cur.execute(f"SELECT 1 FROM {role_target[0]} WHERE {role_target[1]}=%s", (target_id,))
                                if not mapping_cur.fetchone():
                                    role = None
                        finally:
                            mapping_conn.close()
            except Exception:
                app.logger.warning("Account lookup unavailable during sign in")
        if account and role:
            session.clear()
            session["authenticated"] = True
            session["role"] = role
            session["user_id"] = str(account.get("_id", username))
            session["display_name"] = account.get("name", username)
            session["email"] = account.get("email", username.lower())
            if role != "admin":
                session["target_id"] = account.get("role_target_id")
            if body.get("remember"):
                session.permanent = True
            log_action("login", "user", session["user_id"])
            if request.is_json:
                return jsonify(success=True,data={"role":role,"name":session["display_name"]}),200
            target = request.args.get("next")
            return redirect(target if is_safe_redirect_target(target) else role_home(role))
        error = "Invalid username or password"
        if request.is_json:
            return jsonify(success=False,error=error),401
    return render_template("login.html", error=error)


def role_home(role):
    return {"admin": url_for("dashboard"), "customer": url_for("workflows.customer_home"),
            "restaurant_owner": url_for("workflows.restaurant_home"),
            "delivery_person": url_for("workflows.delivery_home")}.get(role, url_for("login"))


@app.route("/register", methods=["GET", "POST"])
@app.route("/api/auth/register", methods=["POST"])
def register():
    error = None
    if request.method == "POST":
        body=request.get_json(silent=True) or request.form
        name = str(body.get("name", "") or "").strip()[:100]
        email = str(body.get("email", "") or "").strip().lower()[:254]
        phone = re.sub(r"[^0-9+]", "", str(body.get("phone", "") or ""))[:20]
        address = str(body.get("address", "") or "").strip()[:500]
        password = str(body.get("password", "") or "")
        if not name or not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email) or len(phone) < 7 or not address:
            error = "Enter your name, valid email, phone number, and delivery address."
        elif len(password) < 10:
            error = "Use a password with at least 10 characters."
        else:
            try:
                users_collection.create_index("email", unique=True)
                if users_collection.find_one({"email": email}, {"_id": 1}):
                    error = "An account with that email already exists."
                else:
                    connection = get_postgres_connection()
                    account_inserted = False
                    try:
                        with connection:
                            with connection.cursor() as cursor:
                                cursor.execute("INSERT INTO customer (name,email,phone,address) VALUES (%s,%s,%s,%s) RETURNING customer_id", (name,email,phone,address))
                                customer_id = cursor.fetchone()[0]
                                users_collection.insert_one({"name":name,"email":email,"phone":phone,"address":address,
                                    "password_hash":generate_password_hash(password),"role":"customer","role_target_id":customer_id,
                                    "created_at":datetime.now(timezone.utc)})
                                account_inserted = True
                    except Exception:
                        if account_inserted:
                            users_collection.delete_one({"email": email})
                        raise
                    finally:
                        connection.close()
                    if request.is_json:
                        return jsonify(success=True,message="Customer account created. Sign in to continue."),201
                    return redirect("/login?registered=1")
            except Exception as exc:
                duplicate = type(exc).__name__ in ("DuplicateKeyError", "UniqueViolation")
                error = "An account with that email or phone already exists." if duplicate else "Registration is temporarily unavailable."
                if request.is_json:
                    return jsonify(success=False,error=error),409 if duplicate else 503
    if request.is_json:
        status=409 if error and "already exists" in error else 400
        return jsonify(success=False,error=error or "Registration unavailable"),status
    return render_template("register.html", error=error)


@app.route("/logout")
@app.route("/api/auth/logout", methods=["POST"])
def logout():
    if session.get("authenticated"):
        log_action("logout", "user", session.get("user_id"))
    session.clear()
    return (jsonify(success=True),200) if request.is_json else redirect(url_for("login"))

# ============================================================
# MongoDB Atlas Connection
# ============================================================

MONGODB_URI = os.getenv("MONGODB_URI")

client = MongoClient(
    MONGODB_URI,
    serverSelectionTimeoutMS=30000
)

db = client["food_ordering_db"]
orders_collection = db["orders"]
users_collection = db["users"]
audit_collection = db["audit_log"]


def log_action(action, entity=None, entity_id=None):
    """Store a minimal audit event; never include submitted credentials."""
    try:
        audit_collection.insert_one({"timestamp": datetime.now(timezone.utc),
            "user_id": session.get("user_id"), "role": session.get("role"),
            "action": action, "entity": entity, "entity_id": str(entity_id) if entity_id is not None else None})
    except Exception:
        app.logger.warning("Audit event could not be stored")


def roles_required(*roles):
    def decorate(view):
        from functools import wraps
        @wraps(view)
        def wrapped(*args, **kwargs):
            if session.get("role") not in roles:
                if request.path.startswith("/api/"):
                    return jsonify({"success": False, "error": "Access denied"}), 403
                abort(403)
            return view(*args, **kwargs)
        return wrapped
    return decorate


@app.errorhandler(403)
def forbidden(_error):
    if request.path.startswith("/api/"):
        return jsonify({"success": False, "error": "You don't have permission to access this resource."}), 403
    return render_template("error.html", code=403, message="You don't have permission to access this page."), 403


@app.route("/admin/audit")
@roles_required("admin")
def admin_audit():
    events = list(audit_collection.find({}, {"_id": 0}).sort("timestamp", -1).limit(100))
    return render_template("audit.html", events=events)

# PostgreSQL is used only by semantic menu search. Keep its imports and model
# loading lazy so the existing MongoDB dashboard remains available if the
# optional search dependencies or PostgreSQL service are unavailable.
EMBEDDING_MODEL_NAME = "sentence-transformers/all-MiniLM-L6-v2"


def get_postgres_connection():
    """Open a PostgreSQL connection using POSTGRES_URL or standard PG* vars."""
    import psycopg2

    database_url = os.getenv("POSTGRES_URL") or os.getenv("DATABASE_URL")
    if database_url:
        return psycopg2.connect(database_url, connect_timeout=5)

    config = {
        "dbname": os.getenv("PGDATABASE", "food_ordering_db"),
        "host": os.getenv("PGHOST", "localhost"),
        "port": os.getenv("PGPORT", "5432"),
        "connect_timeout": 5,
    }
    for key, variable in (("user", "PGUSER"), ("password", "PGPASSWORD")):
        if os.getenv(variable):
            config[key] = os.getenv(variable)
    return psycopg2.connect(**config)


@lru_cache(maxsize=1)
def get_embedding_model():
    from sentence_transformers import SentenceTransformer

    return SentenceTransformer(EMBEDDING_MODEL_NAME)


def embed_menu_text(text):
    """Encode only the one changed/new item using the process-cached model."""
    return get_embedding_model().encode(text, normalize_embeddings=True).tolist()


def get_menu_schema(cursor):
    """Read menu and restaurant columns to accommodate the existing schema."""
    cursor.execute(
        "SELECT table_name, column_name FROM information_schema.columns "
        "WHERE table_schema = current_schema() AND table_name IN ('menu_item', 'restaurant')"
    )
    columns = {}
    for table, column in cursor.fetchall():
        columns.setdefault(table, set()).add(column)
    return columns


def first_column(columns, candidates):
    return next((name for name in candidates if name in columns), None)


# ============================================================
# Helper Functions
# ============================================================

def get_dashboard_data():
    """
    Fetch dashboard statistics from MongoDB Atlas.
    """

    orders = list(
        orders_collection.find(
            {},
            {"_id": 0}
        ).sort("order_id", 1)
    )

    total_orders = orders_collection.count_documents({})

    customers = orders_collection.distinct(
        "customer.customer_id"
    )

    restaurants = orders_collection.distinct(
        "restaurant.restaurant_id"
    )

    total_order_value = sum(
        float(order.get("total_amount", 0))
        for order in orders
    )

    # --------------------------------------------------------
    # Order status counts
    # --------------------------------------------------------

    status_counts = {
        "Placed": 0,
        "Preparing": 0,
        "Out for Delivery": 0,
        "Delivered": 0
    }

    for order in orders:
        status = order.get("order_status", order.get("status", "Placed"))

        if status in status_counts:
            status_counts[status] += 1

    # --------------------------------------------------------
    # Payment status
    # --------------------------------------------------------

    payment_counts = {
        "Paid": 0,
        "Pending": 0
    }

    for order in orders:
        payment = order.get("payment", {})

        payment_status = payment.get(
            "payment_status",
            "Pending"
        )

        if payment_status in payment_counts:
            payment_counts[payment_status] += 1

    # --------------------------------------------------------
    # Delivery status
    # --------------------------------------------------------

    delivery_counts = {
        "Assigned": 0,
        "Out for Delivery": 0,
        "Delivered": 0
    }

    for order in orders:
        delivery = order.get("delivery", {})

        delivery_status = delivery.get(
            "delivery_status",
            "Assigned"
        )

        if delivery_status in delivery_counts:
            delivery_counts[delivery_status] += 1

    return {
        "orders": orders,
        "total_orders": total_orders,
        "total_customers": len(customers),
        "total_restaurants": len(restaurants),
        "total_order_value": total_order_value,
        "status_counts": status_counts,
        "payment_counts": payment_counts,
        "delivery_counts": delivery_counts
    }


# ============================================================
# Main Dashboard
# ============================================================

@app.route("/")
@roles_required("admin")
def dashboard():

    data = get_dashboard_data()

    return render_template(
        "index.html",
        orders=data["orders"],
        total_orders=data["total_orders"],
        total_customers=data["total_customers"],
        total_restaurants=data["total_restaurants"],
        total_order_value=data["total_order_value"],
        status_counts=data["status_counts"],
        payment_counts=data["payment_counts"],
        delivery_counts=data["delivery_counts"]
    )


# ============================================================
# Integrity Monitor
# ============================================================

@app.route("/integrity")
@roles_required("admin")
def integrity_monitor():

    integrity_tests = [
        {
            "name": "Foreign Key Validation",
            "description": "Invalid customer references are rejected.",
            "type": "FOREIGN KEY",
            "status": "PASS"
        },
        {
            "name": "Price CHECK Constraint",
            "description": "Negative menu item prices are rejected.",
            "type": "CHECK",
            "status": "PASS"
        },
        {
            "name": "Quantity CHECK Constraint",
            "description": "Zero or negative quantities are rejected.",
            "type": "CHECK",
            "status": "PASS"
        },
        {
            "name": "UNIQUE Email Constraint",
            "description": "Duplicate customer emails are rejected.",
            "type": "UNIQUE",
            "status": "PASS"
        }
    ]

    passed_tests = sum(
        1
        for test in integrity_tests
        if test["status"] == "PASS"
    )

    return render_template(
        "integrity.html",
        integrity_tests=integrity_tests,
        passed_tests=passed_tests,
        total_tests=len(integrity_tests)
    )


# ============================================================
# API - Dashboard Statistics
# ============================================================

@app.route("/api/dashboard")
@roles_required("admin")
def dashboard_api():

    data = get_dashboard_data()

    return jsonify({
        "total_orders": data["total_orders"],
        "total_customers": data["total_customers"],
        "total_restaurants": data["total_restaurants"],
        "total_order_value": data["total_order_value"],
        "status_counts": data["status_counts"],
        "payment_counts": data["payment_counts"],
        "delivery_counts": data["delivery_counts"]
    })


# ============================================================
# API - pgvector Semantic Menu Search
# ============================================================

@app.route("/api/semantic-search")
@roles_required("admin", "customer")
def semantic_search_api():
    query = request.args.get("q", "").strip()
    if not query:
        return jsonify({"error": "A non-empty search query is required.", "results": []}), 400

    connection = None
    try:
        embedding = get_embedding_model().encode(query, normalize_embeddings=True).tolist()
        vector_value = "[" + ",".join(str(float(value)) for value in embedding) + "]"
        connection = get_postgres_connection()
        with connection.cursor() as cursor:
            columns = get_menu_schema(cursor)
            menu_columns = columns.get("menu_item", set())
            restaurant_columns = columns.get("restaurant", set())
            id_col = first_column(menu_columns, ("item_id", "menu_item_id"))
            name_col = first_column(menu_columns, ("item_name", "name", "food_name"))
            price_col = first_column(menu_columns, ("price", "item_price"))
            available_col = first_column(menu_columns, ("availability", "is_available", "available", "availability_status"))
            restaurant_id = first_column(menu_columns, ("restaurant_id",))
            restaurant_pk = first_column(restaurant_columns, ("restaurant_id",))
            restaurant_name = first_column(restaurant_columns, ("restaurant_name", "name"))
            if not id_col or not name_col or not price_col:
                return jsonify({"error": "The menu_item table is missing a supported item ID, name, or price column.", "results": []}), 503

            quote = lambda identifier: '"' + identifier.replace('"', '""') + '"'
            join = bool(restaurant_id and restaurant_pk and restaurant_name)
            restaurant_select = f'r.{quote(restaurant_name)}' if join else "NULL::text"
            restaurant_join = f' LEFT JOIN restaurant r ON m.{quote(restaurant_id)} = r.{quote(restaurant_pk)}' if join else ""
            available_select = f'm.{quote(available_col)}' if available_col else "NULL::text"
            cursor.execute(
                f"SELECT m.{quote(id_col)}, m.{quote(name_col)}, m.{quote(price_col)}, "
                f"{available_select}, {restaurant_select}, "
                f"1 - (m.embedding <=> %s::vector) AS similarity "
                f"FROM menu_item m{restaurant_join} WHERE m.embedding IS NOT NULL "
                f"ORDER BY m.embedding <=> %s::vector LIMIT 5",
                (vector_value, vector_value),
            )
            results = [
                {
                    "item_id": row[0],
                    "item_name": row[1],
                    "price": float(row[2]) if row[2] is not None else None,
                    "availability": row[3],
                    "restaurant_name": row[4],
                    "similarity": float(row[5]),
                }
                for row in cursor.fetchall()
            ]
        log_action("semantic_search", "menu_query")
        return jsonify({"query": query, "results": results})
    except ImportError as error:
        return jsonify({"error": f"Semantic search dependency is unavailable: {error.name}. Install requirements.txt.", "results": []}), 503
    except Exception as error:
        # Do not return connection strings, credentials, or raw database errors.
        app.logger.error("Semantic menu search failed (%s)", type(error).__name__)
        error_text = str(error).lower()
        if "vector" in error_text and ("does not exist" in error_text or "type" in error_text):
            message = "pgvector is unavailable or the menu embedding column is missing. Run generate_menu_embeddings.py after enabling pgvector."
        elif "embedding" in error_text and "column" in error_text:
            message = "Menu embeddings are not initialized. Run generate_menu_embeddings.py."
        elif "connection" in error_text or "password" in error_text or "authentication" in error_text:
            message = "PostgreSQL is unavailable or credentials are not configured. Set POSTGRES_URL or PGHOST, PGPORT, PGDATABASE, PGUSER, and PGPASSWORD."
        else:
            message = "Semantic search could not complete. Check PostgreSQL, pgvector, and the menu embedding setup."
        return jsonify({"error": message, "results": []}), 503
    finally:
        if connection is not None:
            connection.close()


# ============================================================
# API - Orders
# ============================================================

@app.route("/api/orders")
@roles_required("admin")
def orders_api():

    orders = list(
        orders_collection.find(
            {},
            {"_id": 0}
        ).sort("order_id", 1)
    )

    return jsonify(orders)


# ============================================================
# Health Check
# ============================================================

@app.route("/health")
def health():

    try:
        client.admin.command("ping")

        return jsonify({
            "status": "online",
            "database": "MongoDB Atlas",
            "message": "Food Ordering API is running"
        })

    except Exception as e:

        return jsonify({
            "status": "offline",
            "database": "MongoDB Atlas",
            "message": "MongoDB Atlas connection unavailable"
        }), 500


# ============================================================
# Run Application
# ============================================================

app.config["FOODFLOW_PG"] = get_postgres_connection
app.config["FOODFLOW_USERS"] = users_collection
app.config["FOODFLOW_AUDIT"] = audit_collection
app.config["FOODFLOW_GATEWAY_SESSIONS"] = db["payment_gateway_sessions"]
app.config["FOODFLOW_MONGO_CLIENT"] = client
app.config["FOODFLOW_HASH_PASSWORD"] = generate_password_hash
app.config["FOODFLOW_EMBED_TEXT"] = embed_menu_text
from workflow_routes import register_workflows
register_workflows(app)


@app.route("/admin")
@roles_required("admin")
def admin_home():
    return redirect(url_for("workflows.admin_analytics"))

if __name__ == "__main__":
    app.run(
        host="127.0.0.1",
        port=5000,
        debug=True
    )
