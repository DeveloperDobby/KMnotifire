#!/usr/bin/env python3
"""Ranked-product hot-deal monitor. Read-only storefront access; Discord webhook notifications.

No RADAR executable, site login or cookies required. Unofficial Storefront APIs may
change or refuse GitHub-hosted runner traffic; failures never mean 'sold out'.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
from urllib.parse import urlparse

from bs4 import BeautifulSoup
import requests
from product_metadata import parse_pdp_nuxt_retail
from ranking_sources import collect_rankings, validate_ranking_url

from site_origin import PUBLIC_ORIGIN, API_ORIGIN, PUBLIC_HOSTS, REQUEST_HOSTS, SITE_HOST, VERSION_HEADER
STATE_VERSION = 1
ID_PATTERN = re.compile(r'/products/(\d+)(?:[/?#]|$)')
CURRENCY_PATTERN = re.compile(r'\$\s*([\d,]+(?:\.\d+)?)|(?:USD|US\$)\s*([\d,]+(?:\.\d+)?)|([\d,]+(?:\.\d+)?)\s*(?:원|KRW)|([\d,]+(?:\.\d+)?)\s*(?:USD|달러)', re.I)
BOT_BLOCK = ('captcha', 'access denied', 'verify you are human', 'checking your browser')


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def money(value) -> int | None:
    """Convert positive amounts only; don't accept bool or unparseable strings."""
    if isinstance(value, bool) or value is None:
        return None
    try:
        val = Decimal(str(value).replace(',', '').replace('₩', '').replace('원', '').strip())
        if not val.is_finite() or val <= 0:
            return None
        return int(val.quantize(Decimal('1'), rounding=ROUND_HALF_UP))
    except (ValueError, TypeError, InvalidOperation):
        return None


def usd_amount(value) -> Decimal | None:
    try:
        amount = Decimal(str(value).replace(',', '').replace('$', '').strip())
        return amount if amount.is_finite() and amount > 0 else None
    except (ValueError, InvalidOperation):
        return None


def round_krw(usd: Decimal, rate: Decimal) -> int:
    return int((usd * rate).quantize(Decimal('1'), rounding=ROUND_HALF_UP))


def deal_percent(price: int, reference: int) -> Decimal:
    return (Decimal(reference - price) * 100 / Decimal(reference)).quantize(Decimal('0.1'))


def is_hotdeal(price: int, reference: int, discount: Decimal) -> bool:
    return price > 0 and reference > 0 and Decimal(price) * 100 <= Decimal(reference) * (100 - discount)


def parse_product_id(raw) -> int:
    if isinstance(raw, int) and not isinstance(raw, bool) and raw > 0:
        return raw
    text = str(raw).strip()
    if text.isdecimal() and int(text) > 0:
        return int(text)
    parsed = urlparse(text)
    if parsed.hostname not in PUBLIC_HOSTS or parsed.scheme != 'https':
        raise ValueError(f'지원되지 않는 상품 주소입니다: {text[:80]}')
    match = ID_PATTERN.search(parsed.path)
    if not match:
        raise ValueError(f'상품 ID를 찾을 수 없습니다: {text[:80]}')
    return int(match.group(1))


def load_json(path: Path) -> dict:
    data = json.loads(path.read_text(encoding='utf-8'))
    if not isinstance(data, dict):
        raise ValueError(f'{path}: JSON object가 필요합니다')
    return data


def save_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + '.tmp')
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False) + '\n', encoding='utf-8')
    tmp.replace(path)


def read_state(path: Path) -> dict:
    if not path.exists():
        return {'version': STATE_VERSION, 'active_deals': {}, 'discovery': {'ids': [], 'updated_at': ''}, 'cursor': 0}
    state = load_json(path)
    if state.get('version') != STATE_VERSION or not isinstance(state.get('active_deals'), dict):
        raise ValueError('상태 파일 버전 불일치 — 기존 파일을 백업한 뒤 확인해야 합니다')
    state.setdefault('discovery', {'ids': [], 'updated_at': ''})
    state.setdefault('cursor', 0)
    state.setdefault('ranking_priority_cursor', 0)
    return state


def _ld_products(value):
    if isinstance(value, list):
        for item in value:
            yield from _ld_products(item)
    elif isinstance(value, dict):
        if value.get('@type') == 'Product' or (isinstance(value.get('@type'), list) and 'Product' in value['@type']):
            yield value
        if '@graph' in value:
            yield from _ld_products(value['@graph'])


def parse_retail(text: str) -> tuple[Decimal, str] | None:
    match = CURRENCY_PATTERN.search(text[:90])
    if not match:
        return None
    if match.group(1) or match.group(2) or match.group(4):
        amount = usd_amount(match.group(1) or match.group(2) or match.group(4))
        return (amount, 'USD') if amount else None
    amount = usd_amount(match.group(3))
    return (amount, 'KRW') if amount else None


