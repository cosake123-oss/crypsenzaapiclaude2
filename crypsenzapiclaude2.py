import asyncio
import threading
import time
import os
import base64
import itertools
from datetime import datetime
import aiohttp
from flask import Flask, jsonify, render_template_string

import base58
from solders.keypair import Keypair
from solders.pubkey import Pubkey
from solders.transaction import VersionedTransaction
from solana.rpc.async_api import AsyncClient
from solana.rpc.models import TxOpts
from solana.rpc.commitment import Confirmed

# ============================================================
# CONFIGURAZIONE PRINCIPALE
# ============================================================
# False = Modalita Demo (Paper Trading) | True = Soldi Veri! Di default resta sempre in Demo:
# per attivare il trading reale su Render, imposta la variabile d'ambiente REAL_TRADING=true.
REAL_TRADING = os.environ.get("REAL_TRADING", "false").strip().lower() == "true"

# --- MULTI API KEY (inserisci tutte le tue chiavi) ---
HELIUS_API_KEYS = [k.strip() for k in os.environ.get("HELIUS_API_KEYS", "").split(",") if k.strip()]

BIRDEYE_API_KEYS = [k.strip() for k in os.environ.get("BIRDEYE_API_KEYS", "").split(",") if k.strip()]

SOLANA_PRIVATE_KEY = os.environ.get("SOLANA_PRIVATE_KEY", "")

# Fallback pubblici
PUBLIC_RPC = "https://api.mainnet-beta.solana.com"
DEXSCREENER_API = "https://api.dexscreener.com/latest/dex/tokens/{}"
RUGCHECK_API = "https://api.rugcheck.xyz/v1/tokens/{}/report"
RUGCHECK_NEW_TOKENS_API = "https://api.rugcheck.xyz/v1/stats/new_tokens"

JUPITER_QUOTE_API = "https://quote-api.jup.ag/v6/quote"
JUPITER_SWAP_API = "https://quote-api.jup.ag/v6/swap"
JUPITER_PRICE_API = "https://api.jup.ag/price/v2"
BIRDEYE_PRICE_API = "https://public-api.birdeye.so/defi/price"
BIRDEYE_NEW_LISTING_API = "https://public-api.birdeye.so/defi/v2/tokens/new_listing"

SOL_MINT = "So11111111111111111111111111111111111111112"
SOL_DECIMALS = 9
USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
USDC_DECIMALS = 6

BASE_CURRENCY = "SOL"
if BASE_CURRENCY == "SOL":
    INPUT_MINT = SOL_MINT
    INPUT_DECIMALS = SOL_DECIMALS
else:
    INPUT_MINT = USDC_MINT
    INPUT_DECIMALS = USDC_DECIMALS

SLIPPAGE_BPS = 300
PRIORITY_FEE_LAMPORTS = 200000

SIMULATE_REAL_COSTS = True
SIMULATED_SLIPPAGE_PCT = 0.015
SIMULATED_PLATFORM_FEE_PCT = 0.01
SIMULATED_NETWORK_FEE_USD = 0.05
RUG_DETECTION_DROP_PCT = 0.50

TRADE_AMOUNT_USD = 15.0
STOP_LOSS_PCT = 0.15
TAKE_PROFIT_PCT = 0.30
MAX_TOP_HOLDERS_PCT = 25.0
MIN_LP_LOCKED_PCT = 60.0  # % minima di liquidity pool bloccata/burnata richiesta
MAX_OPEN_POSITIONS = 8
MONITOR_INTERVAL = 4

# ============================================================
# MULTI-KEY ROTATION
# ============================================================
_helius_cycle = itertools.cycle(HELIUS_API_KEYS) if HELIUS_API_KEYS else None
_birdeye_cycle = itertools.cycle(BIRDEYE_API_KEYS) if BIRDEYE_API_KEYS else None

def get_next_helius_key():
    return next(_helius_cycle) if _helius_cycle else None

def get_next_birdeye_key():
    return next(_birdeye_cycle) if _birdeye_cycle else None

def get_helius_rpc_url():
    key = get_next_helius_key()
    if key:
        return f"https://mainnet.helius-rpc.com/?api-key={key}"
    return PUBLIC_RPC

# ============================================================
# CACHE & HELPERS
# ============================================================
_token_decimals_cache = {}
_sol_price_cache = {"price": None, "ts": 0}

