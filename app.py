from flask import Flask, jsonify, render_template, request, session, redirect
import base64
import os
import sqlite3
from datetime import date, datetime, timedelta
from pathlib import Path

import requests
from werkzeug.security import check_password_hash, generate_password_hash


BASE_DIR = Path(__file__).resolve().parent
INSTANCE_DIR = BASE_DIR / "instance"
INSTANCE_DIR.mkdir(parents=True, exist_ok=True)


def get_db_path():
    return Path(os.environ.get("DATABASE_PATH", str(INSTANCE_DIR / "trustcircle.db")))


def get_db():
    db_path = get_db_path()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys = ON")
    return con


def ensure_schema():
    schema_path = BASE_DIR / "schema.sql"
    if not schema_path.exists():
        return

    with get_db() as con:
        con.executescript(schema_path.read_text())


def normalize_phone_number(phone_number):
    phone = str(phone_number or "").strip().replace(" ", "").replace("-", "")
    if not phone:
        return ""
    if phone.startswith("+"):
        phone = phone[1:]
    if phone.startswith("0"):
        phone = "254" + phone[1:]
    if phone.startswith("254") and len(phone) == 12:
        return phone
    return phone


def get_mpesa_config():
    env_name = (os.environ.get("MPESA_ENV") or "sandbox").lower()
    if env_name not in {"sandbox", "production"}:
        env_name = "sandbox"

    base_url = (
        "https://sandbox.safaricom.co.ke"
        if env_name == "sandbox"
        else "https://api.safaricom.co.ke"
    )

    return {
        "env": env_name,
        "base_url": base_url,
        "consumer_key": os.environ.get("MPESA_CONSUMER_KEY", ""),
        "consumer_secret": os.environ.get("MPESA_CONSUMER_SECRET", ""),
        "shortcode": os.environ.get("MPESA_SHORTCODE", "174379"),
        "passkey": os.environ.get("MPESA_PASSKEY", ""),
        "callback_url": os.environ.get(
            "MPESA_CALLBACK_URL",
            "https://example.com/api/mpesa/callback",
        ),
    }


def get_mpesa_access_token():
    config = get_mpesa_config()
    if not config["consumer_key"] or not config["consumer_secret"]:
        raise RuntimeError("M-Pesa credentials are not configured.")

    response = requests.get(
        f"{config['base_url']}/oauth/v1/generate?grant_type=client_credentials",
        auth=(config["consumer_key"], config["consumer_secret"]),
        timeout=20,
    )

    if response.status_code != 200:
        raise RuntimeError(f"M-Pesa token request failed: {response.text}")

    payload = response.json()
    token = payload.get("access_token")
    if not token:
        raise RuntimeError("M-Pesa token was not returned.")

    return token


def generate_mpesa_password(timestamp, shortcode, passkey):
    raw = f"{shortcode}{passkey}{timestamp}".encode()
    return base64.b64encode(raw).decode()


def initiate_mpesa_stk_push(amount, phone_number, account_reference="TrustCircle"):
    config = get_mpesa_config()
    if not config["passkey"] or not config["shortcode"]:
        raise RuntimeError("M-Pesa config is incomplete.")

    phone = normalize_phone_number(phone_number)
    if not phone:
        raise ValueError("Phone number is required.")

    amount_value = float(amount)
    if amount_value <= 0:
        raise ValueError("Amount must be greater than zero.")

    timestamp = datetime.now().strftime("%Y%m%d%H%M%S")
    password = generate_mpesa_password(timestamp, config["shortcode"], config["passkey"])
    token = get_mpesa_access_token()

    payload = {
        "BusinessShortCode": config["shortcode"],
        "Password": password,
        "Timestamp": timestamp,
        "TransactionType": "CustomerPayBillOnline",
        "Amount": int(amount_value),
        "PartyA": phone,
        "PartyB": config["shortcode"],
        "PhoneNumber": phone,
        "CallBackURL": config["callback_url"],
        "AccountReference": account_reference,
        "TransactionDesc": f"TrustCircle wallet top-up: {account_reference}",
    }

    response = requests.post(
        f"{config['base_url']}/mpesa/stkpush/v1/processrequest",
        json=payload,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
        timeout=30,
    )

    if response.status_code != 200:
        raise RuntimeError(f"M-Pesa STK push failed: {response.text}")

    return response.json()


