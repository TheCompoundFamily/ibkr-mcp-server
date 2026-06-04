#!/usr/bin/env python3
"""
ibkr_bridge.py — HTTP bridge for The Compound Family backtest page.
Run this alongside IBKR TWS to get real option quotes on the website.

Usage: python3 ~/ibkr-mcp-server/ibkr_bridge.py
"""

import asyncio
import json
import logging
import math
import os
import ssl
import subprocess
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urlparse, parse_qs

from ib_async import IB, Stock, Option, util
from ibkr_mcp_server.config import settings

BRIDGE_PORT = 7499
TCF_API      = 'https://api.thecompoundfamily.com'
TOKEN_FILE   = os.path.expanduser('~/.compound_api_token')
CERT_DIR     = os.path.expanduser('~/.ibkr-certs')
CERT_FILE    = os.path.join(CERT_DIR, 'localhost+1.pem')
KEY_FILE     = os.path.join(CERT_DIR, 'localhost+1-key.pem')
logging.basicConfig(level=logging.WARNING)


def _ensure_https_cert():
    """Generate localhost HTTPS cert via mkcert (trusted by Safari/Chrome/Firefox)."""
    if os.path.exists(CERT_FILE) and os.path.exists(KEY_FILE):
        return True
    os.makedirs(CERT_DIR, exist_ok=True)
    mkcert = subprocess.run(['which', 'mkcert'], capture_output=True, text=True).stdout.strip()
    if not mkcert:
        # Try Homebrew path
        mkcert = '/opt/homebrew/bin/mkcert'
        if not os.path.exists(mkcert):
            return False
    # Install root CA to login keychain (no sudo needed)
    subprocess.run([mkcert, '-install'], capture_output=True)
    caroot = subprocess.run([mkcert, '-CAROOT'], capture_output=True, text=True).stdout.strip()
    ca_cert = os.path.join(caroot, 'rootCA.pem')
    subprocess.run([
        'security', 'add-trusted-cert', '-d', '-r', 'trustRoot',
        '-k', os.path.expanduser('~/Library/Keychains/login.keychain-db'), ca_cert
    ], capture_output=True)
    subprocess.run(
        [mkcert, '-cert-file', CERT_FILE, '-key-file', KEY_FILE, 'localhost', '127.0.0.1'],
        capture_output=True
    )
    return os.path.exists(CERT_FILE)

# ── Persistent token helpers ───────────────────────────────────

def load_token():
    try:
        with open(TOKEN_FILE) as f:
            return f.read().strip()
    except FileNotFoundError:
        return None

def save_token(token: str):
    with open(TOKEN_FILE, 'w') as f:
        f.write(token.strip())
    os.chmod(TOKEN_FILE, 0o600)
    print(f'✓ Token saved to {TOKEN_FILE}')

