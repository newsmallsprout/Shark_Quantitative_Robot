with open("strategy/runner.py", "r") as f:
    code = f.read()

if "from strategy.dual import get_config" not in code:
    code = code.replace("import os", "import os\nfrom strategy.dual import get_config")

with open("strategy/runner.py", "w") as f:
    f.write(code)
