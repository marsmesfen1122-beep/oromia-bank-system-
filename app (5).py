import hashlib, secrets, sqlite3, uuid, random
from datetime import datetime, timedelta

import pandas as pd
import streamlit as st
import extras as X

DB = "oromia_bank_v2.db"
LOGO = __import__("os").path.join(__import__("os").path.dirname(__file__), "logo.png")
TIER_LIMITS = {"basic": 20_000.0, "standard": 50_000.0, "premium": 200_000.0}  # ETB per day
FRAUD_AMOUNT = 20_000.0  # flag transactions >= this amount
FRAUD_BURST = 5          # flag if > N transactions in 10 minutes
MAX_FAILS = 5

st.set_page_config(page_title="Oromia Bank", page_icon=LOGO, layout="wide")


# ---------------------------------------------------------------- database
def conn():
    c = sqlite3.connect(DB, timeout=10, isolation_level=None)
    c.row_factory = sqlite3.Row
    return c


def now():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def hash_pw(pw, salt):
    return hashlib.pbkdf2_hmac("sha256", pw.encode(), bytes.fromhex(salt), 100_000).hex()


def init_db():
    c = conn()
    c.executescript("""
    CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY, username TEXT UNIQUE, pw_hash TEXT, salt TEXT,
        role TEXT, full_name TEXT, phone TEXT, fails INTEGER DEFAULT 0, locked INTEGER DEFAULT 0, tier TEXT DEFAULT 'standard');
    CREATE TABLE IF NOT EXISTS accounts(id INTEGER PRIMARY KEY, acc_no TEXT UNIQUE, user_id INTEGER,
        type TEXT, balance REAL DEFAULT 0, status TEXT DEFAULT 'active');
    CREATE TABLE IF NOT EXISTS transactions(id INTEGER PRIMARY KEY, ref TEXT UNIQUE, idem_key TEXT UNIQUE,
        type TEXT, from_acc TEXT, to_acc TEXT, amount REAL, status TEXT, flag TEXT DEFAULT '',
        note TEXT DEFAULT '', created_by TEXT, created_at TEXT);
    CREATE TABLE IF NOT EXISTS notifications(id INTEGER PRIMARY KEY, user_id INTEGER, msg TEXT, created_at TEXT);
    CREATE TABLE IF NOT EXISTS audit(id INTEGER PRIMARY KEY, actor TEXT, action TEXT, detail TEXT, ts TEXT);
    CREATE TABLE IF NOT EXISTS ledger(id INTEGER PRIMARY KEY, ref TEXT, acc TEXT, debit REAL, credit REAL);
    CREATE TABLE IF NOT EXISTS scheduled(id INTEGER PRIMARY KEY, owner TEXT, from_acc TEXT, to_acc TEXT, amount REAL,
        every_days INTEGER, next_run TEXT, active INTEGER DEFAULT 1);
    """)
    if not c.execute("SELECT 1 FROM users").fetchone():
        for u, p, r, n, ph in [("admin", "admin123", "admin", "System Admin", "0911000001"),
                               ("teller", "teller123", "teller", "Tigist Bekele", "0911000002"),
                               ("abebe", "abebe123", "customer", "Abebe Kebede", "0911000003"),
                               ("chaltu", "chaltu123", "customer", "Chaltu Tadesse", "0911000004")]:
            create_user(c, u, p, r, n, ph, opening=10_000 if r == "customer" else 0)
    X.seed(c)
    c.close()


def create_user(c, username, pw, role, name, phone, opening=0.0):
    salt = secrets.token_hex(16)
    cur = c.execute("INSERT INTO users(username,pw_hash,salt,role,full_name,phone) VALUES(?,?,?,?,?,?)",
                    (username, hash_pw(pw, salt), salt, role, name, phone))
    if role == "customer":
        acc_no = "1000" + "".join(random.choices("0123456789", k=9))
        c.execute("INSERT INTO accounts(acc_no,user_id,type,balance) VALUES(?,?,?,?)",
                  (acc_no, cur.lastrowid, "savings", opening))
        if opening:
            post(c, "OPENING", None, acc_no, opening)


def audit(c, actor, action, detail=""):
    c.execute("INSERT INTO audit(actor,action,detail,ts) VALUES(?,?,?,?)", (actor, action, detail, now()))


