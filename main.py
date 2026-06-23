import os
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import create_engine, text
from pydantic import BaseModel
from typing import List
from passlib.context import CryptContext

app = FastAPI(title="Bill Splitter API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://postgres:123@127.0.0.1:5433/bill_splitter")
engine = create_engine(DATABASE_URL)

# Фикс для Render
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

# --- БЛОК АВТОСОЗДАНИЯ ТАБЛИЦ ПРИ ЗАПУСКЕ ---
SQL_CREATE_TABLES = """
CREATE TABLE IF NOT EXISTS users (
    id SERIAL PRIMARY KEY,
    username VARCHAR(50) NOT NULL,
    email VARCHAR(100) UNIQUE NOT NULL,
    password_hash VARCHAR(255) NOT NULL,
    phone VARCHAR(20),
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS groups (
    id SERIAL PRIMARY KEY,
    title VARCHAR(100) NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS group_members (
    user_id INTEGER REFERENCES users(id) ON DELETE CASCADE,
    group_id INTEGER REFERENCES groups(id) ON DELETE CASCADE,
    PRIMARY KEY (user_id, group_id)
);

CREATE TABLE IF NOT EXISTS expenses (
    id SERIAL PRIMARY KEY,
    group_id INTEGER REFERENCES groups(id) ON DELETE CASCADE,
    paid_by INTEGER REFERENCES users(id),
    amount NUMERIC(10, 2) NOT NULL,
    description VARCHAR(255) NOT NULL,
    category VARCHAR(50) DEFAULT 'Другое',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS expense_splits (
    id SERIAL PRIMARY KEY,
    expense_id INTEGER REFERENCES expenses(id) ON DELETE CASCADE,
    user_id INTEGER REFERENCES users(id),
    owed_amount NUMERIC(10, 2) NOT NULL
);

CREATE TABLE IF NOT EXISTS settlements (
    id SERIAL PRIMARY KEY,
    group_id INTEGER REFERENCES groups(id) ON DELETE CASCADE,
    from_user INTEGER REFERENCES users(id),
    to_user INTEGER REFERENCES users(id),
    amount NUMERIC(10, 2) NOT NULL,
    status VARCHAR(20) DEFAULT 'pending',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
"""
try:
    with engine.connect() as conn:
        conn.execute(text(SQL_CREATE_TABLES))
        conn.commit()
        print("✅ Таблицы успешно проверены/созданы в БД!")
except Exception as e:
    print("❌ Ошибка при создании таблиц:", e)
# --- КОНЕЦ БЛОКА ---


pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

def verify_password(plain_password, hashed_password):
    return pwd_context.verify(plain_password, hashed_password)

def get_password_hash(password):
    return pwd_context.hash(password)


class UserAuth(BaseModel):
    username: str
    email: str
    password: str
    phone: str = ""

class UserLogin(BaseModel):
    email: str
    password: str

class GroupCreate(BaseModel):
    title: str
    creator_id: int

class ParticipantSplit(BaseModel):
    user_id: int
    amount: float

class ExpenseCreate(BaseModel):
    group_id: int
    paid_by: int
    amount: float
    description: str
    category: str = "Другое"
    participants: List[ParticipantSplit]

class AddMember(BaseModel):
    group_id: int
    email: str

class SettlementCreate(BaseModel):
    group_id: int
    from_user: int
    to_user: int
    amount: float


# --- AUTH ---
@app.post("/api/register")
def register(user: UserAuth):
    hashed_pwd = get_password_hash(user.password)
    with engine.connect() as conn:
        res = conn.execute(
            text("INSERT INTO users (username, email, password_hash, phone) VALUES (:n, :e, :p, :ph) RETURNING id;"),
            {"n": user.username, "e": user.email, "p": hashed_pwd, "ph": user.phone}
        )
        conn.commit()
        return {"status": "success", "user_id": res.fetchone()[0], "username": user.username, "email": user.email}

@app.post("/api/login")
def login(user: UserLogin):
    with engine.connect() as conn:
        res = conn.execute(text("SELECT id, username, email, password_hash FROM users WHERE email = :e"),
                           {"e": user.email}).fetchone()
        if res and verify_password(user.password, res[3]):
            return {"status": "success", "user_id": res[0], "username": res[1], "email": res[2]}
        return {"status": "error", "message": "Неверный email или пароль"}