def create_repayment_schedule(con, loan_id):
    loan = con.execute(
        """
        SELECT id, amount, term_months
        FROM loans
        WHERE id = ?
        """,
        (loan_id,),
    ).fetchone()

    if not loan:
        return

    existing = con.execute(
        """
        SELECT COUNT(*) AS count
        FROM repayments
        WHERE loan_id = ?
        """,
        (loan_id,),
    ).fetchone()

    if existing["count"] > 0:
        return

    principal = float(loan["amount"])
    total_repayment = principal + (principal * 0.18)
    months = int(loan["term_months"])
    instalment = total_repayment / months
    start_date = datetime.now()

    for number in range(1, months + 1):
        due_date = (start_date + timedelta(days=30 * number)).date().isoformat()
        con.execute(
            """
            INSERT INTO repayments (loan_id, amount, due_date, status)
            VALUES (?, ?, ?, ?)
            """,
            (loan_id, round(instalment, 2), due_date, "pending"),
        )


def mark_overdue_repayments(con):
    today = date.today().isoformat()
    con.execute(
        """
        UPDATE repayments
        SET status = 'overdue'
        WHERE due_date < ? AND status = 'pending'
        """,
        (today,),
    )

    con.execute(
        """
        UPDATE loans
        SET status = 'defaulted'
        WHERE id IN (
            SELECT loan_id
            FROM repayments
            WHERE status = 'overdue'
            GROUP BY loan_id
        )
        AND status != 'completed'
        """
    )


def disburse_loan_funds(con, loan_id):
    loan = con.execute(
        """
        SELECT id, borrower_id, amount, funded_amount, status
        FROM loans
        WHERE id = ?
        """,
        (loan_id,),
    ).fetchone()

    if not loan:
        return

    if loan["status"] == "disbursed":
        return

    borrower_wallet = con.execute(
        "SELECT balance FROM wallets WHERE user_id = ?",
        (loan["borrower_id"],),
    ).fetchone()

    if borrower_wallet is None:
        con.execute(
            "INSERT INTO wallets (user_id, balance) VALUES (?, ?)",
            (loan["borrower_id"], 0),
        )
        borrower_wallet = con.execute(
            "SELECT balance FROM wallets WHERE user_id = ?",
            (loan["borrower_id"],),
        ).fetchone()

    new_balance = float(borrower_wallet["balance"]) + float(loan["amount"])
    con.execute(
        "UPDATE wallets SET balance = ? WHERE user_id = ?",
        (new_balance, loan["borrower_id"]),
    )

    con.execute(
        """
        INSERT INTO transactions (user_id, loan_id, transaction_type, amount, reference)
        VALUES (?, ?, ?, ?, ?)
        """,
        (loan["borrower_id"], loan_id, "loan_disbursed", loan["amount"], f"Loan disbursed for loan {loan_id}"),
    )

    con.execute(
        "UPDATE loans SET status = 'disbursed' WHERE id = ?",
        (loan_id,),
    )

    create_repayment_schedule(con, loan_id)