def post_to_tcf(quotes: list, token: str = None) -> dict:
    """POST quotes to TCF API. Returns response dict or error."""
    token = token or load_token()
    if not token:
        return {'error': 'No API token. Run: curl localhost:7499/save-token?token=<your_token>'}
    body = json.dumps({'quotes': quotes}).encode()
    req = urllib.request.Request(
        f'{TCF_API}/backtest/option-quote',
        data=body,
        headers={
            'Authorization': f'Bearer {token}',
            'Content-Type':  'application/json',
            'User-Agent':    'Mozilla/5.0 TCF-Bridge/1.0',
            'Origin':        'https://thecompoundfamily.com',
        },
        method='POST'
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        return {'error': f'HTTP {e.code}: {e.read().decode()}'}

# ── Global IB connection ───────────────────────────────────────
_ib    = None
_loop  = asyncio.new_event_loop()

def get_ib():
    global _ib
    if _ib is None:
        _ib = IB()
    return _ib

def run_async(coro):
    return _loop.run_until_complete(coro)

async def ensure_connected():
    ib = get_ib()
    if not ib.isConnected():
        try:
            await ib.connectAsync(
                settings.ibkr_host, settings.ibkr_port,
                clientId=15   # +1 to avoid conflict with MCP server
            )
            print(f"✓ Connected to IBKR TWS on {settings.ibkr_host}:{settings.ibkr_port}")
        except Exception as e:
            print(f"✗ IBKR connection failed: {e}")
            return False
    return ib.isConnected()

# ── Option chain helpers ───────────────────────────────────────

async def get_stock_price(symbol: str):
    ib = get_ib()
    contract = Stock(symbol, 'SMART', 'USD')
    await ib.qualifyContractsAsync(contract)
    ticker = ib.reqMktData(contract, '233', False, False)
    await asyncio.sleep(2)
    ib.cancelMktData(contract)
    price = ticker.last or ticker.close or ticker.bid
    return float(price) if price else None

def _valid_price(v):
    """Return float if v is a real IBKR price (not None, nan, -1, or 0)."""
    try:
        f = float(v)
        return f if f > 0 and not math.isnan(f) and not math.isinf(f) else None
    except (TypeError, ValueError):
        return None

async def get_option_quote(symbol: str, strike: float, expiry: str):
    """Get bid/ask for a specific PUT option."""
    ib = get_ib()
    contract = Option(symbol, expiry, strike, 'P', 'SMART')
    details  = await ib.reqContractDetailsAsync(contract)
    if not details:
        return None
    qualified = details[0].contract
    ib.reqMarketDataType(3)  # delayed data — no streaming subscription needed
    ticker = ib.reqMktData(qualified, '', False, False)
    await asyncio.sleep(6)
    bid = _valid_price(ticker.bid)
    ask = _valid_price(ticker.ask)
    if bid is None:  # try frozen delayed
        ib.cancelMktData(qualified)
        ib.reqMarketDataType(4)
        ticker = ib.reqMktData(qualified, '', False, False)
        await asyncio.sleep(5)
    ib.cancelMktData(qualified)
    ib.reqMarketDataType(1)
    bid = _valid_price(ticker.bid)
    ask = _valid_price(ticker.ask)
    iv  = _valid_price(ticker.impliedVolatility)
    return {'bid': bid, 'ask': ask, 'iv_ann': round(iv * 100, 1) if iv else None,
            'mid': round((bid + ask) / 2, 4) if bid and ask else None}

async def get_option_quotes_batch(symbol: str, strikes: list, expiry: str):
    """Batch-fetch bid/ask for multiple PUT options — one wait for all."""
    ib = get_ib()
    # Try live data first; fall back to delayed (free, 15-min delay)
    ib.reqMarketDataType(1)
    contracts = [Option(symbol, expiry, float(s), 'P', 'SMART') for s in strikes]
    await ib.qualifyContractsAsync(*contracts)
    qualified = [c for c in contracts if c.conId]
    if not qualified:
        # Retry with delayed data
        ib.reqMarketDataType(3)
        await ib.qualifyContractsAsync(*contracts)
        qualified = [c for c in contracts if c.conId]
    if not qualified:
        return {}
    # Use delayed data mode (free, no subscription needed for API)
    ib.reqMarketDataType(3)   # 3 = delayed, 4 = delayed frozen (after hours)
    tickers = {}
    for c in qualified:
        t = ib.reqMktData(c, '', False, False)
        tickers[c.strike] = t
    await asyncio.sleep(8)
    # If still no data — try delayed frozen (last known price)
    all_none = all(_valid_price(t.bid) is None for t in tickers.values())
    if all_none:
        for c in qualified:
            ib.cancelMktData(c)
        ib.reqMarketDataType(4)  # delayed frozen
        tickers = {}
        for c in qualified:
            t = ib.reqMktData(c, '', False, False)
            tickers[c.strike] = t
        await asyncio.sleep(8)
    for c in qualified:
        ib.cancelMktData(c)
    ib.reqMarketDataType(1)  # reset to live
    result = {}
    for strike, t in tickers.items():
        bid = _valid_price(t.bid)
        ask = _valid_price(t.ask)
        iv  = _valid_price(t.impliedVolatility)
        result[float(strike)] = {
            'bid': bid, 'ask': ask,
            'iv_ann': round(iv * 100, 1) if iv else None,
            'mid': round((bid + ask) / 2, 4) if bid and ask else None,
        }
    return result

async def get_nearest_friday_expiry(symbol: str):
    """Find nearest available weekly expiry."""
    ib = get_ib()
    contract = Stock(symbol, 'SMART', 'USD')
    await ib.qualifyContractsAsync(contract)
    chains = await ib.reqSecDefOptParamsAsync(
        symbol, '', contract.secType, contract.conId
    )
    if not chains:
        return None
    # Find SMART exchange chain
    smart = next((c for c in chains if c.exchange == 'SMART'), chains[0])
    from datetime import date
    today = date.today()
    future = sorted([e for e in smart.expirations if e >= today.strftime('%Y%m%d')])
    return future[0] if future else None


# ── HTTP handler ───────────────────────────────────────────────

class BridgeHandler(BaseHTTPRequestHandler):
    def log_message(self, *args): pass

    def send_json(self, data, status=200):
        body = json.dumps(data).encode()
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Content-Length', len(body))
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'GET, OPTIONS')
        self.end_headers()

    def do_GET(self):
        parsed = urlparse(self.path)
        params = {k: v[0] for k, v in parse_qs(parsed.query).items()}
        path   = parsed.path

        if path == '/save-token':
            token = params.get('token', '').strip()
            if not token:
                self.send_json({'error': 'Pass ?token=<your_tcf_api_token>'}, 400)
                return
            save_token(token)
            self.send_json({'ok': True, 'message': f'Token saved to {TOKEN_FILE}'})

        elif path == '/health':
            connected = run_async(ensure_connected())
            self.send_json({
                'connected':   connected,
                'status':      'connected' if connected else 'disconnected',
                'host':        settings.ibkr_host,
                'port':        settings.ibkr_port,
                'token_saved': load_token() is not None,
            })

        elif path == '/spread-quote':
            symbol = params.get('symbol', 'SOXL').upper()
            try:
                short_k = float(params['short_strike'])
                long_k  = float(params['long_strike'])
            except (KeyError, ValueError):
                self.send_json({'error': 'short_strike and long_strike required'}, 400)
                return

            connected = run_async(ensure_connected())
            if not connected:
                self.send_json({'error': 'IBKR TWS not connected — open TWS first'})
                return

            expiry = run_async(get_nearest_friday_expiry(symbol))
            if not expiry:
                self.send_json({'error': f'No expiry found for {symbol}'})
                return

            stock_price = run_async(get_stock_price(symbol))
            short_q = run_async(get_option_quote(symbol, short_k, expiry))
            long_q  = run_async(get_option_quote(symbol, long_k,  expiry))

            if not short_q or not long_q:
                self.send_json({'error': 'Could not get option quotes — check symbol and strikes'})
                return

            spread_width = (short_k - long_k) * 100
            net_mid = None
            if short_q['mid'] and long_q['mid']:
                net_mid = round((short_q['mid'] - long_q['mid']) * 100, 2)

            self.send_json({
                'symbol':         symbol,
                'expiry':         expiry,
                'stock_price':    stock_price,
                'short_strike':   short_k,
                'long_strike':    long_k,
                'spread_width':   spread_width,
                'short_bid':      round(short_q['bid'] * 100, 2) if short_q['bid'] else None,
                'short_ask':      round(short_q['ask'] * 100, 2) if short_q['ask'] else None,
                'long_bid':       round(long_q['bid']  * 100, 2) if long_q['bid']  else None,
                'long_ask':       round(long_q['ask']  * 100, 2) if long_q['ask']  else None,
                'net_credit_mid': net_mid,
                'max_loss_mid':   round(spread_width - net_mid, 2) if net_mid else None,
                'short_iv_ann':   short_q['iv_ann'],
                'long_iv_ann':    long_q['iv_ann'],
            })

        elif path == '/qualify-options':
            symbol   = params.get('symbol', 'SOXL').upper()
            expiry   = params.get('expiry', '')
            spread_w = int(params.get('spread_width', 10))
            stock_p  = float(params.get('stock_price', 0))

            connected = run_async(ensure_connected())
            if not connected:
                self.send_json({'error': 'IBKR TWS not connected'})
                return

            if not expiry:
                expiry = run_async(get_nearest_friday_expiry(symbol))
            if not expiry:
                self.send_json({'error': f'No expiry found for {symbol}'})
                return

            thresholds = [3, 5, 7, 8, 10, 12, 14, 15, 17, 20, 23, 25, 28, 30]
            all_strikes = set()
            strike_map = {}
            for t in thresholds:
                s = round(stock_p * (1 - t / 100)) if stock_p else 0
                l = s - spread_w
                strike_map[t] = (s, l)
                all_strikes.update([s, l])

            ib = get_ib()
            contracts = [Option(symbol, expiry, float(s), 'P', 'SMART') for s in all_strikes if s > 0]
            await_result = run_async(ib.qualifyContractsAsync(*contracts))
            result = []
            for c in contracts:
                if c.conId:
                    result.append({'strike': c.strike, 'conId': c.conId, 'localSymbol': c.localSymbol})

            self.send_json({'expiry': expiry, 'contracts': result})

        elif path == '/full-chain':
            symbol    = params.get('symbol', 'SOXL').upper()
            spread_w  = int(params.get('spread_width', 10))

            connected = run_async(ensure_connected())
            if not connected:
                self.send_json({'error': 'IBKR TWS not connected — open TWS first'})
                return

            expiry = run_async(get_nearest_friday_expiry(symbol))
            if not expiry:
                self.send_json({'error': f'No expiry found for {symbol}'})
                return

            # Accept stock_price as param (saves a data subscription request)
            stock_price = None
            if 'stock_price' in params:
                try:
                    stock_price = float(params['stock_price'])
                except ValueError:
                    pass
            if not stock_price:
                stock_price = run_async(get_stock_price(symbol))
            if not stock_price or math.isnan(stock_price):
                self.send_json({'error': 'Could not get stock price — pass ?stock_price=<value>'})
                return

            thresholds = [3, 5, 7, 8, 10, 12, 14, 15, 17, 20, 23, 25, 28, 30]
            strike_map = {}
            all_strikes = set()
            for t in thresholds:
                s = round(stock_price * (1 - t / 100))
                l = s - spread_w
                strike_map[t] = (s, l)
                all_strikes.add(s)
                all_strikes.add(l)

            quotes = run_async(get_option_quotes_batch(symbol, list(all_strikes), expiry))

            results = []
            for t in thresholds:
                s, l = strike_map[t]
                sq = quotes.get(float(s))
                lq = quotes.get(float(l))
                if not sq or not lq:
                    continue
                net_mid = None
                if sq['mid'] and lq['mid']:
                    net_mid = round((sq['mid'] - lq['mid']) * 100, 2)
                if net_mid is None or net_mid <= 0:
                    continue
                results.append({
                    'threshold':      t,
                    'short_strike':   s,
                    'long_strike':    l,
                    # bid/ask per share (not per contract) for API compatibility
                    'short_bid':      sq['bid'],
                    'short_ask':      sq['ask'],
                    'long_bid':       lq['bid'],
                    'long_ask':       lq['ask'],
                    'net_credit_mid': net_mid,
                    'short_iv_ann':   sq['iv_ann'],
                    'long_iv_ann':    lq['iv_ann'],
                })

            # Auto-POST to TCF API if token is saved
            api_result = None
            token = params.get('token') or load_token()
            if results and token:
                api_quotes = [{
                    'symbol':       symbol,
                    'short_strike': r['short_strike'],
                    'long_strike':  r['long_strike'],
                    'net_credit':   r['net_credit_mid'],
                    'stock_price':  stock_price,
                    'short_bid':    r['short_bid'],
                    'short_ask':    r['short_ask'],
                    'long_bid':     r['long_bid'],
                    'long_ask':     r['long_ask'],
                    'short_iv_ann': r['short_iv_ann'],
                    'long_iv_ann':  r['long_iv_ann'],
                    'expiry':       expiry,
                } for r in results]
                api_result = post_to_tcf(api_quotes, token)
                if 'error' not in api_result:
                    print(f'✅ {len(api_quotes)} {symbol} quotes posted to TCF Strike Sweep')
                else:
                    print(f'⚠ TCF POST failed: {api_result}')

            self.send_json({
                'symbol':      symbol,
                'expiry':      expiry,
                'stock_price': stock_price,
                'count':       len(results),
                'quotes':      results,
                'api_posted':  api_result,
            })

        else:
            self.send_json({'error': 'Unknown endpoint'}, 404)


# ── Main ───────────────────────────────────────────────────────

if __name__ == '__main__':
    run_async(ensure_connected())
    server = HTTPServer(('127.0.0.1', BRIDGE_PORT), BridgeHandler)

    # HTTPS required for Safari (blocks http://localhost from HTTPS pages)
    https_ok = _ensure_https_cert()
    if https_ok:
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(CERT_FILE, KEY_FILE)
        server.socket = ctx.wrap_socket(server.socket, server_side=True)
        protocol = 'https'
    else:
        protocol = 'http'

    print(f'✓ IBKR Bridge ready — {protocol}://localhost:{BRIDGE_PORT}\n')
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print('\nBridge stopped.')
        if _ib and _ib.isConnected():
            _ib.disconnect()
