from datetime import date, datetime


def parse_date(value):
    """Convert an ISO date string into a date object."""
    if isinstance(value, date):
        return value
    return datetime.strptime(value[:10], "%Y-%m-%d").date()


def calculate_payment_score(invoices, today=None):
    """
    Calculate a retailer's payment-discipline score.

    Each invoice must contain:
        total_paise: total invoice amount in paise
        due_date: invoice payment deadline, YYYY-MM-DD
        payments: list of dictionaries containing:
            amount_paise: payment amount in paise
            date: payment date, YYYY-MM-DD

    Proposed weights:
        On-time rate: 50%
        Payment delay: 30%
        Overdue amount: 20%
    """

    if today is None:
        today = date.today()
    else:
        today = parse_date(today)

    eligible = []
    overdue_amount = 0
    overdue_invoice_total = 0
    late_delays = []

    for invoice in invoices:
        total = int(invoice["total_paise"])
        due_date = parse_date(invoice["due_date"])

        payments = sorted(
            invoice.get("payments", []),
            key=lambda p: parse_date(p["date"])
        )

        total_paid = 0
        paid_by_due_date = 0
        full_payment_date = None

        for payment in payments:
            amount = int(payment["amount_paise"])
            payment_date = parse_date(payment["date"])

            # Ignore invalid future-dated payment records.
            if payment_date > today:
                continue

            previous_paid = total_paid
            total_paid += amount

            if payment_date <= due_date:
                paid_by_due_date += amount

            # Record when cumulative payments first cover the invoice.
            if (
                full_payment_date is None
                and previous_paid < total
                and total_paid >= total
            ):
                full_payment_date = payment_date

        # Don't count invoices that are not yet due,
        # unless they have already been paid in full.
        if due_date > today and full_payment_date is None:
            continue

        eligible.append(invoice)

        paid_by_due_date = min(paid_by_due_date, total)
        outstanding = max(0, total - total_paid)

        if paid_by_due_date >= total:
            on_time = True
        else:
            on_time = False

        if not on_time and full_payment_date is not None:
            delay = max(
                0, (full_payment_date - due_date).days
            )
            late_delays.append(delay)

        if due_date <= today:
            overdue_invoice_total += total

            if due_date < today:
                overdue_amount += outstanding

    count = len(eligible)

    if count == 0:
        return {
            "score": None,
            "grade": None,
            "status": "No eligible invoices",
            "eligible_invoices": 0,
            "on_time_rate": None,
            "average_delay_days": None,
            "overdue_amount_paise": 0,
            "overdue_ratio": None,
        }

    on_time_count = 0

    for invoice in eligible:
        total = int(invoice["total_paise"])
        due_date = parse_date(invoice["due_date"])

        paid_by_due = sum(
            int(p["amount_paise"])
            for p in invoice.get("payments", [])
            if parse_date(p["date"]) <= due_date
            and parse_date(p["date"]) <= today
        )

        if paid_by_due >= total:
            on_time_count += 1

    on_time_rate = (on_time_count / count) * 100

    average_delay = (
        sum(late_delays) / len(late_delays)
        if late_delays else 0
    )

    delay_score = max(0, 100 - 5 * average_delay)

    overdue_ratio = (
        min(100, overdue_amount / overdue_invoice_total * 100)
        if overdue_invoice_total > 0 else 0
    )

    overdue_score = 100 - overdue_ratio

    score = round(
        0.50 * on_time_rate
        + 0.30 * delay_score
        + 0.20 * overdue_score
    )

    if count < 5:
        grade = None
        status = "Provisional - insufficient history"
    else:
        if score >= 90:
            grade = "A"
        elif score >= 75:
            grade = "B"
        elif score >= 60:
            grade = "C"
        elif score >= 40:
            grade = "D"
        else:
            grade = "E"

        status = "Calculated"

    return {
        "score": score,
        "grade": grade,
        "status": status,
        "eligible_invoices": count,
        "on_time_rate": round(on_time_rate, 2),
        "average_delay_days": round(average_delay, 2),
        "overdue_amount_paise": overdue_amount,
        "overdue_ratio": round(overdue_ratio, 2),
    }