import abc
import asyncio
import aiohttp
import json
from typing import Dict, Any
from src.utils.logger import log

class BaseLLM(abc.ABC):
    @abc.abstractmethod
    async def analyze(self, prompt: str) -> Dict[str, Any]:
        """Analyze market data and return structured JSON."""
        pass

class DeepSeekLLM(BaseLLM):
    def __init__(self, api_key: str, base_url: str = "", model_name: str = ""):
        self.api_key = api_key
        self.base_url = base_url or "https://api.deepseek.com/v1"
        self.model_name = model_name or "deepseek-chat"

    async def analyze(self, prompt: str) -> Dict[str, Any]:
        headers = {
            "Authorization": f"Bearer {self.api_key}", 
            "Content-Type": "application/json"
        }
        payload = {
            "model": self.model_name,
            "messages": [
                {"role": "system", "content": "You are a quantitative trading AI. Always output valid JSON."},
                {"role": "user", "content": prompt}
            ],
            "response_format": {"type": "json_object"}
        }
        async with aiohttp.ClientSession() as session:
            async with session.post(f"{self.base_url}/chat/completions", json=payload, headers=headers) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    try:
                        choices = data.get("choices") or []
                        content = choices[0]["message"]["content"]
                    except Exception as e:
                        log.error(f"DeepSeek malformed response: {type(e).__name__}: {e!r} | body={data!r}")
                        raise ValueError("Malformed DeepSeek response payload")
                    try:
                        return json.loads(content)
                    except json.JSONDecodeError:
                        log.error(f"DeepSeek returned invalid JSON: {content}")
                        raise ValueError("Invalid JSON from LLM")
                else:
                    text = await resp.text()
                    raise ConnectionError(f"DeepSeek API Error {resp.status}: {text}")

class OpenAILLM(BaseLLM):
    def __init__(self, api_key: str, base_url: str = "", model_name: str = ""):
        self.api_key = api_key
        self.base_url = base_url or "https://api.openai.com/v1"
        self.model_name = model_name or "gpt-4-turbo-preview"

    async def analyze(self, prompt: str) -> Dict[str, Any]:
        headers = {
            "Authorization": f"Bearer {self.api_key}", 
            "Content-Type": "application/json"
        }
        payload = {
            "model": self.model_name,
            "messages": [
                {"role": "system", "content": "You are a quantitative trading AI. Always output valid JSON."},
                {"role": "user", "content": prompt}
            ],
            "response_format": {"type": "json_object"}
        }
        async with aiohttp.ClientSession() as session:
            async with session.post(f"{self.base_url}/chat/completions", json=payload, headers=headers) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    try:
                        choices = data.get("choices") or []
                        content = choices[0]["message"]["content"]
                    except Exception as e:
                        log.error(f"OpenAI malformed response: {type(e).__name__}: {e!r} | body={data!r}")
                        raise ValueError("Malformed OpenAI response payload")
                    try:
                        return json.loads(content)
                    except json.JSONDecodeError:
                        log.error(f"OpenAI returned invalid JSON: {content}")
                        raise ValueError("Invalid JSON from LLM")
                else:
                    text = await resp.text()
                    raise ConnectionError(f"OpenAI API Error {resp.status}: {text}")

class OllamaLLM(BaseLLM):
    """Local LLM using Ollama API"""
    def __init__(self, base_url: str = "", model_name: str = ""):
        self.base_url = base_url or "http://localhost:11434/api"
        self.model_name = model_name or "llama3"

    async def analyze(self, prompt: str) -> Dict[str, Any]:
        payload = {
            "model": self.model_name,
            "prompt": prompt,
            "stream": False,
            "format": "json"
        }
        async with aiohttp.ClientSession() as session:
            async with session.post(f"{self.base_url}/generate", json=payload) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    content = data.get("response", "{}")
                    try:
                        return json.loads(content)
                    except json.JSONDecodeError:
                        log.error(f"Ollama returned invalid JSON: {content}")
                        raise ValueError("Invalid JSON from LLM")
                else:
                    text = await resp.text()
                    raise ConnectionError(f"Ollama API Error {resp.status}: {text}")