def get_keypair() -> Keypair:
    if not SOLANA_PRIVATE_KEY:
        raise ValueError("SOLANA_PRIVATE_KEY non impostata.")
    secret_bytes = base58.b58decode(SOLANA_PRIVATE_KEY)
    return Keypair.from_bytes(secret_bytes)

async def get_sol_price_usd(session):
    now = time.time()
    if _sol_price_cache["price"] and now - _sol_price_cache["ts"] < 20:
        return _sol_price_cache["price"]
    try:
        async with session.get(JUPITER_PRICE_API, params={"ids": SOL_MINT}) as response:
            if response.status == 200:
                data = await response.json()
                price = float(data["data"][SOL_MINT]["price"])
                _sol_price_cache["price"] = price
                _sol_price_cache["ts"] = now
                return price
    except Exception as e:
        print(f"⚠️ Errore prezzo SOL: {e}")
    return _sol_price_cache["price"]

# ============================================================
# BOT DATA + DASHBOARD
# ============================================================
bot_data = {
    "mode": "DEMO (Paper Trading)" if not REAL_TRADING else "REALE ⚠️",
    "balance_usd": 100.0,
    "profitto_perdita": 0.0,
    "open_positions": {},
    "history": []
}

app = Flask(__name__)

HTML_TEMPLATE = """
<!DOCTYPE html>
<html lang="it">
<head>
    <meta charset="UTF-8">
    <title>MetaTrader Style Live Dashboard</title>
    <style>
        body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif; background-color: #000000; color: #ffffff; margin: 0; padding: 15px; max-width: 480px; }
        .header-stats { display: flex; justify-content: space-between; font-size: 14px; color: #aaaaaa; border-bottom: 1px solid #222222; padding-bottom: 10px; margin-bottom: 10px; }
        .header-stats span { color: #ffffff; font-weight: bold; }
        .mode-tag { color: #f59e0b; }
        .trade-row { display: flex; justify-content: space-between; align-items: flex-start; padding: 10px 0; border-bottom: 1px solid #1a1a1a; font-size: 15px; }
        .trade-left { display: flex; flex-direction: column; }
        .trade-right { display: flex; flex-direction: column; text-align: right; }
        .symbol-line { font-weight: bold; margin-bottom: 3px; }
        .action-buy { color: #3b82f6; font-weight: normal; }
        .action-sell { color: #ef4444; font-weight: normal; }
        .wallet-sub { color: #06b6d4; font-size: 11px; font-family: monospace; margin-top: 2px; word-break: break-all; }
        .price-path { color: #888888; font-size: 13px; font-family: monospace; }
        .time { color: #888888; font-size: 12px; margin-bottom: 3px; }
        .pnl { font-size: 15px; font-weight: bold; font-family: monospace; }
        .profit { color: #3b82f6; }
        .loss { color: #ef4444; }
        .empty-state { color: #555555; text-align: center; padding: 30px 0; font-size: 14px; }
    </style>
</head>
<body>
    <div class="header-stats">
        <div>Mod: <span class="mode-tag" id="mode">...</span></div>
        <div>Saldo: $<span id="balance">0.00</span></div>
        <div>P&L: $<span id="total-pnl">0.00</span></div>
    </div>
    <div id="history-container">
        <div class="empty-state">In attesa di operazioni di mercato...</div>
    </div>
    <script>
        function updateDashboard() {
            fetch('/api/data')
                .then(r => r.json())
                .then(data => {
                    document.getElementById('mode').innerText = data.mode;
                    document.getElementById('balance').innerText = data.balance_usd.toFixed(2);
                    const pnlElem = document.getElementById('total-pnl');
                    const totPnl = data.profitto_perdita;
                    pnlElem.innerText = (totPnl >= 0 ? '+' : '') + totPnl.toFixed(2);
                    pnlElem.className = totPnl >= 0 ? 'profit' : 'loss';
                    const container = document.getElementById('history-container');
                    if (data.history.length > 0) {
                        container.innerHTML = '';
                        data.history.slice().reverse().forEach(trade => {
                            const pnlClass = trade.pnl >= 0 ? 'profit' : 'loss';
                            const actionClass = trade.action.toLowerCase() === 'buy' ? 'action-buy' : 'action-sell';
                            container.innerHTML += `
                                <div class="trade-row">
                                    <div class="trade-left">
                                        <div class="symbol-line">${trade.symbol}, <span class="${actionClass}">${trade.action} ${trade.size}</span></div>
                                        <div class="wallet-sub">Token: ${trade.wallet}</div>
                                        <div class="price-path">${trade.entry_price} &rarr; ${trade.exit_price}</div>
                                    </div>
                                    <div class="trade-right">
                                        <div class="time">${trade.time}</div>
                                        <div class="pnl ${pnlClass}">${trade.pnl.toFixed(2)}</div>
                                    </div>
                                </div>`;
                        });
                    }
                });
        }
        setInterval(updateDashboard, 1000);
        updateDashboard();
    </script>
</body>
</html>
"""