def parse_product_html(html: str, pid: int) -> dict:
    """Release price is NEVER inferred from 'offers.price' or the current ask."""
    if any(s in html.lower() for s in BOT_BLOCK):
        raise RuntimeError('사이트 차단/캡차 화면 감지')
    soup = BeautifulSoup(html, 'html.parser')
    name = ''
    release = None
    for tag in soup.find_all('script', type='application/ld+json'):
        try:
            raw = json.loads(tag.string or tag.get_text())
        except (TypeError, ValueError):
            continue
        for product in _ld_products(raw):
            if not name:
                name = str(product.get('name') or '').strip()
            # Release/MSRP fields only. "price" and "offers.price" are asks.
            retail_raw = product.get('retail_price', product.get('release_price'))
            if retail_raw is not None and release is None:
                currency = str(product.get('local_price_currency') or product.get('priceCurrency') or 'KRW').upper()
                if isinstance(retail_raw, str) and ('$' in retail_raw or 'USD' in retail_raw.upper()):
                    parsed = parse_retail(retail_raw)
                    if parsed:
                        release = parsed
                else:
                    amount = usd_amount(retail_raw)
                    if amount and currency in ('USD', 'KRW'):
                        release = (amount, currency)
    title_tag = soup.find('meta', attrs={'property': 'og:title'})
    if not name and title_tag:
        name = str(title_tag.get('content') or '').split(' 정품')[0].strip()
    if not name and soup.title:
        name = soup.title.get_text(strip=True).split(' | ')[0].strip()
    if not name:
        _, _, nuxt_name = parse_pdp_nuxt_retail(html, pid)
        name = nuxt_name
    if not name:
        raise ValueError(f'상품 {pid}: 이름 파싱 실패')

    # Prefer the user-visible release label; JSON-LD is a secondary fallback.
    # Never use the current lowest ask (offers.price) as a release price.
    visible = soup.get_text(' ', strip=True)
    for match in re.finditer(r'발매가\s*[:：]?\s*([^\n]{0,60})', visible):
        parsed = parse_retail(match.group(1))
        if parsed:
            release = parsed
            break
    # New ZIP's Nuxt parsing helps when JSON-LD/visible release is absent.
    # Only accept release data scoped to this exact product ID.
    nuxt_raw, nuxt_currency, nuxt_name = parse_pdp_nuxt_retail(html, pid)
    if release is None and nuxt_raw:
        parsed = parse_retail(nuxt_raw)
        if parsed:
            release = parsed
        else:
            amount = usd_amount(nuxt_raw)
            if amount and nuxt_currency in ('KRW', 'USD'):
                release = (amount, nuxt_currency)
    if (not name or name.startswith(SITE_HOST.split('.', 1)[0].upper())) and nuxt_name:
        name = nuxt_name
    return {'id': pid, 'name': name[:220], 'url': f'{PUBLIC_ORIGIN}/products/{pid}', 'retail': release}


class Blocked(RuntimeError):
    pass