# --- GROUPS ---
@app.get("/api/my-groups/{user_id}")
def get_my_groups(user_id: int):
    with engine.connect() as conn:
        res = conn.execute(text(
            "SELECT g.id, g.title FROM groups g JOIN group_members gm ON g.id = gm.group_id WHERE gm.user_id = :uid"),
                           {"uid": user_id})
        return {"status": "success", "data": [{"id": r[0], "title": r[1]} for r in res]}

@app.post("/api/groups")
def create_group(group: GroupCreate):
    with engine.connect() as conn:
        res = conn.execute(text("INSERT INTO groups (title) VALUES (:t) RETURNING id;"), {"t": group.title})
        nid = res.fetchone()[0]
        conn.execute(text("INSERT INTO group_members (user_id, group_id) VALUES (:uid, :gid)"),
                     {"uid": group.creator_id, "gid": nid})
        conn.commit()
        return {"status": "success", "data": {"id": nid, "title": group.title}}

@app.delete("/api/groups/delete/{group_id}")
def delete_group(group_id: int):
    with engine.connect() as conn:
        conn.execute(text("DELETE FROM settlements WHERE group_id = :gid"), {"gid": group_id})
        conn.execute(text("DELETE FROM expense_splits WHERE expense_id IN (SELECT id FROM expenses WHERE group_id = :gid)"), {"gid": group_id})
        conn.execute(text("DELETE FROM expenses WHERE group_id = :gid"), {"gid": group_id})
        conn.execute(text("DELETE FROM group_members WHERE group_id = :gid"), {"gid": group_id})
        conn.execute(text("DELETE FROM groups WHERE id = :gid"), {"gid": group_id})
        conn.commit()
        return {"status": "success", "message": "Группа полностью удалена"}


# --- EXPENSES (CRUD) ---
@app.post("/api/expenses/add")
def create_expense(exp: ExpenseCreate):
    with engine.connect() as conn:
        res = conn.execute(text(
            "INSERT INTO expenses (group_id, paid_by, amount, description, category) VALUES (:gid, :uid, :a, :d, :c) RETURNING id;"),
            {"gid": exp.group_id, "uid": exp.paid_by, "a": exp.amount, "d": exp.description, "c": exp.category})
        eid = res.fetchone()[0]
        for p in exp.participants:
            conn.execute(text("INSERT INTO expense_splits (expense_id, user_id, owed_amount) VALUES (:eid, :uid, :oa)"),
                         {"eid": eid, "uid": p.user_id, "oa": p.amount})
        conn.commit()
        return {"status": "success"}

@app.put("/api/expenses/update/{expense_id}")
def update_expense(expense_id: int, exp: ExpenseCreate):
    with engine.connect() as conn:
        conn.execute(text("UPDATE expenses SET amount = :a, description = :d, category = :c WHERE id = :eid"),
                     {"a": exp.amount, "d": exp.description, "c": exp.category, "eid": expense_id})
        conn.execute(text("DELETE FROM expense_splits WHERE expense_id = :eid"), {"eid": expense_id})
        for p in exp.participants:
            conn.execute(text("INSERT INTO expense_splits (expense_id, user_id, owed_amount) VALUES (:eid, :uid, :oa)"),
                         {"eid": expense_id, "uid": p.user_id, "oa": p.amount})
        conn.commit()
        return {"status": "success"}

@app.delete("/api/expenses/delete/{expense_id}")
def delete_expense(expense_id: int):
    with engine.connect() as conn:
        conn.execute(text("DELETE FROM expense_splits WHERE expense_id = :eid"), {"eid": expense_id})
        conn.execute(text("DELETE FROM expenses WHERE id = :eid"), {"eid": expense_id})
        conn.commit()
        return {"status": "success"}

@app.get("/api/expenses/list/{group_id}")
def get_expenses(group_id: int):
    with engine.connect() as conn:
        query = text("""
            SELECT e.id, e.amount, e.description, u.username, e.category
            FROM expenses e JOIN users u ON e.paid_by = u.id 
            WHERE e.group_id = :gid ORDER BY e.created_at DESC;
        """)
        res = conn.execute(query, {"gid": group_id})
        data = []
        for r in res:
            p_res = conn.execute(text("SELECT user_id, owed_amount FROM expense_splits WHERE expense_id = :eid"), {"eid": r[0]})
            parts = [{"user_id": p[0], "amount": float(p[1])} for p in p_res]
            data.append({"id": r[0], "amount": float(r[1]), "description": r[2], "paid_by": r[3], "category": r[4], "participants": parts})
        return {"status": "success", "data": data}