@app.route('/')
def index():
    return render_template_string(HTML_TEMPLATE)

@app.route('/api/data')
def api_data():
    return jsonify(bot_data)

def start_web_server():
    import logging
    log = logging.getLogger('werkzeug')
    log.setLevel(logging.ERROR)
    port = int(os.environ.get("PORT", 8079))  # Render assegna la porta tramite $PORT
    app.run(host='0.0.0.0', port=port, debug=False, use_reloader=False)

# ============================================================
# API FUNCTIONS
# ============================================================
async def fetch_latest_discovered_tokens(session):
    tokens_to_test = []

    # 1. RugCheck new_tokens (fonte primaria, più prudente: stessa usata da file 2/3)
    try:
        async with session.get(RUGCHECK_NEW_TOKENS_API) as response:
            if response.status == 200:
                data = await response.json()
                for item in data:
                    mint = item.get("mint") or item.get("token")
                    symbol = item.get("symbol", "MEME")
                    if mint:
                        tokens_to_test.append({"symbol": symbol, "address": mint})
    except Exception as e:
        print(f"⚠️ RugCheck new tokens fallito: {e}")

    # 2. Fallback Birdeye new_listing (token appena nati, più rischiosi: usato solo se RugCheck non risponde)
    if not tokens_to_test:
        key = get_next_birdeye_key()
        if key:
            headers = {"X-API-KEY": key, "x-chain": "solana", "accept": "application/json"}
            try:
                async with session.get(
                    BIRDEYE_NEW_LISTING_API,
                    params={"limit": 20, "meme_platform_enabled": "true"},
                    headers=headers,
                    timeout=aiohttp.ClientTimeout(total=8)
                ) as response:
                    if response.status == 200:
                        data = await response.json()
                        items = data.get("data", {}).get("items", []) or data.get("items", [])
                        for item in items:
                            mint = item.get("address") or item.get("mint")
                            symbol = item.get("symbol") or item.get("name") or "MEME"
                            if mint:
                                tokens_to_test.append({"symbol": symbol, "address": mint})
            except Exception as e:
                print(f"⚠️ Birdeye new listing fallito: {e}")

    return tokens_to_test

async def check_token_safety(session, token_symbol, token_address):
    print(f"\n🛡️ Analisi [{token_symbol}] - {token_address}")
    try:
        async with session.get(RUGCHECK_API.format(token_address)) as response:
            if response.status == 200:
                data = await response.json()

                mint_auth = data.get("tokenMeta", {}).get("mintAuthority")
                if mint_auth is not None:
                    print(f"❌ Scartato: Mint Authority attiva")
                    return False

                top_holders = data.get("topHolders", [])
                top_percentage = sum([h.get("pct", 0) for h in top_holders[:10]])
                if top_percentage > MAX_TOP_HOLDERS_PCT:
                    print(f"❌ Scartato: Top 10 troppo concentrati ({top_percentage:.1f}%)")
                    return False

                lp_info = data.get("lpLocked", {}) or data.get("markets", [{}])[0].get("lp", {}) if data.get("markets") else data.get("lpLocked", {})
                lp_locked_pct = lp_info.get("lpLockedPct", 0) if isinstance(lp_info, dict) else 0
                lp_is_locked = lp_info.get("lpLocked", False) if isinstance(lp_info, dict) else False
                if not lp_is_locked or lp_locked_pct < MIN_LP_LOCKED_PCT:
                    print(f"❌ Scartato: liquidity pool non bloccata a sufficienza ({lp_locked_pct:.1f}%)")
                    return False

                risks = data.get("risks", [])
                for r in risks:
                    if r.get("danger") is True:
                        print(f"❌ Scartato: {r.get('description')}")
                        return False

                print(f"✅ Token OK [{token_symbol}]")
                return True
            else:
                print(f"⚠️ RugCheck status {response.status}")
    except Exception as e:
        print(f"⚠️ Errore sicurezza: {e}")
    return False