def pay_repayment_for_id(con, repayment_id, user_id):
    repayment = con.execute(
        """
        SELECT r.*, l.borrower_id, l.status AS loan_status
        FROM repayments r
        JOIN loans l ON l.id = r.loan_id
        WHERE r.id = ?
        """,
        (repayment_id,),
    ).fetchone()

    if not repayment:
        return {"error": "Repayment not found."}, 404

    if repayment["borrower_id"] != user_id:
        return {"error": "You cannot pay this repayment."}, 403

    if repayment["status"] == "paid":
        return {"error": "This repayment has already been paid."}, 400

    wallet = con.execute(
        "SELECT balance FROM wallets WHERE user_id = ?",
        (user_id,),
    ).fetchone()

    if not wallet:
        return {"error": "Wallet not found."}, 404

    if float(repayment["amount"]) > float(wallet["balance"]):
        return {"error": "Insufficient wallet balance to complete this repayment."}, 400

    new_balance = float(wallet["balance"]) - float(repayment["amount"])
    con.execute(
        "UPDATE wallets SET balance = ? WHERE user_id = ?",
        (new_balance, user_id),
    )
    con.execute(
        """
        UPDATE repayments
        SET status = 'paid', paid_at = ?
        WHERE id = ?
        """,
        (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), repayment_id),
    )
    con.execute(
        """
        INSERT INTO transactions (user_id, loan_id, transaction_type, amount, reference)
        VALUES (?, ?, ?, ?, ?)
        """,
        (user_id, repayment["loan_id"], "repayment", repayment["amount"], f"Repayment for loan {repayment['loan_id']}"),
    )

    remaining = con.execute(
        """
        SELECT COUNT(*) AS count
        FROM repayments
        WHERE loan_id = ? AND status != 'paid'
        """,
        (repayment["loan_id"],),
    ).fetchone()

    if int(remaining["count"]) == 0:
        con.execute(
            "UPDATE loans SET status = 'completed' WHERE id = ?",
            (repayment["loan_id"],),
        )

    update_user_trust_score(user_id, 5)
    con.commit()

    return {
        "message": "Repayment successful.",
        "repayment_id": repayment_id,
        "amount": float(repayment["amount"]),
        "remaining_balance": new_balance,
    }, 200


def update_user_trust_score(user_id, delta):
    with get_db() as con:
        row = con.execute(
            "SELECT trust_score FROM users WHERE id = ?",
            (user_id,),
        ).fetchone()

        if not row:
            return None

        new_score = min(100, max(0, int(row["trust_score"]) + int(delta)))
        con.execute(
            "UPDATE users SET trust_score = ? WHERE id = ?",
            (new_score, user_id),
        )
        con.commit()
        return new_score


def get_current_user():
    user_id = session.get("user_id")
    if not user_id:
        return None

    with get_db() as con:
        user = con.execute(
            """
            SELECT id, full_name, email, trust_score, created_at
            FROM users
            WHERE id = ?
            """,
            (user_id,),
        ).fetchone()

    return user


