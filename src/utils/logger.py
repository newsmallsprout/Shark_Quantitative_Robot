import sys
from loguru import logger
import os

def setup_logger(log_dir: str = "logs", level: str = "INFO"):
    if not os.path.exists(log_dir):
        os.makedirs(log_dir)
        
    logger.remove()  # Remove default handler
    
    # Console handler
    logger.add(
        sys.stderr,
        format="<green>{time:YYYY-MM-DD HH:mm:ss}</green> | <level>{level: <8}</level> | <cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> - <level>{message}</level>",
        level=level
    )
    
    # File handler
    logger.add(
        os.path.join(log_dir, "gate_attack_{time}.log"),
        rotation="500 MB",
        retention="10 days",
        level="DEBUG",
        compression="zip"
    )
    
    return logger

# Global logger instance
log = logger