async def get_market_price(session, token_address):
    # 1. Birdeye
    key = get_next_birdeye_key()
    if key:
        headers = {"X-API-KEY": key, "x-chain": "solana", "accept": "application/json"}
        try:
            async with session.get(
                BIRDEYE_PRICE_API,
                params={"address": token_address},
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=6)
            ) as response:
                if response.status == 200:
                    data = await response.json()
                    price = data.get("data", {}).get("value")
                    if price is not None:
                        return float(price)
        except Exception:
            pass

    # 2. Fallback DexScreener
    for _ in range(3):
        try:
            async with session.get(DEXSCREENER_API.format(token_address)) as response:
                if response.status == 200:
                    data = await response.json()
                    pairs = data.get("pairs")
                    if pairs and len(pairs) > 0:
                        price_usd = pairs[0].get("priceUsd")
                        if price_usd:
                            return float(price_usd)
        except Exception:
            pass
        await asyncio.sleep(1)
    return 0.0

async def jupiter_get_quote(session, input_mint, output_mint, amount_raw, slippage_bps=SLIPPAGE_BPS):
    params = {
        "inputMint": input_mint,
        "outputMint": output_mint,
        "amount": str(int(amount_raw)),
        "slippageBps": str(slippage_bps),
    }
    try:
        async with session.get(JUPITER_QUOTE_API, params=params) as response:
            if response.status == 200:
                return await response.json()
            print(f"⚠️ Jupiter quote error {response.status}")
    except Exception as e:
        print(f"⚠️ Errore quote Jupiter: {e}")
    return None

async def jupiter_get_swap_tx(session, quote_response, user_pubkey):
    payload = {
        "quoteResponse": quote_response,
        "userPublicKey": str(user_pubkey),
        "wrapAndUnwrapSol": True,
        "prioritizationFeeLamports": PRIORITY_FEE_LAMPORTS,
        "dynamicComputeUnitLimit": True,
    }
    try:
        async with session.post(JUPITER_SWAP_API, json=payload) as response:
            if response.status == 200:
                data = await response.json()
                return data.get("swapTransaction")
            print(f"⚠️ Jupiter swap error {response.status}")
    except Exception as e:
        print(f"⚠️ Errore swap Jupiter: {e}")
    return None

async def sign_and_send_transaction(rpc_client, keypair, swap_tx_b64):
    raw_tx = base64.b64decode(swap_tx_b64)
    unsigned_tx = VersionedTransaction.from_bytes(raw_tx)
    signed_tx = VersionedTransaction(unsigned_tx.message, [keypair])
    result = await rpc_client.send_raw_transaction(
        bytes(signed_tx),
        opts=TxOpts(skip_preflight=True, max_retries=3, preflight_commitment=Confirmed),
    )
    signature = result.value
    await rpc_client.confirm_transaction(signature, commitment=Confirmed)
    return str(signature)

async def get_token_decimals(rpc_client, mint_address):
    if mint_address in _token_decimals_cache:
        return _token_decimals_cache[mint_address]
    resp = await rpc_client.get_token_supply(Pubkey.from_string(mint_address))
    decimals = resp.value.decimals
    _token_decimals_cache[mint_address] = decimals
    return decimals

