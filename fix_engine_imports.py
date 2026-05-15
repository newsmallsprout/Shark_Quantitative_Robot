with open("core/engine.py", "r") as f:
    lines = f.readlines()

new_lines = []
skip = False
for i, line in enumerate(lines):
    # Remove AI_ENABLED dummy
    if "from ai_strategy import" in line:
        pass # we actually don't have ai_strategy.py anymore, it was deleted in the git restore? Wait. I should check.
