"""
商业强校验开关（仓库默认即商业行为）。

- COMMERCIAL_DISTRIBUTION=True：忽略 SKIP_LICENSE_CHECK，须有效 license.key + 公钥。
- 单测在 tests/conftest.py 首行将本模块打回 False，仅 pytest 进程内生效。

向客户交付混淆包时由 scripts/build_commercial_release.py 再经 PyArmor 混淆本文件，
勿单独分发「仅改回 False」的明文源码。
"""

COMMERCIAL_DISTRIBUTION = True
