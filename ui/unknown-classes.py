"""Which classes appear in the rendered HTML but NOT in the built stylesheet?

A class split across two Python string literals is never seen by Tailwind's text
extractor, and the failure is SILENT: the markup looks right, the rule does not exist.
It is only visible by comparing the two.
"""
import re, sys, pathlib
html = pathlib.Path(sys.argv[1]).read_text()
css = pathlib.Path(sys.argv[2]).read_text()
used = set()
for m in re.finditer(r"""class=(?:'([^']*)'|"([^"]*)"|([^\s>'"]+))""", html):
    used.update((m.group(1) or m.group(2) or m.group(3) or "").split())
# Classes the board defines itself in @layer components, not Tailwind utilities.
own = {"meter", "track", "k", "v", "tasks", "trail", "mark", "pj", "day", "qgrid", "pane",
       "spine", "spine-stop", "spine-land", "over", "warn"}
# NO prefix list, and no daisyUI names here. `startswith(("badge","btn","input","card"))`
# skipped EVERYTHING beginning with them, and therefore also `input-bordered` — a daisyUI
# 4 class that does not exist in daisyUI 5, and that sat dead in board.py while this very
# check reported "all green". The tool meant to catch silently dead classes was itself
# silent. The first attempt at a fix added those four names as EXACT exceptions instead —
# the same anti-pattern in miniature: a day when daisyUI stops giving `.btn` a rule of its
# own would pass silently again. The substring check below finds them on its own (`.badge{`,
# `.btn{`, `.input{`, `.card{` appear literally in board.css), so the only right answer is
# to have no exceptions for them at all.
missing = []
for c in sorted(used):
    if c in own:
        continue
    if re.search(r"(?<![\w-])\." + re.escape(c).replace("\\:", "\\\\:") + r"(?![\w-])",
                 css.replace("\\:", "\\:")):
        continue
    # Tailwind escapes colon, dot, comma, parens, brackets and slash in the selector
    esc = "".join("\\" + ch if ch in ":.,()[]/%#&*" else ch for ch in c)
    if ("." + esc) in css:
        continue
    missing.append(c)
print("\n".join(missing) if missing else "(none)")
sys.exit(1 if missing else 0)
