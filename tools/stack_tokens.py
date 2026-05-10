"""Central stack token registry used by scanning, inference, and benchmarking.

Keep token definitions here so all pipeline stages use one source of truth.
"""

from __future__ import annotations

from typing import Dict, Iterable, List, Set


# Canonical token aliases used to normalize labels and extraction signals.
TOKEN_ALIASES: Dict[str, str] = {
    "nextjs": "next",
    "golang": "go",
}


# Package hints used while extracting stack tokens from repositories.
NODE_PACKAGE_TOKENS: Dict[str, str] = {
    "next": "next",
    "@next/react": "next",
    "nuxt": "nuxt",
    "react": "react",
    "vue": "vue",
    "svelte": "svelte",
    "angular": "angular",
    "remix": "remix",
    "astro": "astro",
    "gatsby": "gatsby",
    "vite": "vite",
    "@remix-run/react": "remix",
    "@remix-run/node": "remix",
    "nest": "nestjs",
    "@nestjs/core": "nestjs",
    "fastify": "fastify",
    "hapi": "hapi",
    "koa": "koa",
}

PYTHON_PACKAGE_TOKENS: Dict[str, str] = {
    "fastapi": "fastapi",
    "flask": "flask",
    "django": "django",
    "uvicorn": "uvicorn",
    "gunicorn": "gunicorn",
    "streamlit": "streamlit",
    "gradio": "gradio",
}


# Port defaults and precedence for framework-driven inference.
FRAMEWORK_DEFAULTS: Dict[str, int] = {
    "astro": 4321,
    "bun": 3000,
    "streamlit": 8501,
    "gradio": 7860,
    "vite": 5173,
    "next": 3000,
    "nuxt": 3000,
    "remix": 3000,
    "react": 3000,
    "vue": 3000,
    "svelte": 3000,
    "angular": 4200,
    "nestjs": 3000,
    "fastify": 3000,
    "koa": 3000,
    "hapi": 3000,
    "flask": 5000,
    "django": 8000,
    "fastapi": 8000,
    "uvicorn": 8000,
    "gunicorn": 8000,
    "rails": 3000,
    "spring": 8080,
    "gatsby": 8000,
    "phoenix": 4000,
    "dotnet": 80,
    "aspnetcore": 80,
    "go": 8080,
    "rust": 8080,
    "java": 8080,
}

PORT_INFERENCE_PRIORITY: List[str] = [
    "streamlit",
    "gradio",
    "vite",
    "astro",
    "uvicorn",
    "fastapi",
    "gunicorn",
    "django",
    "flask",
    "phoenix",
    "next",
    "nuxt",
    "remix",
    "bun",
    "nestjs",
    "fastify",
    "koa",
    "hapi",
    "react",
    "vue",
    "svelte",
    "angular",
    "gatsby",
    "spring",
    "go",
    "java",
    "dotnet",
    "aspnetcore",
]


# Canonical ordering for required_stack_tokens label generation.
LABEL_REQUIRED_TOKEN_ORDER: List[str] = [
    "next",
    "node",
    "python",
    "fastapi",
    "go",
    "nginx",
]


KNOWN_STACK_TOKENS: Set[str] = {
    "angular",
    "aspnetcore",
    "astro",
    "bun",
    "django",
    "dotnet",
    "fastapi",
    "fastify",
    "flask",
    "gatsby",
    "go",
    "gradio",
    "gunicorn",
    "hapi",
    "java",
    "koa",
    "nestjs",
    "next",
    "nginx",
    "node",
    "nuxt",
    "php",
    "python",
    "react",
    "remix",
    "ruby",
    "rust",
    "socket.io",
    "streamlit",
    "svelte",
    "uvicorn",
    "vite",
    "vue",
}

STACK_TOKEN_DISPLAY_NAMES: Dict[str, str] = {
    "aspnetcore": "ASP.NET Core",
    "astro": "Astro",
    "bun": "Bun",
    "django": "Django",
    "dotnet": ".NET",
    "fastapi": "FastAPI",
    "fastify": "Fastify",
    "flask": "Flask",
    "gatsby": "Gatsby",
    "go": "Go",
    "gradio": "Gradio",
    "gunicorn": "Gunicorn",
    "hapi": "Hapi",
    "java": "Java",
    "koa": "Koa",
    "nestjs": "NestJS",
    "next": "Next.js",
    "nginx": "Nginx",
    "node": "Node.js",
    "nuxt": "Nuxt",
    "php": "PHP",
    "python": "Python",
    "react": "React",
    "remix": "Remix",
    "ruby": "Ruby",
    "rust": "Rust",
    "socket.io": "Socket.IO",
    "svelte": "Svelte",
    "streamlit": "Streamlit",
    "uvicorn": "Uvicorn",
    "vite": "Vite",
    "vue": "Vue",
}

STACK_SUMMARY_ORDER: List[str] = [
    "node",
    "python",
    "go",
    "java",
    "dotnet",
    "php",
    "ruby",
    "rust",
    "next",
    "nuxt",
    "react",
    "vue",
    "svelte",
    "angular",
    "remix",
    "astro",
    "vite",
    "nestjs",
    "fastify",
    "koa",
    "hapi",
    "fastapi",
    "flask",
    "django",
    "uvicorn",
    "gunicorn",
    "streamlit",
    "gradio",
    "socket.io",
    "nginx",
    "bun",
    "aspnetcore",
]


def normalize_stack_token(token: str) -> str:
    """Normalize aliases to canonical token names."""
    lowered = (token or "").strip().lower()
    return TOKEN_ALIASES.get(lowered, lowered)


def is_known_stack_token(token: str) -> bool:
    """Return True when the token is part of the canonical registry."""
    return normalize_stack_token(token) in KNOWN_STACK_TOKENS


def normalize_stack_tokens(tokens: Iterable[str]) -> List[str]:
    """Normalize, deduplicate, and sort stack tokens using canonical ordering."""
    normalized = {normalize_stack_token(token) for token in tokens if normalize_stack_token(token) in KNOWN_STACK_TOKENS}
    ordered: List[str] = []
    for token in STACK_SUMMARY_ORDER:
        if token in normalized:
            ordered.append(token)
    for token in sorted(normalized):
        if token not in ordered:
            ordered.append(token)
    return ordered


def unknown_stack_tokens(tokens: Iterable[str]) -> List[str]:
    """Return normalized unknown tokens preserving input order."""
    unknown: List[str] = []
    seen: Set[str] = set()
    for token in tokens:
        normalized = normalize_stack_token(token)
        if normalized in KNOWN_STACK_TOKENS or normalized in seen:
            continue
        seen.add(normalized)
        unknown.append(normalized)
    return unknown


def render_stack_summary(tokens: Iterable[str]) -> str:
    """Render canonical tokens into a stable human-readable summary string."""
    normalized = normalize_stack_tokens(tokens)
    if not normalized:
        return "Unknown"
    return ", ".join(STACK_TOKEN_DISPLAY_NAMES.get(token, token) for token in normalized)