class SiteClient:
    def __init__(self, timeout: int = 18, gap_seconds: float = 2.0):
        self.session = requests.Session()
        self.session.headers.update({
            'User-Agent': 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36',
            'Accept': 'application/json, text/html;q=0.9,*/*;q=0.8',
            'Referer': f'{PUBLIC_ORIGIN}/',
            'Accept-Language': 'ko-KR,ko;q=0.9,en-US;q=0.6',
            VERSION_HEADER: '30',
        })
        self.timeout = timeout
        self.gap = max(0.5, float(gap_seconds))
        self.next_request_at = 0.0
        self.blocked_until = 0.0

    def get(self, url: str, params: dict | None = None) -> requests.Response:
        host = urlparse(url).hostname
        if host not in REQUEST_HOSTS or not url.startswith('https://'):
            raise ValueError('허용되지 않은 요청 대상')
        backoff_remaining = self.blocked_until - time.monotonic()
        if backoff_remaining > 0:
            raise Blocked(f'서비스 요청 대기: {backoff_remaining:.0f}초 후 재시도')
        sleep = self.next_request_at - time.monotonic()
        if sleep > 0:
            time.sleep(sleep)
        self.next_request_at = time.monotonic() + self.gap
        response = self.session.get(url, params=params, timeout=self.timeout)
        if response.status_code in (403, 429):
            self.blocked_until = time.monotonic() + max(60, int(os.getenv('SITE_BLOCK_BACKOFF_SECONDS', '900')))
            raise Blocked(f'상점 HTTP {response.status_code}: GitHub 러너 요청 거부/제한')
        response.raise_for_status()
        if any(s in response.text[:1200].lower() for s in BOT_BLOCK):
            raise Blocked('서비스 CAPTCHA/차단 페이지')
        return response

    def product(self, pid: int) -> dict:
        resp = self.get(f'{PUBLIC_ORIGIN}/products/{pid}')
        if len(resp.text) < 500:
            raise RuntimeError(f'{pid}: 비정상적으로 짧은 상품 페이지')
        return parse_product_html(resp.text, pid)

    def api_items(self, pid: int, kind: str, cursor: int | str = 1, per_page: int = 50) -> dict:
        if kind not in ('asks', 'sales'):
            raise ValueError('잘못된 API 종류')
        resp = self.get(f'{API_ORIGIN}/api/p/products/{pid}/{kind}', {'cursor': cursor, 'per_page': per_page})
        try:
            raw = resp.json()
        except ValueError as exc:
            raise RuntimeError(f'{pid} {kind}: JSON 응답 아님') from exc
        if not isinstance(raw, dict) or not isinstance(raw.get('items'), list):
            raise RuntimeError(f'{pid} {kind}: 예상하지 못한 API 형식. 거래/매물 없음으로 취급하지 않습니다')
        return raw

    def recent_sale(self, pid: int) -> int | None:
        data = self.api_items(pid, 'sales', per_page=10)
        # Endpoint default is newest-first. Verify timestamps where returned.
        items = data['items']
        dated = [x for x in items if isinstance(x, dict) and x.get('date_created')]
        if dated:
            items = sorted(dated, key=lambda x: x.get('date_created', ''), reverse=True)
        for item in items:
            if isinstance(item, dict):
                amount = money(item.get('price'))
                if amount:
                    return amount
        return None

    def sales_by_size(self, pid: int, max_pages: int = 2) -> tuple[int | None, dict[str, int]]:
        """Return newest whole-product and available size-specific prices.

        Uses the baseline's documented /sales API, rather than making one
        speculative transaction-history-summary request for every size.
        Only the first N pages are available: absent sizes use product-wide
        recent price if there was one; no fabricated sale prices.
        """
        latest: int | None = None
        prices: dict[str, int] = {}
        cursor: int | str = 1
        visited = set()
        for _ in range(max(1, min(5, max_pages))):
            if str(cursor) in visited:
                break
            visited.add(str(cursor))
            page = self.api_items(pid, 'sales', cursor=cursor, per_page=50)
            records = [item for item in page['items'] if isinstance(item, dict)]
            if any(item.get('date_created') for item in records):
                records.sort(key=lambda x: str(x.get('date_created') or ''), reverse=True)
            for item in records:
                price = money(item.get('price'))
                if price is None:
                    continue
                if latest is None:
                    latest = price
                opt = item.get('product_option')
                option = item.get('option') or (opt.get('name_display') if isinstance(opt, dict) else None)
                size = str(option or '').strip()
                if size and size not in prices:
                    prices[size] = price
            if not page.get('next_cursor') or not records:
                break
            cursor = page['next_cursor']
        return latest, prices

    def asks(self, pid: int, max_pages: int = 4) -> list[dict]:
        result = []
        cursor: int | str = 1
        seen = set()
        for _ in range(max_pages):
            if str(cursor) in seen:
                break
            seen.add(str(cursor))
            data = self.api_items(pid, 'asks', cursor=cursor)
            result.extend(item for item in data['items'] if isinstance(item, dict))
            next_cursor = data.get('next_cursor')
            if not next_cursor or not data['items']:
                break
            cursor = next_cursor
        return result


def fx_rate(state: dict, config: dict, *, http_get=requests.get) -> Decimal:
    override = os.getenv('USD_KRW_RATE', '').strip()
    if override:
        rate = Decimal(override)
        if not rate.is_finite() or not 500 <= rate <= 3000:
            raise ValueError('USD_KRW_RATE 유효 범위 500~3000 KRW')
        return rate
    fx = state.get('fx', {})
    try:
        last = datetime.fromisoformat(fx['updated_at'])
        rate = Decimal(str(fx['USD_KRW']))
        if now_utc() - last.astimezone(timezone.utc) < timedelta(hours=24) and 500 <= rate <= 3000:
            return rate
    except (KeyError, ValueError, InvalidOperation, TypeError):
        pass
    # USD base spot quote; no API key required. Fail closed if unavailable.
    resp = http_get('https://open.er-api.com/v6/latest/USD', timeout=12)
    resp.raise_for_status()
    payload = resp.json()
    if payload.get('result') != 'success':
        raise RuntimeError('환율 서비스가 정상 응답을 반환하지 않았습니다')
    rate = Decimal(str(payload.get('rates', {}).get('KRW', '0')))
    if not rate.is_finite() or not 500 <= rate <= 3000:
        raise RuntimeError('USD/KRW 환율 누락 또는 비정상')
    state['fx'] = {'USD_KRW': str(rate), 'updated_at': now_utc().isoformat()}
    return rate


def get_reference(product: dict, sale: int | None, state: dict, config: dict, rate_fetch=fx_rate) -> tuple[int, str] | None:
    if sale:
        return sale, '최근거래가'
    retail = product.get('retail')
    if retail is None:
        return None
    amount, currency = retail
    if currency == 'KRW':
        value = money(amount)
        return (value, '발매가') if value else None
    if currency == 'USD':
        rate = rate_fetch(state, config)
        return round_krw(Decimal(str(amount)), rate), f'발매가 (USD {amount} × {rate} KRW)'
    return None


