"""Extract ports and stack tokens from cloned repos with high accuracy."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

from tools.stack_tokens import (
    FRAMEWORK_DEFAULTS,
    NODE_PACKAGE_TOKENS,
    PORT_INFERENCE_PRIORITY,
    PYTHON_PACKAGE_TOKENS,
)

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass


def _try_shallow_clone(repo_url: str, temp_dir: str, timeout: int = 30, github_token: Optional[str] = None) -> bool:
    """Try to shallow clone the repo. Return True if successful."""
    try:
        # Use HTTPS with token if available to bypass rate limits
        clone_url = repo_url
        if github_token and "github.com" in repo_url:
            clone_url = repo_url.replace("https://github.com/", f"https://{github_token}@github.com/")
        
        cmd = [
            "git",
            "clone",
            "--depth",
            "1",
            clone_url,
            temp_dir,
        ]
        subprocess.run(cmd, timeout=timeout, capture_output=True, check=True)
        return True
    except (subprocess.TimeoutExpired, subprocess.CalledProcessError, FileNotFoundError):
        return False


def _try_archive_download(repo_url: str, temp_dir: str, github_token: Optional[str] = None, timeout: int = 30) -> bool:
    """Try to download repo as archive. Return True if successful."""
    try:
        # Extract owner/repo from URL
        repo_path = repo_url.split("github.com/")[-1].rstrip("/")
        
        # Try main branch first, then master
        for branch in ["main", "master"]:
            archive_url = f"https://github.com/{repo_path}/archive/refs/heads/{branch}.zip"
            try:
                import urllib.request
                
                # Use token in request headers if available
                req = urllib.request.Request(archive_url)
                if github_token:
                    req.add_header('Authorization', f'token {github_token}')
                
                zip_path = os.path.join(temp_dir, "archive.zip")
                with urllib.request.urlopen(req) as response:
                    with open(zip_path, 'wb') as f:
                        f.write(response.read())
                
                # Extract to temp_dir
                shutil.unpack_archive(zip_path, temp_dir)
                # Clean up zip
                os.remove(zip_path)
                # Move extracted content up one level (archive creates a subdir)
                extracted_dirs = [d for d in os.listdir(temp_dir) if os.path.isdir(os.path.join(temp_dir, d))]
                if extracted_dirs:
                    subdir = os.path.join(temp_dir, extracted_dirs[0])
                    for item in os.listdir(subdir):
                        src = os.path.join(subdir, item)
                        dst = os.path.join(temp_dir, item)
                        shutil.move(src, dst)
                    os.rmdir(subdir)
                return True
            except Exception:
                continue
        return False
    except Exception:
        return False


def _extract_ports_from_dockerfile(repo_dir: str, build_context: str = ".") -> List[Tuple[int, float]]:
    """Extract EXPOSE statements from Dockerfiles in build_context. Returns (port, confidence) tuples."""
    ports: List[Tuple[int, float]] = []
    
    context_path = Path(repo_dir) / (build_context if build_context != "." else "")
    if not context_path.exists():
        return ports
    
    for dockerfile in context_path.glob("Dockerfile*"):
        try:
            content = dockerfile.read_text(encoding="utf-8", errors="ignore")
            # Find EXPOSE statements
            matches = re.findall(r"EXPOSE\s+(\d+)", content, re.IGNORECASE)
            for match in matches:
                port = int(match)
                ports.append((port, 0.95))  # High confidence for explicit EXPOSE
        except Exception:
            pass
    
    return ports


def _extract_ports_from_compose(repo_dir: str, build_context: str = ".") -> List[Tuple[int, float]]:
    """Extract ports from docker-compose.yml files. Returns (port, confidence) tuples."""
    ports: List[Tuple[int, float]] = []
    
    context_path = Path(repo_dir) / (build_context if build_context != "." else "")
    if not context_path.exists():
        context_path = Path(repo_dir)
    
    for compose_file in context_path.glob("docker-compose*.y*ml"):
        try:
            import yaml
            content = yaml.safe_load(compose_file.read_text(encoding="utf-8"))
            if isinstance(content, dict):
                services = content.get("services", {})
                for service_name, service_config in services.items():
                    if isinstance(service_config, dict):
                        # Look for ports list
                        service_ports = service_config.get("ports", [])
                        if isinstance(service_ports, list):
                            for port_spec in service_ports:
                                if isinstance(port_spec, str):
                                    # Format: "8080:80" or "8080"
                                    port_match = re.match(r"^(\d+)(?::\d+)?", port_spec)
                                    if port_match:
                                        port = int(port_match.group(1))
                                        ports.append((port, 0.85))  # Good confidence
                                elif isinstance(port_spec, int):
                                    ports.append((port_spec, 0.85))
        except Exception:
            pass
    
    return ports


def _extract_ports_from_package_json(repo_dir: str, build_context: str = ".") -> List[Tuple[int, float]]:
    """Extract ports from package.json start script and PORT env var. Returns (port, confidence) tuples."""
    ports: List[Tuple[int, float]] = []
    
    context_path = Path(repo_dir) / (build_context if build_context != "." else "")
    if not context_path.exists():
        context_path = Path(repo_dir)
    
    package_json = context_path / "package.json"
    if not package_json.exists():
        return ports
    
    try:
        data = json.loads(package_json.read_text(encoding="utf-8"))
        scripts = data.get("scripts", {})

        # Check common runtime scripts for explicit ports.
        candidate_scripts = [
            scripts.get("start", ""),
            scripts.get("dev", ""),
            scripts.get("serve", ""),
            scripts.get("preview", ""),
        ]
        for script in candidate_scripts:
            if not script:
                continue
            port_patterns = [
                r"-p\s+(\d+)",
                r"--port[=\s]+(\d+)",
                r"localhost:(\d+)",
                r":\s*(\d+)[,\s]",
                r"listen['\"]?\s*:\s*(\d+)",
            ]
            for pattern in port_patterns:
                match = re.search(pattern, script)
                if match:
                    ports.append((int(match.group(1)), 0.75))

        # Add framework-specific default ports if not explicitly found
        deps = {}
        deps.update(data.get("dependencies", {}))
        deps.update(data.get("devDependencies", {}))
        
        # Check for frameworks with non-standard ports from the registry
        for dep in deps:
            if dep in FRAMEWORK_DEFAULTS:
                default_port = FRAMEWORK_DEFAULTS[dep]
                # Don't add if we already found this port explicitly
                has_explicit_port = any(port == default_port for port, _ in ports)
                if not has_explicit_port:
                    # Lower confidence since this is inferred, not explicit
                    ports.append((default_port, 0.60))
    except Exception:
        pass
    
    return ports


def _extract_ports_from_env_files(repo_dir: str, build_context: str = ".") -> List[Tuple[int, float]]:
    """Extract PORT from .env, .env.example, .env.local files. Returns (port, confidence) tuples."""
    ports: List[Tuple[int, float]] = []
    
    context_path = Path(repo_dir) / (build_context if build_context != "." else "")
    if not context_path.exists():
        context_path = Path(repo_dir)
    
    env_files = [".env", ".env.example", ".env.local", ".env.production"]
    for env_name in env_files:
        env_file = context_path / env_name
        if env_file.exists():
            try:
                content = env_file.read_text(encoding="utf-8", errors="ignore")
                # Look for PORT=, SERVER_PORT=, etc.
                port_patterns = [
                    r"^PORT=(\d+)",
                    r"^SERVER_PORT=(\d+)",
                    r"^ASPNETCORE_URLS=.*:(\d+)",
                ]
                for pattern in port_patterns:
                    match = re.search(pattern, content, re.MULTILINE)
                    if match:
                        port = int(match.group(1))
                        ports.append((port, 0.70))  # Medium confidence
            except Exception:
                pass
    
    return ports


def _extract_ports_from_config_files(repo_dir: str, build_context: str = ".") -> List[Tuple[int, float]]:
    """Extract ports from next.config.js, vite.config.js, etc. Returns (port, confidence) tuples."""
    ports: List[Tuple[int, float]] = []
    
    context_path = Path(repo_dir) / (build_context if build_context != "." else "")
    if not context_path.exists():
        context_path = Path(repo_dir)
    
    candidate_files = {
        context_path / "next.config.js",
        context_path / "nuxt.config.js",
        context_path / "vite.config.js",
        context_path / "webpack.config.js",
        context_path / "gulpfile.js",
        context_path / "config.js",
        context_path / "config.cjs",
        context_path / "config.mjs",
    }

    # Common backend patterns keep port in config/*.js.
    candidate_files.update(context_path.glob("config/*.js"))
    candidate_files.update(context_path.glob("config/*.cjs"))
    candidate_files.update(context_path.glob("config/*.mjs"))

    port_patterns = [
        r"\bport\s*[:=]\s*(\d{2,5})\b",
        r"\bPORT\s*[:=]\s*(\d{2,5})\b",
        r"server.*port\s*[:=]\s*(\d{2,5})",
        r"listen['\"]?\s*:\s*(\d{2,5})",
        r"process\.env\.PORT\s*\|\|\s*(\d{2,5})",
        r"process\.env\.PORT\s*\?\?\s*(\d{2,5})",
        r"process\.env\[['\"]PORT['\"]\]\s*\|\|\s*(\d{2,5})",
    ]

    for config_file in candidate_files:
        if not config_file.exists() or not config_file.is_file():
            continue
        try:
            content = config_file.read_text(encoding="utf-8", errors="ignore")
            for pattern in port_patterns:
                for match in re.finditer(pattern, content, re.IGNORECASE):
                    ports.append((int(match.group(1)), 0.72))
        except Exception:
            pass
    
    return ports


def _extract_stack_tokens(repo_dir: str, build_context: str = ".") -> Set[str]:
    """Extract stack technology tokens from the requested build context."""
    tokens: Set[str] = set()

    repo_path = Path(repo_dir)
    context_path = repo_path / (build_context if build_context != "." else "")
    if not context_path.exists():
        context_path = repo_path
    
    # Check package.json
    package_json = context_path / "package.json"
    if package_json.exists():
        try:
            data = json.loads(package_json.read_text(encoding="utf-8"))
            deps = {}
            deps.update(data.get("dependencies", {}))
            deps.update(data.get("devDependencies", {}))
            package_manager = str(data.get("packageManager", "")).lower()
            
            for pkg_name, token in NODE_PACKAGE_TOKENS.items():
                if pkg_name in deps:
                    tokens.add(token)

            if package_manager.startswith("bun@"):
                tokens.add("bun")
            
            # Always add node if package.json exists
            tokens.add("node")
        except Exception:
            pass
    
    # Check requirements.txt
    requirements = context_path / "requirements.txt"
    if requirements.exists():
        try:
            content = requirements.read_text(encoding="utf-8", errors="ignore")
            for pkg_name, token in PYTHON_PACKAGE_TOKENS.items():
                if pkg_name in content.lower():
                    tokens.add(token)
            tokens.add("python")
        except Exception:
            pass

    pyproject_toml = context_path / "pyproject.toml"
    if pyproject_toml.exists():
        try:
            content = pyproject_toml.read_text(encoding="utf-8", errors="ignore").lower()
            for pkg_name, token in PYTHON_PACKAGE_TOKENS.items():
                if pkg_name in content:
                    tokens.add(token)
            tokens.add("python")
        except Exception:
            pass

    if (context_path / "bun.lockb").exists() or (context_path / "bun.lock").exists():
        tokens.add("bun")
    
    # Check go.mod
    go_mod = context_path / "go.mod"
    if go_mod.exists():
        tokens.add("go")
    
    # Check Dockerfile for base image hints
    for dockerfile in context_path.glob("Dockerfile*"):
        try:
            content = dockerfile.read_text(encoding="utf-8", errors="ignore").lower()
            if "node:" in content:
                tokens.add("node")
            if "python:" in content:
                tokens.add("python")
            if "golang:" in content or "go:" in content:
                tokens.add("go")
            if "ruby:" in content:
                tokens.add("ruby")
            if "php:" in content:
                tokens.add("php")
            if "java:" in content or "openjdk" in content or "temurin" in content:
                tokens.add("java")
            if "mcr.microsoft.com/dotnet" in content:
                tokens.add("dotnet")
            if "oven/bun" in content:
                tokens.add("bun")
            if "nginx" in content:
                tokens.add("nginx")
            if "uvicorn" in content:
                tokens.add("uvicorn")
            if "gunicorn" in content:
                tokens.add("gunicorn")
        except Exception:
            pass
    
    # Check docker-compose
    for compose_file in context_path.glob("docker-compose*.y*ml"):
        try:
            import yaml
            content = yaml.safe_load(compose_file.read_text(encoding="utf-8"))
            if isinstance(content, dict):
                services = content.get("services", {})
                for service_name, service_config in services.items():
                    if isinstance(service_config, dict):
                        image = service_config.get("image", "").lower()
                        if "node" in image or "node:" in image:
                            tokens.add("node")
                        if "python" in image:
                            tokens.add("python")
                        if "nginx" in image:
                            tokens.add("nginx")
                        if "postgres" in image:
                            tokens.add("postgres")
                        if "redis" in image:
                            tokens.add("redis")
                        if "mongo" in image:
                            tokens.add("mongodb")

                        # Parse command hints to infer Python app servers.
                        command = str(service_config.get("command", "")).lower()
                        if "uvicorn" in command:
                            tokens.add("uvicorn")
                            tokens.add("python")
                        if "gunicorn" in command:
                            tokens.add("gunicorn")
                            tokens.add("python")
        except Exception:
            pass

    # Conservative runtime inference for FastAPI apps when an ASGI server is not explicit.
    if "fastapi" in tokens and "python" in tokens and "uvicorn" not in tokens and "gunicorn" not in tokens:
        tokens.add("uvicorn")
    
    return tokens


def _default_port_from_stack_tokens(stack_tokens: List[str]) -> Tuple[Optional[int], str]:
    """Select the most informative framework default instead of relying on token sort order."""
    token_set = {token.lower() for token in stack_tokens}

    for token in PORT_INFERENCE_PRIORITY:
        if token in token_set and token in FRAMEWORK_DEFAULTS:
            return FRAMEWORK_DEFAULTS[token], token

    for token in stack_tokens:
        normalized = token.lower()
        if normalized in FRAMEWORK_DEFAULTS:
            return FRAMEWORK_DEFAULTS[normalized], normalized

    return None, ""


def extract_port_and_stack_from_key_files(
    key_files: Dict[str, str],
    build_context: str = ".",
) -> Dict[str, Any]:
    """
    Extract port and stack tokens directly from key_files dict (from scanner).
    
    Args:
        key_files: Dict of file paths to file contents (from scanner repo_scan)
        build_context: Sub-path within repo to analyze
    
    Returns:
        {
            "success": bool,
            "port": int or None,
            "port_confidence": float (0-1),
            "port_source": str (e.g., "dockerfile", "compose", "package_json", "default"),
            "stack_tokens": [str],
            "error": str or None,
        }
    """
    if not isinstance(key_files, dict):
        return {
            "success": False,
            "port": None,
            "port_confidence": 0.0,
            "port_source": "none",
            "stack_tokens": [],
            "error": "key_files must be a dictionary",
        }
    
    context_prefix = build_context if build_context != "." else ""
    if context_prefix:
        context_prefix = context_prefix.rstrip("/") + "/"
    
    # Extract stack tokens from key_files
    tokens: Set[str] = set()
    
    # Check package.json
    for file_path, content in key_files.items():
        if not isinstance(content, str):
            continue
        norm_path = file_path.replace("\\", "/").lower()
        if norm_path.endswith("package.json"):
            if context_prefix and not norm_path.startswith(context_prefix):
                continue
            try:
                data = json.loads(content)
                deps = {}
                deps.update(data.get("dependencies", {}))
                deps.update(data.get("devDependencies", {}))
                for pkg_name, token in NODE_PACKAGE_TOKENS.items():
                    if pkg_name in deps:
                        tokens.add(token)
                tokens.add("node")
            except Exception:
                pass
    
    # Check requirements.txt and pyproject.toml
    for file_path, content in key_files.items():
        if not isinstance(content, str):
            continue
        norm_path = file_path.replace("\\", "/").lower()
        if context_prefix and not norm_path.startswith(context_prefix):
            continue
        if norm_path.endswith("requirements.txt"):
            for pkg_name, token in PYTHON_PACKAGE_TOKENS.items():
                if pkg_name in content.lower():
                    tokens.add(token)
            tokens.add("python")
        elif norm_path.endswith("pyproject.toml"):
            for pkg_name, token in PYTHON_PACKAGE_TOKENS.items():
                if pkg_name in content.lower():
                    tokens.add(token)
            tokens.add("python")
    
    # Check Dockerfile for base image hints
    for file_path, content in key_files.items():
        if not isinstance(content, str):
            continue
        norm_path = file_path.replace("\\", "/").lower()
        if context_prefix and not norm_path.startswith(context_prefix):
            continue
        if "dockerfile" in norm_path:
            content_lower = content.lower()
            if "node:" in content_lower:
                tokens.add("node")
            if "python:" in content_lower:
                tokens.add("python")
            if "golang:" in content_lower or "go:" in content_lower:
                tokens.add("go")
            if "ruby:" in content_lower:
                tokens.add("ruby")
            if "php:" in content_lower:
                tokens.add("php")
            if "java:" in content_lower or "openjdk" in content_lower or "temurin" in content_lower:
                tokens.add("java")
            if "mcr.microsoft.com/dotnet" in content_lower:
                tokens.add("dotnet")
            if "oven/bun" in content_lower:
                tokens.add("bun")
            if "nginx" in content_lower:
                tokens.add("nginx")
            if "uvicorn" in content_lower:
                tokens.add("uvicorn")
            if "gunicorn" in content_lower:
                tokens.add("gunicorn")
    
    # Check docker-compose
    for file_path, content in key_files.items():
        if not isinstance(content, str):
            continue
        norm_path = file_path.replace("\\", "/").lower()
        if context_prefix and not norm_path.startswith(context_prefix):
            continue
        if "docker-compose" in norm_path or "compose.yml" in norm_path or "compose.yaml" in norm_path:
            try:
                import yaml
                compose_content = yaml.safe_load(content)
                if isinstance(compose_content, dict):
                    services = compose_content.get("services", {})
                    for service_name, service_config in services.items():
                        if isinstance(service_config, dict):
                            image = service_config.get("image", "").lower()
                            if "node" in image or "node:" in image:
                                tokens.add("node")
                            if "python" in image:
                                tokens.add("python")
                            if "nginx" in image:
                                tokens.add("nginx")
                            command = str(service_config.get("command", "")).lower()
                            if "uvicorn" in command:
                                tokens.add("uvicorn")
                                tokens.add("python")
                            if "gunicorn" in command:
                                tokens.add("gunicorn")
                                tokens.add("python")
            except Exception:
                pass
    
    stack_tokens = sorted(tokens)
    
    # Extract ports in ranked order of confidence
    all_ports: List[Tuple[int, float, str]] = []
    
    # 1. Dockerfile EXPOSE
    for file_path, content in key_files.items():
        if not isinstance(content, str):
            continue
        norm_path = file_path.replace("\\", "/").lower()
        if context_prefix and not norm_path.startswith(context_prefix):
            continue
        if "dockerfile" in norm_path:
            matches = re.findall(r"EXPOSE\s+(\d+)", content, re.IGNORECASE)
            for match in matches:
                all_ports.append((int(match), 0.95, "dockerfile"))
    
    # 2. docker-compose ports
    for file_path, content in key_files.items():
        if not isinstance(content, str):
            continue
        norm_path = file_path.replace("\\", "/").lower()
        if context_prefix and not norm_path.startswith(context_prefix):
            continue
        if "docker-compose" in norm_path or "compose.yml" in norm_path or "compose.yaml" in norm_path:
            try:
                import yaml
                compose_content = yaml.safe_load(content)
                if isinstance(compose_content, dict):
                    services = compose_content.get("services", {})
                    for service_name, service_config in services.items():
                        if isinstance(service_config, dict):
                            service_ports = service_config.get("ports", [])
                            if isinstance(service_ports, list):
                                for port_spec in service_ports:
                                    if isinstance(port_spec, str):
                                        port_match = re.match(r"^(\d+)(?::\d+)?", port_spec)
                                        if port_match:
                                            all_ports.append((int(port_match.group(1)), 0.85, "compose"))
                                    elif isinstance(port_spec, int):
                                        all_ports.append((port_spec, 0.85, "compose"))
            except Exception:
                pass
    
    # 3. package.json script
    for file_path, content in key_files.items():
        if not isinstance(content, str):
            continue
        norm_path = file_path.replace("\\", "/").lower()
        if context_prefix and not norm_path.startswith(context_prefix):
            continue
        if norm_path.endswith("package.json"):
            try:
                data = json.loads(content)
                scripts = data.get("scripts", {})
                for script in [scripts.get("start", ""), scripts.get("dev", ""), scripts.get("serve", ""), scripts.get("preview", "")]:
                    if not script:
                        continue
                    port_patterns = [
                        r"-p\s+(\d+)",
                        r"--port[=\s]+(\d+)",
                        r"localhost:(\d+)",
                        r":\s*(\d+)[,\s]",
                        r"listen['\"]?\s*:\s*(\d+)",
                    ]
                    for pattern in port_patterns:
                        match = re.search(pattern, script)
                        if match:
                            all_ports.append((int(match.group(1)), 0.75, "package_json"))
            except Exception:
                pass
    
    # 4. .env files
    for file_path, content in key_files.items():
        if not isinstance(content, str):
            continue
        norm_path = file_path.replace("\\", "/").lower()
        if context_prefix and not norm_path.startswith(context_prefix):
            continue
        if any(norm_path.endswith(env) for env in [".env.example", ".env.local", ".env.production"]):
            port_patterns = [
                r"^PORT=(\d+)",
                r"^SERVER_PORT=(\d+)",
                r"^ASPNETCORE_URLS=.*:(\d+)",
            ]
            for pattern in port_patterns:
                match = re.search(pattern, content, re.MULTILINE)
                if match:
                    all_ports.append((int(match.group(1)), 0.70, "env_file"))
    
    # 5. Config files (next.config.js, vite.config.js, webpack.config.js, etc.)
    for file_path, content in key_files.items():
        if not isinstance(content, str):
            continue
        norm_path = file_path.replace("\\", "/").lower()
        if context_prefix and not norm_path.startswith(context_prefix):
            continue
        is_config = any(norm_path.endswith(cfg) for cfg in [
            "next.config.js", "next.config.mjs", "next.config.ts",
            "nuxt.config.js", "nuxt.config.ts",
            "vite.config.js", "vite.config.ts",
            "webpack.config.js", "webpack.config.cjs", "webpack.config.mjs", "webpack.config.ts",
            "config.js", "config.cjs", "config.mjs",
        ]) or (norm_path.startswith("config/") and norm_path.endswith((".js", ".cjs", ".mjs", ".ts")))
        
        if is_config:
            port_patterns = [
                r"\bport\s*[:=]\s*(\d{2,5})\b",
                r"\bPORT\s*[:=]\s*(\d{2,5})\b",
                r"server.*port\s*[:=]\s*(\d{2,5})",
                r"listen['\"]?\s*:\s*(\d{2,5})",
                r"process\.env\.PORT\s*\|\|\s*(\d{2,5})",
                r"process\.env\.PORT\s*\?\?\s*(\d{2,5})",
                r"process\.env\[['\"]PORT['\"]\]\s*\|\|\s*(\d{2,5})",
            ]
            for pattern in port_patterns:
                for match in re.finditer(pattern, content, re.IGNORECASE):
                    all_ports.append((int(match.group(1)), 0.72, "config_file"))
    
    # 6. Framework defaults from stack tokens (lowest priority)
    for token in stack_tokens:
        token_lower = token.lower()
        if token_lower in FRAMEWORK_DEFAULTS:
            default_port = FRAMEWORK_DEFAULTS[token_lower]
            # Don't add if we already found this port explicitly
            has_explicit_port = any(port == default_port for port, _, _ in all_ports)
            if not has_explicit_port:
                # Lower confidence since this is inferred from framework, not explicit config
                all_ports.append((default_port, 0.60, f"framework_default:{token}"))
    
    # Sort by confidence descending, pick the best
    detected_port = None
    port_confidence = 0.0
    port_source = "none"
    
    if all_ports:
        all_ports.sort(key=lambda x: x[1], reverse=True)
        detected_port, port_confidence, port_source = all_ports[0]
    
    # Final fallback if no ports found at all
    if detected_port is None:
        detected_port = 3000
        port_confidence = 0.30
        port_source = "final_default"
    
    return {
        "success": True,
        "port": detected_port,
        "port_confidence": round(port_confidence, 2),
        "port_source": port_source,
        "stack_tokens": stack_tokens,
        "error": None,
    }


def extract_port_and_stack(
    repo_url: str,
    build_context: str = ".",
    timeout: int = 60,
    github_token: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Clone repo and extract port and stack tokens with high accuracy.
    
    Args:
        repo_url: GitHub repository URL
        build_context: Sub-path within repo to analyze
        timeout: Timeout in seconds
        github_token: Optional GitHub token to bypass rate limits
    
    Returns:
        {
            "success": bool,
            "port": int or None,
            "port_confidence": float (0-1),
            "port_source": str (e.g., "dockerfile", "compose", "package_json", "default"),
            "stack_tokens": [str],
            "error": str or None,
        }
    """
    
    # Load GITHUB_TOKEN from env if not provided
    if not github_token:
        github_token = os.getenv("GITHUB_TOKEN")
    
    with tempfile.TemporaryDirectory() as temp_dir:
        try:
            # Try to clone or download the repo
            cloned = _try_shallow_clone(repo_url, temp_dir, timeout=min(timeout, 30), github_token=github_token)
            if not cloned:
                cloned = _try_archive_download(repo_url, temp_dir, github_token=github_token, timeout=min(timeout, 30))
            
            if not cloned:
                return {
                    "success": False,
                    "port": None,
                    "port_confidence": 0.0,
                    "port_source": "none",
                    "stack_tokens": [],
                    "error": "Failed to clone or download repository",
                }
            
            # Extract stack tokens from the requested build context.
            stack_tokens = sorted(_extract_stack_tokens(temp_dir, build_context=build_context))
            
            # Extract ports in ranked order of confidence
            all_ports: List[Tuple[int, float, str]] = []
            
            # 1. Dockerfile EXPOSE (highest confidence)
            for port, conf in _extract_ports_from_dockerfile(temp_dir, build_context):
                all_ports.append((port, conf, "dockerfile"))
            
            # 2. docker-compose ports
            for port, conf in _extract_ports_from_compose(temp_dir, build_context):
                all_ports.append((port, conf, "compose"))
            
            # 3. package.json script
            for port, conf in _extract_ports_from_package_json(temp_dir, build_context):
                all_ports.append((port, conf, "package_json"))
            
            # 4. .env files
            for port, conf in _extract_ports_from_env_files(temp_dir, build_context):
                all_ports.append((port, conf, "env_file"))
            
            # 5. Config files
            for port, conf in _extract_ports_from_config_files(temp_dir, build_context):
                all_ports.append((port, conf, "config_file"))
            
            # Sort by confidence descending, pick the best
            detected_port = None
            port_confidence = 0.0
            port_source = "none"
            
            if all_ports:
                all_ports.sort(key=lambda x: x[1], reverse=True)
                detected_port, port_confidence, port_source = all_ports[0]
            else:
                # Fall back to framework default based on stack tokens
                detected_port, default_token = _default_port_from_stack_tokens(stack_tokens)
                if detected_port is not None:
                    port_confidence = 0.45
                    port_source = f"framework_default:{default_token}"
            
            # Final fallback
            if detected_port is None:
                detected_port = 3000
                port_confidence = 0.30
                port_source = "final_default"
            
            return {
                "success": True,
                "port": detected_port,
                "port_confidence": round(port_confidence, 2),
                "port_source": port_source,
                "stack_tokens": stack_tokens,
                "error": None,
            }
        
        except Exception as e:
            return {
                "success": False,
                "port": None,
                "port_confidence": 0.0,
                "port_source": "none",
                "stack_tokens": [],
                "error": str(e),
            }
