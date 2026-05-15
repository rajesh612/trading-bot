#!/usr/bin/env python3
"""
Usage: python tools/migrate_imports.py <old_module> <new_dotted_path>

Example:
  python tools/migrate_imports.py telegram_notify notifications.telegram
  python tools/migrate_imports.py macro_regime macro.regime
"""
import re
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent
SKIP = {".venv", "__pycache__", ".git", "docs", "tools"}


def _py_files():
    for p in ROOT.rglob("*.py"):
        if not any(s in p.parts for s in SKIP):
            yield p


def migrate(old: str, new: str) -> None:
    alias = old  # preserve original name as alias so call sites need no change
    changed = []
    for path in _py_files():
        src = path.read_text()
        # from <old> import X  →  from <new> import X  (any indentation)
        out = re.sub(
            rf"(\bfrom\s+){re.escape(old)}(\s+import\b)",
            rf"\g<1>{new}\2",
            src,
        )
        # import <old> as Y  →  import <new> as Y  (preserve explicit alias, any indentation)
        out = re.sub(
            rf"(\bimport\s+){re.escape(old)}(\s+as\s+\w+)",
            rf"\g<1>{new}\2",
            out,
        )
        # import <old>  (end of statement)  →  import <new> as <alias>
        out = re.sub(
            rf"(\bimport\s+){re.escape(old)}(?=\s*(?:#|$))",
            rf"\g<1>{new} as {alias}",
            out,
            flags=re.MULTILINE,
        )
        if out != src:
            path.write_text(out)
            changed.append(str(path.relative_to(ROOT)))

    print(f"Updated {len(changed)} files:")
    for f in sorted(changed):
        print(f"  {f}")


if __name__ == "__main__":
    if len(sys.argv) != 3:
        print(__doc__)
        sys.exit(1)
    migrate(sys.argv[1], sys.argv[2])