# ============================================================
# BUY / SELL
# ============================================================
async def execute_buy_order(session, rpc_client, keypair, token_symbol, token_address, entry_price):
    if not REAL_TRADING:
        if SIMULATE_REAL_COSTS:
            entry_price_effective = entry_price * (1 + SIMULATED_SLIPPAGE_PCT)
            effective_usd = TRADE_AMOUNT_USD * (1 - SIMULATED_PLATFORM_FEE_PCT) - SIMULATED_NETWORK_FEE_USD
            tokens_bought = effective_usd / entry_price_effective
        else:
            tokens_bought = TRADE_AMOUNT_USD / entry_price

        bot_data["balance_usd"] -= TRADE_AMOUNT_USD
        bot_data["open_positions"][token_symbol] = {
            "entry_price": entry_price,
            "amount": tokens_bought,
            "address": token_address,
            "invested_usd": TRADE_AMOUNT_USD,
        }
        print(f"🟢 [DEMO] {token_symbol} @ ${entry_price:.6f}")
        return

    if BASE_CURRENCY == "SOL":
        sol_price = await get_sol_price_usd(session)
        if not sol_price:
            print(f"⚠️ Prezzo SOL non disponibile, skip {token_symbol}")
            return
        amount_input_raw = int((TRADE_AMOUNT_USD / sol_price) * (10 ** INPUT_DECIMALS))
    else:
        amount_input_raw = int(TRADE_AMOUNT_USD * (10 ** INPUT_DECIMALS))

    quote = await jupiter_get_quote(session, INPUT_MINT, token_address, amount_input_raw)
    if not quote:
        print(f"⚠️ Nessuna quote per {token_symbol}")
        return

    swap_tx_b64 = await jupiter_get_swap_tx(session, quote, keypair.pubkey())
    if not swap_tx_b64:
        print(f"⚠️ Impossibile costruire swap per {token_symbol}")
        return

    try:
        signature = await sign_and_send_transaction(rpc_client, keypair, swap_tx_b64)
    except Exception as e:
        print(f"❌ Errore BUY {token_symbol}: {e}")
        return

    decimals = await get_token_decimals(rpc_client, token_address)
    tokens_bought = int(quote["outAmount"]) / (10 ** decimals)

    bot_data["open_positions"][token_symbol] = {
        "entry_price": entry_price,
        "amount": tokens_bought,
        "address": token_address,
        "invested_usd": TRADE_AMOUNT_USD,
    }
    print(f"🟢 [REALE] {token_symbol} | Tx: {signature}")

async def execute_sell_order(session, rpc_client, keypair, token_symbol, pos, current_price):
    if not REAL_TRADING:
        invested = pos.get("invested_usd", TRADE_AMOUNT_USD)
        en_price = pos["entry_price"]
        pnl_pct_raw = (current_price - en_price) / en_price

        if SIMULATE_REAL_COSTS and pnl_pct_raw <= -RUG_DETECTION_DROP_PCT:
            sell_value = 0.0
            pnl_usd = -invested - SIMULATED_NETWORK_FEE_USD
            print(f"💀 [RUG/ILLIQUIDITÀ] {token_symbol}")
        elif SIMULATE_REAL_COSTS:
            exit_price_effective = current_price * (1 - SIMULATED_SLIPPAGE_PCT)
            gross_sell_value = pos["amount"] * exit_price_effective
            sell_value = gross_sell_value * (1 - SIMULATED_PLATFORM_FEE_PCT) - SIMULATED_NETWORK_FEE_USD
            pnl_usd = sell_value - invested
        else:
            sell_value = pos["amount"] * current_price
            pnl_usd = sell_value - invested

        bot_data["balance_usd"] += sell_value
        bot_data["profitto_perdita"] += pnl_usd
    else:
        decimals = await get_token_decimals(rpc_client, pos["address"])
        amount_token_raw = int(pos["amount"] * (10 ** decimals))
        quote = await jupiter_get_quote(session, pos["address"], INPUT_MINT, amount_token_raw)
        if not quote:
            print(f"⚠️ Nessuna quote vendita {token_symbol}")
            return
        swap_tx_b64 = await jupiter_get_swap_tx(session, quote, keypair.pubkey())
        if not swap_tx_b64:
            print(f"⚠️ Impossibile swap vendita {token_symbol}")
            return
        try:
            signature = await sign_and_send_transaction(rpc_client, keypair, swap_tx_b64)
        except Exception as e:
            print(f"❌ Errore SELL {token_symbol}: {e}")
            return

        received_raw = int(quote["outAmount"]) / (10 ** INPUT_DECIMALS)
        if BASE_CURRENCY == "SOL":
            sol_price = await get_sol_price_usd(session)
            sell_value = received_raw * sol_price if sol_price else received_raw * current_price
        else:
            sell_value = received_raw
        pnl_usd = sell_value - pos.get("invested_usd", TRADE_AMOUNT_USD)
        bot_data["profitto_perdita"] += pnl_usd
        print(f"🔴 [REALE CHIUSO] {token_symbol} | Tx: {signature}")

    trade_record = {
        "symbol": token_symbol,
        "wallet": pos["address"],
        "action": "buy",
        "size": f"{pos['amount']:.2f}",
        "entry_price": f"{pos['entry_price']:.6f}",
        "exit_price": f"{current_price:.6f}",
        "pnl": pnl_usd,
        "time": datetime.now().strftime("%Y.%m.%d %H:%M:%S")
    }
    bot_data["history"].append(trade_record)
    del bot_data["open_positions"][token_symbol]
    if not REAL_TRADING:
        print(f"🔴 [DEMO CHIUSO] {token_symbol} | P&L: ${pnl_usd:.2f}")

