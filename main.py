import hashlib, json, os, secrets, shutil, uuid
from datetime import datetime
from typing import Optional
from fastapi import FastAPI, Depends, Header, HTTPException, UploadFile, File
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from sqlmodel import SQLModel, Field, Session, create_engine, select

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

def sess():
    with Session(engine) as s: yield s

def me(x_token: str = Header(""), s: Session = Depends(sess)):
    u = s.exec(select(User).where(User.token == x_token)).first() if x_token else None
    if not u: raise HTTPException(401, "Please sign in again")
    return u

def need(u, *roles):
    if u.role not in roles: raise HTTPException(403, "Not allowed for your role")

def out(o):
    d = o.model_dump(); d["items"] = json.loads(d.pop("items_json")); return d

class Login(BaseModel): phone: str; password: str
class ArticleIn(BaseModel): name: str; buy: float; sell: float
class OrderIn(BaseModel):
    op_id: str; retailer_name: str; gstin: str; address: str = ""; items: list[dict]
class Act(BaseModel): action: str
class Collect(BaseModel): op_id: str; amount: float

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
                    items.append({"name": a.name, "qty": q, "price_paise": a.sell_paise})
                    total += q * a.sell_paise
            if not items:
                res.append({"op_id": b.op_id, "ack": True}); continue
            o = Order(op_id=b.op_id, salesperson_id=u.id, retailer_name=b.retailer_name,
                      gstin=b.gstin.strip().upper(), address=b.address, items_json=json.dumps(items),
                      total_paise=total, created=datetime.now().isoformat(timespec="seconds"))
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
    o.proof, o.status = "/" + path, "delivered"; s.add(o); s.commit(); s.refresh(o); return out(o)

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

app.mount("/uploads", StaticFiles(directory="uploads"))
app.mount("/", StaticFiles(directory="static", html=True))
