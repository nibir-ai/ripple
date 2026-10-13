"""Generate a real two-commit demo. Never overwrites an existing destination."""

from __future__ import annotations

import argparse
import os
import subprocess
from pathlib import Path


def create_demo(
    destination: Path,
    package: bool = False,
    rename: bool = False,
    tests: bool = False,
) -> Path:
    if sum((package, rename, tests)) > 1:
        raise ValueError("Choose only one demo mode.")
    destination = destination.resolve()
    destination.mkdir(parents=True, exist_ok=False)
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update(
        GIT_CONFIG_GLOBAL=os.devnull,
        GIT_CONFIG_NOSYSTEM="1",
        GIT_AUTHOR_NAME="Ripple Demo",
        GIT_AUTHOR_EMAIL="demo@example.invalid",
        GIT_COMMITTER_NAME="Ripple Demo",
        GIT_COMMITTER_EMAIL="demo@example.invalid",
        GIT_AUTHOR_DATE="2026-10-12T00:00:00+0000",
        GIT_COMMITTER_DATE="2026-10-12T00:00:00+0000",
    )

    def git(*args: str) -> None:
        subprocess.run(
            ["git", "-c", "core.hooksPath=" + os.devnull, *args],
            cwd=destination,
            env=env,
            check=True,
            capture_output=True,
            timeout=20,
        )

    git("init", "--quiet")
    if rename:
        source = "def calculate_total(x):\n    return x * 1.1\n"
        caller = "from pricing import calculate_total\n\n"
        caller += "def checkout(x):\n    return calculate_total(x)\n"
        caller += "\ndef endpoint(x):\n    return checkout(x)\n"
        (destination / "pricing.py").write_bytes(source.encode())
        (destination / "api.py").write_bytes(caller.encode())
        git("add", "--all")
        git("commit", "--quiet", "-m", "before file rename")
        (destination / "pricing.py").rename(destination / "totals.py")
        # Leave the old import in place to show before-only callers and head diagnostics.
        git("add", "--all")
        git("commit", "--quiet", "-m", "rename with obsolete import")
        return destination
    if package:
        (destination / "src/shop").mkdir(parents=True)
        sources = {
            "pyproject.toml": '[tool.ripple]\nsource_roots = ["src"]\n',
            "src/shop/__init__.py": "",
            "src/shop/pricing.py": "def calculate_total(x):\n    return x * 1.1\n",
            "src/shop/services.py": "from .pricing import calculate_total as total\n\n"
            "class Checkout:\n    @staticmethod\n    def charge(x):\n        return total(x)\n\n"
            "    @classmethod\n    def receipt(cls, x):\n        return cls.charge(x)\n",
            "src/shop/api.py": "from .services import Checkout\n\n"
            "def endpoint(x):\n    return Checkout.receipt(x)\n",
        }
        for path, contents in sources.items():
            (destination / path).write_bytes(contents.encode())
        git("add", "--all")
        git("commit", "--quiet", "-m", "before")
        (destination / "src/shop/pricing.py").write_bytes(
            sources["src/shop/pricing.py"].replace("1.1", "1.2").encode()
        )
        git("add", "--all")
        git("commit", "--quiet", "-m", "after")
        return destination
    (destination / "service.py").write_text(
        "def calculate_total(x):\n    return x * 1.1\n", encoding="utf-8"
    )
    (destination / "api.py").write_text(
        "from service import calculate_total as total\n\n"
        "def checkout(x):\n    return total(x)\n\n"
        "def endpoint(x):\n    return checkout(x)\n",
        encoding="utf-8",
    )
    if tests:
        (destination / "tests").mkdir()
        (destination / "tests/test_service.py").write_text(
            "from service import calculate_total\nfrom api import endpoint\n\n"
            "def test_total(): return calculate_total(10)\n\n"
            "def test_endpoint(): return endpoint(10)\n",
            encoding="utf-8",
        )
        (destination / "tests/test_import.py").write_text(
            "from service import calculate_total\n\ndef test_unrelated(): pass\n",
            encoding="utf-8",
        )
    git("add", "--all")
    git("commit", "--quiet", "-m", "before")
    (destination / "service.py").write_text(
        "def calculate_total(x):\n    return x * 1.2\n", encoding="utf-8"
    )
    git("add", "--", "service.py")
    git("commit", "--quiet", "-m", "after")
    return destination


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--destination", type=Path, required=True)
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--package", action="store_true", help="Generate src-layout method demo")
    modes.add_argument("--rename", action="store_true", help="Generate rename/obsolete import demo")
    modes.add_argument("--tests", action="store_true", help="Generate static related-test demo")
    args = parser.parse_args()
    root = create_demo(args.destination, args.package, args.rename, args.tests)
    print(f'Demo created. Run: ripple analyze --repo "{root}"')