# --- АНАЛИТИКА ---
@app.get("/api/stats/{group_id}")
def get_stats(group_id: int):
    with engine.connect() as conn:
        res = conn.execute(text("SELECT category, SUM(amount) FROM expenses WHERE group_id = :gid GROUP BY category"),
                           {"gid": group_id})
        return [{"name": r[0], "value": float(r[1])} for r in res]

@app.get("/api/stats/users/{group_id}")
def get_user_stats(group_id: int):
    with engine.connect() as conn:
        res = conn.execute(text("SELECT u.username, SUM(e.amount) FROM expenses e JOIN users u ON e.paid_by = u.id WHERE e.group_id = :gid GROUP BY u.username"),
                           {"gid": group_id})
        return [{"name": r[0], "amount": float(r[1])} for r in res]

@app.get("/api/stats/dates/{group_id}")
def get_date_stats(group_id: int):
    with engine.connect() as conn:
        res = conn.execute(text("SELECT DATE(created_at), SUM(amount) FROM expenses WHERE group_id = :gid GROUP BY DATE(created_at) ORDER BY DATE(created_at)"),
                           {"gid": group_id})
        return [{"date": str(r[0]), "amount": float(r[1])} for r in res]


# --- СИСТЕМА ДОЛГОВ И ПЕРЕВОДОВ ---
@app.post("/api/settlements")
def create_settlement(s: SettlementCreate):
    with engine.connect() as conn:
        conn.execute(text("INSERT INTO settlements (group_id, from_user, to_user, amount) VALUES (:g, :f, :t, :a)"),
                     {"g": s.group_id, "f": s.from_user, "t": s.to_user, "a": s.amount})
        conn.commit()
        return {"status": "success"}

@app.post("/api/settlements/{settlement_id}/accept")
def accept_settlement(settlement_id: int):
    with engine.connect() as conn:
        conn.execute(text("UPDATE settlements SET status = 'accepted' WHERE id = :id"), {"id": settlement_id})
        conn.commit()
        return {"status": "success"}

@app.get("/api/debts/{group_id}")
def get_debts(group_id: int):
    with engine.connect() as conn:
        q_exp = text("""
            WITH user_paid AS (SELECT paid_by as uid, SUM(amount) as p FROM expenses WHERE group_id = :gid GROUP BY paid_by),
            user_debts AS (SELECT es.user_id as uid, SUM(es.owed_amount) as d FROM expense_splits es JOIN expenses e ON es.expense_id = e.id WHERE e.group_id = :gid GROUP BY es.user_id)
            SELECT u.id, u.username, u.phone, COALESCE(p.p, 0) - COALESCE(d.d, 0) as net
            FROM group_members gm JOIN users u ON u.id = gm.user_id
            LEFT JOIN user_paid p ON u.id = p.uid LEFT JOIN user_debts d ON u.id = d.uid WHERE gm.group_id = :gid
        """)
        net_balances = {row[0]: {"name": row[1], "phone": row[2] or "", "net": float(row[3])} for row in
                        conn.execute(q_exp, {"gid": group_id}).fetchall()}

        q_set = text("SELECT from_user, to_user, amount FROM settlements WHERE group_id = :gid AND status = 'accepted'")
        for frm, to, amt in conn.execute(q_set, {"gid": group_id}).fetchall():
            if frm in net_balances: net_balances[frm]["net"] += float(amt)
            if to in net_balances: net_balances[to]["net"] -= float(amt)

        debtors, creditors = [], []
        for uid, data in net_balances.items():
            if data["net"] < -0.01:
                debtors.append([uid, data["name"], -data["net"]])
            elif data["net"] > 0.01:
                creditors.append([uid, data["name"], data["phone"], data["net"]])

        transactions = []
        i, j = 0, 0
        while i < len(debtors) and j < len(creditors):
            d_id, d_name, d_amt = debtors[i]
            c_id, c_name, c_phone, c_amt = creditors[j]
            settle_amt = min(d_amt, c_amt)
            transactions.append({
                "from_id": d_id, "from_name": d_name, "to_id": c_id, "to_name": c_name, "to_phone": c_phone,
                "amount": round(settle_amt, 2)
            })
            debtors[i][2] -= settle_amt
            creditors[j][3] -= settle_amt
            if debtors[i][2] < 0.01: i += 1
            if creditors[j][3] < 0.01: j += 1

        q_pend = text("""
            SELECT s.id, s.from_user, u1.username, s.to_user, u2.username, s.amount 
            FROM settlements s JOIN users u1 ON s.from_user = u1.id JOIN users u2 ON s.to_user = u2.id
            WHERE s.group_id = :gid AND s.status = 'pending'
        """)
        pending = [
            {"id": r[0], "from_id": r[1], "from_name": r[2], "to_id": r[3], "to_name": r[4], "amount": float(r[5])} for
            r in conn.execute(q_pend, {"gid": group_id}).fetchall()]

        return {"status": "success", "transactions": transactions, "pending": pending}