def shipping_kind(item: dict) -> str | None:
    """Only notify when API marks delivery mode unambiguously.

    null/missing is documented in some unauthenticated asks responses and is
    ambiguous; DO NOT incorrectly label it regular delivery.
    """
    # Never include explicitly overseas delivery, even when the immediate flag is false.
    if item.get('is_overseas_delivery_item') is True or item.get('is_overseas') is True:
        return None
    for key in ('delivery_type', 'shipping_type', 'delivery_method'):
        if key in item and item[key] is not None:
            code = str(item[key]).strip().upper()
            if code in ('OVERSEAS', 'INTERNATIONAL', 'GLOBAL'):
                return None
            if code in ('FAST', 'EXPRESS', 'IMMEDIATE', 'QUICK'):
                return '빠른배송'
            if code in ('NORMAL', 'STANDARD', 'GENERAL', 'REGULAR'):
                return '일반배송'
    value = item.get('is_immediate_delivery_item')
    if value is True:
        return '빠른배송'
    if value is False:
        return '일반배송'
    return None


def cheap_asks(pid: int, name: str, reference: int, reference_label: str,
               asks: list[dict], discount: Decimal,
               size_prices: dict[str, int] | None = None) -> list[dict]:
    deals = {}
    for item in asks:
        price = money(item.get('price'))
        size_option = item.get('product_option')
        size = str(item.get('option') or (size_option.get('name_display') if isinstance(size_option, dict) else None) or '').strip()
        kind = shipping_kind(item)
        qty = item.get('quantity')
        if qty is not None and (not isinstance(qty, (int, float)) or qty <= 0):
            continue
        # A size's latest observed sale is more relevant than the latest
        # whole-product sale. Other sizes retain the product-wide reference.
        own_price = (size_prices or {}).get(size)
        own_ref = own_price or reference
        own_label = f'최근거래가 (사이즈 {size})' if own_price else reference_label
        if not price or not size or not kind or not is_hotdeal(price, own_ref, discount):
            continue
        # Distinct availability = product × size × shipping mode.
        key = f'{pid}|{size}|{kind}'
        record = {'key': key, 'id': pid, 'name': name, 'size': size, 'shipping': kind,
                  'price': price, 'reference': own_ref, 'reference_label': own_label,
                  'discount': str(deal_percent(price, own_ref)),
                  'url': f'{PUBLIC_ORIGIN}/products/{pid}'}
        if key not in deals or price < deals[key]['price']:
            deals[key] = record
    return sorted(deals.values(), key=lambda x: (Decimal(x['discount']) * -1, x['price']))


def discover(config: dict, state: dict, client: SiteClient | None = None) -> list[int]:
    """Ranking-only cumulative discovery. Never switch to general shop/search.

    Historical product IDs are append-only from the first successful ranking
    scan onwards. Legacy generic-catalog IDs are deliberately NOT imported.
    """
    settings = config['discovery']
    if settings.get('mode') != 'ranking':
        raise ValueError('discovery.mode는 ranking이어야 합니다 (전체 상품 탐색 금지)')
    old = state.get('discovery', {})
    # A legacy state may contain non-ranking catalogue IDs: never treat them
    # as previously ranked products during an upgrade.
    previous = [parse_product_id(x) for x in old.get('ids', [])] if old.get('mode') == 'ranking' else []
    if not settings.get('enabled', True) or time.monotonic() < state.get('_discovery_retry_at', 0):
        return previous
    try:
        updated = datetime.fromisoformat(old['updated_at'])
        if old.get('mode') == 'ranking' and now_utc() - updated.astimezone(timezone.utc) < timedelta(seconds=int(settings.get('refresh_seconds', 3600))):
            return previous
    except (ValueError, TypeError, KeyError):
        pass

    urls = settings.get('urls', [])
    fresh, success_urls, warnings = collect_rankings(
        client or SiteClient(int(config.get('timeout_seconds', 18)), float(config.get('api_gap_seconds', 1.5))),
        urls,
        scroll_steps=int(settings.get('scroll_steps', 0)),
        max_items_per_page=int(settings.get('max_items_per_page', 200)),
    )
    for warning in warnings:
        print(f'[RANKING WARNING] {warning}', file=sys.stderr, flush=True)
    ids = list(dict.fromkeys(previous + fresh))  # No eviction or max_products cap.
    complete = len(success_urls) == len(urls)
    # On a partial refresh, keep current ranking products from previously
    # successful sources, rather than interpreting a failed source as empty.
    recent = (fresh if complete else list(dict.fromkeys(
        [parse_product_id(x) for x in old.get('current_ids', [])] + fresh)))
    if old.get('mode') != 'ranking':
        state['cursor'] = 0
        state.pop('next_product_id', None)
        state['ranking_priority_cursor'] = 0
        # Keep dedup records only for goods actually confirmed on the ranking.
        state['active_deals'] = {k: v for k, v in state.get('active_deals', {}).items()
                                 if k.split('|', 1)[0].isdecimal() and int(k.split('|', 1)[0]) in set(ids)}
    state['discovery'] = {'mode': 'ranking', 'ids': ids,
                          'current_ids': recent, 'updated_at': now_utc().isoformat()}
    print(f'[RANKING] 금회 {len(fresh)}개 / 누적 {len(ids)}개 / 성공 랭킹 {len(success_urls)}/{len(urls)} (과거 이력 삭제 없음)', flush=True)
    return ids


