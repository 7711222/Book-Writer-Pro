"""Deterministic KDP royalty estimator — pure Python, no deps.

Two products, one estimate each:

  EBOOK — Kindle's 35% vs 70% royalty plans. The 70% plan is only *eligible* when the
  list price is between $2.99 and $9.99 (US); outside that band only 35% is available.
  The 70% plan also subtracts a per-MB delivery fee ($0.15/MB on Amazon.com, with a
  $0.01 minimum). We report the applicable royalty AND the alternate plan so the
  author can compare.

  PAPERBACK — KDP prints on demand and pays a percentage of list price minus a
  printing cost. Since KDP's June 10, 2025 update, US print books earn 50% when the
  list price is below $9.99 and 60% at or above $9.99. The printing cost follows
  KDP's published US B&W formula: a flat $2.30 for 24-108 pages, else $1.00 plus
  $0.012 per TOTAL page (white and cream B&W paper share the same rate).
  Paperback royalty = rate * list_price - printing_cost, floored at 0.

All money is rounded to cents. These are ESTIMATES against KDP's published rates
(current as of mid-2025) — confirm in the KDP UI.
"""
from __future__ import annotations

from typing import Any, Dict, Tuple

# ---------------------------------------------------------------------------
# Ebook constants
# ---------------------------------------------------------------------------
EBOOK_70_MIN = 2.99             # inclusive lower bound for 70% eligibility (US)
EBOOK_70_MAX = 9.99             # inclusive upper bound for 70% eligibility (US)
DELIVERY_FEE_PER_MB_US = 0.15   # Amazon.com delivery fee on the 70% plan ($/MB)
MIN_DELIVERY_FEE = 0.01         # KDP charges at least $0.01 on the 70% plan

# ---------------------------------------------------------------------------
# Paperback printing cost — US, B&W ink. KDP's published formula:
#   24-108 pages   -> flat $2.30 per book (no per-page charge)
#   110+ pages     -> $1.00 fixed + $0.012 x TOTAL page count
# White and cream B&W paper cost the same per page in the US. KDP's exact
# figures change over time; confirm in the KDP UI.
# ---------------------------------------------------------------------------
PB_SMALL_FLAT_COST_US = 2.30    # flat charge for the 24-108 page tier
PB_SMALL_MAX_PAGES = 108        # last page count on the flat-fee tier
PB_LARGE_FIXED_COST_US = 1.00   # fixed charge for 110+ page books...
PB_LARGE_PER_PAGE_US = 0.012    # ...plus this per TOTAL page

# Paperback royalty rate — tiered by US list price since KDP's June 10, 2025
# update: below $9.99 pays 50%; $9.99 and above pays 60%.
PAPERBACK_RATE_PRICE_THRESHOLD = 9.99
PAPERBACK_ROYALTY_RATE_LOW = 0.50    # list price below $9.99
PAPERBACK_ROYALTY_RATE_HIGH = 0.60   # list price at or above $9.99


def _money(x: float) -> float:
    """Round to cents (half-up enough for planning; banker's rounding is fine here)."""
    return round(float(x) + 1e-9, 2)


def estimate_page_count(graph, words_per_page: int = 300) -> int:
    """Estimate paperback page count from total chapter words (min 24)."""
    total = 0
    for n in getattr(graph, "chapters", {}):
        rec = graph.chapters[n]
        wc = getattr(rec, "word_count", 0) or 0
        if not wc and getattr(rec, "text", ""):
            wc = len(rec.text.split())
        total += wc
    wpp = words_per_page or 300
    return max(24, int(round(total / wpp)) or 24)


def _paperback_printing_cost(page_count: int, paper: str = "white") -> float:
    """US B&W print cost per KDP's published formula.

    White and cream B&W paper share the same per-page rate in the US, so
    ``paper`` does not change the cost (kept for API compatibility).
    """
    if page_count <= PB_SMALL_MAX_PAGES:
        cost = PB_SMALL_FLAT_COST_US
    else:
        cost = PB_LARGE_FIXED_COST_US + page_count * PB_LARGE_PER_PAGE_US
    return _money(cost)


