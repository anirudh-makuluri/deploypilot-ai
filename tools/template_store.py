"""Supabase-backed Dockerfile template store.

Templates are matched against repo signals (stack tokens, monorepo flags, etc.),
filled with concrete variables, and returned as ready-to-use Dockerfiles.
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

from db import supabase

# ─── Default Templates ────────────────────────────────────────────────────────

DEFAULT_TEMPLATES: List[Dict[str, Any]] = [
    {
        "name": "pnpm_monorepo_turbo",
        "description": "pnpm workspace monorepo with Turborepo (Next.js standalone)",
        "match_stack_tokens": ["next", "pnpm"],
        "match_signals": {"is_monorepo": True, "has_turbo": True},
        "priority": 20,
        "variables": {"port": 3000, "node_version": "20", "service_path": "apps/web"},
        "template_content": (
            "FROM node:{{node_version}}-alpine AS base\n"
            "WORKDIR /app\n"
            "RUN addgroup -S app && adduser -S app -G app\n\n"
            "FROM base AS deps\n"
            "RUN apk add --no-cache libc6-compat\n"
            "COPY package.json pnpm-lock.yaml pnpm-workspace.yaml* ./\n"
            "RUN mkdir -p {{service_path}}\n"
            "COPY {{service_path}}/package.json ./{{service_path}}/package.json\n"
            "RUN corepack enable pnpm && pnpm i --frozen-lockfile --filter ./{{service_path}}...\n\n"
            "FROM deps AS builder\n"
            "COPY . .\n"
            "RUN npx --yes turbo run build --filter=./{{service_path}}...\n\n"
            "FROM base AS runner\n"
            "ENV NODE_ENV=production\n"
            "ENV PORT={{port}}\n"
            'ENV HOSTNAME="0.0.0.0"\n'
            "COPY --from=builder /app/{{service_path}}/public ./{{service_path}}/public\n"
            "COPY --from=builder /app/{{service_path}}/.next/standalone ./\n"
            "COPY --from=builder /app/{{service_path}}/.next/static ./{{service_path}}/.next/static\n"
            "USER app\n"
            "EXPOSE {{port}}\n"
            'CMD ["node", "{{service_path}}/server.js"]\n'
        ),
    },
    {
        "name": "pnpm_monorepo_basic",
        "description": "pnpm workspace monorepo without Turborepo",
        "match_stack_tokens": ["pnpm"],
        "match_signals": {"is_monorepo": True},
        "priority": 15,
        "variables": {"port": 3000, "node_version": "20", "service_path": "apps/web"},
        "template_content": (
            "FROM node:{{node_version}}-alpine AS base\n"
            "WORKDIR /app\n"
            "RUN addgroup -S app && adduser -S app -G app\n\n"
            "FROM base AS deps\n"
            "RUN apk add --no-cache libc6-compat\n"
            "COPY package.json pnpm-lock.yaml pnpm-workspace.yaml* ./\n"
            "RUN mkdir -p {{service_path}}\n"
            "COPY {{service_path}}/package.json ./{{service_path}}/package.json\n"
            "RUN corepack enable pnpm && pnpm i --frozen-lockfile --filter ./{{service_path}}...\n\n"
            "FROM deps AS builder\n"
            "COPY . .\n"
            "WORKDIR /app/{{service_path}}\n"
            "RUN pnpm build\n\n"
            "FROM base AS runner\n"
            "ENV NODE_ENV=production\n"
            "ENV PORT={{port}}\n"
            "COPY --from=builder /app /app\n"
            "USER app\n"
            "EXPOSE {{port}}\n"
            'CMD ["pnpm", "start"]\n'
        ),
    },
    {
        "name": "nextjs_standalone",
        "description": "Next.js with standalone output (non-monorepo)",
        "match_stack_tokens": ["next"],
        "match_signals": {"has_standalone": True},
        "priority": 10,
        "variables": {"port": 3000, "node_version": "20"},
        "template_content": (
            "FROM node:{{node_version}}-alpine AS base\n"
            "WORKDIR /app\n"
            "RUN addgroup -S app && adduser -S app -G app\n\n"
            "FROM base AS deps\n"
            "RUN apk add --no-cache libc6-compat\n"
            "COPY package*.json pnpm-lock.yaml* yarn.lock* ./\n"
            "RUN corepack enable pnpm 2>/dev/null; \\\n"
            "    if [ -f pnpm-lock.yaml ]; then pnpm i --frozen-lockfile; \\\n"
            "    elif [ -f yarn.lock ]; then yarn install --frozen-lockfile; \\\n"
            "    else npm ci; fi\n\n"
            "FROM deps AS builder\n"
            "COPY . .\n"
            "RUN npm run build\n\n"
            "FROM base AS runner\n"
            "ENV NODE_ENV=production\n"
            "ENV PORT={{port}}\n"
            'ENV HOSTNAME="0.0.0.0"\n'
            "COPY --from=builder /app/public ./public\n"
            "COPY --from=builder /app/.next/standalone ./\n"
            "COPY --from=builder /app/.next/static ./.next/static\n"
            "USER app\n"
            "EXPOSE {{port}}\n"
            'CMD ["node", "server.js"]\n'
        ),
    },
    {
        "name": "node_express",
        "description": "Plain Node.js / Express app",
        "match_stack_tokens": ["node"],
        "match_signals": {},
        "priority": 5,
        "variables": {"port": 3000, "node_version": "20"},
        "template_content": (
            "FROM node:{{node_version}}-alpine AS base\n"
            "WORKDIR /app\n"
            "RUN addgroup -S app && adduser -S app -G app\n\n"
            "FROM base AS deps\n"
            "COPY package*.json pnpm-lock.yaml* yarn.lock* ./\n"
            "RUN corepack enable pnpm 2>/dev/null; \\\n"
            "    if [ -f pnpm-lock.yaml ]; then pnpm i --frozen-lockfile; \\\n"
            "    elif [ -f yarn.lock ]; then yarn install --frozen-lockfile; \\\n"
            "    else npm ci; fi\n\n"
            "FROM deps AS build\n"
            "COPY . .\n"
            "RUN if grep -q '\"build\"' package.json 2>/dev/null; then npm run build; fi\n\n"
            "FROM base AS runner\n"
            "COPY --from=build /app /app\n"
            "USER app\n"
            "EXPOSE {{port}}\n"
            'CMD ["npm", "start"]\n'
        ),
    },
    {
        "name": "vite_static",
        "description": "Vite SPA with nginx static serving",
        "match_stack_tokens": ["vite"],
        "match_signals": {},
        "priority": 8,
        "variables": {"port": 80, "node_version": "20"},
        "template_content": (
            "FROM node:{{node_version}}-alpine AS builder\n"
            "WORKDIR /app\n"
            "COPY package*.json pnpm-lock.yaml* yarn.lock* ./\n"
            "RUN corepack enable pnpm 2>/dev/null; \\\n"
            "    if [ -f pnpm-lock.yaml ]; then pnpm i --frozen-lockfile; \\\n"
            "    elif [ -f yarn.lock ]; then yarn install --frozen-lockfile; \\\n"
            "    else npm ci; fi\n"
            "COPY . .\n"
            "RUN npm run build\n\n"
            "FROM nginx:alpine AS runner\n"
            "COPY --from=builder /app/dist /usr/share/nginx/html\n"
            "EXPOSE {{port}}\n"
            'CMD ["nginx", "-g", "daemon off;"]\n'
        ),
    },
    {
        "name": "python_uvicorn",
        "description": "FastAPI / Flask with uvicorn",
        "match_stack_tokens": ["python", "fastapi"],
        "match_signals": {},
        "priority": 8,
        "variables": {"port": 8000, "python_version": "3.11"},
        "template_content": (
            "FROM python:{{python_version}}-slim\n\n"
            "WORKDIR /app\n"
            "ENV PYTHONDONTWRITEBYTECODE=1\n"
            "ENV PYTHONUNBUFFERED=1\n\n"
            "COPY requirements*.txt ./\n"
            "RUN pip install --no-cache-dir -r requirements.txt\n\n"
            "COPY . .\n"
            "RUN useradd -m appuser\n"
            "USER appuser\n"
            "EXPOSE {{port}}\n"
            'CMD ["python", "-m", "uvicorn", "main:app", "--host", "0.0.0.0", "--port", "{{port}}"]\n'
        ),
    },
    {
        "name": "python_django",
        "description": "Django with gunicorn",
        "match_stack_tokens": ["python", "django"],
        "match_signals": {},
        "priority": 9,
        "variables": {"port": 8000, "python_version": "3.11", "wsgi_module": "config.wsgi"},
        "template_content": (
            "FROM python:{{python_version}}-slim\n\n"
            "WORKDIR /app\n"
            "ENV PYTHONDONTWRITEBYTECODE=1\n"
            "ENV PYTHONUNBUFFERED=1\n\n"
            "COPY requirements*.txt ./\n"
            "RUN pip install --no-cache-dir -r requirements.txt\n\n"
            "COPY . .\n"
            "RUN python manage.py collectstatic --noinput 2>/dev/null || true\n"
            "RUN useradd -m appuser\n"
            "USER appuser\n"
            "EXPOSE {{port}}\n"
            'CMD ["gunicorn", "{{wsgi_module}}:application", "--bind", "0.0.0.0:{{port}}", "--workers", "3"]\n'
        ),
    },
    {
        "name": "go_binary",
        "description": "Go binary with scratch runner",
        "match_stack_tokens": ["go"],
        "match_signals": {},
        "priority": 8,
        "variables": {"port": 8080, "go_version": "1.22"},
        "template_content": (
            "FROM golang:{{go_version}}-alpine AS builder\n"
            "WORKDIR /app\n"
            "COPY go.mod go.sum* ./\n"
            "RUN go mod download\n"
            "COPY . .\n"
            "RUN CGO_ENABLED=0 GOOS=linux go build -o /app/server .\n\n"
            "FROM scratch\n"
            "COPY --from=builder /app/server /server\n"
            "EXPOSE {{port}}\n"
            'ENTRYPOINT ["/server"]\n'
        ),
    },
]


# ─── Template Variable Filling ────────────────────────────────────────────────

def fill_template(template_content: str, variables: Dict[str, Any]) -> str:
    """Replace {{placeholder}} tokens in a template with actual values."""
    result = template_content
    for key, value in variables.items():
        result = result.replace("{{" + key + "}}", str(value))
    # Warn about unfilled placeholders (but don't crash)
    unfilled = re.findall(r"\{\{(\w+)\}\}", result)
    if unfilled:
        print(f"[template_store] Warning: unfilled placeholders: {unfilled}")
    return result


# ─── Template Matching ─────────────────────────────────────────────────────────

def _coerce_bool(value: Any) -> bool:
    """Coerce a value to bool, handling JSON string representations."""
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.lower() in ("true", "1", "yes")
    return bool(value)


def _score_template(
    template: Dict[str, Any],
    stack_tokens: List[str],
    signals: Dict[str, Any],
) -> float:
    """Score a template against the current repo's stack tokens and signals.
    
    Returns a float score; higher is better. Returns -1 if the template
    is disqualified (required signal mismatch).
    """
    name = template.get("name", "?")
    match_tokens = set(template.get("match_stack_tokens") or [])
    match_signals = template.get("match_signals") or {}
    
    # All match_signals must be satisfied (hard requirement)
    for signal_key, signal_value in match_signals.items():
        actual = signals.get(signal_key)
        # Coerce both sides to bool for resilient comparison
        if isinstance(signal_value, bool):
            if _coerce_bool(actual) != signal_value:
                print(f"  [score] {name}: DISQUALIFIED — signal '{signal_key}' expected={signal_value}, got={actual}")
                return -1.0
        elif actual != signal_value:
            print(f"  [score] {name}: DISQUALIFIED — signal '{signal_key}' expected={signal_value}, got={actual}")
            return -1.0
    
    # Score based on token overlap
    token_set = {t.lower() for t in stack_tokens}
    overlap = len(match_tokens.intersection(token_set))
    if not overlap and match_tokens:
        missing = match_tokens - token_set
        print(f"  [score] {name}: DISQUALIFIED — missing required tokens: {missing}")
        return -1.0
    
    token_score = overlap / max(len(match_tokens), 1)
    priority = float(template.get("priority", 0))
    signal_bonus = len(match_signals) * 0.1
    
    total = token_score + (priority * 0.01) + signal_bonus
    print(f"  [score] {name}: {total:.3f} (token_overlap={overlap}/{len(match_tokens)}, priority={priority}, signal_bonus={signal_bonus:.1f})")
    return total


def match_template(
    stack_tokens: List[str],
    signals: Dict[str, Any],
) -> Optional[Dict[str, Any]]:
    """Find the best matching template from Supabase for the given repo signals.
    
    Returns the template row dict if a match is found, None otherwise.
    """
    if not supabase:
        return _match_template_local(stack_tokens, signals)
    
    try:
        response = (
            supabase.table("dockerfile_templates")
            .select("*")
            .eq("is_active", True)
            .order("priority", desc=True)
            .execute()
        )
        templates = response.data or []
    except Exception as e:
        print(f"[template_store] Supabase query failed, falling back to local: {e}")
        return _match_template_local(stack_tokens, signals)
    
    if not templates:
        return _match_template_local(stack_tokens, signals)
    
    return _pick_best(templates, stack_tokens, signals)


def _match_template_local(
    stack_tokens: List[str],
    signals: Dict[str, Any],
) -> Optional[Dict[str, Any]]:
    """Fallback: match against the hardcoded DEFAULT_TEMPLATES."""
    return _pick_best(DEFAULT_TEMPLATES, stack_tokens, signals)


def _pick_best(
    templates: List[Dict[str, Any]],
    stack_tokens: List[str],
    signals: Dict[str, Any],
) -> Optional[Dict[str, Any]]:
    """Score all templates and return the best match, or None."""
    print(f"[template_store] Scoring {len(templates)} templates...")
    best_score = -1.0
    best_template = None
    
    for tpl in templates:
        score = _score_template(tpl, stack_tokens, signals)
        if score > best_score:
            best_score = score
            best_template = tpl
    
    if best_template:
        print(f"[template_store] Winner: {best_template.get('name')} (score={best_score:.3f})")
    else:
        print(f"[template_store] No template matched")
    return best_template if best_score > 0 else None


# ─── CRUD Helpers ──────────────────────────────────────────────────────────────

def seed_default_templates() -> Dict[str, int]:
    """Insert or update the built-in default templates into Supabase.
    
    Returns {"inserted": N, "updated": N, "skipped": N}.
    """
    if not supabase:
        return {"inserted": 0, "updated": 0, "skipped": 0, "error": "Supabase not configured"}
    
    inserted = 0
    updated = 0
    skipped = 0
    
    for tpl in DEFAULT_TEMPLATES:
        row = {
            "name": tpl["name"],
            "description": tpl["description"],
            "match_stack_tokens": tpl["match_stack_tokens"],
            "match_signals": tpl.get("match_signals", {}),
            "priority": tpl.get("priority", 0),
            "template_content": tpl["template_content"],
            "variables": tpl.get("variables", {}),
            "is_active": True,
        }
        
        try:
            existing = (
                supabase.table("dockerfile_templates")
                .select("id")
                .eq("name", tpl["name"])
                .limit(1)
                .execute()
            )
            if existing.data:
                supabase.table("dockerfile_templates").update(row).eq("name", tpl["name"]).execute()
                updated += 1
            else:
                supabase.table("dockerfile_templates").insert(row).execute()
                inserted += 1
        except Exception as e:
            print(f"[template_store] Failed to seed '{tpl['name']}': {e}")
            skipped += 1
    
    return {"inserted": inserted, "updated": updated, "skipped": skipped}


def list_templates(active_only: bool = True) -> List[Dict[str, Any]]:
    """List all templates from Supabase."""
    if not supabase:
        return DEFAULT_TEMPLATES if active_only else DEFAULT_TEMPLATES
    
    try:
        query = supabase.table("dockerfile_templates").select("*")
        if active_only:
            query = query.eq("is_active", True)
        response = query.order("priority", desc=True).execute()
        return response.data or []
    except Exception:
        return []


def upsert_template(template: Dict[str, Any]) -> Dict[str, Any]:
    """Insert or update a single template by name."""
    if not supabase:
        raise RuntimeError("Supabase not configured")
    
    name = template.get("name")
    if not name:
        raise ValueError("Template must have a 'name' field")
    
    row = {
        "name": name,
        "description": template.get("description", ""),
        "match_stack_tokens": template.get("match_stack_tokens", []),
        "match_signals": template.get("match_signals", {}),
        "priority": template.get("priority", 0),
        "template_content": template.get("template_content", ""),
        "variables": template.get("variables", {}),
        "is_active": template.get("is_active", True),
    }
    
    existing = (
        supabase.table("dockerfile_templates")
        .select("id")
        .eq("name", name)
        .limit(1)
        .execute()
    )
    
    if existing.data:
        supabase.table("dockerfile_templates").update(row).eq("name", name).execute()
        return {"action": "updated", "name": name}
    else:
        supabase.table("dockerfile_templates").insert(row).execute()
        return {"action": "inserted", "name": name}


def delete_template(name: str) -> bool:
    """Soft-delete a template by name. Returns True if found and deactivated."""
    if not supabase:
        raise RuntimeError("Supabase not configured")
    
    existing = (
        supabase.table("dockerfile_templates")
        .select("id")
        .eq("name", name)
        .limit(1)
        .execute()
    )
    
    if not existing.data:
        return False
    
    supabase.table("dockerfile_templates").update({"is_active": False}).eq("name", name).execute()
    return True