def validate_config(config: dict) -> None:
    disc = config.get('discount_percent', 20)
    if not (0 < Decimal(str(disc)) < 100):
        raise ValueError('discount_percent는 0~100 사이여야 합니다')
    for k in ('max_products_per_cycle', 'max_ask_pages', 'max_notifications_per_cycle'):
        if not (1 <= int(config[k]) <= 100):
            raise ValueError(f'{k}는 1~100')
    if not (10 <= float(config.get('cycle_time_budget_seconds', 52)) <= 55):
        raise ValueError('cycle_time_budget_seconds는 10~55초 범위여야 합니다 (60초 주기 보호)')
    if not (0.5 <= float(config.get('api_gap_seconds', 1.5)) <= 30):
        raise ValueError('api_gap_seconds는 0.5~30초 범위여야 합니다')
    settings = config.get('discovery', {})
    if settings.get('mode') != 'ranking' or not settings.get('enabled', True):
        raise ValueError('랭킹 상품만 감시하므로 discovery.mode=ranking / enabled=true가 필요합니다')
    if config.get('product_ids'):
        raise ValueError('랭킹 외 상품 제외를 위해 product_ids는 빈 배열이어야 합니다')
    urls = settings.get('urls', [])
    if not isinstance(urls, list) or not urls or len(urls) > 30:
        raise ValueError('ranking.urls에 허용된 공식 랭킹 페이지 1~30개가 필요합니다')
    for url in urls:
        validate_ranking_url(url)
    if not (0 <= int(settings.get('scroll_steps', 0)) <= 15):
        raise ValueError('scroll_steps 범위는 0~15입니다')
    if not (1 <= int(settings.get('max_items_per_page', 200)) <= 1000):
        raise ValueError('max_items_per_page 범위는 1~1000입니다')
    if int(settings.get('refresh_seconds', 3600)) < 60:
        raise ValueError('refresh_seconds는 최소 60초입니다')
    if not (0 <= int(settings.get('priority_per_cycle', 2)) <= 99):
        raise ValueError('priority_per_cycle 범위는 0~99입니다')


def ranking_history(state: dict) -> list[int]:
    source = state.get('discovery', {})
    return [parse_product_id(x) for x in source.get('ids', [])] if source.get('mode') == 'ranking' else []


def scan_plan(state: dict, ids: list[int], limit: int, priority_count: int) -> tuple[list[tuple[int, str]], int, int, list[int]]:
    """Allocate some slots to current ranking and the rest to *all* history.

    Both cursors persist, so recently ranked products cannot starve older ones.
    """
    limit = min(limit, len(ids))
    next_id = state.get('_next_pid', state.get('next_product_id'))
    history_cursor = (ids.index(next_id) if next_id in ids
                      else int(state.get('_runtime_cursor', state.get('cursor', 0))) % len(ids))
    current = [pid for pid in dict.fromkeys(state.get('discovery', {}).get('current_ids', [])) if pid in ids]
    priority_count = min(max(0, priority_count), max(0, limit - 1), len(current))
    current_cursor = int(state.get('_ranking_priority_cursor', state.get('ranking_priority_cursor', 0))) % len(current) if current else 0
    priority = [current[(current_cursor + i) % len(current)] for i in range(priority_count)]
    historical = []
    pos = history_cursor
    # Keep independent history slots even if every ID is in today's ranking.
    while len(historical) < limit - len(priority):
        pid = ids[pos]
        pos = (pos + 1) % len(ids)
        if pid in priority:
            continue  # Don't inspect a product twice within one tick.
        historical.append(pid)
    # History comes first, then alternates with current-ranking priority.
    # Under a slow API/time budget, older ranked items still make progress.
    plan: list[tuple[int, str]] = []
    hi = pi = 0
    while hi < len(historical) or pi < len(priority):
        if hi < len(historical):
            plan.append((historical[hi], 'history'))
            hi += 1
        if pi < len(priority):
            plan.append((priority[pi], 'priority'))
            pi += 1
    return plan, history_cursor, current_cursor, current


def discord_webhook() -> str:
    url = os.environ.get('DISCORD_WEBHOOK_URL', '').strip()
    p = urlparse(url)
    if p.scheme != 'https' or p.hostname not in ('discord.com', 'discordapp.com') or p.query or p.fragment or not re.fullmatch(r'/api(?:/v\d+)?/webhooks/\d+/[A-Za-z0-9_-]+/?', p.path):
        raise ValueError('GitHub Secrets의 DISCORD_WEBHOOK_URL이 정상 Discord 웹훅이 아닙니다')
    return url


