#!/bin/sh
set -e
echo "[Precheck] Running Python syntax checks..."
python3 -m py_compile $(find . -name "*.py" -not -path "./.venv/*" -not -path "./rl/vendor/*")
echo "[Precheck] Syntax check passed!"
