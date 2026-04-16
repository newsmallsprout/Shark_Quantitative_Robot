#!/usr/bin/env python3
"""
生成「商业发行」混淆包：COMMERCIAL_DISTRIBUTION=True + PyArmor 混淆 src/ 与 main.py。

依赖: pip install -r requirements-obfuscate.txt

输出: dist/commercial_obfuscated/
  - 混淆后的 src/、main.py、pyarmor_runtime_*

向客户只分发该目录 + requirements.txt + config 模板 + license/public.pem，
勿分发未混淆源码。

用法:
  python scripts/build_commercial_release.py
  python scripts/build_commercial_release.py -O dist/my_release

PyArmor 试用版对脚本体量有限制，整包混淆可能在超大模块（如 src/core/paper_engine.py）上报
「out of license」。可用 --trial-safe 排除该文件并以明文复制到输出目录（其余仍混淆），或购买
正式版后去掉该选项以全量混淆。
"""

from __future__ import annotations

import argparse
import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STAGING = ROOT / "dist" / "_commercial_staging"
PROFILE_REL = Path("src") / "shark_build_profile.py"

# 体量过大时易触发 PyArmor 试用版「out of license」；--trial-safe 时排除并以明文复制。
# 列表随仓库中大文件变化可再增删；正式版全量混淆时勿使用 --trial-safe。
TRIAL_SAFE_PLAIN_MODULES = (
    "src/core/paper_engine.py",
    "src/strategy/beta_neutral_hf.py",
    "src/strategy/engine.py",
    "src/api/server.py",
    "src/core/config_manager.py",
    "src/exchange/gate_gateway.py",
    "src/strategy/core_strategy.py",
    "src/strategy/tuner.py",
)


def _pyarmor_base() -> list[str]:
    exe = shutil.which("pyarmor")
    if exe:
        return [exe]
    return [sys.executable, "-m", "pyarmor.cli"]


def _check_pyarmor() -> None:
    try:
        import pyarmor  # noqa: F401
    except ImportError:
        sys.stderr.write("请先安装: pip install -r requirements-obfuscate.txt\n")
        raise SystemExit(1) from None
    if shutil.which("pyarmor") is None:
        sys.stderr.write("未找到 pyarmor 可执行文件，请将 pip 的 Scripts/bin 加入 PATH。\n")
        raise SystemExit(1) from None


def _copy_source_tree(staging: Path) -> None:
    if staging.exists():
        shutil.rmtree(staging)
    staging.mkdir(parents=True)
    shutil.copytree(
        ROOT / "src",
        staging / "src",
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc", ".pytest_cache"),
    )
    shutil.copy2(ROOT / "main.py", staging / "main.py")


def _run_pyarmor(cmd: list[str], cwd: str) -> None:
    """Run PyArmor; on failure, print hints for common trial / license errors."""
    try:
        subprocess.run(cmd, cwd=cwd, check=True)
    except subprocess.CalledProcessError:
        sys.stderr.write(
            "\nPyArmor 未成功（常见：试用版脚本条数/体积超限，报 out of license）。\n"
            "可选：\n"
            "  1) 购买 PyArmor 正式版后在本机注册: pyarmor register <license.zip>\n"
            "  2) 使用试用版友好模式（排除超大模块、明文复制）:\n"
            "       python scripts/build_commercial_release.py --trial-safe\n"
            "  3) 先对部分目录做轻量混淆: python scripts/obfuscate_release.py -O dist/obfuscated\n"
            "  说明见 docs/客户交付手册.md「发行方构建混淆包」。\n\n"
        )
        raise


def _patch_commercial_flag(staging: Path) -> None:
    p = staging / PROFILE_REL
    text = p.read_text(encoding="utf-8")
    if re.search(r"^COMMERCIAL_DISTRIBUTION\s*=\s*True\s*$", text, flags=re.MULTILINE):
        return
    new, n = re.subn(
        r"^COMMERCIAL_DISTRIBUTION\s*=\s*False\s*$",
        "COMMERCIAL_DISTRIBUTION = True",
        text,
        count=1,
        flags=re.MULTILINE,
    )
    if n != 1:
        raise RuntimeError("无法在 shark_build_profile.py 中写入 COMMERCIAL_DISTRIBUTION = True")
    p.write_text(new, encoding="utf-8")


def _copy_plain_modules(staging: Path, out: Path, rel_paths: tuple[str, ...]) -> None:
    """将未参与混淆的源码按相对路径复制到输出树（与 -O 布局一致）。"""
    for rel in rel_paths:
        src = staging / rel
        if not src.is_file():
            raise FileNotFoundError(f"staging 中缺少明文模块: {rel}")
        dst = out / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)


def main() -> None:
    ap = argparse.ArgumentParser(description="Build PyArmor obfuscated commercial release tree.")
    ap.add_argument(
        "-O",
        "--output",
        type=Path,
        default=ROOT / "dist" / "commercial_obfuscated",
        help="输出目录",
    )
    ap.add_argument(
        "--trial-safe",
        action="store_true",
        help="排除超大模块并以明文复制（缓解 PyArmor 试用版 out of license），默认排除 paper_engine.py",
    )
    ap.add_argument(
        "--exclude",
        action="append",
        default=[],
        metavar="PATTERN",
        help="传给 pyarmor gen 的 --exclude，可重复；路径相对 staging 根（如 src/core/foo.py）",
    )
    ap.add_argument("--keep-staging", action="store_true", help="保留 dist/_commercial_staging 便于排查")
    args = ap.parse_args()
    out: Path = args.output.resolve()
    _check_pyarmor()

    _copy_source_tree(STAGING)
    _patch_commercial_flag(STAGING)

    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)

    exclude_patterns: list[str] = list(args.exclude)
    plain_copy: list[str] = []
    if args.trial_safe:
        for rel in TRIAL_SAFE_PLAIN_MODULES:
            exclude_patterns.append(rel)
            plain_copy.append(rel)

    # 先整包混淆 src，再混淆入口脚本（输出合并到同一目录）
    cmd_src = [
        *_pyarmor_base(),
        "gen",
        "-O",
        str(out),
        "-r",
        "src",
    ]
    for pat in exclude_patterns:
        cmd_src.extend(["--exclude", pat])
    cmd_main = [
        *_pyarmor_base(),
        "gen",
        "-O",
        str(out),
        "main.py",
    ]
    print("Running:", " ".join(cmd_src))
    _run_pyarmor(cmd_src, cwd=str(STAGING))
    if plain_copy:
        _copy_plain_modules(STAGING, out, tuple(plain_copy))
        print("已以明文复制（未混淆）:", ", ".join(plain_copy))
    print("Running:", " ".join(cmd_main))
    _run_pyarmor(cmd_main, cwd=str(STAGING))

    print()
    print("商业混淆包已生成:", out)
    print("说明:")
    print("  - COMMERCIAL_DISTRIBUTION=True 已写入 staging 并经混淆；发行包内无法再用 SKIP_LICENSE_CHECK 跳过。")
    print("  - 请连同 pyarmor_runtime_*、requirements.txt、config、公钥与客户 license.key 一并交付运行环境。")
    if plain_copy:
        print("  - 部分模块为明文（见上方列表）；正式发行建议购买 PyArmor 后全量混淆或按需调整 --exclude。")
    if not args.keep_staging:
        shutil.rmtree(STAGING, ignore_errors=True)
    else:
        print("  - 临时 staging 保留在:", STAGING)


if __name__ == "__main__":
    main()
