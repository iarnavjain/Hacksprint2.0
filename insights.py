"""ROI and overdraft maths for the distributor. All money is in paise (integers).

Kept free of FastAPI/SQLModel on purpose: it takes plain dicts, so it is easy to test.
"""
from datetime import date

# A bill paid back the same day would otherwise show a huge "ROI per 30 days".
# Treat any cycle shorter than this many days as this many days.
MIN_DAYS = 7


def _day(s):
    return date.fromisoformat(s[:10])


def _order_cost(o, buy_by_name):
    """Cost of goods on one order, or None if any item's buy price is unknown."""
    cost = 0
    for it in o["items"]:
        buy = it.get("buy_paise")
        if buy is None:
            buy = buy_by_name.get(it["name"])      # older orders: use today's buy price
        if buy is None:
            return None
        cost += it["qty"] * buy
    return cost


def build_insights(orders, buy_by_name, today):
    """orders: list of dicts (status, gstin, retailer_name, total_paise, paid_paise,
    created, last_payment, items). Newest order first, so the first name seen per GSTIN is the latest.

    ROI and "days to get paid" use only fully paid (closed) bills, because only those have
    money fully back. Unpaid delivered bills are reported as outstanding.
    """
    retailers, articles = {}, {}
    outstanding = overdue = skipped = 0
    t_rev = t_cost = t_profit = 0
    t_cost_days = 0                       # sum of cost * days, for a cost-weighted average
    closed_n = 0

    for o in orders:
        if o["status"] not in ("delivered", "closed"):
            continue
        age = max((today - _day(o["created"])).days, 0)
        due = o["total_paise"] - o["paid_paise"]
        r = retailers.setdefault(o["gstin"], {
            "gstin": o["gstin"], "name": o["retailer_name"], "bills_paid": 0,
            "revenue_paise": 0, "cost_paise": 0, "profit_paise": 0, "_days": 0,
            "outstanding_paise": 0, "oldest_open_days": 0})
        if due > 0:
            r["outstanding_paise"] += due
            r["oldest_open_days"] = max(r["oldest_open_days"], age)
            outstanding += due
            if age > 30:
                overdue += due
        if o["status"] != "closed":
            continue

        cost = _order_cost(o, buy_by_name)
        if cost is None:
            skipped += 1
            continue
        paid_on = _day(o["last_payment"]) if o.get("last_payment") else today
        days = max((paid_on - _day(o["created"])).days, 0)
        rev = o["total_paise"]
        profit = rev - cost

        r["bills_paid"] += 1
        r["revenue_paise"] += rev
        r["cost_paise"] += cost
        r["profit_paise"] += profit
        r["_days"] += days
        t_rev += rev; t_cost += cost; t_profit += profit
        t_cost_days += cost * days
        closed_n += 1

        for it in o["items"]:
            buy = it.get("buy_paise")
            if buy is None:
                buy = buy_by_name[it["name"]]
            a = articles.setdefault(it["name"], {"name": it["name"], "qty": 0, "revenue_paise": 0, "cost_paise": 0,
                                                 "profit_paise": 0, "bills": 0, "_cost_days": 0})
            line_cost = it["qty"] * buy
            a["qty"] += it["qty"]
            a["revenue_paise"] += it["qty"] * it["price_paise"]
            a["cost_paise"] += line_cost
            a["profit_paise"] += it["qty"] * (it["price_paise"] - buy)
            a["bills"] += 1
            a["_cost_days"] += line_cost * days       # days = how long the whole bill took to get paid

    rows = []
    for r in retailers.values():
        n = r.pop("bills_paid"); d = r.pop("_days")
        r["bills_paid"] = n
        if n and r["cost_paise"]:
            avg = d / n
            r["avg_days"] = round(avg, 1)
            r["roi_pct"] = round(r["profit_paise"] / r["cost_paise"] * 100, 1)
            r["margin_pct"] = round(r["profit_paise"] / r["revenue_paise"] * 100, 1)
            r["roi_30d_pct"] = round(r["roi_pct"] * 30 / max(avg, MIN_DAYS), 1)
        else:
            r["avg_days"] = r["roi_pct"] = r["margin_pct"] = r["roi_30d_pct"] = None
        rows.append(r)
    rows.sort(key=lambda r: (r["roi_30d_pct"] is None, -(r["roi_30d_pct"] or 0)))

    summary = {"closed_orders": closed_n, "revenue_paise": t_rev, "cost_paise": t_cost,
               "profit_paise": t_profit, "outstanding_paise": outstanding,
               "overdue_paise": overdue, "skipped_no_cost": skipped}
    if t_cost:
        avg = t_cost_days / t_cost
        summary["avg_days"] = round(avg, 1)
        summary["roi_pct"] = round(t_profit / t_cost * 100, 1)
        summary["roi_30d_pct"] = round(summary["roi_pct"] * 30 / max(avg, MIN_DAYS), 1)
    else:
        summary["avg_days"] = summary["roi_pct"] = summary["roi_30d_pct"] = None

    items = []
    for a in articles.values():
        cd = a.pop("_cost_days")
        if a["cost_paise"]:
            avg = cd / a["cost_paise"]
            a["avg_days"] = round(avg, 1)
            a["roi_pct"] = round(a["profit_paise"] / a["cost_paise"] * 100, 1)
            a["margin_pct"] = round(a["profit_paise"] / a["revenue_paise"] * 100, 1)
            a["roi_30d_pct"] = round(a["roi_pct"] * 30 / max(avg, MIN_DAYS), 1)
        else:
            a["avg_days"] = a["roi_pct"] = a["margin_pct"] = a["roi_30d_pct"] = None
        items.append(a)
    items.sort(key=lambda a: -(a["roi_30d_pct"] or 0))
    return {"summary": summary, "retailers": rows, "articles": items}


