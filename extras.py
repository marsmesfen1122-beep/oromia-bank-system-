import io, random, secrets, threading, time, uuid
from datetime import datetime, timedelta
from types import SimpleNamespace

import pandas as pd
import streamlit as st

E = None
BILLERS = {"Ethiopian Electric Utility": "BILL000000001", "Water & Sewerage": "BILL000000002",
           "Ethio Telecom Airtime": "BILL000000003"}
BANKS = {"Commercial Bank of Ethiopia": "CBE", "Awash Bank": "AWASH", "Dashen Bank": "DASHEN", "Bank of Abyssinia": "BOA"}
SWITCH_FEE = 5.0
CUST = ["Pay Bills", "USSD", "Interbank", "QR Pay", "Scheduled", "Profile"]
ADMIN = ["Reconciliation", "Load Test", "Tiers"]
EXTRA = set(CUST + ADMIN)
TR = {"am": {"My Account": "የእኔ መለያ", "Transfer": "ገንዘብ ማስተላለፍ", "History": "ታሪክ", "Notifications": "ማሳወቂያዎች",
             "Pay Bills": "ሂሳብ ክፍያ", "Interbank": "ባንክ ለባንክ", "QR Pay": "QR ክፍያ", "Scheduled": "የታቀዱ", "Profile": "መገለጫ"},
      "om": {"My Account": "Herrega Koo", "Transfer": "Maallaqa Dabarsi", "History": "Seenaa", "Notifications": "Beeksisa",
             "Pay Bills": "Kaffaltii Biilii", "Interbank": "Baankii Gidduu", "QR Pay": "Kaffaltii QR",
             "Scheduled": "Karoorfame", "Profile": "Odeeffannoo Koo"}}


def init(env):
    global E
    E = SimpleNamespace(**env)


def tr(p, lang):
    return TR.get(lang, {}).get(p, p)


def seed(c):
    for no in BILLERS.values():
        c.execute("INSERT OR IGNORE INTO accounts(acc_no,user_id,type,balance) VALUES(?,0,'biller',0)", (no,))


# ---------- OTP (simulated SMS)
def otp_ui(user):
    s = st.session_state
    c1, c2 = st.columns([1, 2])
    if c1.button("Request OTP"):
        s.otp = str(100000 + secrets.randbelow(900000))
        E.run("INSERT INTO notifications(user_id,msg,created_at) VALUES(?,?,?)",
              (user["id"], f"📱 SMS to {user['phone']}: your OTP is {s.otp}", E.now()))
        st.info(f"Demo SMS to {user['phone']}: OTP {s.otp}")
    return c2.text_input("Enter OTP")


def otp_valid(code):
    ok = bool(code) and code == st.session_state.get("otp")
    if ok:
        del st.session_state["otp"]
    return ok


# ---------- PDF statement
def pdf_bytes(user, df):
    from fpdf import FPDF
    p = FPDF(); p.add_page(); p.image(E.LOGO, w=45); p.set_font("Helvetica", size=14)
    p.cell(0, 10, "Oromia Bank - Account Statement", new_x="LMARGIN", new_y="NEXT")
    p.set_font("Helvetica", size=9)
    p.cell(0, 6, f"{user['full_name']} | generated {E.now()}", new_x="LMARGIN", new_y="NEXT")
    for r in df.itertuples():
        p.cell(0, 6, f"{r.created_at}  {r.ref}  {r.type}  ETB {r.amount:,.2f}  {r.status}", new_x="LMARGIN", new_y="NEXT")
    return bytes(p.output())


# ---------- scheduled transfers
def run_due():
    for r in E.q("SELECT * FROM scheduled WHERE active=1 AND next_run<=?", (E.now()[:10],)).itertuples():
        E.process("transfer", r.from_acc, r.to_acc, r.amount, f"sched{r.id}-{r.next_run}", "scheduler", "scheduled")
        nxt = (datetime.strptime(r.next_run, "%Y-%m-%d") + timedelta(days=int(r.every_days))).strftime("%Y-%m-%d")
        E.run("UPDATE scheduled SET next_run=?, active=? WHERE id=?", (nxt, int(r.every_days > 0), r.id))