def notify(c, user_id, msg):
    c.execute("INSERT INTO notifications(user_id,msg,created_at) VALUES(?,?,?)", (user_id, msg, now()))


# ---------------------------------------------------------------- transactions
def process(kind, src, dst, amount, idem, actor, note=""):
    """Atomic deposit / withdraw / transfer. Returns (ok, message)."""
    amount = round(float(amount), 2)
    if amount <= 0:
        return False, "Amount must be greater than zero."
    c = conn()
    try:
        c.execute("BEGIN IMMEDIATE")  # write lock -> prevents concurrent overdraw
        dup = c.execute("SELECT ref FROM transactions WHERE idem_key=?", (idem,)).fetchone()
        if dup:
            c.execute("ROLLBACK")
            return True, f"Duplicate request ignored (existing ref {dup['ref']})."

        def get(no):
            return c.execute("SELECT * FROM accounts WHERE acc_no=?", (no,)).fetchone() if no else None

        a_src, a_dst = get(src), get(dst)
        if kind in ("withdraw", "transfer") and not a_src:
            raise ValueError("Source account not found.")
        if kind in ("deposit", "transfer") and not a_dst:
            raise ValueError("Destination account not found.")
        if kind == "transfer" and src == dst:
            raise ValueError("Cannot transfer to the same account.")
        for a in (a_src, a_dst):
            if a and a["status"] != "active":
                raise ValueError(f"Account {a['acc_no']} is {a['status']}.")
        if a_src and kind != "deposit":
            if a_src["balance"] < amount:
                raise ValueError("Insufficient balance.")
            today = c.execute("SELECT COALESCE(SUM(amount),0) s FROM transactions WHERE from_acc=? AND status='completed'"
                              " AND created_at>=?", (src, now()[:10])).fetchone()["s"]
            tr_ = c.execute("SELECT tier FROM users WHERE id=?", (a_src["user_id"],)).fetchone()
            limit = TIER_LIMITS.get(tr_["tier"] if tr_ else "premium", 50_000.0)
            if today + amount > limit:
                raise ValueError(f"Daily limit of {limit:,.0f} ETB exceeded for your tier.")

        # fraud rules
        flag = []
        if amount >= FRAUD_AMOUNT:
            flag.append("large amount")
        if src:
            since = (datetime.now() - timedelta(minutes=10)).strftime("%Y-%m-%d %H:%M:%S")
            n = c.execute("SELECT COUNT(*) n FROM transactions WHERE from_acc=? AND created_at>=?", (src, since)).fetchone()["n"]
            if n >= FRAUD_BURST:
                flag.append("rapid transactions")
        if datetime.now().hour < 5:
            flag.append("unusual hour")

        if a_src and kind != "deposit":
            c.execute("UPDATE accounts SET balance=balance-? WHERE acc_no=?", (amount, src))
        if a_dst and kind != "withdraw":
            c.execute("UPDATE accounts SET balance=balance+? WHERE acc_no=?", (amount, dst))

        ref = "TXN" + datetime.now().strftime("%y%m%d%H%M%S") + uuid.uuid4().hex[:5].upper()
        c.execute("INSERT INTO transactions(ref,idem_key,type,from_acc,to_acc,amount,status,flag,note,created_by,created_at)"
                  " VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                  (ref, idem, kind, src, dst, amount, "completed", ", ".join(flag), note, actor, now()))
        post(c, ref, src, dst, amount)
        if a_src and kind != "deposit":
            notify(c, a_src["user_id"], f"DEBIT ETB {amount:,.2f} from {src}. Ref {ref}")
        if a_dst and kind != "withdraw":
            notify(c, a_dst["user_id"], f"CREDIT ETB {amount:,.2f} to {dst}. Ref {ref}")
        audit(c, actor, kind.upper(), f"{ref} {src or '-'} -> {dst or '-'} ETB {amount}")
        c.execute("COMMIT")
        return True, f"Success. Reference: {ref}" + (f"  ⚠️ flagged: {', '.join(flag)}" if flag else "")
    except Exception as e:
        try:
            c.execute("ROLLBACK")
        except Exception:
            pass
        return False, str(e)
    finally:
        c.close()


def post(c, ref, src, dst, amt):
    """Double-entry ledger: every movement debits one account and credits another (CASH = vault)."""
    c.execute("INSERT INTO ledger(ref,acc,debit,credit) VALUES(?,?,?,0)", (ref, src or "CASH", amt))
    c.execute("INSERT INTO ledger(ref,acc,debit,credit) VALUES(?,?,0,?)", (ref, dst or "CASH", amt))


def reverse(ref, actor):
    c = conn()
    try:
        c.execute("BEGIN IMMEDIATE")
        t = c.execute("SELECT * FROM transactions WHERE ref=?", (ref,)).fetchone()
        if not t or t["status"] != "completed":
            raise ValueError("Transaction not found or not reversible.")
        if t["to_acc"] and t["type"] != "withdraw":
            c.execute("UPDATE accounts SET balance=balance-? WHERE acc_no=?", (t["amount"], t["to_acc"]))
        if t["from_acc"] and t["type"] != "deposit":
            c.execute("UPDATE accounts SET balance=balance+? WHERE acc_no=?", (t["amount"], t["from_acc"]))
        c.execute("UPDATE transactions SET status='reversed' WHERE ref=?", (ref,))
        post(c, "REV-" + ref, t["to_acc"], t["from_acc"], t["amount"])
        audit(c, actor, "REVERSE", ref)
        c.execute("COMMIT")
        return True, f"{ref} reversed."
    except Exception as e:
        c.execute("ROLLBACK")
        return False, str(e)
    finally:
        c.close()


def q(sql, params=()):
    c = conn()
    try:
        return pd.read_sql_query(sql, c, params=params)
    finally:
        c.close()


def run(sql, params=()):
    c = conn()
    c.execute(sql, params)
    c.close()


# ---------------------------------------------------------------- auth
def login(username, pw):
    c = conn()
    u = c.execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()
    if not u:
        c.close(); return None, "Invalid credentials."
    if u["locked"]:
        c.close(); return None, "Account locked after too many failed attempts. Contact admin."
    if hash_pw(pw, u["salt"]) == u["pw_hash"]:
        c.execute("UPDATE users SET fails=0 WHERE id=?", (u["id"],))
        audit(c, username, "LOGIN"); c.close()
        return dict(u), ""
    fails = u["fails"] + 1
    c.execute("UPDATE users SET fails=?, locked=? WHERE id=?", (fails, int(fails >= MAX_FAILS), u["id"]))
    audit(c, username, "LOGIN_FAILED", f"attempt {fails}"); c.close()
    return None, "Invalid credentials."


def auth_screen():
    _, mid, _ = st.columns([1, 2, 1])
    mid.image(LOGO, use_container_width=True)
    st.markdown("<h3 style='text-align:center'>Real-Time Transaction System</h3>", unsafe_allow_html=True)
    t1, t2 = st.tabs(["Login", "Register (customer)"])
    with t1:
        u = st.text_input("Username", key="lu")
        p = st.text_input("Password", type="password", key="lp")
        if st.button("Login", type="primary"):
            user, err = login(u.strip(), p)
            if user:
                st.session_state.user = user
                st.rerun()
            st.error(err)
        st.caption("Demo: admin/admin123 · teller/teller123 · abebe/abebe123 · chaltu/chaltu123")
    with t2:
        name = st.text_input("Full name")
        phone = st.text_input("Phone (09XXXXXXXX)")
        ru = st.text_input("Choose username")
        rp = st.text_input("Choose password (min 6 chars)", type="password")
        if st.button("Create account"):
            if not (name and phone and ru and len(rp) >= 6):
                st.error("Fill all fields; password needs 6+ characters.")
            else:
                c = conn()
                try:
                    create_user(c, ru.strip(), rp, "customer", name, phone)
                    audit(c, ru, "REGISTER"); st.success("Registered. You can log in now.")
                except sqlite3.IntegrityError:
                    st.error("Username already taken.")
                finally:
                    c.close()


# ---------------------------------------------------------------- shared UI
def idem_key():
    st.session_state.setdefault("idem", uuid.uuid4().hex)
    return st.session_state.idem


def result(ok, msg):
    if ok:
        st.session_state.idem = uuid.uuid4().hex  # new key after success
        st.success(msg)
    else:
        st.error(msg)


def history_view(where="1=1", params=()):
    df = q(f"SELECT created_at,ref,type,from_acc,to_acc,amount,status,flag FROM transactions WHERE {where} "
           "ORDER BY id DESC", params)
    c1, c2, c3 = st.columns(3)
    types = c1.multiselect("Type", ["deposit", "withdraw", "transfer"])
    mn = c2.number_input("Min amount", 0.0, step=100.0)
    d = c3.text_input("Date starts with (YYYY-MM-DD)")
    if types: df = df[df["type"].isin(types)]
    df = df[df["amount"] >= mn]
    if d: df = df[df["created_at"].str.startswith(d)]
    st.dataframe(df, use_container_width=True, hide_index=True)
    st.download_button("⬇️ Export CSV", df.to_csv(index=False), "statement.csv", "text/csv")
    st.download_button("⬇️ Export PDF", X.pdf_bytes(st.session_state.user, df), "statement.pdf", "application/pdf")


# ---------------------------------------------------------------- customer pages
def customer(user, page):
    accs = q("SELECT acc_no,type,balance,status FROM accounts WHERE user_id=?", (user["id"],))
    mine = accs["acc_no"].tolist()
    if page == "My Account":
        st.subheader("My Accounts")
        for _, a in accs.iterrows():
            st.metric(f"{a.type.title()} · {a.acc_no} ({a.status})", f"ETB {a.balance:,.2f}")
        st.markdown("**Mini statement (last 5)**")
        ph = ",".join("?" * len(mine))
        st.dataframe(q(f"SELECT created_at,ref,type,from_acc,to_acc,amount FROM transactions WHERE from_acc IN ({ph}) "
                       f"OR to_acc IN ({ph}) ORDER BY id DESC LIMIT 5", mine + mine), hide_index=True)
    elif page == "Transfer":
        st.subheader("Transfer money")
        by = st.radio("Send to", ["Account number", "Phone number"], horizontal=True)
        target = st.text_input(by)
        amt = st.number_input("Amount (ETB)", 0.0, step=50.0)
        note = st.text_input("Note (optional)")
        otp = X.otp_ui(user)
        if st.button("Send", type="primary"):
            if not X.otp_valid(otp):
                st.error("Invalid or missing OTP. Click 'Request OTP' first."); st.stop()
            dst = target.strip()
            if by == "Phone number":
                r = q("SELECT a.acc_no FROM accounts a JOIN users u ON u.id=a.user_id WHERE u.phone=?", (dst,))
                dst = r.iloc[0, 0] if len(r) else ""
            result(*process("transfer", mine[0], dst, amt, idem_key(), user["username"], note))
    elif page == "History":
        st.subheader("Transaction history")
        ph = ",".join("?" * len(mine))
        history_view(f"(from_acc IN ({ph}) OR to_acc IN ({ph}))", mine + mine)
    elif page == "Notifications":
        st.subheader("Real-time notifications")
        live_notifications(user["id"])


@st.fragment(run_every=3)
def live_notifications(uid):
    n = q("SELECT created_at,msg FROM notifications WHERE user_id=? ORDER BY id DESC LIMIT 20", (uid,))
    st.caption("Auto-refreshing every 3 seconds")
    st.dataframe(n, use_container_width=True, hide_index=True)


# ---------------------------------------------------------------- teller pages
def teller(user, page):
    if page == "Deposit / Withdraw":
        st.subheader("Counter operations")
        kind = st.radio("Operation", ["deposit", "withdraw"], horizontal=True)
        acc = st.text_input("Account number")
        amt = st.number_input("Amount (ETB)", 0.0, step=100.0)
        if st.button("Submit", type="primary"):
            src, dst = (acc, None) if kind == "withdraw" else (None, acc)
            result(*process(kind, src, dst, amt, idem_key(), user["username"]))
    else:
        st.subheader("Customer search")
        s = st.text_input("Name, phone or account number")
        st.dataframe(q("SELECT u.full_name,u.phone,a.acc_no,a.balance,a.status FROM users u JOIN accounts a ON a.user_id=u.id "
                       "WHERE u.full_name LIKE ? OR u.phone LIKE ? OR a.acc_no LIKE ?", (f"%{s}%",) * 3), hide_index=True)


# ---------------------------------------------------------------- admin pages
@st.fragment(run_every=3)
def live_monitor():
    st.caption("Auto-refreshing every 3 seconds")
    df = q("SELECT created_at,ref,type,from_acc,to_acc,amount,status,flag FROM transactions ORDER BY id DESC LIMIT 25")
    st.dataframe(df, use_container_width=True, hide_index=True)


def admin(user, page):
    if page == "Live Monitor":
        st.subheader("Live transaction monitor")
        live_monitor()
        st.markdown("**🚩 Flagged transactions**")
        st.dataframe(q("SELECT created_at,ref,type,amount,flag,status FROM transactions WHERE flag!='' ORDER BY id DESC"),
                     use_container_width=True, hide_index=True)
        ref = st.text_input("Reverse transaction (enter reference)")
        if st.button("Reverse"):
            ok, m = reverse(ref.strip(), user["username"]); (st.success if ok else st.error)(m)
    elif page == "Accounts":
        st.subheader("Account control")
        st.dataframe(q("SELECT u.username,u.full_name,u.locked,a.acc_no,a.balance,a.status FROM accounts a "
                       "JOIN users u ON u.id=a.user_id"), hide_index=True, use_container_width=True)
        acc = st.text_input("Account number")
        c1, c2, c3 = st.columns(3)
        if c1.button("Freeze"): run("UPDATE accounts SET status='frozen' WHERE acc_no=?", (acc,)); st.warning("Frozen")
        if c2.button("Unfreeze"): run("UPDATE accounts SET status='active' WHERE acc_no=?", (acc,)); st.success("Active")
        usr = st.text_input("Unlock username")
        if c3.button("Unlock user"): run("UPDATE users SET locked=0,fails=0 WHERE username=?", (usr,)); st.success("Unlocked")
    elif page == "Reports":
        st.subheader("Daily report")
        df = q("SELECT * FROM transactions")
        today = df[df["created_at"].str.startswith(now()[:10])] if len(df) else df
        a, b, c = st.columns(3)
        a.metric("Transactions today", len(today))
        b.metric("Volume today (ETB)", f"{today['amount'].sum() if len(today) else 0:,.2f}")
        c.metric("Flagged", int((df["flag"] != "").sum()) if len(df) else 0)
        if len(df):
            df["hour"] = df["created_at"].str[:13]
            st.markdown("**Transactions per hour**"); st.bar_chart(df.groupby("hour").size())
            st.markdown("**Volume by type**"); st.bar_chart(df.groupby("type")["amount"].sum())
        bal = q("SELECT SUM(balance) s FROM accounts").iloc[0, 0] or 0
        st.info(f"Total customer balances held: ETB {bal:,.2f}")
    elif page == "Audit Log":
        st.subheader("Audit log")
        st.dataframe(q("SELECT ts,actor,action,detail FROM audit ORDER BY id DESC LIMIT 300"),
                     use_container_width=True, hide_index=True)


# ---------------------------------------------------------------- main
X.init(globals())
init_db()
X.run_due()
user = st.session_state.get("user")
if not user:
    auth_screen()
    st.stop()

PAGES = {"customer": ["My Account", "Transfer", "History", "Notifications"] + X.CUST,
         "teller": ["Deposit / Withdraw", "Customers"],
         "admin": ["Live Monitor", "Accounts", "Reports", "Audit Log"] + X.ADMIN}
with st.sidebar:
    st.image(LOGO, use_container_width=True)
    st.write(f"**{user['full_name']}** ({user['role']})")
    lang = st.selectbox("Language", ["en", "am", "om"],
                        format_func={"en": "English", "am": "አማርኛ", "om": "Afaan Oromoo"}.get)
    page = st.radio("Menu", PAGES[user["role"]], format_func=lambda p: X.tr(p, lang))
    if st.button("Logout"):
        audit_c = conn(); audit(audit_c, user["username"], "LOGOUT"); audit_c.close()
        st.session_state.clear(); st.rerun()

if page in X.EXTRA:
    X.render(user, page)
else:
    {"customer": customer, "teller": teller, "admin": admin}[user["role"]](user, page)
