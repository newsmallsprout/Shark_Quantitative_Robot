#!/usr/bin/env python3
"""Shark 2.0 — Entry point."""
import asyncio
import sys
from pathlib import Path
ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
try: from dotenv import load_dotenv; load_dotenv(ROOT / ".env")
except ImportError: pass
from core.engine import main
asyncio.run(main())