def scheduled(user, mine):
    st.subheader("Scheduled / recurring transfers")
    to = st.text_input("To account"); amt = st.number_input("Amount (ETB)", 0.0, step=50.0)
    days = st.number_input("Repeat every N days (0 = once)", 0, 365, 7); first = st.date_input("First run date")
    if st.button("Schedule", type="primary"):
        E.run("INSERT INTO scheduled(owner,from_acc,to_acc,amount,every_days,next_run) VALUES(?,?,?,?,?,?)",
              (user["username"], mine[0], to.strip(), amt, days, str(first)))
        st.success("Scheduled.")
    st.dataframe(E.q("SELECT id,to_acc,amount,every_days,next_run,active FROM scheduled WHERE owner=?",
                     (user["username"],)), hide_index=True)
    cid = st.number_input("Cancel schedule id", 0, step=1)
    if st.button("Cancel schedule"):
        E.run("UPDATE scheduled SET active=0 WHERE id=? AND owner=?", (cid, user["username"])); st.success("Cancelled.")


# ---------- customer extras
def bills(user, mine):
    st.subheader("Bill payment (simulated)")
    b = st.selectbox("Biller", list(BILLERS)); no = st.text_input("Meter / customer / phone number")
    amt = st.number_input("Amount (ETB)", 0.0, step=10.0)
    if st.button("Pay bill", type="primary"):
        E.result(*E.process("transfer", mine[0], BILLERS[b], amt, E.idem_key(), user["username"], f"{b} ref {no}"))


def acc_by_phone(ph):
    r = E.q("SELECT a.acc_no FROM accounts a JOIN users u ON u.id=a.user_id WHERE u.phone=?", (ph,))
    return r.iloc[0, 0] if len(r) else ""


def ussd(user, mine):
    st.subheader("USSD simulator — works on basic phones")
    if st.text_input("Dial", "*889#").strip() != "*889#":
        return
    st.code("Oromia Bank\n1. Balance\n2. Send money\n3. Buy airtime\n4. Last 3 transactions")
    ch = st.text_input("Reply with option").strip()
    if ch == "1":
        bal = E.q("SELECT balance FROM accounts WHERE acc_no=?", (mine[0],)).iloc[0, 0]
        st.code(f"Balance: ETB {bal:,.2f}")
    elif ch in ("2", "3"):
        ph = st.text_input("Recipient phone") if ch == "2" else ""
        amt = st.number_input("Amount (ETB)", 0.0, step=10.0); pin = st.text_input("PIN (your password)", type="password")
        if st.button("Confirm"):
            if not E.login(user["username"], pin)[0]:
                st.error("Wrong PIN.")
            else:
                dst = acc_by_phone(ph) if ch == "2" else BILLERS["Ethio Telecom Airtime"]
                E.result(*E.process("transfer", mine[0], dst, amt, E.idem_key(), user["username"], "USSD"))
    elif ch == "4":
        st.dataframe(E.q("SELECT created_at,type,amount FROM transactions WHERE from_acc=? OR to_acc=? ORDER BY id DESC LIMIT 3",
                         (mine[0], mine[0])), hide_index=True)


def interbank(user, mine):
    st.subheader("Interbank transfer (mock EthSwitch)")
    bank = st.selectbox("Destination bank", list(BANKS)); ext = st.text_input("Beneficiary account number")
    amt = st.number_input("Amount (ETB)", 0.0, step=100.0); st.caption(f"Switch fee: ETB {SWITCH_FEE:.2f}")
    otp = otp_ui(user)
    if st.button("Send via switch", type="primary"):
        if not otp_valid(otp):
            st.error("Invalid or missing OTP.")
        elif not (ext.isdigit() and 10 <= len(ext) <= 13):
            st.error("Switch rejected: invalid beneficiary account.")
        else:
            ok, msg = E.process("withdraw", mine[0], None, amt + SWITCH_FEE, E.idem_key(), user["username"],
                                f"SWITCH {BANKS[bank]}:{ext}")
            E.result(ok, msg + (f"  Switch ref ETS{uuid.uuid4().hex[:8].upper()}" if ok else ""))


def qr_pay(user, mine):
    import qrcode
    st.subheader("QR payments")
    buf = io.BytesIO(); qrcode.make(f"OROMIA|{mine[0]}").save(buf, format="PNG")
    st.image(buf.getvalue(), caption=f"Your QR · {mine[0]}", width=200)
    p = st.text_input("Scanned QR text (OROMIA|account)"); amt = st.number_input("Amount (ETB)", 0.0, step=10.0)
    if st.button("Pay QR", type="primary"):
        if not p.startswith("OROMIA|"):
            st.error("Invalid QR code.")
        else:
            E.result(*E.process("transfer", mine[0], p.split("|")[1], amt, E.idem_key(), user["username"], "QR payment"))