# ============================================================
# MAIN LOOP
# ============================================================
async def trading_bot_loop():
    print("🚀 Bot avviato (multi-posizione max 5)")
    print(f"💰 Modalità: {'REALE ⚠️' if REAL_TRADING else 'DEMO (Paper Trading)'}")
    print(f"🔑 Helius keys: {len(HELIUS_API_KEYS)} | Birdeye keys: {len(BIRDEYE_API_KEYS)}")

    keypair = None
    rpc_client = None
    if REAL_TRADING:
        keypair = get_keypair()
        rpc_url = get_helius_rpc_url()
        rpc_client = AsyncClient(rpc_url)
        print(f"🔑 Wallet: {keypair.pubkey()}")
        print(f"🔗 RPC: {rpc_url[:60]}...")

    async with aiohttp.ClientSession() as session:
        while True:
            # Monitor posizioni aperte
            if bot_data["open_positions"]:
                for token_symbol, pos in list(bot_data["open_positions"].items()):
                    current_price = await get_market_price(session, pos["address"])
                    if current_price == 0:
                        continue
                    en_price = pos["entry_price"]
                    pnl_pct = (current_price - en_price) / en_price
                    print(f"📊 [MONITOR] {token_symbol} | ${current_price:.6f} | {pnl_pct*100:+.2f}%")
                    if pnl_pct <= -STOP_LOSS_PCT or pnl_pct >= TAKE_PROFIT_PCT:
                        await execute_sell_order(session, rpc_client, keypair, token_symbol, pos, current_price)

            # Cerca nuovi token
            if len(bot_data["open_positions"]) < MAX_OPEN_POSITIONS:
                print("\n🔄 Ricerca nuove memecoin...")
                fresh_tokens = await fetch_latest_discovered_tokens(session)

                if fresh_tokens:
                    for target in fresh_tokens:
                        if len(bot_data["open_positions"]) >= MAX_OPEN_POSITIONS:
                            break
                        if not REAL_TRADING and bot_data["balance_usd"] < TRADE_AMOUNT_USD:
                            print(f"🔒 Saldo insufficiente (${bot_data['balance_usd']:.2f}), stop acquisti.")
                            break
                        token_symbol = target["symbol"]
                        token_address = target["address"]
                        if token_symbol in bot_data["open_positions"]:
                            continue

                        is_safe = await check_token_safety(session, token_symbol, token_address)
                        if not is_safe:
                            await asyncio.sleep(0.2)
                            continue

                        entry_price = await get_market_price(session, token_address)
                        if entry_price == 0:
                            print(f"⚠️ Prezzo non trovato per {token_symbol}")
                            continue

                        await execute_buy_order(session, rpc_client, keypair, token_symbol, token_address, entry_price)
                else:
                    print("⏳ Nessun nuovo token trovato.")
            else:
                print(f"🔒 Limite {MAX_OPEN_POSITIONS} posizioni raggiunto.")

            await asyncio.sleep(MONITOR_INTERVAL)

    if rpc_client:
        await rpc_client.close()

if __name__ == '__main__':
    threading.Thread(target=start_web_server, daemon=True).start()
    print("🌐 Dashboard: http://0.0.0.0:8079  (o IP_di_Kali:8080)")

    try:
        asyncio.run(trading_bot_loop())
    except KeyboardInterrupt:
        print("\nBot arrestato.")
    except ValueError as e:
        print(f"\n❌ Configurazione mancante: {e}")