def pick_basis(ins, item=None):
    """The numbers an overdraft estimate should use: all items together, or one item's own history.
    Returns None if that item has no fully paid bills yet."""
    s = ins["summary"]
    if not item:
        return s
    a = next((x for x in ins["articles"] if x["name"] == item), None)
    if not a or not a["cost_paise"]:
        return None
    return dict(s, closed_orders=a["bills"], cost_paise=a["cost_paise"],
                avg_days=a["avg_days"], roi_pct=a["roi_pct"])


def overdraft_advice(summary, amount_paise, rate_pct, days=None):
    """Is borrowing `amount_paise` on overdraft at `rate_pct` a year worth it?

    Assumes the borrowed money is used to restock and earns the same ROI per day as the
    fully paid bills so far. It is an estimate, not a bank figure.
    """
    if not summary["closed_orders"] or not summary["cost_paise"]:
        return {"verdict": "nodata",
                "message": "Not enough data yet. Close at least one bill (collect the full payment) to estimate ROI."}
    cycle = max(summary["avg_days"], MIN_DAYS)
    days = days or round(cycle)
    roi = summary["roi_pct"] / 100

    profit = amount_paise * roi * days / cycle
    interest = amount_paise * rate_pct / 100 * days / 365
    if interest == 0:
        verdict = "green"
    elif profit >= 2 * interest:
        verdict = "green"
    elif profit >= interest:
        verdict = "amber"
    else:
        verdict = "red"
    profit_r, interest_r = round(profit), round(interest)     # net = shown profit - shown interest
    return {
        "verdict": verdict, "days": days,
        "interest_paise": interest_r, "expected_profit_paise": profit_r,
        "net_paise": profit_r - interest_r,
        "breakeven_rate_pct": round(roi / cycle * 365 * 100, 1),
        "roi_pct": summary["roi_pct"], "cycle_days": round(cycle, 1), "bills": summary["closed_orders"],
        "receivables_paise": summary["outstanding_paise"],
        "overdue_paise": summary["overdue_paise"],
    }