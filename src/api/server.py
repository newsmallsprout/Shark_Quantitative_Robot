from fastapi import FastAPI, HTTPException, Body, UploadFile, File
from fastapi.staticfiles import StaticFiles
from src.core.config_manager import config_manager
from src.utils.logger import log
from src.ai.scorer import ai_scorer
from src.ai.regime import regime_classifier
from src.core.globals import bot_context
import os
import shutil

app = FastAPI(title="Gate Attack Bot Config Center")

# API Routes
@app.get("/api/config")
async def get_config():
    """Get current configuration"""
    return config_manager.get_config().model_dump()

@app.post("/api/config/exchange")
async def update_exchange(
    api_key: str = Body(..., embed=True), 
    api_secret: str = Body(..., embed=True),
    sandbox_mode: bool = Body(False, embed=True)
):
    """Update Exchange API Keys"""
    try:
        config_manager.update_exchange_config(api_key, api_secret, sandbox_mode)
        
        # Trigger hot reload of exchange connection
        exchange = bot_context.get_exchange()
        if exchange:
            await exchange.reload_config()
            
        log.info("Exchange config updated via API")
        return {"status": "success", "message": "Exchange config updated and connection reloaded"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/config/risk")
async def update_risk(risk_settings: dict):
    """Update Risk Parameters"""
    try:
        config_manager.update_risk_config(**risk_settings)
        log.info("Risk config updated via API")
        return {"status": "success", "message": "Risk config updated"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/config/strategy")
async def update_strategy(strategy_settings: dict):
    """Update Strategy Parameters"""
    try:
        config_manager.update_strategy_config(**strategy_settings)
        log.info("Strategy config updated via API")
        return {"status": "success", "message": "Strategy config updated"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/license/upload")
async def upload_license(file: UploadFile = File(...)):
    """Upload new license file"""
    try:
        # Ensure directory exists
        os.makedirs("license", exist_ok=True)
        file_path = "license/license.key"
        
        with open(file_path, "wb") as buffer:
            shutil.copyfileobj(file.file, buffer)
            
        log.info("New license uploaded via API")
        # Here you might want to trigger a reload/re-validation
        return {"status": "success", "message": "License uploaded successfully. Please restart bot."}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/status")
async def get_status():
    """Get Bot Status"""
    sm = bot_context.get_state_machine()
    engine = bot_context.get_strategy_engine()
    
    if not sm or not engine:
        return {"status": "stopped", "state": "UNKNOWN"}
        
    return {
        "status": "running" if engine.is_running else "stopped",
        "state": sm.state
    }

@app.post("/api/control")
async def control_bot(command: dict = Body(...)):
    """Start/Stop Bot"""
    engine = bot_context.get_strategy_engine()
    if not engine:
         raise HTTPException(status_code=503, detail="Bot not initialized")
         
    action = command.get("action")
    if action == "START":
        # In a real async loop this is tricky because engine.start() is a loop.
        # Usually we just toggle a 'pause' flag or similar.
        # For this architecture, we assume the loop is always running but can be paused/resumed
        # OR we just update the StateMachine to allow trading.
        engine.resume() # Assuming we implement resume/pause
        return {"status": "success", "message": "Bot Resumed"}
    elif action == "STOP":
        engine.pause()
        return {"status": "success", "message": "Bot Paused"}
    
    return {"status": "error", "message": "Invalid action"}

@app.get("/api/market_analysis")
async def get_market_analysis():
    """Get Real-time AI Analysis"""
    exchange = bot_context.get_exchange()
    if not exchange:
         return {"symbol": "-", "regime": "OFFLINE", "ai_score": 0, "timestamp": 0}

    # Fetch real ticker from the primary symbol
    symbol = config_manager.get_config().strategy.symbols[0] # Get first symbol
    ticker = await exchange.fetch_ticker(symbol)
    
    if not ticker:
         return {"symbol": symbol, "regime": "NO_DATA", "ai_score": 0, "timestamp": 0}

    regime = regime_classifier.analyze(ticker)
    score = ai_scorer.score(symbol, ticker, "buy") 
    
    # Format score to 2 decimal places
    formatted_score = round(score, 2)
    
    return {
        "symbol": symbol,
        "regime": regime,
        "ai_score": formatted_score,
        "timestamp": ticker.get('timestamp', 0)
    }

@app.get("/api/market_quotes")
async def get_market_quotes():
    """Get Real-time Market Quotes for Top 20 Pairs"""
    exchange = bot_context.get_exchange()
    if not exchange:
        return []

    # Top 20 Crypto Pairs (Futures usually)
    symbols = [
        "BTC/USDT", "ETH/USDT", "SOL/USDT", "BNB/USDT", "XRP/USDT",
        "ADA/USDT", "DOGE/USDT", "AVAX/USDT", "DOT/USDT", "LINK/USDT",
        "LTC/USDT", "TRX/USDT", "BCH/USDT", "UNI/USDT",
        "ATOM/USDT", "XLM/USDT", "ETC/USDT", "FIL/USDT", "NEAR/USDT"
    ]
    
    try:
        tickers = await exchange.fetch_tickers(symbols)
        # Convert dict to list for frontend
        quote_list = []
        for symbol, ticker in tickers.items():
            quote_list.append({
                "symbol": symbol,
                "price": ticker['last'],
                "change_pct": ticker['percentage'],
                "volume": ticker['quoteVolume']
            })
        
        # Sort by volume desc
        quote_list.sort(key=lambda x: x['volume'], reverse=True)
        return quote_list
    except Exception as e:
        log.error(f"Error fetching quotes: {e}")
        return []

@app.get("/api/recent_signals")
async def get_recent_signals():
    """Get Recent Signals Log"""
    # In real app, read from database or memory buffer
    return []

@app.get("/api/account_info")
async def get_account_info():
    """Get Positions and Open Orders"""
    exchange = bot_context.get_exchange()
    if not exchange:
        return {"positions": [], "orders": [], "balance": 0.0, "daily_pnl": 0.0, "win_rate": 0.0}
        
    try:
        # Fetch Real Data
        balance_data = await exchange.fetch_balance()
        # CCXT balance structure: {'total': {'USDT': 1000}, 'free': ...}
        total_balance = balance_data.get('total', {}).get('USDT', 0.0)
        
        # We need to implement fetch_positions in UnifiedExchange or access execution_exchange directly
        # For now, let's assume UnifiedExchange exposes it or we access the internal execution_exchange
        positions = []
        try:
            raw_pos = await exchange.fetch_positions()
            # Normalize positions
            for p in raw_pos:
                if float(p['size']) != 0:
                    positions.append({
                        "symbol": p['symbol'],
                        "side": p['side'],
                        "size": float(p['size']),
                        "entry_price": float(p['entryPrice']),
                        "unrealized_pnl": float(p['unrealizedPnl']),
                        "pnl_percent": round((float(p['unrealizedPnl']) / (float(p['entryPrice']) * float(p['size']) / float(p['leverage']))) * 100, 2) if float(p['size']) else 0
                    })
        except Exception as e:
            log.error(f"Error processing positions: {e}")

        # Orders
        orders = []
        try:
            raw_orders = await exchange.fetch_open_orders()
            for o in raw_orders:
                orders.append({
                    "symbol": o['symbol'],
                    "side": o['side'],
                    "type": o['type'],
                    "price": o['price'],
                    "amount": o['amount'],
                    "status": o['status']
                })
        except Exception as e:
             log.error(f"Error processing orders: {e}")

        return {
            "positions": positions,
            "orders": orders,
            "balance": total_balance,
            "daily_pnl": 0.0, # Need to track this separately in database or logic
            "win_rate": 0.0   # Need history for this
        }
    except Exception as e:
        log.error(f"Error in account_info: {e}")
        return {"positions": [], "orders": [], "balance": 0.0, "daily_pnl": 0.0, "win_rate": 0.0}

@app.get("/api/logs")
async def get_logs():
    """Get System Logs"""
    # Use data_collector csv for strategy execution logs
    # or read the structured logs from loguru
    # Here we focus on 'bot operation logs' which might be the main log file
    
    try:
        log_dir = "logs"
        if not os.path.exists(log_dir):
            return {"logs": []}
            
        files = [os.path.join(log_dir, f) for f in os.listdir(log_dir) if f.endswith(".log")]
        if not files:
             return {"logs": []}
             
        latest_file = max(files, key=os.path.getctime)
        with open(latest_file, "r") as f:
            lines = f.readlines()
            # Filter for meaningful operation logs if needed
            return {"logs": lines[-50:]} 
    except Exception as e:
        return {"logs": [f"Error reading logs: {e}"]}


# Serve Static Files (Frontend)
if os.path.exists("src/web"):
    app.mount("/", StaticFiles(directory="src/web", html=True), name="static")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
