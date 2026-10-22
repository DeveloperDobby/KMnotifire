"""Product-page Nuxt metadata fallback (NOT a product catalogue crawler).

All catalogue/search enumeration was removed in ranking-only mode. The only
job of this module is to read release price/name scoped to one PDP product ID.
"""
from __future__ import annotations

import json
from bs4 import BeautifulSoup


def nuxt_data(html: str) -> list:
    soup = BeautifulSoup(html, 'html.parser')
    tag = soup.find('script', id='__NUXT_DATA__')
    if not tag:
        return []
    try:
        data = json.loads(tag.string or tag.get_text())
    except (ValueError, TypeError):
        return []
    return data if isinstance(data, list) else []


def _unpack(candidate):
    if isinstance(candidate, dict):
        return candidate
    if isinstance(candidate, str) and candidate.startswith('{'):
        try:
            obj = json.loads(candidate)
            return obj if isinstance(obj, dict) else None
        except ValueError:
            pass
    return None


def parse_pdp_nuxt_retail(html: str, pid: int):
    """Return (raw_release, currency, name) for *exactly* this product ID.

    Current sale/ask prices must never masquerade as the release price.
    """
    for candidate in nuxt_data(html):
        obj = _unpack(candidate)
        if not obj or str(obj.get('product_id')) != str(pid):
            continue
        raw = obj.get('release_price') or obj.get('retail_price')
        currency = str(obj.get('local_price_currency') or obj.get('currency') or '').upper()
        name = str(obj.get('product_name_ko') or obj.get('product_name_en') or '').strip()
        if raw is not None and not isinstance(raw, (dict, list, bool)):
            return str(raw), currency, name
        if name:
            return None, currency, name
    return None, '', ''
