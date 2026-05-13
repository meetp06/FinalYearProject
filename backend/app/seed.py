"""Seed Postgres with 15 demo customers and 120 orders randomly distributed.

Run (fresh seed):
    docker compose run --rm api python -m app.seed

Run (add customers + reassign orders only, keeps refunds/emails):
    docker compose run --rm api python -m app.seed --reseed

Idempotent: customers are upserted, orders are reassigned deterministically.
"""
from __future__ import annotations

import logging
import random
import sys
from datetime import datetime, timedelta
from typing import Any, Dict, List

from .db import session_scope, engine
from .models import Base, Customer, Order

log = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO)

RNG = random.Random(20260502)

# ── 15 demo customers ──────────────────────────────────────────────────────────
CUSTOMERS: List[Dict[str, Any]] = [
    {"id": "1",  "name": "Demo Customer",     "email": "meetp0006@gmail.com",               "tier": "Gold",     "days_ago": 365},
    {"id": "2",  "name": "Alice Johnson",      "email": "alice.johnson@demo.atlas",       "tier": "Platinum", "days_ago": 720},
    {"id": "3",  "name": "Bob Martinez",       "email": "bob.martinez@demo.atlas",        "tier": "Silver",   "days_ago": 180},
    {"id": "4",  "name": "Carol Williams",     "email": "carol.williams@demo.atlas",      "tier": "Gold",     "days_ago": 540},
    {"id": "5",  "name": "David Chen",         "email": "david.chen@demo.atlas",          "tier": "Bronze",   "days_ago": 45},
    {"id": "6",  "name": "Emma Davis",         "email": "emma.davis@demo.atlas",          "tier": "Platinum", "days_ago": 900},
    {"id": "7",  "name": "Frank Wilson",       "email": "frank.wilson@demo.atlas",        "tier": "Silver",   "days_ago": 210},
    {"id": "8",  "name": "Grace Lee",          "email": "grace.lee@demo.atlas",           "tier": "Gold",     "days_ago": 630},
    {"id": "9",  "name": "Henry Brown",        "email": "henry.brown@demo.atlas",         "tier": "Bronze",   "days_ago": 30},
    {"id": "10", "name": "Isabella Taylor",    "email": "isabella.taylor@demo.atlas",     "tier": "Gold",     "days_ago": 480},
    {"id": "11", "name": "James Anderson",     "email": "james.anderson@demo.atlas",      "tier": "Silver",   "days_ago": 150},
    {"id": "12", "name": "Katherine Thomas",   "email": "katherine.thomas@demo.atlas",    "tier": "Platinum", "days_ago": 1080},
    {"id": "13", "name": "Liam Jackson",       "email": "liam.jackson@demo.atlas",        "tier": "Bronze",   "days_ago": 60},
    {"id": "14", "name": "Maria Garcia",       "email": "maria.garcia@demo.atlas",        "tier": "Gold",     "days_ago": 390},
    {"id": "15", "name": "Noah White",         "email": "noah.white@demo.atlas",          "tier": "Silver",   "days_ago": 270},
]

_LINE_ITEMS = (
    ("USB-C hub", "Wireless earbuds", "Phone case", "Laptop sleeve", "HDMI cable"),
    ("Coffee beans 1kg", "Travel mug", "Desk lamp", "Webcam HD", "Keyboard mechanical"),
    ("Monitor stand", "SSD 1TB", "USB flash drive", "Surge protector", "Mouse pad XL"),
    ("Fitness tracker", "Yoga mat", "Water bottle insulated", "Backpack commuter", "Sunglasses"),
    ("Garden hose", "LED strip lights", "Tool kit", "Phone charger brick", "Screen wipes"),
)


def _synthetic_orders() -> List[Dict[str, Any]]:
    """Build 120 orders: 1 flagship (customer 1) + 119 generated."""
    statuses = ("Delivered", "Shipped", "Out for Delivery", "Processing", "Cancelled")
    carriers = ("UPS", "FedEx", "USPS", "DHL", "UPS")

    # All customer IDs for random assignment of the 119 generated orders
    cust_ids = [c["id"] for c in CUSTOMERS]
    # Shuffle assignment deterministically so every customer gets orders
    assignment: List[str] = []
    while len(assignment) < 119:
        shuffled = cust_ids[:]
        RNG.shuffle(shuffled)
        assignment.extend(shuffled)
    assignment = assignment[:119]

    out: List[Dict[str, Any]] = []

    # Flagship — always customer 1, the primary demo order
    out.append({
        "id": "ORD-88210",
        "customer_id": "1",
        "status": "Shipped",
        "carrier": "UPS",
        "tracking": "1Z999AA10123456784",
        "eta": "2026-05-08",
        "items": [{"title": "Blue Wireless Headphones", "qty": 1}],
        "total_cents": 8999,
        "currency": "USD",
    })

    for seq in range(1, 120):
        st = statuses[seq % len(statuses)]
        if st in {"Processing", "Cancelled"}:
            carrier = tracking = eta = None
        else:
            carrier = carriers[seq % len(carriers)]
            digits = f"{(8800000000000 + seq * 991) % 10**12:012d}"
            tracking = f"1Z{digits}US" if carrier == "UPS" else f"{digits}-TRK-{seq}"
            eta = f"2026-{(seq % 7) + 5:02d}-{(seq % 26) + 1:02d}"

        group = _LINE_ITEMS[seq % len(_LINE_ITEMS)]
        n_items = RNG.randint(1, min(4, len(group)))
        pics = RNG.sample(group, k=n_items)
        items = [{"title": p, "qty": RNG.randint(1, 3)} for p in pics]
        qty_sum = sum(x["qty"] for x in items)
        unit = RNG.randint(800, 15000)
        total_cents = min(599_999, max(599, qty_sum * unit // max(1, len(items))))

        out.append({
            "id": f"ORD-GEN-{seq:05d}",
            "customer_id": assignment[seq - 1],
            "status": st,
            "carrier": carrier,
            "tracking": tracking,
            "eta": eta,
            "items": items,
            "total_cents": total_cents,
            "currency": "USD",
        })

    return out


def seed() -> None:
    Base.metadata.create_all(bind=engine)
    orders = _synthetic_orders()

    with session_scope() as db:
        # Upsert all 15 customers
        for c in CUSTOMERS:
            existing = db.get(Customer, c["id"])
            if existing is None:
                member_since = datetime.utcnow() - timedelta(days=c["days_ago"])
                db.add(Customer(
                    id=c["id"],
                    name=c["name"],
                    email=c["email"],
                    tier=c["tier"],
                    member_since=member_since,
                ))
                log.info("Inserted customer id=%s name=%s", c["id"], c["name"])
            else:
                log.info("Customer id=%s already present; skipping", c["id"])

        # Insert or update-assign orders
        created = reassigned = skipped = 0
        for o in orders:
            existing = db.get(Order, o["id"])
            if existing is None:
                db.add(Order(**o))
                created += 1
            else:
                if existing.customer_id != o["customer_id"]:
                    existing.customer_id = o["customer_id"]
                    reassigned += 1
                else:
                    skipped += 1

    log.info(
        "Orders: +%d created, %d reassigned, %d unchanged",
        created, reassigned, skipped,
    )

    # Print customer order summary
    with session_scope() as db:
        log.info("\n── Customer order distribution ──")
        for c in CUSTOMERS:
            count = db.query(Order).filter(Order.customer_id == c["id"]).count()
            log.info("  [%s] %-22s %-9s %d orders", c["id"].rjust(2), c["name"], c["tier"], count)


if __name__ == "__main__":
    seed()
