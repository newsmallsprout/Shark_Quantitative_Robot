import unittest

from src.core import symbol_mapper
from src.core.config_manager import config_manager


class TestSymbolMapper(unittest.TestCase):
    def test_bn_to_gate_default(self):
        self.assertEqual(symbol_mapper.bn_usdm_to_gate("DOGEUSDT"), "DOGE/USDT")
        self.assertEqual(symbol_mapper.bn_usdm_to_gate("PEPEUSDT"), "PEPE/USDT")
        self.assertEqual(symbol_mapper.bn_usdm_to_gate(""), "")
        self.assertEqual(symbol_mapper.bn_usdm_to_gate("BTCUSD_PERP"), "")

    def test_gate_to_bn_default(self):
        self.assertEqual(symbol_mapper.gate_to_bn_usdm("DOGE/USDT"), "DOGEUSDT")
        self.assertEqual(symbol_mapper.gate_to_bn_usdm("doge/usdt"), "DOGEUSDT")

    def test_overrides(self):
        cfg = config_manager.config
        ll = cfg.binance_leadlag
        prev = ll.symbol_overrides
        try:
            config_manager.config = cfg.model_copy(
                update={
                    "binance_leadlag": ll.model_copy(
                        update={"symbol_overrides": {"MEME": "MEME2/USDT"}}
                    )
                }
            )
            self.assertEqual(symbol_mapper.bn_usdm_to_gate("MEMEUSDT"), "MEME2/USDT")
            self.assertEqual(symbol_mapper.gate_to_bn_usdm("MEME2/USDT"), "MEMEUSDT")
        finally:
            config_manager.config = cfg.model_copy(
                update={
                    "binance_leadlag": ll.model_copy(update={"symbol_overrides": prev})
                }
            )


if __name__ == "__main__":
    unittest.main()