def create_app():
    app = Flask(__name__)
    app.config.update(
        SECRET_KEY=os.environ.get("SECRET_KEY", "change-me-in-production"),
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=False,
        SESSION_PERMANENT=True,
        PERMANENT_SESSION_LIFETIME=timedelta(days=30),
        JSON_SORT_KEYS=False,
    )

    ensure_schema()

    @app.get("/")
    def home():
        return render_template("index.html")

    @app.get("/login")
    def login_page():
        if get_current_user():
            return redirect("/dashboard")
        return render_template("login.html")

    @app.get("/dashboard")
    def dashboard_page():
        if not get_current_user():
            return redirect("/login")
        return render_template("dashboard.html")

    @app.post("/api/register")
    def register():
        data = request.get_json()
        if not data:
            return jsonify({"error": "Registration information is required."}), 400

        full_name = str(data.get("full_name", "")).strip()
        email = str(data.get("email", "")).strip().lower()
        password = str(data.get("password", ""))

        if len(full_name) < 2:
            return jsonify({"error": "Please enter your full name."}), 400

        if not email or "@" not in email:
            return jsonify({"error": "Please enter a valid email address."}), 400

        if len(password) < 8:
            return jsonify({"error": "Password must contain at least 8 characters."}), 400

        password_hash = generate_password_hash(password)

        try:
            with get_db() as con:
                existing_user = con.execute(
                    "SELECT id FROM users WHERE email = ?",
                    (email,),
                ).fetchone()

                if existing_user:
                    return jsonify({"error": "An account with this email already exists."}), 409

                cursor = con.execute(
                    """
                    INSERT INTO users (full_name, email, password_hash, trust_score)
                    VALUES (?, ?, ?, ?)
                    """,
                    (full_name, email, password_hash, 70),
                )
                user_id = cursor.lastrowid

                con.execute(
                    "INSERT INTO wallets (user_id, balance) VALUES (?, ?)",
                    (user_id, 20000),
                )
                con.commit()

            session.clear()
            session["user_id"] = user_id

            return jsonify({
                "message": "Registration successful.",
                "user": {"id": user_id, "full_name": full_name, "email": email},
            }), 201
        except sqlite3.IntegrityError:
            return jsonify({"error": "An account with this email already exists."}), 409

    @app.post("/api/login")
    def login():
        data = request.get_json()
        if not data:
            return jsonify({"error": "Login information is required."}), 400

        email = str(data.get("email", "")).strip().lower()
        password = str(data.get("password", ""))

        with get_db() as con:
            user = con.execute(
                "SELECT * FROM users WHERE email = ?",
                (email,),
            ).fetchone()

        if not user:
            return jsonify({"error": "Invalid email address or password."}), 401

        if not check_password_hash(user["password_hash"], password):
            return jsonify({"error": "Invalid email address or password."}), 401

        session.clear()
        session["user_id"] = user["id"]

        return jsonify({
            "message": "Login successful.",
            "user": {
                "id": user["id"],
                "full_name": user["full_name"],
                "email": user["email"],
                "trust_score": user["trust_score"],
            },
        })

    @app.post("/api/logout")
    def logout():
        session.clear()
        return jsonify({"message": "You have been logged out."})

    @app.get("/api/me")
    def me():
        user = get_current_user()
        if not user:
            return jsonify({"logged_in": False})

        return jsonify({
            "logged_in": True,
            "user": {
                "id": user["id"],
                "full_name": user["full_name"],
                "email": user["email"],
                "trust_score": user["trust_score"],
            },
        })

    @app.get("/api/wallet")
    def wallet():
        user = get_current_user()
        if not user:
            return jsonify({"error": "Please login first."}), 401

        with get_db() as con:
            row = con.execute(
                "SELECT balance FROM wallets WHERE user_id = ?",
                (user["id"],),
            ).fetchone()

        if not row:
            return jsonify({"balance": 0})

        return jsonify({"balance": row["balance"]})

    @app.post("/api/wallet/deposit")
    def wallet_deposit():
        user = get_current_user()
        if not user:
            return jsonify({"error": "Please login first."}), 401

        data = request.get_json() or {}
        try:
            amount = float(data.get("amount", 0))
        except (TypeError, ValueError):
            return jsonify({"error": "Invalid deposit amount."}), 400

        if amount <= 0:
            return jsonify({"error": "Deposit amount must be greater than zero."}), 400

        with get_db() as con:
            wallet = con.execute(
                "SELECT balance FROM wallets WHERE user_id = ?",
                (user["id"],),
            ).fetchone()

            if not wallet:
                return jsonify({"error": "Wallet not found."}), 404

            new_balance = float(wallet["balance"]) + amount
            con.execute(
                "UPDATE wallets SET balance = ? WHERE user_id = ?",
                (new_balance, user["id"]),
            )
            con.execute(
                """
                INSERT INTO transactions (user_id, transaction_type, amount, reference)
                VALUES (?, ?, ?, ?)
                """,
                (user["id"], "deposit", amount, "Wallet deposit"),
            )
            con.commit()

        return jsonify({
            "message": "Deposit successful.",
            "amount": amount,
            "balance": new_balance,
        })

    @app.post("/api/wallet/mpesa")
    def wallet_mpesa_deposit():
        user = get_current_user()
        if not user:
            return jsonify({"error": "Please login first."}), 401

        data = request.get_json() or {}
        try:
            amount = float(data.get("amount", 0))
        except (TypeError, ValueError):
            return jsonify({"error": "Invalid amount."}), 400

        phone_number = str(data.get("phone_number", "")).strip()
        if amount <= 0:
            return jsonify({"error": "Amount must be greater than zero."}), 400

        try:
            result = initiate_mpesa_stk_push(amount, phone_number, account_reference=f"TrustCircle-{user['id']}")
            return jsonify({
                "message": "M-Pesa STK push request has been sent.",
                "amount": amount,
                "phone_number": normalize_phone_number(phone_number),
                "result": result,
            })
        except Exception as exc:
            print("MPESA ERROR:", exc)
            return jsonify({"error": str(exc)}), 500


    @app.post("/api/mpesa/callback")
    def mpesa_callback():
        payload = request.get_json(silent=True) or {}
        body = payload.get("Body", {})
        stk_callback = body.get("stkCallback", {})
        result_code = stk_callback.get("ResultCode")
        result_desc = stk_callback.get("ResultDesc")

        if result_code == 0:
            metadata = stk_callback.get("CallbackMetadata", {}).get("Item", [])
            callback_values = {}
            for item in metadata:
                name = item.get("Name")
                value = item.get("Value")
                if name:
                    callback_values[name] = value

            amount = callback_values.get("Amount")
            receipt = callback_values.get("MpesaReceiptNumber")
            phone = callback_values.get("PhoneNumber")
            if amount is not None and receipt:
                with get_db() as con:
                    con.execute(
                        "INSERT INTO transactions (user_id, transaction_type, amount, reference) VALUES (?, ?, ?, ?)",
                        (0, "mpesa_deposit", float(amount), f"M-Pesa receipt {receipt}"),
                    )
                    con.commit()

        return jsonify({"ResultCode": 0, "ResultDesc": result_desc or "Accepted"})

    @app.post("/api/wallet/withdraw")
    def wallet_withdraw():
        user = get_current_user()
        if not user:
            return jsonify({"error": "Please login first."}), 401

        data = request.get_json() or {}
        try:
            amount = float(data.get("amount", 0))
        except (TypeError, ValueError):
            return jsonify({"error": "Invalid withdrawal amount."}), 400

        if amount <= 0:
            return jsonify({"error": "Withdrawal amount must be greater than zero."}), 400

        with get_db() as con:
            wallet = con.execute(
                "SELECT balance FROM wallets WHERE user_id = ?",
                (user["id"],),
            ).fetchone()

            if not wallet:
                return jsonify({"error": "Wallet not found."}), 404

            if amount > wallet["balance"]:
                return jsonify({"error": "Insufficient wallet balance."}), 400

            new_balance = float(wallet["balance"]) - amount
            con.execute(
                "UPDATE wallets SET balance = ? WHERE user_id = ?",
                (new_balance, user["id"]),
            )
            con.execute(
                """
                INSERT INTO transactions (user_id, transaction_type, amount, reference)
                VALUES (?, ?, ?, ?)
                """,
                (user["id"], "withdrawal", amount, "Wallet withdrawal"),
            )
            con.commit()

        return jsonify({
            "message": "Withdrawal successful.",
            "amount": amount,
            "balance": new_balance,
        })

    @app.get("/api/transactions")
    def transactions():
        user = get_current_user()
        if not user:
            return jsonify({"error": "Please login first."}), 401

        with get_db() as con:
            rows = con.execute(
                """
                SELECT id, loan_id, transaction_type, amount, reference, created_at
                FROM transactions
                WHERE user_id = ?
                ORDER BY created_at DESC
                LIMIT 25
                """,
                (user["id"],),
            ).fetchall()

        return jsonify([dict(row) for row in rows])

    @app.get("/api/dashboard")
    def dashboard():
        user = get_current_user()
        if not user:
            return jsonify({"error": "Please login first."}), 401

        with get_db() as con:
            wallet = con.execute(
                "SELECT balance FROM wallets WHERE user_id = ?",
                (user["id"],),
            ).fetchone()

            my_loans = con.execute(
                """
                SELECT COALESCE(SUM(amount), 0) AS total_requested,
                        COALESCE(SUM(funded_amount), 0) AS total_funded
                FROM loans
                WHERE borrower_id = ?
                """,
                (user["id"],),
            ).fetchone()

            my_funding = con.execute(
                """
                SELECT COALESCE(SUM(amount), 0) AS total_lent,
                        COALESCE(SUM(amount * lender_return_rate), 0) AS total_expected_profit
                FROM contributions
                WHERE lender_id = ?
                """,
                (user["id"],),
            ).fetchone()

            recent_transactions = con.execute(
                """
                SELECT id, loan_id, transaction_type, amount, reference, created_at
                FROM transactions
                WHERE user_id = ?
                ORDER BY created_at DESC
                LIMIT 8
                """,
                (user["id"],),
            ).fetchall()

        wallet_balance = float(wallet["balance"]) if wallet else 0.0
        total_requested = float(my_loans["total_requested"]) if my_loans else 0.0
        total_funded = float(my_loans["total_funded"]) if my_loans else 0.0
        total_lent = float(my_funding["total_lent"]) if my_funding else 0.0
        total_expected_profit = float(my_funding["total_expected_profit"]) if my_funding else 0.0

        return jsonify({
            "user": {
                "id": user["id"],
                "full_name": user["full_name"],
                "email": user["email"],
                "trust_score": user["trust_score"],
            },
            "wallet_balance": wallet_balance,
            "summary": {
                "total_requested": total_requested,
                "total_funded": total_funded,
                "total_lent": total_lent,
                "expected_profit": total_expected_profit,
            },
            "recent_transactions": [dict(row) for row in recent_transactions],
        })

    @app.get("/api/loans")
    def loans():
        with get_db() as con:
            mark_overdue_repayments(con)
            rows = con.execute(
                """
                SELECT loans.*, users.full_name AS borrower_name, users.trust_score
                FROM loans
                JOIN users ON users.id = loans.borrower_id
                ORDER BY loans.created_at DESC
                """
            ).fetchall()

        return jsonify([dict(row) for row in rows])

    @app.post("/api/loans")
    def create_loan():
        user = get_current_user()
        if not user:
            return jsonify({"error": "Please login before requesting a loan."}), 401

        data = request.get_json()
        if not data:
            return jsonify({"error": "No loan information received."}), 400

        try:
            amount = float(data.get("amount", 0))
            term = int(data.get("term", 0))
        except (TypeError, ValueError):
            return jsonify({"error": "Invalid amount or repayment period."}), 400

        loan_type = str(data.get("type", "")).strip()
        description = str(data.get("description", "")).strip()

        if amount < 1000 or amount > 100000:
            return jsonify({"error": "Loan amount must be between KES 1,000 and KES 100,000."}), 400

        if term not in [1, 2, 3, 6]:
            return jsonify({"error": "Invalid repayment period."}), 400

        allowed_types = ["Personal", "Business", "School fees", "Emergency"]
        if loan_type not in allowed_types:
            return jsonify({"error": "Invalid loan type."}), 400

        with get_db() as con:
            cursor = con.execute(
                """
                INSERT INTO loans (borrower_id, loan_type, amount, funded_amount, term_months, description, status)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (user["id"], loan_type, amount, 0, term, description, "funding"),
            )
            loan_id = cursor.lastrowid
            con.commit()

        return jsonify({"message": "Loan request successfully saved.", "loan_id": loan_id}), 201

    @app.post("/api/loans/<int:loan_id>/fund")
    def fund_loan(loan_id):
        user = get_current_user()
        if not user:
            return jsonify({"error": "Please login before funding a loan."}), 401

        data = request.get_json()
        if not data:
            return jsonify({"error": "No funding information received."}), 400

        try:
            amount = float(data.get("amount", 0))
        except (TypeError, ValueError):
            return jsonify({"error": "Invalid funding amount."}), 400

        if amount < 100:
            return jsonify({"error": "Minimum contribution is KES 100."}), 400

        lender_id = user["id"]

        with get_db() as con:
            loan = con.execute(
                "SELECT * FROM loans WHERE id = ?",
                (loan_id,),
            ).fetchone()

            if not loan:
                return jsonify({"error": "Loan not found."}), 404

            if loan["borrower_id"] == lender_id:
                return jsonify({"error": "You cannot fund your own loan request."}), 400

            remaining = loan["amount"] - loan["funded_amount"]
            if remaining <= 0:
                return jsonify({"error": "This loan is already fully funded."}), 400

            if amount > remaining:
                return jsonify({"error": f"Only KES {remaining:,.0f} is still needed."}), 400

            wallet_row = con.execute(
                "SELECT * FROM wallets WHERE user_id = ?",
                (lender_id,),
            ).fetchone()

            if not wallet_row:
                return jsonify({"error": "Wallet not found."}), 404

            if amount > wallet_row["balance"]:
                return jsonify({"error": "Insufficient wallet balance."}), 400

            new_funded_amount = loan["funded_amount"] + amount
            new_balance = wallet_row["balance"] - amount
            new_status = "disbursed" if new_funded_amount >= loan["amount"] else "funding"

            try:
                con.execute(
                    """
                    INSERT INTO contributions (loan_id, lender_id, amount, lender_return_rate)
                    VALUES (?, ?, ?, ?)
                    """,
                    (loan_id, lender_id, amount, 0.12),
                )

                con.execute(
                    "UPDATE loans SET funded_amount = ?, status = ? WHERE id = ?",
                    (new_funded_amount, new_status, loan_id),
                )

                con.execute(
                    "UPDATE wallets SET balance = ? WHERE user_id = ?",
                    (new_balance, lender_id),
                )

                con.execute(
                    """
                    INSERT INTO transactions (user_id, loan_id, transaction_type, amount, reference)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (lender_id, loan_id, "funding", amount, f"Funding contribution to loan {loan_id}"),
                )

                if new_status == "disbursed":
                    disburse_loan_funds(con, loan_id)

                con.commit()
            except Exception as error:
                con.rollback()
                print("Funding error:", error)
                return jsonify({"error": "Funding transaction failed."}), 500

        return jsonify({
            "message": f"KES {amount:,.0f} successfully contributed.",
            "loan_id": loan_id,
            "contribution": amount,
            "funded_amount": new_funded_amount,
            "remaining": loan["amount"] - new_funded_amount,
            "wallet_balance": new_balance,
            "status": new_status,
        }), 201

    @app.get("/api/my-funding")
    def my_funding():
        user = get_current_user()
        if not user:
            return jsonify({"error": "Please login to view your funding."}), 401

        lender_id = user["id"]

        with get_db() as con:
            rows = con.execute(
                """
                SELECT contributions.id, contributions.loan_id, contributions.amount,
                        contributions.lender_return_rate, contributions.created_at,
                        loans.loan_type, loans.amount AS loan_amount, loans.funded_amount,
                        loans.term_months, loans.status, users.full_name AS borrower_name
                FROM contributions
                JOIN loans ON loans.id = contributions.loan_id
                JOIN users ON users.id = loans.borrower_id
                WHERE contributions.lender_id = ?
                ORDER BY contributions.created_at DESC
                """,
                (lender_id,),
            ).fetchall()

        funding = []
        total_contributed = 0
        total_expected_profit = 0
        total_expected_back = 0

        for row in rows:
            contribution = float(row["amount"])
            rate = float(row["lender_return_rate"])
            expected_profit = contribution * rate
            expected_back = contribution + expected_profit

            total_contributed += contribution
            total_expected_profit += expected_profit
            total_expected_back += expected_back

            funding.append({
                "id": row["id"],
                "loan_id": row["loan_id"],
                "borrower_name": row["borrower_name"],
                "loan_type": row["loan_type"],
                "contribution": contribution,
                "return_rate": rate,
                "expected_profit": expected_profit,
                "expected_back": expected_back,
                "loan_amount": row["loan_amount"],
                "funded_amount": row["funded_amount"],
                "term_months": row["term_months"],
                "status": row["status"],
                "created_at": row["created_at"],
            })

        return jsonify({
            "funding": funding,
            "summary": {
                "total_contributed": total_contributed,
                "expected_profit": total_expected_profit,
                "expected_back": total_expected_back,
            },
        })

    @app.get("/api/my-loans")
    def my_loans():
        user = get_current_user()
        if not user:
            return jsonify({"error": "Please login to view your loans."}), 401

        with get_db() as con:
            rows = con.execute(
                """
                SELECT id, loan_type, amount, funded_amount, term_months, description, status, created_at
                FROM loans
                WHERE borrower_id = ?
                ORDER BY created_at DESC
                """,
                (user["id"],),
            ).fetchall()

        loans_list = []
        total_requested = 0
        total_funded = 0
        total_remaining = 0

        for row in rows:
            amount = float(row["amount"])
            funded = float(row["funded_amount"])
            remaining = max(0, amount - funded)
            funding_percent = round(funded / amount * 100) if amount > 0 else 0

            total_requested += amount
            total_funded += funded
            total_remaining += remaining

            loans_list.append({
                "id": row["id"],
                "loan_type": row["loan_type"],
                "amount": amount,
                "funded_amount": funded,
                "remaining": remaining,
                "funding_percent": funding_percent,
                "term_months": row["term_months"],
                "description": row["description"],
                "status": row["status"],
                "created_at": row["created_at"],
            })

        return jsonify({
            "loans": loans_list,
            "summary": {
                "number_of_loans": len(loans_list),
                "total_requested": total_requested,
                "total_funded": total_funded,
                "total_remaining": total_remaining,
            },
        })

    @app.get("/api/repayments")
    def repayments():
        user = get_current_user()
        if not user:
            return jsonify({"error": "Please login first."}), 401

        with get_db() as con:
            mark_overdue_repayments(con)
            rows = con.execute(
                """
                SELECT r.id, r.loan_id, r.amount, r.due_date, r.paid_at, r.status,
                        l.loan_type, l.amount AS loan_amount
                FROM repayments r
                JOIN loans l ON l.id = r.loan_id
                WHERE l.borrower_id = ?
                ORDER BY r.due_date ASC
                """,
                (user["id"],),
            ).fetchall()

        return jsonify([dict(row) for row in rows])

    @app.post("/api/loans/<int:loan_id>/repay")
    def repay_loan(loan_id):
        user = get_current_user()
        if not user:
            return jsonify({"error": "Please login first."}), 401

        with get_db() as con:
            loan = con.execute(
                "SELECT borrower_id FROM loans WHERE id = ?",
                (loan_id,),
            ).fetchone()

            if not loan:
                return jsonify({"error": "Loan not found."}), 404

            if loan["borrower_id"] != user["id"]:
                return jsonify({"error": "You can only repay your own loan."}), 403

            mark_overdue_repayments(con)
            repayment = con.execute(
                """
                SELECT *
                FROM repayments
                WHERE loan_id = ? AND status != 'paid'
                ORDER BY due_date ASC
                LIMIT 1
                """,
                (loan_id,),
            ).fetchone()

            if not repayment:
                return jsonify({"error": "No outstanding repayment is due for this loan."}), 400

            result, status_code = pay_repayment_for_id(con, repayment["id"], user["id"])
            return jsonify(result), status_code

    @app.post("/api/repayments/<int:repayment_id>/pay")
    def pay_repayment(repayment_id):
        user = get_current_user()
        if not user:
            return jsonify({"error": "Please login first."}), 401

        with get_db() as con:
            mark_overdue_repayments(con)
            result, status_code = pay_repayment_for_id(con, repayment_id, user["id"])
            return jsonify(result), status_code

    return app


app = create_app()


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "5000")), debug=False)
