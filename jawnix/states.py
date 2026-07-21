from __future__ import annotations

import re

import phonenumbers
from phonenumbers import geocoder


US_STATES = frozenset(
    "AL AK AZ AR CA CO CT DE FL GA HI ID IL IN IA KS KY LA ME MD MA MI MN MS "
    "MO MT NE NV NH NJ NM NY NC ND OH OK OR PA RI SC SD TN TX UT VT VA WA WV WI WY DC".split()
)

STATE_NAMES = {
    name: code
    for code, name in {
        "AL": "Alabama", "AK": "Alaska", "AZ": "Arizona", "AR": "Arkansas", "CA": "California",
        "CO": "Colorado", "CT": "Connecticut", "DE": "Delaware", "FL": "Florida", "GA": "Georgia",
        "HI": "Hawaii", "ID": "Idaho", "IL": "Illinois", "IN": "Indiana", "IA": "Iowa",
        "KS": "Kansas", "KY": "Kentucky", "LA": "Louisiana", "ME": "Maine", "MD": "Maryland",
        "MA": "Massachusetts", "MI": "Michigan", "MN": "Minnesota", "MS": "Mississippi", "MO": "Missouri",
        "MT": "Montana", "NE": "Nebraska", "NV": "Nevada", "NH": "New Hampshire", "NJ": "New Jersey",
        "NM": "New Mexico", "NY": "New York", "NC": "North Carolina", "ND": "North Dakota", "OH": "Ohio",
        "OK": "Oklahoma", "OR": "Oregon", "PA": "Pennsylvania", "RI": "Rhode Island", "SC": "South Carolina",
        "SD": "South Dakota", "TN": "Tennessee", "TX": "Texas", "UT": "Utah", "VT": "Vermont",
        "VA": "Virginia", "WA": "Washington", "WV": "West Virginia", "WI": "Wisconsin", "WY": "Wyoming",
        "DC": "Washington D.C.",
    }.items()
}

_NON_DIGIT = re.compile(r"\D+")


def normalize_phone(value: object) -> str | None:
    digits = _NON_DIGIT.sub("", str(value or ""))
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]
    return digits if len(digits) == 10 else None


def derive_state(phone: str) -> str | None:
    normalized = normalize_phone(phone)
    if not normalized:
        return None
    try:
        description = geocoder.description_for_number(phonenumbers.parse(f"+1{normalized}"), "en")
    except phonenumbers.NumberParseException:
        return None
    if description in STATE_NAMES:
        return STATE_NAMES[description]
    if description in US_STATES:
        return description
    match = re.search(r",\s*([A-Z]{2})$", description)
    if match and match.group(1) in US_STATES:
        return match.group(1)
    return None


def normalize_states(values: list[str] | tuple[str, ...] | set[str]) -> list[str]:
    normalized = sorted({str(value).strip().upper() for value in values if str(value).strip()})
    invalid = [state for state in normalized if state not in US_STATES]
    if invalid:
        raise ValueError(f"Unsupported state code(s): {', '.join(invalid)}")
    return normalized


def truncate_utf8(value: str, max_bytes: int = 180) -> str:
    encoded = str(value or "").replace("\r", " ").replace("\n", " ").encode("utf-8")
    if len(encoded) <= max_bytes:
        return encoded.decode("utf-8")
    return encoded[:max_bytes].decode("utf-8", errors="ignore")
