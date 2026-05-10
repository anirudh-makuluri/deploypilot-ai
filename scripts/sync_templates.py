#!/usr/bin/env python3
"""Sync default Dockerfile templates into Supabase.

This upserts templates by name:
- existing template names are updated
- missing template names are inserted
"""

from pathlib import Path
import sys

# Ensure repo root is importable when running as `python3 scripts/sync_templates.py`.
REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tools.template_store import seed_default_templates


if __name__ == "__main__":
    result = seed_default_templates()
    print(result)
