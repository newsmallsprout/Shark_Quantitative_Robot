from fastapi import FastAPI, HTTPException, Body, UploadFile, File
from fastapi.staticfiles import StaticFiles
from src.core.config_manager import config_manager
from src.utils.logger import log
from src.ai.scorer import ai_scorer
from src.ai.regime import regime_classifier
import os
import shutil

app = FastAPI(title="Gate Attack Bot Config Center")

# API Routes
@app.get("/api/config")
async def get_config():
    """Get current configuration"""
    return config_manager.get_config().model_dump()

@app.post("/api/config/exchange")
async def update_exchange(api_key: str = Body(..., embed=True), api_secret: str = Body(..., embed=True)):
    """Update Exchange API Keys"""
    try:
        config_manager.update_exchange_config(api_key, api_secret)
        log.info("Exchange config updated via API")
        return {"status": "success", "message": "Exchange config updated"}
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
    # In a real app, query StateMachine
    return {"status": "running", "state": "OBSERVE"}

@app.get("/api/market_analysis")
async def get_market_analysis():
    """Get Real-time AI Analysis"""
    ticker = {"last": 0, "quoteVolume": 0} 
    symbol = "BTC/USDT"
    
    regime = regime_classifier.analyze(ticker)
    score = ai_scorer.score(symbol, ticker, "buy") 
    
    return {
        "symbol": symbol,
        "regime": regime,
        "ai_score": score,
        "timestamp": 0
    }

@app.get("/api/recent_signals")
async def get_recent_signals():
    """Get Recent Signals Log"""
    # In real app, read from database or memory buffer
    return []

@app.get("/api/account_info")
async def get_account_info():
    """Get Positions and Open Orders"""
    # In a real implementation, this would call exchange.fetch_positions() and exchange.fetch_open_orders()
    return {
        "positions": [],
        "orders": [],
        "balance": 0.0,
        "daily_pnl": 0.0,
        "win_rate": 0.0
    }

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