class MockLLM(BaseLLM):
    """Fallback mock LLM for testing when no keys are provided"""
    async def analyze(self, prompt: str) -> Dict[str, Any]:
        import random

        await asyncio.sleep(0.05)
        if "Darwin Protocol reflection" in prompt:
            return {
                "reflection": "Mock researcher: insufficient evidence to change parameters.",
                "patches": {},
            }

        if "L1_PARAM_TUNING" in prompt:
            return {
                "halt_trading": False,
                "cvd_burst_mult": None,
                "cvd_stop_mult": None,
                "min_atr_bps": None,
                "position_scale": None,
            }

        if "DARWIN_BATCH_EVOLUTION" in prompt:
            return {
                "schema": "darwin.evolution.v1",
                "reflection": "Mock L3 batch: insufficient pattern; no patch.",
                "batch_stats": {"n": 0, "note": "mock"},
                "patches": {},
            }

        regimes = ["STABLE", "VOLATILE"]
        matrix = ["STABLE", "STABLE", "STABLE", "TRENDING_UP", "TRENDING_DOWN"]
        return {
            "regime": random.choice(regimes),
            "matrix_regime": random.choice(matrix),
            "score": random.uniform(30.0, 90.0),
            "suggested_leverage_cap": random.randint(60, 100),
            "tp_atr_multiplier": round(random.uniform(1.0, 2.0), 2),
            "sl_atr_multiplier": round(random.uniform(1.0, 3.0), 2),
            "reason": "Mock microstructure analysis generated by internal fallback.",
        }

class LLMFactory:
    @staticmethod
    def create_from_darwin_config() -> BaseLLM:
        """从 config/settings.yaml 的 darwin.* 与环境变量解析密钥并创建 LLM。"""
        import os

        from src.core.config_manager import config_manager

        cfg = config_manager.get_config().darwin
        prov = (cfg.llm_provider or "mock").lower()
        key = (getattr(cfg, "llm_api_key", "") or "").strip()
        if not key:
            key = (os.environ.get("DARWIN_LLM_API_KEY") or "").strip()
        if not key and prov == "openai":
            key = (os.environ.get("OPENAI_API_KEY") or "").strip()
        if not key and prov == "deepseek":
            key = (os.environ.get("DEEPSEEK_API_KEY") or "").strip()
        base = (getattr(cfg, "llm_base_url", "") or "").strip()
        model = (getattr(cfg, "llm_model_name", "") or "").strip()
        if prov == "mock" and key:
            log.warning(
                "[LLM] darwin.llm_provider was 'mock' but llm_api_key is set — "
                "using provider 'deepseek'. Set darwin.llm_provider to 'openai' if your key is OpenAI."
            )
            prov = "deepseek"
        log.info(
            f"[LLM] darwin.llm_provider={prov!r} api_key_configured={bool(key)} "
            f"base_url_set={bool(base)} model_set={bool(model)}"
        )
        return LLMFactory.create(prov, key, base, model)

    @staticmethod
    def create(provider: str, api_key: str = "", base_url: str = "", model_name: str = "") -> BaseLLM:
        provider = provider.lower()
        if provider == "deepseek":
            if not api_key:
                log.warning("DeepSeek API key missing. Falling back to MockLLM.")
                return MockLLM()
            return DeepSeekLLM(api_key, base_url, model_name)
        elif provider == "openai":
            if not api_key:
                log.warning("OpenAI API key missing. Falling back to MockLLM.")
                return MockLLM()
            return OpenAILLM(api_key, base_url, model_name)
        elif provider == "ollama":
            return OllamaLLM(base_url, model_name)
        elif provider == "mock":
            return MockLLM()
        else:
            log.warning(f"Unknown LLM provider '{provider}'. Falling back to MockLLM.")
            return MockLLM()
