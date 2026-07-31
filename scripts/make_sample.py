"""Generate deterministic sample tables for Exhibit demos.

Run: python scripts/make_sample.py  → writes sample_data/{sales,products,customers}.csv

- sales.csv     : the primary fact table, with a clear June revenue decline.
- products.csv  : one row per product (joins sales on `product`) with unit_cost,
                  enabling margin/profitability analysis.
- customers.csv : one row per customer (joins sales on `customer_id`) with tier,
                  country, and industry, enabling cohort/segmentation analysis.

Everything is seeded so the demo is reproducible.
"""

from __future__ import annotations

import csv
import random
from datetime import date, timedelta
from pathlib import Path

random.seed(15445)

SEGMENTS = ["Consumer", "Corporate", "Home Office"]
REGIONS = ["East", "West", "Central", "South"]
PRODUCTS = {
    "Widget": (18.0, 25.0),
    "Gadget": (40.0, 60.0),
    "Gizmo": (8.0, 14.0),
    "Doohickey": (95.0, 140.0),
}

# Relative monthly demand multiplier (index 1..8 = Jan..Aug). June (=6) collapses.
MONTH_FACTOR = {1: 1.0, 2: 1.05, 3: 1.15, 4: 1.2, 5: 1.25, 6: 0.6, 7: 0.85, 8: 1.1}
ORDERS_PER_MONTH = 30

# Product reference data (joins sales on `product`). Costs sit below typical unit
# prices so margins are positive but vary by product.
PRODUCT_INFO = {
    "Widget": {"category": "Hardware", "unit_cost": 12.50, "supplier": "Acme"},
    "Gadget": {"category": "Electronics", "unit_cost": 31.00, "supplier": "Globex"},
    "Gizmo": {"category": "Hardware", "unit_cost": 6.25, "supplier": "Acme"},
    "Doohickey": {"category": "Premium", "unit_cost": 82.00, "supplier": "Initech"},
}

TIERS = ["SMB", "Mid-Market", "Enterprise"]
COUNTRIES = ["United States", "United Kingdom", "Canada", "Germany", "Australia"]
INDUSTRIES = ["Retail", "Finance", "Technology", "Healthcare", "Manufacturing"]
NUM_CUSTOMERS = 120


def month_dates(year: int, month: int, n: int):
    start = date(year, month, 1)
    end = (date(year, month + 1, 1) if month < 12 else date(year + 1, 1, 1)) - timedelta(days=1)
    span = (end - start).days
    return [start + timedelta(days=random.randint(0, span)) for _ in range(n)]


def make_sales(out_dir: Path) -> None:
    rows = []
    order_id = 1000
    for month in range(1, 9):
        factor = MONTH_FACTOR[month]
        n = max(5, round(ORDERS_PER_MONTH * factor))
        for d in sorted(month_dates(2024, month, n)):
            order_id += 1
            segment = random.choices(SEGMENTS, weights=[5, 4, 3])[0]
            region = random.choice(REGIONS)
            local = factor
            if month == 6 and (segment == "Corporate" or region == "East"):
                local *= 0.6
            product = random.choice(list(PRODUCTS))
            lo, hi = PRODUCTS[product]
            unit_price = round(random.uniform(lo, hi), 2)
            quantity = max(1, round(random.uniform(1, 6) * local))
            revenue = round(quantity * unit_price, 2)
            rows.append({
                "order_id": order_id,
                "order_date": d.isoformat(),
                "customer_id": f"C{random.randint(1, NUM_CUSTOMERS):04d}",
                "segment": segment,
                "region": region,
                "product": product,
                "quantity": quantity,
                "unit_price": unit_price,
                "revenue": revenue,
            })
    _write(out_dir / "sales.csv", rows)


def make_products(out_dir: Path) -> None:
    rows = [
        {"product": name, "category": info["category"],
         "unit_cost": info["unit_cost"], "supplier": info["supplier"]}
        for name, info in PRODUCT_INFO.items()
    ]
    _write(out_dir / "products.csv", rows)


def make_customers(out_dir: Path) -> None:
    start = date(2022, 1, 1)
    span = (date(2024, 6, 30) - start).days
    rows = []
    for i in range(1, NUM_CUSTOMERS + 1):
        signup = start + timedelta(days=random.randint(0, span))
        rows.append({
            "customer_id": f"C{i:04d}",
            "signup_date": signup.isoformat(),
            "tier": random.choices(TIERS, weights=[5, 3, 2])[0],
            "country": random.choice(COUNTRIES),
            "industry": random.choice(INDUSTRIES),
        })
    _write(out_dir / "customers.csv", rows)


def _write(path: Path, rows: list) -> None:
    with path.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)
    print(f"Wrote {len(rows)} rows -> {path}")


def main() -> None:
    out = Path(__file__).resolve().parent.parent / "sample_data"
    out.mkdir(parents=True, exist_ok=True)
    # order matters for reproducibility (shared RNG stream): sales, then customers
    make_sales(out)
    make_products(out)
    make_customers(out)


if __name__ == "__main__":
    main()