def profile(user, mine):
    st.subheader("Profile")
    tier = E.q("SELECT tier FROM users WHERE id=?", (user["id"],)).iloc[0, 0]
    st.write(f"**{user['full_name']}** · {user['phone']} · tier: **{tier}** · limit ETB {E.TIER_LIMITS[tier]:,.0f}/day")
    t = st.selectbox("Open a new account", ["savings", "current"])
    if st.button("Open account"):
        no = "1000" + "".join(random.choices("0123456789", k=9))
        E.run("INSERT INTO accounts(acc_no,user_id,type,balance) VALUES(?,?,?,0)", (no, user["id"], t))
        st.success(f"Opened {t} account {no}")
    new = st.text_input("New password (min 6)", type="password")
    if st.button("Change password") and len(new) >= 6:
        salt = secrets.token_hex(16)
        E.run("UPDATE users SET pw_hash=?, salt=? WHERE id=?", (E.hash_pw(new, salt), salt, user["id"])); st.success("Updated.")


# ---------- admin extras
def recon(user, mine):
    st.subheader("Reconciliation: accounts vs double-entry ledger")
    led = E.q("SELECT acc, SUM(credit)-SUM(debit) AS ledger FROM ledger GROUP BY acc")
    m = E.q("SELECT acc_no AS acc, balance FROM accounts").merge(led, on="acc", how="left").fillna(0)
    m["diff"] = (m["balance"] - m["ledger"]).round(2)
    t = E.q("SELECT COALESCE(SUM(debit),0) d, COALESCE(SUM(credit),0) c FROM ledger").iloc[0]
    st.metric("Total debits vs credits", f"{t.d:,.2f} / {t.c:,.2f}")
    (st.success if abs(t.d - t.c) < 0.01 and (m["diff"] == 0).all() else st.error)(
        "Ledger balanced and matches all accounts." if (m["diff"] == 0).all() else "Mismatch found!")
    st.dataframe(m, hide_index=True, use_container_width=True)


def loadtest(user, mine):
    st.subheader("Concurrency load test")
    n = st.slider("Concurrent transfers", 10, 200, 50)
    if st.button("Run test", type="primary"):
        a = E.q("SELECT acc_no FROM accounts WHERE user_id>0 AND status='active' AND balance>=? ORDER BY id LIMIT 2", (n,))
        if len(a) < 2:
            st.error("Need two funded customer accounts."); return
        s, d = a.iloc[0, 0], a.iloc[1, 0]
        before = E.q("SELECT SUM(balance) s FROM accounts").iloc[0, 0]
        res = []; t0 = time.time()
        ths = [threading.Thread(target=lambda: res.append(E.process("transfer", s, d, 1, uuid.uuid4().hex, "loadtest")[0]))
               for _ in range(n)]
        [t.start() for t in ths]; [t.join() for t in ths]
        el = time.time() - t0
        after = E.q("SELECT SUM(balance) s FROM accounts").iloc[0, 0]
        c1, c2, c3 = st.columns(3)
        c1.metric("Succeeded", f"{sum(res)}/{n}"); c2.metric("Throughput", f"{n / el:.1f} tx/s")
        c3.metric("Money conserved", "Yes" if abs(before - after) < 0.01 else "NO")


def tiers(user, mine):
    st.subheader("Customer tiers & limits")
    st.table(pd.DataFrame(E.TIER_LIMITS.items(), columns=["tier", "daily limit (ETB)"]))
    u = st.text_input("Username"); t = st.selectbox("Tier", list(E.TIER_LIMITS))
    if st.button("Set tier"):
        E.run("UPDATE users SET tier=? WHERE username=?", (t, u)); st.success("Updated.")


def render(user, page):
    mine = E.q("SELECT acc_no FROM accounts WHERE user_id=?", (user["id"],))["acc_no"].tolist()
    {"Pay Bills": bills, "USSD": ussd, "Interbank": interbank, "QR Pay": qr_pay, "Scheduled": scheduled,
     "Profile": profile, "Reconciliation": recon, "Load Test": loadtest, "Tiers": tiers}[page](user, mine)