def _paperback_royalty_rate(price: float) -> float:
    """Tiered US paperback rate: 50% below $9.99, 60% at or above (June 2025)."""
    if price >= PAPERBACK_RATE_PRICE_THRESHOLD:
        return PAPERBACK_ROYALTY_RATE_HIGH
    return PAPERBACK_ROYALTY_RATE_LOW


def estimate_royalties(
    *,
    list_price: float,
    marketplace: str = "US",
    page_count: int,
    trim: Tuple[float, float] = (6.0, 9.0),
    paper: str = "white",
    ebook_file_mb: float = 1.0,
) -> Dict[str, Any]:
    """Estimate ebook + paperback per-sale royalties for a list price.

    Returns ``{ebook:{...}, paperback:{...}, assumptions:[...], note:...}``.
    Deterministic and dependency-free. Money rounded to cents.
    """
    price = max(0.0, float(list_price))
    mb = max(0.0, float(ebook_file_mb))
    pages = max(1, int(page_count))

    # ---- EBOOK ------------------------------------------------------------
    eligible_70 = EBOOK_70_MIN <= price <= EBOOK_70_MAX
    delivery_fee = _money(max(MIN_DELIVERY_FEE, mb * DELIVERY_FEE_PER_MB_US))

    royalty_70 = _money(max(0.0, 0.70 * (price - delivery_fee))) if eligible_70 else None
    royalty_35 = _money(max(0.0, 0.35 * price))

    if eligible_70:
        plan = "70%"
        ebook_royalty = royalty_70
        alternate = {"plan": "35%", "royalty_per_sale": royalty_35,
                     "delivery_fee": 0.0}
    else:
        plan = "35%"
        ebook_royalty = royalty_35
        # show what 70% *would* pay if it were eligible, for comparison context
        alternate = {
            "plan": "70%",
            "eligible": False,
            "reason": f"price must be ${EBOOK_70_MIN}-${EBOOK_70_MAX} for the 70% plan",
            "royalty_per_sale": _money(max(0.0, 0.70 * (price - delivery_fee))),
            "delivery_fee": delivery_fee,
        }

    ebook = {
        "list_price": _money(price),
        "plan": plan,
        "royalty_per_sale": ebook_royalty,
        "delivery_fee": delivery_fee if plan == "70%" else 0.0,
        "eligible_for_70": eligible_70,
        "alternate_plan": alternate,
    }

    # ---- PAPERBACK --------------------------------------------------------
    printing_cost = _paperback_printing_cost(pages, paper=paper)
    pb_rate = _paperback_royalty_rate(price)
    pb_royalty = _money(max(0.0, pb_rate * price - printing_cost))
    paperback = {
        "list_price": _money(price),
        "trim": [float(trim[0]), float(trim[1])],
        "paper": (paper or "white").lower(),
        "page_count": pages,
        "royalty_rate": pb_rate,
        "printing_cost": printing_cost,
        "royalty_per_sale": pb_royalty,
        "below_cost": (pb_rate * price) < printing_cost,
    }

    assumptions = [
        f"Marketplace: {marketplace} (figures use US constants).",
        f"Ebook 70% plan eligible only for list price ${EBOOK_70_MIN}-${EBOOK_70_MAX}; "
        f"otherwise 35%.",
        f"Ebook 70% plan subtracts a delivery fee of ${DELIVERY_FEE_PER_MB_US}/MB "
        f"(minimum ${MIN_DELIVERY_FEE:.2f}; file ~{mb} MB).",
        f"Paperback royalty = {pb_rate:.0%} x list price - printing cost "
        f"({PAPERBACK_ROYALTY_RATE_LOW:.0%} below "
        f"${PAPERBACK_RATE_PRICE_THRESHOLD}, {PAPERBACK_ROYALTY_RATE_HIGH:.0%} at or "
        f"above — KDP US rates since June 2025).",
        f"Printing cost (US, B&W, {(paper or 'white').lower()} paper): "
        f"${PB_SMALL_FLAT_COST_US:.2f} flat for 24-{PB_SMALL_MAX_PAGES} pages, else "
        f"${PB_LARGE_FIXED_COST_US:.2f} + ${PB_LARGE_PER_PAGE_US}/page (all pages).",
    ]

    return {
        "ebook": ebook,
        "paperback": paperback,
        "assumptions": assumptions,
        "note": "estimates — confirm in KDP",
    }