def post_discord(deal: dict) -> None:
    webhook = discord_webhook()
    uid = os.environ.get('DISCORD_USER_ID', '').strip()
    if uid and not re.fullmatch(r'\d{15,22}', uid):
        raise ValueError('DISCORD_USER_ID는 15~22자리 숫자여야 합니다')
    mention = f'<@{uid}>' if uid else ''
    payload = {
        'content': mention,
        'allowed_mentions': {'parse': [], 'users': [uid] if uid else []},
        'embeds': [{
            'title': f"🔥 -{deal['discount']}% | {deal['name'][:170]}",
            'url': deal['url'], 'color': 0xe0901c,
            'fields': [
                {'name': '사이즈', 'value': deal['size'][:100], 'inline': True},
                {'name': '배송', 'value': deal['shipping'], 'inline': True},
                {'name': '즉시 구매가', 'value': f"{deal['price']:,}원", 'inline': True},
                {'name': deal['reference_label'][:150], 'value': f"{deal['reference']:,}원", 'inline': False},
            ],
            'description': f"[상품 페이지에서 사이즈 선택 후 구매하기]({deal['url']})\n※ 결제 수수료 및 배송비는 할인 판정에 포함하지 않습니다.",
        }],
    }
    for attempt in range(3):
        response = requests.post(webhook, json=payload, timeout=15)
        if response.status_code in (200, 204):
            return
        if response.status_code == 429 or response.status_code >= 500:
            delay = 2 + attempt * 2
            if response.status_code == 429:
                try:
                    delay = min(20, max(1, float(response.json().get('retry_after', delay))))
                except (ValueError, TypeError):
                    pass
            time.sleep(delay)
            continue
        raise RuntimeError(f'Discord 전송 실패 HTTP {response.status_code}')
    raise RuntimeError('Discord 여러 차례 재시도 후에도 전송 실패')


def checkpoint(path: Path) -> None:
    """Only commit the state file. Preserves the ZIP's git-backed deduplication."""
    if not Path('.git').exists():
        raise RuntimeError('git 동기화는 GitHub 저장소 checkout에서 실행해야 합니다')
    branch = os.environ.get('GITHUB_REF_NAME', '')
    if not re.fullmatch(r'[A-Za-z0-9._/-]{1,180}', branch) or branch.startswith('-'):
        raise ValueError('GITHUB_REF_NAME을 확인해 주세요')
    relative = str(path.resolve().relative_to(Path.cwd().resolve()))

    def git(*args):
        return subprocess.run(['git', *args], text=True, capture_output=True, check=True, timeout=60).stdout.strip()

    if git('status', '--porcelain', '--', relative):
        git('add', '--', relative)
        staged = subprocess.run(['git', 'diff', '--cached', '--quiet', '--', relative])
        if staged.returncode == 1:
            git('-c', 'user.name=github-actions[bot]',
                '-c', 'user.email=41898282+github-actions[bot]@users.noreply.github.com',
                'commit', '-m', 'chore: save ranked deal state [skip ci]', '--', relative)
        elif staged.returncode != 0:
            raise RuntimeError('git diff 오류')
    ahead = int(git('rev-list', '--count', 'HEAD', '--not', f'origin/{branch}') or '0')
    if ahead:
        git('pull', '--rebase', 'origin', branch)
        git('push', 'origin', f'HEAD:refs/heads/{branch}')
        print('[STATE] 알림/탐색 이력 GitHub 동기화 완료', flush=True)


