"""Pure parsing of the portal's data-volume text (German number format)."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

UNIT_TO_MB = {"KB": 1 / 1024, "MB": 1.0, "GB": 1024.0, "TB": 1024.0 * 1024.0}

_NUM = r"\d[\d.,]*"
_UNIT = r"KB|MB|GB|TB"
INLAND_RE = re.compile(r"\bInland\b", re.IGNORECASE)
ROAMING_RE = re.compile(r"Roaming|Ausland", re.IGNORECASE)
VOLUME_RE = re.compile(
    rf"(?P<rem>{_NUM})\s*(?P<rem_unit>{_UNIT})\s*von\s*(?P<tot>{_NUM})\s*(?P<tot_unit>{_UNIT})",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class Usage:
    remaining_mb: float
    total_mb: float | None = None
    source: str = "dom"
    raw: str = ""

    def short(self) -> str:
        total = f" / {fmt_mb(self.total_mb)}" if self.total_mb else ""
        return f"{fmt_mb(self.remaining_mb)}{total}"


def fmt_mb(mb: float | None) -> str:
    if mb is None:
        return "?"
    if mb >= 1024:
        return f"{mb / 1024:.2f} GB"
    return f"{mb:.0f} MB"


def parse_de_number(s: str) -> float:
    """Parse '1.024,5' / '0,85' / '25' / '1.5' into a float."""
    s = s.strip().rstrip(".,")
    if not s:
        raise ValueError("empty number")
    if "," in s:  # German decimal comma, dots are thousands separators
        return float(s.replace(".", "").replace(",", "."))
    if "." in s:
        head, _, tail = s.rpartition(".")
        # "1.024" / "12.345.678" -> thousands; "0.850" or "1.5" -> decimal
        if len(tail) == 3 and head.replace(".", "").isdigit() and head != "0":
            return float(s.replace(".", ""))
        return float(s)
    return float(s)


def to_mb(value: str, unit: str) -> float:
    return parse_de_number(value) * UNIT_TO_MB[unit.upper()]


def parse_volume_text(text: str) -> Usage | None:
    """'850,5 MB von 25 GB Inland' -> Usage(remaining_mb=850.5, total_mb=25600)."""
    clean = " ".join(text.replace("\xa0", " ").split())
    m = VOLUME_RE.search(clean)
    if not m:
        return None
    try:
        remaining = to_mb(m["rem"], m["rem_unit"])
        total = to_mb(m["tot"], m["tot_unit"])
    except ValueError:
        return None
    return Usage(remaining_mb=remaining, total_mb=total, raw=clean[:200])


def pick_data_usage(texts: list[str]) -> tuple[int, Usage] | None:
    """Choose the domestic data tile among several 'X von Y' texts.

    Returns (index, usage). Prefers 'Inland'; never picks a roaming tile, because
    a wrong (always full) roaming reading would silently stop all claims.
    """
    parsed = [(i, t, parse_volume_text(t)) for i, t in enumerate(texts)]
    valid = [(i, t, u) for i, t, u in parsed if u is not None and not ROAMING_RE.search(t)]
    for i, t, u in valid:
        if INLAND_RE.search(t):
            return i, u
    if valid:
        i, _, u = valid[0]
        return i, u
    return None


# --- Portal JSON API: /scs/bff/scs-209-selfcare-dashboard-bff/selfcare-dashboard/v1/offers
PACK_UNIT_TO_MB = {"bytes": 1 / 1024 / 1024, "kilobytes": 1 / 1024, "megabytes": 1.0, "gigabytes": 1024.0}


def usage_from_offers(data: Any) -> Usage | None:
    """Sum the domestic data packs of active offers. Roaming (EU/FUP) packs are excluded."""
    if not isinstance(data, dict):
        return None
    remaining = total = 0.0
    found = False
    for offer in data.get("subscribedOffers") or []:
        if not isinstance(offer, dict) or offer.get("status", "active") != "active":
            continue
        roaming_refs = {
            v.get("allowanceParameter")
            for v in ((offer.get("unlimitedUsage") or {}).get("includedDataVolume") or [])
            if isinstance(v, dict) and v.get("countryGroup") not in (None, "germanyOnly")
        }
        for pack in offer.get("pack") or []:
            if not isinstance(pack, dict) or pack.get("type") != "data":
                continue
            ref = pack.get("balanceAttributeReference") or ""
            if ref in roaming_refs or ref.endswith("FUP"):
                continue
            factor = PACK_UNIT_TO_MB.get(str(pack.get("unit", "")).lower())
            try:
                allocated, used = float(pack["allocated"]), float(pack["used"])
            except (KeyError, TypeError, ValueError):
                continue
            if factor is None:
                continue
            remaining += max(0.0, allocated - used) * factor
            total += allocated * factor
            found = True
    if not found:
        return None
    return Usage(remaining_mb=remaining, total_mb=total, source="api")


def describe_packs(data: Any) -> list[dict[str, Any]]:
    """Non-personal summary of the data packs (logged around claims, to learn the API)."""
    out = []
    for offer in (data or {}).get("subscribedOffers") or []:
        for pack in offer.get("pack") or []:
            if isinstance(pack, dict) and pack.get("type") == "data":
                out.append({k: pack.get(k) for k in ("balanceAttributeReference", "unit", "allocated", "used")})
    return out
