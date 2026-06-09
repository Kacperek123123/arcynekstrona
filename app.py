import os
import hmac as hmac_module
import hashlib
import struct
import time as time_module
import base64
import secrets as secrets_module
import threading
from flask import Flask, render_template, session, redirect, request, url_for, jsonify
import requests as http
from supabase import create_client

# --- LOGIKA PODTRZYMANIA (KEEP-ALIVE) ---
app = Flask(__name__)

@app.route('/health')
def health_check():
    return "Bot jest aktywny!", 200

def ping_self():
    while True:
        try:
            http.get("http://localhost:5000/health")
        except:
            pass
        time_module.sleep(300)

threading.Thread(target=ping_self, daemon=True).start()
# ----------------------------------------

STEAM_ALPHABET = '23456789BCDFGHJKMNPQRTVWXY'

def generate_steam_code(shared_secret: str):
    try:
        try:
            secret = base64.b64decode(shared_secret)
        except Exception:
            secret = base64.b32decode(shared_secret.upper().replace(' ', ''))

        ts = int(time_module.time())
        timestamp = ts // 30
        seconds_remaining = 30 - (ts % 30)

        msg = struct.pack('>Q', timestamp)
        digest = hmac_module.new(secret, msg, hashlib.sha1).digest()

        start = digest[19] & 0x0F
        code_int = struct.unpack('>I', digest[start:start + 4])[0] & 0x7FFFFFFF

        code = ''
        for _ in range(5):
            code += STEAM_ALPHABET[code_int % len(STEAM_ALPHABET)]
            code_int //= len(STEAM_ALPHABET)

        return code, seconds_remaining
    except Exception:
        return None, 0

app.secret_key = os.environ.get("FLASK_SECRET_KEY", secrets_module.token_hex(32))

SUPABASE_URL = os.environ["SUPABASE_URL"]
SERVICE_KEY = os.environ["SUPABASE_SERVICE_KEY"]
DISCORD_CLIENT_ID = os.environ["DISCORD_CLIENT_ID"]
DISCORD_CLIENT_SECRET = os.environ["DISCORD_CLIENT_SECRET"]
ADMIN_IDS = [i.strip() for i in os.environ.get("ADMIN_IDS", "").split(",") if i.strip()]

supabase = create_client(SUPABASE_URL, SERVICE_KEY)

# --- POPRAWIONA FUNKCJA ---
def get_redirect_uri():
    return "https://arcynek.com.pl/auth/callback"
# --------------------------

def is_admin(user):
    return user and user.get("id") in ADMIN_IDS

@app.route("/")
def index():
    user = session.get("user")
    products = []
    sort = request.args.get("sort", "newest")
    if user:
        q = supabase.table("steam_accounts").select("id, name, price, image_url, description")
        if sort == "price_asc":
            q = q.order("price", desc=False)
        elif sort == "price_desc":
            q = q.order("price", desc=True)
        else:
            q = q.order("created_at", desc=True)
        products = q.execute().data or []
    return render_template("index.html", user=user, products=products, is_admin=is_admin(user), sort=sort)

@app.route("/produkt/<product_id>")
def product_detail(product_id):
    user = session.get("user")
    res = supabase.table("steam_accounts").select("id, name, price, image_url, description").eq("id", product_id).execute()
    if not res.data:
        return redirect(url_for("index"))
    product = res.data[0]
    return render_template("product.html", user=user, product=product, is_admin=is_admin(user))

@app.route("/login")
def login():
    state = secrets_module.token_hex(16)
    session["oauth_state"] = state
    redirect_uri = get_redirect_uri()
    params = (
        f"client_id={DISCORD_CLIENT_ID}"
        f"&redirect_uri={redirect_uri}"
        f"&response_type=code"
        f"&scope=identify"
        f"&state={state}"
    )
    return redirect(f"https://discord.com/api/oauth2/authorize?{params}")

@app.route("/auth/callback")
def auth_callback():
    code = request.args.get("code")
    state = request.args.get("state")
    if not code or state != session.get("oauth_state"):
        return redirect(url_for("index"))

    token_res = http.post("https://discord.com/api/oauth2/token", data={
        "client_id": DISCORD_CLIENT_ID,
        "client_secret": DISCORD_CLIENT_SECRET,
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": get_redirect_uri(),
    })
    token_data = token_res.json()
    access_token = token_data.get("access_token")
    if not access_token:
        return redirect(url_for("index"))

    user_res = http.get("https://discord.com/api/users/@me", headers={"Authorization": f"Bearer {access_token}"})
    u = user_res.json()
    
    session["user"] = {
        "id": u["id"],
        "username": u.get("global_name") or u.get("username"),
        "avatar": u.get("avatar"),
        "raw_username": u.get("username"),
    }
    
    # Rejestruj logowanie
    try:
        supabase.table("users_logins").upsert({"discord_id": u["id"]}).execute()
    except Exception as e:
        print(f"Błąd zapisu logowania: {e}")
        
    return redirect(url_for("index"))
@app.route("/logout")
def logout():
    session.pop("user", None)
    return redirect(url_for("index"))

@app.route("/moje-zamowienia")
def my_orders():
    user = session.get("user")
    if not user:
        return redirect(url_for("index"))
    try:
        res = supabase.table("orders").select("*, steam_accounts(name, login, password, image_url, guard_key)").eq("discord_id", user["id"]).order("created_at", desc=True).execute()
    except Exception:
        res = supabase.table("orders").select("*, steam_accounts(name, login, password, image_url)").eq("discord_id", user["id"]).order("created_at", desc=True).execute()
    orders = res.data or []
    return render_template("orders.html", user=user, orders=orders, is_admin=is_admin(user))