def monitor_once(config: dict, state: dict, client: SiteClient, *, dry_run: bool = False) -> tuple[int, dict]:
    """One scan with a soft time budget; never mark blocked products sold out.

    Larger batches speed up ranking-history coverage on responsive endpoints.
    Stop *starting* new product scans near the minute boundary, preserving the
    cursor for any unprocessed products. An in-flight HTTP call can still overrun
    the time budget, so this is not a hard real-time guarantee.
    """
    cycle_started = time.monotonic()
    budget = float(config.get('cycle_time_budget_seconds', 52))
    issues = 0
    try:
        discovered = discover(config, state, client)
    except Exception as exc:
        if isinstance(exc, Blocked) and hasattr(client, 'blocked_until'):
            client.blocked_until = max(client.blocked_until,
                                       time.monotonic() + max(60, int(os.getenv('SITE_BLOCK_BACKOFF_SECONDS', '900'))))
        discovered = ranking_history(state)
        state['_discovery_retry_at'] = time.monotonic() + 900
        issues += 1
        print(f'[DISCOVERY ERROR] {type(exc).__name__}: {str(exc)[:1800]}', file=sys.stderr, flush=True)
        print('[DISCOVERY HELP] 공식 랭킹 HTTP 5xx는 상품 0개를 뜻하지 않습니다. HTML/브라우저 모두 실패했다면 사이트 장애 또는 실행 환경 접근 제한일 수 있습니다.', file=sys.stderr, flush=True)
    # Strictly no manually supplied or previously catalogued non-ranking IDs.
    ids = list(dict.fromkeys(parse_product_id(x) for x in discovered))
    if not ids:
        print('[ERROR] 랭킹 누적 상품 ID가 없습니다. 먼저 랭킹 탐색이 성공해야 합니다.', file=sys.stderr)
        return 1, state
    limit = int(config['max_products_per_cycle'])
    priority_count = int(config.get('discovery', {}).get('priority_per_cycle', 0)) if config.get('discovery', {}).get('mode') == 'ranking' else 0
    batch, next_history, next_priority, current_ranked = scan_plan(state, ids, limit, priority_count)
    position_by_id = {pid: i for i, pid in enumerate(ids)}
    current_position = {pid: i for i, pid in enumerate(current_ranked)}
    discount = Decimal(str(config.get('discount_percent', 20)))
    cap = int(config['max_notifications_per_cycle'])
    notifications = 0
    good = 0
    attempted = 0
    budget_hit = False
    last_product_seconds = 3.0
    for pid, track in batch:
        # Leave time for state persistence and the next 60-second tick. The
        # actual limit can be smaller than max_products_per_cycle on slow days.
        reserve = max(3.0, min(8.0, last_product_seconds * 1.25))
        if budget - (time.monotonic() - cycle_started) < reserve:
            budget_hit = True
            print(f'[TIME BUDGET] 추가 상품 시작 중단: {attempted}/{len(batch)}개 시도', flush=True)
            break
        attempted += 1
        item_started = time.monotonic()
        try:
            product = client.product(pid)
            size_prices: dict[str, int] = {}
            size_settings = config.get('size_recent_sales', {})
            if size_settings.get('enabled', False):
                sale, size_prices = client.sales_by_size(pid, max_pages=int(size_settings.get('max_pages', 2)))
            else:
                sale = client.recent_sale(pid)
            # No sale and no release price: exclude. No FX required in this case.
            reference = get_reference(product, sale, state, config)
            if reference is None:
                print(f'[SKIP] {pid} {product["name"][:55]}: 최근거래가/발매가 없음', flush=True)
                # Successful full observation: clear old deals, if any.
                deals = []
            else:
                reference_value, reference_label = reference
                asks = client.asks(pid, max_pages=int(config['max_ask_pages']))
                deals = cheap_asks(pid, product['name'], reference_value, reference_label,
                                   asks, discount, size_prices=size_prices)
            current = {d['key']: d for d in deals}
            previous = state['active_deals']
            # Successful read means old listings that disappeared/repriced can re-trigger if they return.
            # Important: only drop old keys after *successful* metadata/sales/asks access.
            for key in [k for k in previous if k.startswith(f'{pid}|') and k not in current]:
                del previous[key]
            for deal in deals:
                old = previous.get(deal['key'])
                # Only new deal or strictly cheaper ask triggers again.
                if old is not None and deal['price'] >= old.get('price', 0):
                    continue
                if notifications >= cap:
                    continue  # Will retry next cycle; do not mark as notified.
                if dry_run:
                    print('[DRY RUN]', json.dumps(deal, ensure_ascii=False), flush=True)
                else:
                    post_discord(deal)
                previous[deal['key']] = {'price': deal['price']}
                notifications += 1
            good += 1
            if track == 'priority':
                next_priority = (current_position[pid] + 1) % len(current_ranked)
            else:
                next_history = (position_by_id[pid] + 1) % len(ids)
            print(f'[SCAN] id={pid} deals={len(deals)} notified={notifications} name={product["name"][:50]}', flush=True)
        except Blocked as exc:
            issues += 1
            # Retry this exact item after the site's backoff, not the next batch.
            if track == 'priority':
                next_priority = current_position[pid]
            else:
                next_history = position_by_id[pid]
            print(f'[BLOCKED] {pid}: {exc}', file=sys.stderr, flush=True)
            break
        except Exception as exc:
            issues += 1
            if track == 'priority':
                next_priority = (current_position[pid] + 1) % len(current_ranked)
            else:
                next_history = (position_by_id[pid] + 1) % len(ids)
            print(f'[ERROR] {pid}: {type(exc).__name__}: {str(exc)[:160]}', file=sys.stderr, flush=True)
        finally:
            last_product_seconds = max(0.1, time.monotonic() - item_started)
    state['_runtime_cursor'] = next_history
    state['_next_pid'] = ids[next_history]
    state['_ranking_priority_cursor'] = next_priority
    print(f'[SUMMARY] 확인시도={attempted}/{len(batch)} 성공상품={good} 실패={issues} 알림={notifications} '
          f'누적상품={len(ids)} 시간예산도달={budget_hit}', flush=True)
    return (1 if issues or good == 0 else 0), state


