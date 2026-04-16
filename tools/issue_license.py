#!/usr/bin/env python3
"""
签发 license.key（需 license/private.pem，勿提交私钥）。

用法（在项目根目录）:
  python tools/issue_license.py <64位hex机器指纹> [--user ID] [--days N] [--type trial] [-o 输出路径]

示例:
  python tools/issue_license.py 7961b9dd4dd85aab59a76380d55362b24d22df8571a1886de9609cc086a4d840 \\
      --user customer_001 --days 30 -o dist/customer_001.license.key
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

_gen_path = os.path.join(ROOT, "tools", "generate_license.py")
_spec = importlib.util.spec_from_file_location("shark_generate_license", _gen_path)
assert _spec and _spec.loader
_gen_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_gen_mod)
generate_license = _gen_mod.generate_license

from src.license_manager.models import LicenseType  # noqa: E402


def _parse_license_type(s: str) -> LicenseType:
    m = {
        "trial": LicenseType.TRIAL,
        "monthly": LicenseType.MONTHLY,
        "quarterly": LicenseType.QUARTERLY,
        "half_year": LicenseType.HALF_YEAR,
        "yearly": LicenseType.YEARLY,
        "lifetime": LicenseType.LIFETIME,
    }
    k = (s or "").strip().lower().replace("-", "_")
    if k not in m:
        raise SystemExit(f"未知 license 类型: {s}，可选: {', '.join(m)}")
    return m[k]


def main() -> None:
    ap = argparse.ArgumentParser(description="用私钥为客户生成 license.key（对方先给你机器指纹 hex）")
    ap.add_argument("fingerprint_hex", help="对方发来的 64 位十六进制机器指纹（无 0x 前缀）")
    ap.add_argument("--user", "-u", default="customer", help="客户标识（写入 license）")
    ap.add_argument("--days", "-d", type=int, default=30, help="有效天数")
    ap.add_argument(
        "--type",
        "-t",
        default="trial",
        help="trial|monthly|quarterly|half_year|yearly|lifetime",
    )
    ap.add_argument(
        "-o",
        "--output",
        default="",
        help="输出文件路径（默认 license/license.key）",
    )
    ap.add_argument(
        "--private-key",
        default=os.path.join(ROOT, "license", "private.pem"),
        help="私钥路径",
    )
    args = ap.parse_args()

    fp = "".join(args.fingerprint_hex.split()).lower()
    if len(fp) != 64:
        raise SystemExit(f"指纹须为 64 位 hex，当前长度 {len(fp)}")
    if any(c not in "0123456789abcdef" for c in fp):
        raise SystemExit("指纹含非十六进制字符")

    pk = os.path.abspath(args.private_key)
    if not os.path.isfile(pk):
        raise SystemExit(f"找不到私钥: {pk}（请先 python tools/generate_keys.py）")

    lt = _parse_license_type(args.type)
    out = args.output.strip() or os.path.join(ROOT, "license", "license.key")
    out = os.path.abspath(out)

    lic = generate_license(pk, user_id=args.user, license_type=lt, duration_days=args.days, fingerprint=fp)

    os.makedirs(os.path.dirname(out) or ".", exist_ok=True)
    with open(out, "w", encoding="utf-8") as f:
        f.write(json.dumps(lic.model_dump(), indent=2, ensure_ascii=False))

    print(f"已写入: {out}")
    print("请连同 license/public.pem 一并交给对方，勿泄露 private.pem。")


if __name__ == "__main__":
    main()