@app.route("/api/guard-code/<order_id>")
def get_guard_code(order_id):
    user = session.get("user")
    if not user:
        return jsonify({"error": "Nie jesteś zalogowany"}), 401
    try:
        rows = supabase.table("orders").select("discord_id, status, steam_accounts(guard_key)").eq("id", order_id).execute().data
        if not rows:
            return jsonify({"error": "Brak zamówienia"}), 404
        o = rows[0]
        if o["discord_id"] != user["id"]:
            return jsonify({"error": "Brak dostępu"}), 403
        if o["status"] != "completed":
            return jsonify({"error": "Zamówienie nie jest jeszcze zrealizowane"}), 403
        guard_key = (o.get("steam_accounts") or {}).get("guard_key")
        if not guard_key:
            return jsonify({"error": "Brak klucza Guard dla tego konta"}), 404
        code, seconds = generate_steam_code(guard_key)
        if not code:
            return jsonify({"error": "Nie można wygenerować kodu — sprawdź czy klucz jest poprawny"}), 500
        return jsonify({"code": code, "expires_in": seconds})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/orders", methods=["POST"])
def create_order():
    user = session.get("user")
    if not user:
        return jsonify({"error": "Nie jesteś zalogowany"}), 401
        
    if supabase.table("blacklist").select("discord_id").eq("discord_id", user["id"]).execute().data:
       return jsonify({"error": "Jesteś na czarnej liście"}), 403
    data = request.get_json()
    product_id = data.get("product_id")
    if not product_id:
        return jsonify({"error": "Brak ID produktu"}), 400
    
    # Usuwamy sprawdzenie czy produkt ma "sold": False oraz czy jest w "pending"
    # Teraz po prostu tworzymy zamówienie:
    order = supabase.table("orders").insert({
        "product_id": product_id,
        "discord_id": user["id"],
        "status": "pending",
        "payment_method": "manual",
    }).select().execute().data[0]
    return jsonify(order)

@app.route("/api/review", methods=["POST"])
def add_review():
    user = session.get("user")
    if not user: return jsonify({"error": "Brak logowania"}), 401
    data = request.get_json()
    # Zapisz do bazy
    supabase.table("reviews").insert({
        "discord_id": user["id"],
        "rating": data["rating"],
        "comment": data.get("comment", "")
    }).execute()
    # Tu później dołączymy kod wysyłający na Discord
    return jsonify({"success": True})

@app.route("/admin")
def admin():
    user = session.get("user")
    if not is_admin(user):
        return redirect(url_for("index"))
    products = supabase.table("steam_accounts").select("*").order("created_at", desc=True).execute().data or []
    orders = supabase.table("orders").select("id, discord_id, status, created_at, steam_accounts(name)").order("created_at", desc=True).limit(50).execute().data or []
    stats = {
        "available": sum(1 for p in products if not p.get("sold")),
        "sold": sum(1 for p in products if p.get("sold")),
        "pending": sum(1 for o in orders if o["status"] in ["pending", "awaiting_payment"]),
        "completed": sum(1 for o in orders if o["status"] == "completed"),
        "revenue": sum((p.get("price") or 0) for p in products if p.get("sold")),
    }
    guard_key_ready = any("guard_key" in p for p in products) or len(products) == 0
    return render_template("admin.html", user=user, products=products, orders=orders, stats=stats, guard_key_ready=guard_key_ready)

@app.route("/admin/add", methods=["POST"])
def admin_add():
    user = session.get("user")
    if not is_admin(user):
        return jsonify({"error": "Brak dostępu"}), 403
    data = {
        "name": request.form["name"],
        "login": request.form["login"],
        "password": request.form["password"],
        "price": float(request.form["price"]),
        "image_url": request.form.get("image_url", "").strip() or None,
        "description": request.form.get("description", "").strip() or None,
        "sold": False,
    }
    guard_key = request.form.get("guard_key", "").strip() or None
    try:
        if guard_key:
            data["guard_key"] = guard_key
        supabase.table("steam_accounts").insert(data).execute()
        return redirect(url_for("admin") + "?success=added")
    except Exception as e:
        err = str(e)
        if "guard_key" in err:
            data.pop("guard_key", None)
            try:
                supabase.table("steam_accounts").insert(data).execute()
                return redirect(url_for("admin") + "?success=added&warn=guard_key")
            except Exception as e2:
                return redirect(url_for("admin") + "?error=" + str(e2)[:200])
        return redirect(url_for("admin") + "?error=" + err[:200])

@app.route("/admin/delete/<product_id>", methods=["POST"])
def admin_delete(product_id):
    user = session.get("user")
    if not is_admin(user):
        return jsonify({"error": "Brak dostępu"}), 403
    supabase.table("orders").delete().eq("product_id", product_id).execute()
    supabase.table("steam_accounts").delete().eq("id", product_id).execute()
    return redirect(url_for("admin"))

@app.route("/admin/toggle/<product_id>", methods=["POST"])
def admin_toggle(product_id):
    user = session.get("user")
    if not is_admin(user):
        return jsonify({"error": "Brak dostępu"}), 403
    product = supabase.table("steam_accounts").select("sold").eq("id", product_id).execute().data
    if product:
        new_val = not product[0]["sold"]
        supabase.table("steam_accounts").update({"sold": new_val}).eq("id", product_id).execute()
    return redirect(url_for("admin"))

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=False)