def loop(config_file: Path, state_file: Path, *, interval: int, duration: int, git_sync: bool, dry_run: bool) -> int:
    config = load_json(config_file)
    validate_config(config)
    state = read_state(state_file)
    if not dry_run:
        discord_webhook()  # Fail fast, even when the first pass has no deals.
    client = SiteClient(int(config.get('timeout_seconds', 18)), float(config.get('api_gap_seconds', 2)))
    deadline = time.monotonic() + duration
    success = 0
    iteration = 0
    print(f'[START] {interval}초 간격, 실행 {duration}초, Git sync={git_sync}', flush=True)
    while True:
        iteration += 1
        started = time.monotonic()
        status = 1
        try:
            status, state = monitor_once(config, state, client, dry_run=dry_run)
            if status == 0:
                success += 1
        except Exception as exc:
            print(f'[ERROR] Loop: {type(exc).__name__}: {str(exc)[:160]}', file=sys.stderr, flush=True)
        if not dry_run:
            # Persist product-ID cursor every 15 ticks (plus final tick) so a
            # new scheduled runner resumes where the last runner stopped.
            # Avoid committing every 60 seconds when only the cursor moves.
            if iteration % 15 == 0 or time.monotonic() >= deadline:
                state['next_product_id'] = state.get('_next_pid', state.get('next_product_id'))
                state['cursor'] = state.get('_runtime_cursor', state.get('cursor', 0))
                state['ranking_priority_cursor'] = state.get('_ranking_priority_cursor', state.get('ranking_priority_cursor', 0))
            save_json(state_file, {k: v for k, v in state.items() if not k.startswith('_')})
            if git_sync:
                try:
                    checkpoint(state_file)
                except Exception as exc:
                    print(f'[STATE ERROR] {type(exc).__name__}: 다음 주기 재시도', file=sys.stderr, flush=True)
        if time.monotonic() >= deadline:
            break
        remaining = min(deadline - time.monotonic(), started + interval - time.monotonic())
        if remaining > 0:
            time.sleep(remaining)
    return 0 if success else 1


def probe(config: dict) -> int:
    """Diagnose rankings independently from PDP / ask API compatibility."""
    client = SiteClient(int(config.get('timeout_seconds', 18)), float(config.get('api_gap_seconds', 2)))
    settings = config['discovery']
    try:
        found, successful, warnings = collect_rankings(
            client, settings['urls'], scroll_steps=int(settings.get('scroll_steps', 0)),
            max_items_per_page=int(settings.get('max_items_per_page', 200)))
    except Exception as exc:
        print(f'[PROBE RANKING FAILED] {type(exc).__name__}: {exc}', file=sys.stderr)
        return 1

    print(json.dumps({'ranking_urls_checked': len(settings['urls']),
                      'ranking_urls_succeeded': len(successful),
                      'ranking_product_ids': len(found),
                      'ranking_sample_ids': found[:10],
                      'ranking_warnings': warnings[:10]}, ensure_ascii=False, indent=2))
    # A ranked product can be a brand-direct item with no secondary-market
    # asks/sales. Don't assume the first ranked ID represents API compatibility.
    errors = []
    for pid in found[:6]:
        try:
            product = client.product(pid)
            sale, sample_sizes = client.sales_by_size(pid, max_pages=1)
            asks = client.asks(pid, max_pages=1)
            by_shipping = {'일반배송': 0, '빠른배송': 0, '미확인': 0}
            for item in asks:
                by_shipping[shipping_kind(item) or '미확인'] += 1
            print(json.dumps({'product_id': pid, 'product_name': product['name'],
                              'release': [str(x) for x in product['retail']] if product['retail'] else None,
                              'recent_sale_krw': sale,
                              'size_recent_sale_sample': dict(list(sample_sizes.items())[:5]),
                              'asks_first_page': len(asks), 'shipping_fields': by_shipping,
                              'caution': '미확인 배송은 오탐 방지를 위해 알림 제외'}, ensure_ascii=False, indent=2))
            return 0 if len(successful) == len(settings['urls']) else 1
        except Blocked as exc:
            print(f'[PROBE PRODUCT BLOCKED] {pid}: {exc}', file=sys.stderr)
            return 1
        except Exception as exc:
            errors.append(f'{pid}: {type(exc).__name__}: {str(exc)[:100]}')
    print('[PROBE PRODUCT FAILED] 랭킹 수집은 성공했지만 상품/거래/매도호가 API는 확인되지 않았습니다. '
          + '; '.join(errors), file=sys.stderr)
    return 1


def main() -> int:
    parser = argparse.ArgumentParser(description='GitHub Actions ranking-only discount notifier')
    parser.add_argument('--config', type=Path, default=Path('config/monitor.json'))
    parser.add_argument('--state', type=Path, default=Path('state/monitor_state.json'))
    parser.add_argument('--loop', action='store_true')
    parser.add_argument('--git-sync', action='store_true')
    parser.add_argument('--probe', action='store_true')
    parser.add_argument('--dry-run', action='store_true')
    args = parser.parse_args()
    if args.dry_run and args.git_sync:
        parser.error('--dry-run과 --git-sync를 동시에 사용할 수 없습니다')
    config = load_json(args.config)
    validate_config(config)
    if args.probe:
        return probe(config)
    interval = int(os.getenv('CHECK_INTERVAL_SECONDS', '60'))
    duration = int(os.getenv('RUN_DURATION_SECONDS', '21480')) if args.loop else 1
    if interval <= 0 or duration <= 0:
        parser.error('모니터링 간격과 시간은 양수여야 합니다')
    return loop(args.config, args.state, interval=interval, duration=duration,
                git_sync=args.git_sync, dry_run=args.dry_run)


if __name__ == '__main__':
    sys.exit(main())
