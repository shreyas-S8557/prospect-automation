"""Fund-Flow public angel investor CSV source."""

from __future__ import annotations

import csv
import re

import httpx

from ..config import FUND_FLOW_CSV_URL
from ..models import InvestorRow
from ..quality import is_us_location, is_valid_person_name, normalize_linkedin


def parse_csv_sectors(sectors: str) -> str:
    if not sectors:
        return ""
    parts = [s.strip() for s in re.split(r"[,;|]", sectors) if s.strip()]
    return "; ".join(dict.fromkeys(parts))


def fetch_fund_flow(client: httpx.Client) -> list[InvestorRow]:
    resp = client.get(FUND_FLOW_CSV_URL)
    resp.raise_for_status()
    rows: list[InvestorRow] = []
    for row in csv.DictReader(resp.text.lstrip("\ufeff").splitlines()):
        if (row.get("Fund Type") or "").strip().lower() != "angel investor":
            continue
        loc = row.get("Location", "")
        if loc and not is_us_location(loc):
            continue
        name = (row.get("Investor Name") or row.get("Partner Name") or "").strip()
        if not is_valid_person_name(name):
            continue
        li = normalize_linkedin(
            row.get("LinkedIn Link", "") or row.get("Website (if available)", "")
        )
        if not li:
            continue
        email = (row.get("Partner Email") or "").strip()
        if email and "@" not in email:
            email = ""
        sectors = parse_csv_sectors(row.get("Fund Focus (Sectors)", ""))
        rows.append({
            "name": name,
            "location": loc or "United States",
            "linkedin_url": li,
            "profile_title": "Angel Investor",
            "summary": (row.get("Fund Description") or "").strip()
            or f"Angel investor focused on {sectors or 'startups'}.",
            "industries": sectors,
            "email": email,
            "phone": "",
            "source": "fund_flow_csv",
        })
    return rows


def build_email_lookup(client: httpx.Client) -> dict[str, str]:
    email_by_name: dict[str, str] = {}
    resp = client.get(FUND_FLOW_CSV_URL)
    if resp.status_code != 200:
        return email_by_name
    for row in csv.DictReader(resp.text.lstrip("\ufeff").splitlines()):
        if (row.get("Fund Type") or "").strip().lower() != "angel investor":
            continue
        name = (row.get("Investor Name") or row.get("Partner Name") or "").strip()
        email = (row.get("Partner Email") or "").strip()
        if name and email and "@" in email:
            email_by_name[name.lower()] = email
    return email_by_name
