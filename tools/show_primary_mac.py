#!/usr/bin/env python3
"""打印本机第一块有线/无线网卡的 MAC（macOS/Linux），用于配置 SHARK_ALLOWED_MAC。"""
import re
import shutil
import subprocess
import sys


def main() -> None:
    if sys.platform == "darwin":
        for cmd in (["ifconfig"], ["networksetup", "-listallhardwareports"]):
            try:
                out = subprocess.check_output(cmd, text=True, stderr=subprocess.DEVNULL)
            except (FileNotFoundError, subprocess.CalledProcessError):
                continue
            m = re.search(r"ether\s+([0-9a-f:]{17})", out, re.I)
            if m:
                print(m.group(1).lower())
                return
    if shutil.which("ip"):
        try:
            out = subprocess.check_output(["ip", "link"], text=True, stderr=subprocess.DEVNULL)
            m = re.search(r"link/ether\s+([0-9a-f:]{17})", out, re.I)
            if m:
                print(m.group(1).lower())
                return
        except subprocess.CalledProcessError:
            pass
    print("未能自动检测。请在终端执行: ifconfig | grep ether", file=sys.stderr)
    sys.exit(1)


if __name__ == "__main__":
    main()
