#!/usr/bin/env python3
"""Render 배포 전 필수 점검 스크립트."""

from __future__ import annotations

import importlib
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

try:
    from dotenv import load_dotenv

    load_dotenv(ROOT / ".env", override=False)
except Exception:
    pass


def _ok(name: str, value: str) -> str:
    return f"[OK] {name}: {value}"


def _warn(name: str, value: str) -> str:
    return f"[WARN] {name}: {value}"


def _check_env(name: str, required: bool = True) -> str:
    raw = os.environ.get(name, "")
    if raw.strip():
        return _ok(name, "set")
    if required:
        return _warn(name, "missing (required)")
    return _warn(name, "missing (optional)")


def _check_import(module_name: str) -> str:
    try:
        importlib.import_module(module_name)
        return _ok(f"import {module_name}", "available")
    except Exception as exc:  # pragma: no cover
        return _warn(f"import {module_name}", str(exc))


def main() -> int:
    print("== Render Preflight ==")
    print(f"root: {ROOT}")
    print()

    checks = [
        _check_env("SUPABASE_URL"),
        _check_env("SUPABASE_KEY"),
        _check_env("ANTHROPIC_API_KEY", required=False),
        _check_env("CLAUDE_API_KEY", required=False),
        _check_env("PERPLEXITY_API_KEY", required=False),
        _check_env("PBS_FETCH", required=False),
        _check_import("fastapi"),
        _check_import("uvicorn"),
        _check_import("reportlab"),
    ]
    for line in checks:
        print(line)

    render_yaml = ROOT / "render.yaml"
    if render_yaml.is_file():
        print(_ok("render.yaml", "found"))
    else:
        print(_warn("render.yaml", "not found"))

    procfile = ROOT / "Procfile"
    if procfile.is_file():
        print(_ok("Procfile", "found"))
    else:
        print(_warn("Procfile", "not found"))

    print()
    print("Tip: Render health check path is /api/health")
    return 0


if __name__ == "__main__":
    sys.exit(main())
