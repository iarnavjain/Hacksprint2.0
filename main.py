import hashlib, json, os, secrets, shutil, uuid
from datetime import date, datetime, timedelta
from typing import Optional
try:                                   # reads DATABASE_URL from a .env file if python-dotenv is installed
    from dotenv import load_dotenv; load_dotenv()
except ImportError:
    pass
from fastapi import FastAPI, Depends, Header, HTTPException, UploadFile, File
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from sqlmodel import SQLModel, Field, Session, create_engine, select
from scoring import calculate_payment_score
from insights import build_insights, overdraft_advice, pick_basis

engine = create_engine(os.environ["DATABASE_URL"], pool_pre_ping=True)
os.makedirs("uploads", exist_ok=True)
app = FastAPI()

class User(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    name: str; role: str
    phone: str = Field(unique=True)
    salt: str; pw: str
    gstin: Optional[str] = None          # retailers only
    token: Optional[str] = None

class Article(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    name: str; buy_paise: int; sell_paise: int

class Retailer(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    name: str
    phone: str = ""
    gstin: str = Field(unique=True)
    address: str = ""

class Order(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    op_id: str = Field(unique=True)      # client-made ID: stops duplicate orders on retry
    salesperson_id: int
    retailer_name: str; gstin: str; address: str = ""
    items_json: str; total_paise: int
    status: str = "requested"            # requested/accepted/rejected/ready/delivered/closed
    created: str
    paid_paise: int = 0
    last_payment: Optional[str] = None
    proof: Optional[str] = None
    credit_days: Optional[int] = None
    due_date: Optional[str] = None       # delivery date + credit days
    delivered_at: Optional[str] = None

class Payment(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    op_id: str = Field(unique=True)      # stops duplicate payments on retry
    order_id: int; amount_paise: int; date: str

def hp(pw, salt): return hashlib.pbkdf2_hmac("sha256", pw.encode(), salt.encode(), 100_000).hex()

@app.on_event("startup")
def seed():
    SQLModel.metadata.create_all(engine)
    with Session(engine) as s:
        if s.exec(select(User)).first(): return
        for name, role, phone, gstin in [
            ("Dinesh (Distributor)", "distributor", "9000000001", None),
            ("Sameer (Salesperson)", "salesperson", "9000000002", None),
            ("Deepak (Delivery)", "delivery", "9000000003", None),
            ("Chetan (Collection)", "collector", "9000000004", None),
            ("Sharma Stores (Retailer)", "retailer", "9000000005", "29ABCDE1234F1Z5")]:
            salt = secrets.token_hex(8)
            s.add(User(name=name, role=role, phone=phone, salt=salt, pw=hp("demo123", salt), gstin=gstin))
        for n, b, p in [("Atta 10kg", 38000, 42000), ("Maida 10kg", 36000, 40000),
                        ("Refined Oil 15L", 165000, 178000), ("Sugar 50kg", 205000, 218000)]:
            s.add(Article(name=n, buy_paise=b, sell_paise=p))
        s.commit()

@app.on_event("startup")
def backfill_retailers():
    with Session(engine) as s:
        known = {r.gstin for r in s.exec(select(Retailer))}
        for g, n, a in {(o.gstin, o.retailer_name, o.address) for o in s.exec(select(Order))}:
            if g not in known:
                s.add(Retailer(name=n, gstin=g, address=a)); known.add(g)
        for u in s.exec(select(User).where(User.role == "retailer")):
            if u.gstin and u.gstin not in known:
                s.add(Retailer(name=u.name.replace(" (Retailer)", ""), gstin=u.gstin, phone=u.phone)); known.add(u.gstin)
        s.commit()

def sess():
    with Session(engine) as s: yield s

def me(x_token: str = Header(""), s: Session = Depends(sess)):
    u = s.exec(select(User).where(User.token == x_token)).first() if x_token else None
    if not u: raise HTTPException(401, "Please sign in again")
    return u

def need(u, *roles):
    if u.role not in roles: raise HTTPException(403, "Not allowed for your role")

def out(o):
    d = o.model_dump()
    d["items"] = [{k: v for k, v in i.items() if k != "buy_paise"}     # buy price stays private
                  for i in json.loads(d.pop("items_json"))]
    late = bool(o.due_date) and o.status == "delivered" and o.due_date < datetime.now().date().isoformat()
    d["overdue_paise"] = o.total_paise - o.paid_paise if late else 0
    return d

class Login(BaseModel): phone: str; password: str
class ArticleIn(BaseModel): name: str; buy: float; sell: float
class OrderIn(BaseModel):
    op_id: str; retailer_name: str; gstin: str; address: str = ""; items: list[dict]
class Act(BaseModel): action: str
class Collect(BaseModel): op_id: str; amount: float
class RetailerIn(BaseModel): name: str; phone: str = ""; gstin: str; address: str = ""
class Credit(BaseModel): days: int

@app.post("/api/login")
def login(b: Login, s: Session = Depends(sess)):
    u = s.exec(select(User).where(User.phone == b.phone.strip())).first()
    if not u or hp(b.password, u.salt) != u.pw: raise HTTPException(401, "Wrong phone or password")
    u.token = secrets.token_hex(16); s.add(u); s.commit()
    return {"token": u.token, "role": u.role, "name": u.name}

@app.get("/api/articles")
def articles(u=Depends(me), s: Session = Depends(sess)):
    need(u, "distributor", "salesperson"); res = []
    for a in s.exec(select(Article)):
        d = {"id": a.id, "name": a.name, "sell_paise": a.sell_paise}
        if u.role == "distributor": d["buy_paise"] = a.buy_paise   # buy price never sent to salesperson
        res.append(d)
    return res

@app.post("/api/articles")
def add_article(b: ArticleIn, u=Depends(me), s: Session = Depends(sess)):
    need(u, "distributor")
    s.add(Article(name=b.name.strip(), buy_paise=round(b.buy * 100), sell_paise=round(b.sell * 100)))
    s.commit(); return {"ok": True}

@app.post("/api/orders")
def new_orders(ops: list[OrderIn], u=Depends(me), s: Session = Depends(sess)):
    need(u, "salesperson"); res = []
    for b in ops:
        o = s.exec(select(Order).where(Order.op_id == b.op_id)).first()
        if not o:
            items, total = [], 0
            for it in b.items:                       # server sets prices, not the phone
                a, q = s.get(Article, it["article_id"]), int(it["qty"])
                if a and q > 0:
                    items.append({"name": a.name, "qty": q, "price_paise": a.sell_paise, "buy_paise": a.buy_paise})
                    total += q * a.sell_paise
            if not items:
                res.append({"op_id": b.op_id, "ack": True}); continue
            o = Order(op_id=b.op_id, salesperson_id=u.id, retailer_name=b.retailer_name,
                      gstin=b.gstin.strip().upper(), address=b.address, items_json=json.dumps(items),
                      total_paise=total, created=datetime.now().isoformat(timespec="seconds"))
            if not s.exec(select(Retailer).where(Retailer.gstin == o.gstin)).first():
                s.add(Retailer(name=o.retailer_name, gstin=o.gstin, address=o.address))
            s.add(o); s.commit(); s.refresh(o)
        res.append({"op_id": b.op_id, "ack": True, "order_id": o.id})
    return res

@app.get("/api/orders")
def orders(u=Depends(me), s: Session = Depends(sess)):
    q = select(Order).order_by(Order.id.desc())
    if u.role == "salesperson": q = q.where(Order.salesperson_id == u.id)
    elif u.role == "retailer": q = q.where(Order.gstin == u.gstin)
    elif u.role == "delivery": q = q.where(Order.status == "ready")
    elif u.role == "collector": q = q.where(Order.status == "delivered")
    return [out(o) for o in s.exec(q)]

@app.post("/api/orders/{oid}/status")
def set_status(oid: int, b: Act, u=Depends(me), s: Session = Depends(sess)):
    need(u, "distributor"); o = s.get(Order, oid)
    flow = {"accept": ("requested", "accepted"), "reject": ("requested", "rejected"), "ready": ("accepted", "ready")}
    if not o or b.action not in flow or o.status != flow[b.action][0]:
        raise HTTPException(409, "That action is not possible right now")
    o.status = flow[b.action][1]; s.add(o); s.commit(); s.refresh(o); return out(o)

@app.post("/api/orders/{oid}/deliver")
def deliver(oid: int, photo: UploadFile = File(...), u=Depends(me), s: Session = Depends(sess)):
    need(u, "delivery"); o = s.get(Order, oid)
    if not o or o.status != "ready": raise HTTPException(409, "Order is not ready for delivery")
    path = f"uploads/{oid}_{uuid.uuid4().hex[:8]}.jpg"
    with open(path, "wb") as f: shutil.copyfileobj(photo.file, f)
    o.proof, o.status = "/" + path, "delivered"
    o.delivered_at = datetime.now().date().isoformat()
    s.add(o); s.commit(); s.refresh(o); return out(o)

@app.post("/api/orders/{oid}/credit")
def set_credit(oid: int, b: Credit, u=Depends(me), s: Session = Depends(sess)):
    need(u, "distributor"); o = s.get(Order, oid)
    if not o or o.status not in ("delivered", "closed"): raise HTTPException(409, "Bill has not been delivered")
    if o.credit_days is not None: raise HTTPException(409, "Credit period is already set")
    if not 0 <= b.days <= 365: raise HTTPException(422, "Enter between 0 and 365 days")
    base = datetime.fromisoformat(o.delivered_at) if o.delivered_at else datetime.now()
    o.credit_days = b.days; o.due_date = (base + timedelta(days=b.days)).date().isoformat()
    s.add(o); s.commit(); s.refresh(o); return out(o)

@app.post("/api/orders/{oid}/collect")
def collect(oid: int, b: Collect, u=Depends(me), s: Session = Depends(sess)):
    need(u, "collector"); o = s.get(Order, oid)
    if s.exec(select(Payment).where(Payment.op_id == b.op_id)).first():
        return {"ack": True, "duplicate": True}
    paise = round(b.amount * 100)
    if not o or o.status != "delivered": raise HTTPException(409, "No pending payment on this bill")
    if paise <= 0 or paise > o.total_paise - o.paid_paise: raise HTTPException(422, "Amount is more than the balance due")
    today = datetime.now().date().isoformat()
    s.add(Payment(op_id=b.op_id, order_id=oid, amount_paise=paise, date=today))
    o.paid_paise += paise; o.last_payment = today
    if o.paid_paise >= o.total_paise: o.status = "closed"
    s.add(o); s.commit(); return {"ack": True}

@app.get("/api/retailers")
def retailers(u=Depends(me), s: Session = Depends(sess)):
    need(u, "distributor"); pays = {}
    for p in s.exec(select(Payment)):
        pays.setdefault(p.order_id, []).append({"amount_paise": p.amount_paise, "date": p.date})
    bills = [o for o in s.exec(select(Order)) if o.due_date and o.status in ("delivered", "closed")]
    res = []
    for r in s.exec(select(Retailer).order_by(Retailer.name)):
        inv = [{"total_paise": o.total_paise, "due_date": o.due_date, "payments": pays.get(o.id, [])}
               for o in bills if o.gstin == r.gstin]
        res.append({**r.model_dump(), "score": calculate_payment_score(inv)})
    return res

@app.post("/api/retailers")
def add_retailer(b: RetailerIn, u=Depends(me), s: Session = Depends(sess)):
    need(u, "distributor")
    g = b.gstin.strip().upper()
    if not b.name.strip() or not g: raise HTTPException(422, "Name and GSTIN are required")
    if s.exec(select(Retailer).where(Retailer.gstin == g)).first():
        raise HTTPException(409, "A retailer with this GSTIN already exists")
    s.add(Retailer(name=b.name.strip(), phone=b.phone.strip(), gstin=g, address=b.address.strip()))
    s.commit(); return {"ok": True}

def _insights(s):
    buy = {a.name: a.buy_paise for a in s.exec(select(Article))}
    all_orders = [dict(o.model_dump(), items=json.loads(o.items_json)) for o in s.exec(select(Order).order_by(Order.id.desc()))]
    return build_insights(all_orders, buy, datetime.now().date())

@app.get("/api/insights")
def insights(u=Depends(me), s: Session = Depends(sess)):
    need(u, "distributor"); return _insights(s)

@app.get("/api/overdraft/advice")
def overdraft(amount: float, rate: float, days: Optional[int] = None, item: Optional[str] = None, u=Depends(me), s: Session = Depends(sess)):
    need(u, "distributor")
    if amount <= 0 or not 0 <= rate <= 100 or (days is not None and not 1 <= days <= 365):
        raise HTTPException(422, "Check the amount, yearly interest rate (0 to 100) and days (1 to 365)")
    basis = pick_basis(_insights(s), item or None)
    if basis is None:
        return {"verdict": "nodata", "message": f"No fully paid bills include {item} yet, so there is nothing to estimate from."}
    res = overdraft_advice(basis, round(amount * 100), rate, days)
    res["item"] = item or None
    return res

app.mount("/uploads", StaticFiles(directory="uploads"))
app.mount("/", StaticFiles(directory="static", html=True))