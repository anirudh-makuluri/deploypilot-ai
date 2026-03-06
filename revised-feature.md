# sd-artifacts: Repo Analyzer for SmartDeploy 🚀

## Overview

**sd-artifacts** is a **LangGraph-powered analyzer service** that inspects GitHub repositories and generates production-ready **Dockerfiles**, **docker-compose.yml**, and **nginx.conf** configurations for deployment to SmartDeploy.

**Status**: MVP (v1) → Neo4j caching (v2)

```
SmartDeploy Frontend → POST /analyze → sd-artifacts → Dockerfile + Compose + Nginx
```

**Live Demo**: [sd-artifacts.anirudh-makuluri.xyz/analyze](https://sd-artifacts.anirudh-makuluri.xyz/analyze)

***

## 🎯 Features

### v1 (MVP)
- ✅ **GitHub repo scanning** (using user's OAuth token)
- ✅ **Stack detection** (Node.js, Python, Next.js, Express, FastAPI, etc.)
- ✅ **Dockerfile generation** (multi-stage, non-root, healthchecks)
- ✅ **docker-compose.yml** (app + postgres/redis when needed)
- ✅ **nginx.conf** (reverse proxy with security headers)
- ✅ **FastAPI service** (`POST /analyze`)
- ✅ **LangGraph multi-agent** (scanner → planner → generator → validator)

### v2 (Future)
- 🔄 **Neo4j knowledge graph** caching (reuse framework analysis)
- 🔄 **Advanced validation** (hadolint, docker-slim integration)
- 🔄 **GitHub PR creation** (optional auto-fix)

***

## 🏗️ Architecture

```
Frontend (SmartDeploy) → FastAPI /analyze → LangGraph Graph → JSON Response
                                                      ↓
                                        scanner → planner → docker_gen → nginx_gen
                                                      │
                                               compose_gen ────────┘
```