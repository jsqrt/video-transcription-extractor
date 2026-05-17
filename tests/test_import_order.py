"""Regression guard: every entry point must import network_isolation
FIRST, before any other module. The whole offline guarantee in TERMS.md
depends on the socket monkey-patch being installed before a third-party
library has a chance to phone home.

If this test fails, do NOT just reorder until it passes — figure out
what new import landed above the guard and verify it doesn't open a
socket at import time.
"""

from __future__ import annotations

import ast
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

ENTRY_POINTS = [
    REPO_ROOT / "app" / "__main__.py",
    REPO_ROOT / "app" / "gui" / "__main__.py",
    REPO_ROOT / "mcp_server" / "__main__.py",
]


class ImportOrderTest(unittest.TestCase):
    def test_each_entry_point_imports_isolation_first(self) -> None:
        for entry in ENTRY_POINTS:
            with self.subTest(entry=str(entry)):
                self.assertTrue(
                    entry.exists(),
                    msg=f"Entry point missing: {entry}",
                )
                tree = ast.parse(entry.read_text(encoding="utf-8"))

                first_import = None
                first_call = None
                for node in tree.body:
                    # ``from __future__ import ...`` is a compile-time
                    # directive, not a real import — it doesn't run code
                    # and cannot open sockets. Skip it.
                    if (
                        isinstance(node, ast.ImportFrom)
                        and node.module == "__future__"
                    ):
                        continue
                    if isinstance(node, (ast.Import, ast.ImportFrom)):
                        if first_import is None:
                            first_import = node
                    elif isinstance(node, ast.Expr) and isinstance(
                        node.value, ast.Call
                    ):
                        if first_call is None:
                            first_call = node.value
                    if first_import is not None and first_call is not None:
                        break

                self.assertIsNotNone(
                    first_import,
                    msg=f"{entry} has no imports",
                )
                module_name = (
                    first_import.module
                    if isinstance(first_import, ast.ImportFrom)
                    else first_import.names[0].name
                )
                self.assertIn(
                    "network_isolation",
                    module_name or "",
                    msg=(
                        f"{entry}: first import must be from "
                        "app.security.network_isolation; got "
                        f"{module_name!r}"
                    ),
                )

                # The first executable statement after the imports must
                # actually CALL enforce_offline_mode(). An import alone
                # is not enough.
                self.assertIsNotNone(
                    first_call,
                    msg=(
                        f"{entry}: imported network_isolation but never "
                        "called enforce_offline_mode()"
                    ),
                )
                call_name = getattr(first_call.func, "id", None) or getattr(
                    first_call.func, "attr", None
                )
                self.assertEqual(
                    call_name,
                    "enforce_offline_mode",
                    msg=(
                        f"{entry}: expected enforce_offline_mode() to be "
                        f"the first call, got {call_name!r}"
                    ),
                )


if __name__ == "__main__":
    unittest.main()