@app.get("/api/balance/{group_id}")
def get_balance(group_id: int):
    with engine.connect() as conn:
        q = text("""
            WITH user_paid AS (SELECT paid_by as uid, SUM(amount) as p FROM expenses WHERE group_id = :gid GROUP BY paid_by),
            user_debts AS (SELECT es.user_id as uid, SUM(es.owed_amount) as d FROM expense_splits es JOIN expenses e ON es.expense_id = e.id WHERE e.group_id = :gid GROUP BY es.user_id),
            s_sent AS (SELECT from_user as uid, SUM(amount) as s FROM settlements WHERE group_id = :gid AND status = 'accepted' GROUP BY from_user),
            s_recv AS (SELECT to_user as uid, SUM(amount) as r FROM settlements WHERE group_id = :gid AND status = 'accepted' GROUP BY to_user)
            SELECT u.username, COALESCE(p.p, 0), COALESCE(d.d, 0), COALESCE(s_sent.s, 0), COALESCE(s_recv.r, 0)
            FROM group_members gm JOIN users u ON u.id = gm.user_id
            LEFT JOIN user_paid p ON u.id = p.uid LEFT JOIN user_debts d ON u.id = d.uid 
            LEFT JOIN s_sent ON u.id = s_sent.uid LEFT JOIN s_recv ON u.id = s_recv.uid
            WHERE gm.group_id = :gid
        """)
        res = conn.execute(q, {"gid": group_id})
        data = []
        total = 0
        for r in res:
            paid, debt, sent, recv = float(r[1]), float(r[2]), float(r[3]), float(r[4])
            total += paid
            calc_balance = (paid + sent) - (debt + recv)
            data.append({"name": r[0], "paid": paid, "balance": round(calc_balance, 2)})
        return {"status": "success", "total_spent": total, "data": data}

@app.post("/api/groups/add_member")
def add_member(data: AddMember):
    with engine.connect() as conn:
        u = conn.execute(text("SELECT id FROM users WHERE email = :e"), {"e": data.email}).fetchone()
        if not u: return {"status": "error", "message": "Не найден"}
        conn.execute(text("INSERT INTO group_members (user_id, group_id) VALUES (:uid, :gid) ON CONFLICT DO NOTHING"),
                     {"uid": u[0], "gid": data.group_id})
        conn.commit()
        return {"status": "success", "message": "Ок"}

@app.get("/api/groups/{group_id}/members")
def get_members(group_id: int):
    with engine.connect() as conn:
        res = conn.execute(text(
            "SELECT u.id, u.username FROM users u JOIN group_members gm ON u.id = gm.user_id WHERE gm.group_id = :gid"),
                           {"gid": group_id})
        return {"status": "success", "data": [{"id": r[0], "name": r[1]} for r in res]}

@app.delete("/api/groups/{group_id}/members/{user_id}")
def remove_member(group_id: int, user_id: int):
    with engine.connect() as conn:
        conn.execute(text("DELETE FROM group_members WHERE group_id = :gid AND user_id = :uid"),
                     {"gid": group_id, "uid": user_id})
        conn.commit()
        return {"status": "success", "message": "Участник удален"}
