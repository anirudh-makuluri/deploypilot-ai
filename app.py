from fastapi import FastAPI, HTTPException, Header, Depends
from fastapi.responses import StreamingResponse
import json
from pydantic import BaseModel, Field
from typing import Optional, List, Dict, Any, cast
from uuid import uuid4
from graph.graph import graph, is_build_verify_enabled
from graph.nodes.llm_config import TokenTracker
from fastapi.middleware.cors import CORSMiddleware
import os
from tools.example_bank import (
    POPULAR_EXAMPLE_REPOS,
    seed_example_bank_from_repos,
    fetch_reference_examples,
)
from tools.template_store import (
    seed_default_templates,
    list_templates,
    upsert_template,
    delete_template,
)

app = FastAPI(title="SD-Artifacts Repo Analyzer")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

class AnalyzeRequest(BaseModel):
    repo_url: str
    github_token: Optional[str] = None
    max_files: Optional[int] = 50
    package_path: str = "."
    service_name: Optional[str] = None
    # If provided, the API can return a cached response without authentication.
    # Without this, requests must be authenticated because the app may need to hit GitHub/LLMs.
    commit_sha: Optional[str] = None

class TokenUsage(BaseModel):
    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0

class FileArtifact(BaseModel):
    name: str = Field(description="File name (e.g., Dockerfile, docker-compose.yml, nginx.conf)")
    content: str = Field(description="Full file content")
    location: str = Field(description="Full file path where it should be placed (e.g., apps/dashboard/Dockerfile)")


class AnalyzeResponse(BaseModel):
    response_id: Optional[str] = None
    commit_sha: str = "unknown"
    stack_tokens: List[str] = Field(default_factory=list)
    files: List[FileArtifact] = Field(default_factory=list)
    risks: List[str] = Field(default_factory=list)
    confidence: float
    token_usage: TokenUsage = TokenUsage()


class SeedExampleBankRequest(BaseModel):
    repo_urls: List[str]
    github_token: Optional[str] = None
    max_files_per_repo: int = 20
    permissive_only: bool = True


class SeedExampleBankResponse(BaseModel):
    inserted: int
    updated: int
    skipped: int
    errors: List[str] = []


class PreviewExamplesRequest(BaseModel):
    artifact_type: str
    detected_stack: str
    stack_tokens: List[str] = Field(default_factory=list)
    service: Optional[Dict[str, str]] = None
    limit: int = 3


class PreviewExamplesResponse(BaseModel):
    examples: List[Dict]


class DeleteCacheRequest(BaseModel):
    response_id: str


class DeleteCacheResponse(BaseModel):
    deleted: int
    response_id: str
    repo_url: str
    commit_sha: Optional[str] = None
    package_path: Optional[str] = None
    service_name: Optional[str] = None


class FeedbackRequest(BaseModel):
    repo_url: str
    commit_sha: str
    package_path: str = "."
    feedback: str
    github_token: Optional[str] = None
    failure_summary: Optional[str] = None
    failure_logs: Optional[str] = None
    failed_artifact_scope: Optional[str] = None


class ResponseStatusRequest(BaseModel):
    response_id: str
    passed: bool


class ResponseStatusResponse(BaseModel):
    response_id: str
    passed: bool
    cache_deleted: int = 0


class TemplateRequest(BaseModel):
    name: str
    description: str = ""
    match_stack_tokens: List[str] = Field(default_factory=list)
    match_signals: Dict = Field(default_factory=dict)
    priority: int = 0
    template_content: str = ""
    variables: Dict = Field(default_factory=dict)
    is_active: bool = True


class TemplateSeedResponse(BaseModel):
    inserted: int = 0
    updated: int = 0
    skipped: int = 0


class HealthResponse(BaseModel):
    status: str
    scope: str
    supabase_configured: bool


def _build_files_array(
    dockerfiles: Dict[str, str],
    docker_compose: Optional[str],
    nginx_conf: Optional[str],
    services: List[Dict],
) -> List[FileArtifact]:
    """Convert legacy dockerfiles/compose/nginx structure to files array."""
    files = []
    
    # Add Dockerfiles
    if isinstance(dockerfiles, dict):
        for key, content in dockerfiles.items():
            if isinstance(content, str) and content.strip():
                # Key can be service name or full path; use as-is for location
                location = key if "/" in key else f"{key}/Dockerfile"
                files.append(FileArtifact(
                    name="Dockerfile",
                    content=content,
                    location=location if location.endswith("Dockerfile") else f"{location}/Dockerfile"
                ))
    
    # Add docker-compose.yml if present
    if isinstance(docker_compose, str) and docker_compose.strip():
        files.append(FileArtifact(
            name="docker-compose.yml",
            content=docker_compose,
            location="docker-compose.yml"
        ))
    
    # Add nginx.conf with fixed location
    if isinstance(nginx_conf, str) and nginx_conf.strip():
        files.append(FileArtifact(
            name="nginx.conf",
            content=nginx_conf,
            location="/etc/nginx/conf.d/nginx.conf"
        ))
    
    return files


def _merge_hadolint_into_risks(
    risks: List[str],
    hadolint_results: Dict[str, str],
) -> List[str]:
    """Append hadolint results to risks list."""
    merged = list(risks) if risks else []
    if isinstance(hadolint_results, dict):
        for service, output in hadolint_results.items():
            if output and output.strip():
                merged.append(f"hadolint ({service}): {output.strip()}")
    return merged


def _store_response_log(
    supabase,
    *,
    response_id: str,
    endpoint: str,
    repo_url: str,
    commit_sha: Optional[str],
    package_path: str,
    service_name: Optional[str],
    from_cache: bool,
    payload: Dict,
) -> None:
    if not supabase:
        return
    for attempt in range(3):
        try:
            supabase.table("analysis_responses").insert(
                {
                    "id": response_id,
                    "endpoint": endpoint,
                    "repo_url": repo_url,
                    "commit_sha": commit_sha,
                    "package_path": package_path or ".",
                    "service_name": service_name,
                    "from_cache": from_cache,
                    "payload": payload,
                }
            ).execute()
            break
        except Exception as e:
            print(f"Failed to store analysis response log (attempt {attempt + 1}/3): {e}")
            if attempt < 2:
                import time

                time.sleep(1)


def _fetch_cached_analysis_or_404(repo_url: str, commit_sha: str, package_path: str = "."):
    from db import supabase

    if not supabase:
        raise HTTPException(status_code=503, detail="Supabase is not configured")

    try:
        existing = (
            supabase.table("analysis_cache")
            .select("result,response_id")
            .eq("repo_url", repo_url)
            .eq("commit_sha", commit_sha)
            .eq("package_path", package_path)
            .is_("service_name", None)
            .single()
            .execute()
        )
    except Exception:
        raise HTTPException(status_code=404, detail=f"No cached analysis found for {repo_url}@{commit_sha}")

    data_dict = cast(Dict[str, Any], existing.data) if existing.data else {}
    cached_result = data_dict.get("result") if data_dict else None
    if not cached_result:
        raise HTTPException(status_code=404, detail=f"No cached analysis found for {repo_url}@{commit_sha}")
    response_id = data_dict.get("response_id") if data_dict else None
    if response_id and isinstance(cached_result, dict):
        cached_result.setdefault("response_id", response_id)

    return supabase, cached_result


def _fetch_cached_analysis_or_404_service_aware(
    repo_url: str,
    commit_sha: str,
    package_path: str = ".",
    service_name: Optional[str] = None,
):
    from db import supabase

    if not supabase:
        raise HTTPException(status_code=503, detail="Supabase is not configured")

    try:
        query = (
            supabase.table("analysis_cache")
            .select("result,response_id")
            .eq("repo_url", repo_url)
            .eq("commit_sha", commit_sha)
            .eq("package_path", package_path)
        )
        if service_name:
            query = query.eq("service_name", service_name)
        else:
            query = query.is_("service_name", None)

        existing = query.single().execute()
    except Exception:
        raise HTTPException(status_code=404, detail=f"No cached analysis found for {repo_url}@{commit_sha}")

    data_dict = cast(Dict[str, Any], existing.data) if existing.data else {}
    cached_result = data_dict.get("result") if data_dict else None
    if not cached_result:
        raise HTTPException(status_code=404, detail=f"No cached analysis found for {repo_url}@{commit_sha}")
    response_id = data_dict.get("response_id") if data_dict else None
    if response_id and isinstance(cached_result, dict):
        cached_result.setdefault("response_id", response_id)

    return supabase, cached_result


def _require_auth(authorization: Optional[str]) -> None:
    expected = os.getenv("SD_API_BEARER_TOKEN")
    if not expected:
        raise HTTPException(status_code=503, detail="API authentication is not configured on the server")
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Unauthorized")
    token = authorization.split(" ", 1)[1].strip()
    if token != expected:
        raise HTTPException(status_code=401, detail="Unauthorized")


def require_auth(authorization: Optional[str] = Header(default=None)) -> None:
    _require_auth(authorization)


@app.get("/health", response_model=HealthResponse)
async def health_check():
    from db import supabase

    return HealthResponse(
        status="ok",
        scope="public",
        supabase_configured=bool(supabase),
    )


@app.get("/healthz", response_model=HealthResponse, dependencies=[Depends(require_auth)])
async def health_check_authenticated():
    from db import supabase

    return HealthResponse(
        status="ok",
        scope="authenticated",
        supabase_configured=bool(supabase),
    )


@app.post("/analyze", response_model=AnalyzeResponse)
async def analyze_repo(req: AnalyzeRequest, authorization: Optional[str] = Header(default=None)):
    from db import supabase
    _require_auth(authorization)
    if req.commit_sha:
        try:
            _supabase, cached_result = _fetch_cached_analysis_or_404_service_aware(
                repo_url=req.repo_url,
                commit_sha=req.commit_sha,
                package_path=req.package_path,
                service_name=req.service_name,
            )
            cached_payload = cast(Dict[str, Any], cached_result) if isinstance(cached_result, dict) else {}
            cached_payload.setdefault("commit_sha", req.commit_sha)
            cached_payload.pop("_cache_package_path", None)
            response_cached_payload = dict(cached_payload)
            response_cached_payload.pop("llm_outputs", None)
            _store_response_log(
                supabase,
                response_id=cached_payload.get("response_id") or str(uuid4()),
                endpoint="/analyze",
                repo_url=req.repo_url,
                commit_sha=req.commit_sha,
                package_path=req.package_path,
                service_name=req.service_name,
                from_cache=True,
                payload=cached_payload,
            )
            return AnalyzeResponse(**response_cached_payload)
        except HTTPException as e:
            if e.status_code != 404:
                raise

    tracker = TokenTracker()
    
    initial_state = {
        "repo_url": req.repo_url,
        "github_token": req.github_token,
        "max_files": req.max_files,
        "package_path": req.package_path,
        "service_name": req.service_name,
    }
    result = graph.invoke(initial_state, config={"callbacks": [tracker]})
    
    # Check for errors from scanner or planner
    if "error" in result:
        raise HTTPException(status_code=400, detail=result["error"])
        
    if "cached_response" in result:
        cached_payload = dict(result["cached_response"])
        cached_payload.setdefault("commit_sha", result.get("commit_sha", "unknown"))
        # Do not leak internal cache metadata fields.
        cached_payload.pop("_cache_package_path", None)
        cached_payload.pop("llm_outputs", None)
        return AnalyzeResponse(**cached_payload)
    
    commit_sha = result.get("commit_sha", "unknown")
    response_id = str(uuid4())
    
    # Build files array from legacy structure
    files = _build_files_array(
        result.get("dockerfiles", {}),
        result.get("docker_compose"),
        result.get("nginx_conf"),
        result.get("services", []),
    )
    
    # Merge hadolint results into risks
    risks = _merge_hadolint_into_risks(
        result.get("risks", []),
        result.get("hadolint_results", {}),
    )

    response = AnalyzeResponse(
        response_id=response_id,
        commit_sha=commit_sha,
        stack_tokens=result.get("stack_tokens", []),
        files=files,
        risks=risks,
        confidence=result.get("confidence", 0.0),
        token_usage=TokenUsage(**tracker.get_usage())
    )
    
    # Save to Supabase cache
    if supabase and commit_sha != "unknown":
        for attempt in range(3):
            try:
                result_dict = response.model_dump() if hasattr(response, 'model_dump') else response.dict()
                result_dict["llm_outputs"] = result.get("llm_outputs", {})
                # Internal cache metadata used to support package-scoped cache reuse.
                result_dict["_cache_package_path"] = req.package_path
                supabase.table("analysis_cache").insert({
                    "response_id": response_id,
                    "repo_url": req.repo_url,
                    "commit_sha": commit_sha,
                    "package_path": req.package_path,
                    "service_name": req.service_name,
                    "result": result_dict
                }).execute()
                break
            except Exception as e:
                print(f"Failed to cache result in Supabase (attempt {attempt + 1}/3): {e}")
                if attempt < 2:
                    import time
                    time.sleep(1)
    response_payload = response.model_dump() if hasattr(response, "model_dump") else response.dict()
    log_payload = dict(response_payload)
    log_payload["llm_outputs"] = result.get("llm_outputs", {})
    _store_response_log(
        supabase,
        response_id=response_id,
        endpoint="/analyze",
        repo_url=req.repo_url,
        commit_sha=commit_sha,
        package_path=req.package_path,
        service_name=req.service_name,
        from_cache=False,
        payload=log_payload,
    )

    return response


@app.post("/examples/seed", response_model=SeedExampleBankResponse, dependencies=[Depends(require_auth)])
async def seed_example_bank(req: SeedExampleBankRequest):
    result = seed_example_bank_from_repos(
        repo_urls=req.repo_urls,
        github_token=req.github_token,
        max_files_per_repo=req.max_files_per_repo,
        permissive_only=req.permissive_only,
    )
    return SeedExampleBankResponse(**result)


@app.post("/examples/seed/popular", response_model=SeedExampleBankResponse, dependencies=[Depends(require_auth)])
async def seed_example_bank_popular(github_token: Optional[str] = None):
    result = seed_example_bank_from_repos(
        repo_urls=POPULAR_EXAMPLE_REPOS,
        github_token=github_token,
        max_files_per_repo=20,
        permissive_only=True,
    )
    return SeedExampleBankResponse(**result)


@app.post("/examples/preview", response_model=PreviewExamplesResponse, dependencies=[Depends(require_auth)])
async def preview_example_bank_matches(req: PreviewExamplesRequest):
    if req.artifact_type not in {"dockerfile", "compose"}:
        raise HTTPException(status_code=400, detail="artifact_type must be 'dockerfile' or 'compose'")

    examples = fetch_reference_examples(
        artifact_type=req.artifact_type,
        detected_stack=req.detected_stack,
        stack_tokens=req.stack_tokens,
        service=req.service,
        limit=req.limit,
    )
    return PreviewExamplesResponse(examples=examples)


@app.delete("/cache", response_model=DeleteCacheResponse, dependencies=[Depends(require_auth)])
async def delete_cached_analysis(req: DeleteCacheRequest):
    from db import supabase

    if not supabase:
        raise HTTPException(status_code=503, detail="Supabase is not configured")

    try:
        response_row = (
            supabase.table("analysis_responses")
            .select("id,repo_url,commit_sha,package_path,service_name")
            .eq("id", req.response_id)
            .single()
            .execute()
        )
    except Exception:
        raise HTTPException(status_code=404, detail=f"Response id not found: {req.response_id}")

    row = cast(Dict[str, Any], response_row.data) if response_row.data else {}
    if not row:
        raise HTTPException(status_code=404, detail=f"Response id not found: {req.response_id}")

    try:
        query = (
            supabase.table("analysis_cache")
            .select("id")
            .eq("repo_url", row.get("repo_url"))
            .eq("commit_sha", row.get("commit_sha"))
            .eq("package_path", row.get("package_path"))
        )
        if row.get("service_name"):
            query = query.eq("service_name", row.get("service_name"))
        else:
            query = query.is_("service_name", None)

        existing = query.execute()
        rows = existing.data or []
        if not rows:
            raise HTTPException(status_code=404, detail=f"No cached result found for response id: {req.response_id}")

        delete_query = (
            supabase.table("analysis_cache")
            .delete()
            .eq("repo_url", row.get("repo_url"))
            .eq("commit_sha", row.get("commit_sha"))
            .eq("package_path", row.get("package_path"))
        )
        if row.get("service_name"):
            delete_query = delete_query.eq("service_name", row.get("service_name"))
        else:
            delete_query = delete_query.is_("service_name", None)
        delete_query.execute()

        return DeleteCacheResponse(
            deleted=len(rows),
            response_id=req.response_id,
            repo_url=cast(str, row.get("repo_url")) or "",
            commit_sha=cast(Optional[str], row.get("commit_sha")),
            package_path=cast(Optional[str], row.get("package_path")),
            service_name=cast(Optional[str], row.get("service_name")),
        )
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to delete cache: {e}")

@app.post("/analyze/stream")
async def analyze_repo_stream(req: AnalyzeRequest, authorization: Optional[str] = Header(default=None)):
    from db import supabase
    async def cached_event_generator(cached_payload: Dict):
        import asyncio
        import random
        # Cache hits should still look like a full run to clients.
        yield f"event: progress\ndata: {json.dumps({'node': 'scanner', 'status': 'completed'})}\n\n"
        remaining_nodes = ["planner", "commands_gen", "docker_gen", "compose_gen", "nginx_gen", "preflight", "verifier"]
        if is_build_verify_enabled():
            remaining_nodes.insert(-2, "build_verify")
        if not cached_payload.get("docker_compose"):
            remaining_nodes = [n for n in remaining_nodes if n != "compose_gen"]
        total_delay_s = random.uniform(4.0, 10.0)
        step_delay_s = total_delay_s / max(1, len(remaining_nodes))
        for node in remaining_nodes:
            await asyncio.sleep(step_delay_s)
            yield f"event: progress\ndata: {json.dumps({'node': node, 'status': 'completed'})}\n\n"

        _store_response_log(
            supabase,
            response_id=cached_payload.get("response_id") or str(uuid4()),
            endpoint="/analyze/stream",
            repo_url=req.repo_url,
            commit_sha=cached_payload.get("commit_sha"),
            package_path=req.package_path,
            service_name=req.service_name,
            from_cache=True,
            payload=cached_payload,
        )
        yield f"event: complete\ndata: {json.dumps(cached_payload)}\n\n"

    async def live_event_generator():
        import asyncio
        import random
        tracker = TokenTracker()
        
        initial_state = {
            "repo_url": req.repo_url,
            "github_token": req.github_token,
            "max_files": req.max_files,
        "package_path": req.package_path,
        "service_name": req.service_name,
    }
        
        try:
            full_state = {}
            async for output in graph.astream(initial_state, config={"callbacks": [tracker]}):
                for node_name, state_update in output.items():
                    full_state.update(state_update)
                    
                    # Yield progress event
                    progress_data = {
                        "node": node_name,
                        "status": "completed",
                    }
                    yield f"event: progress\ndata: {json.dumps(progress_data)}\n\n"
                    
                    if "error" in state_update:
                        yield f"event: error\ndata: {json.dumps({'detail': state_update['error']})}\n\n"
                        return
                        
                    if "cached_response" in state_update:
                        cached = state_update["cached_response"]
                        # Inject current token usage into the cached response before returning
                        if "token_usage" not in cached:
                            usage = TokenUsage(**tracker.get_usage())
                            cached["token_usage"] = usage.model_dump() if hasattr(usage, "model_dump") else usage.dict()
                        cached.setdefault("commit_sha", state_update.get("commit_sha", full_state.get("commit_sha", "unknown")))

                        # Simulate the usual node progression so clients can't infer cache hits.
                        # Total delay is randomized to look like a real run.
                        remaining_nodes = ["planner", "commands_gen", "docker_gen", "compose_gen", "nginx_gen", "preflight", "verifier"]
                        if is_build_verify_enabled():
                            remaining_nodes.insert(-2, "build_verify")
                        if not cached.get("docker_compose"):
                            remaining_nodes = [n for n in remaining_nodes if n != "compose_gen"]
                        total_delay_s = random.uniform(4.0, 10.0)
                        step_delay_s = total_delay_s / max(1, len(remaining_nodes))
                        for node in remaining_nodes:
                            await asyncio.sleep(step_delay_s)
                            yield f"event: progress\ndata: {json.dumps({'node': node, 'status': 'completed'})}\n\n"

                        cached.pop("_cache_package_path", None)
                        cached.pop("llm_outputs", None)
                        yield f"event: complete\ndata: {json.dumps(cached)}\n\n"
                        return
            
            # Build files array from legacy structure
            files = _build_files_array(
                full_state.get("dockerfiles", {}),
                full_state.get("docker_compose"),
                full_state.get("nginx_conf"),
                full_state.get("services", []),
            )
            
            # Merge hadolint results into risks
            risks = _merge_hadolint_into_risks(
                full_state.get("risks", []),
                full_state.get("hadolint_results", {}),
            )
            
            response = AnalyzeResponse(
                response_id=str(uuid4()),
                commit_sha=full_state.get("commit_sha", "unknown"),
                stack_tokens=full_state.get("stack_tokens", []),
                files=files,
                risks=risks,
                confidence=full_state.get("confidence", 0.0),
                token_usage=TokenUsage(**tracker.get_usage())
            )
            
            # Save to Supabase cache
            commit_sha = full_state.get("commit_sha", "unknown")
            if supabase and commit_sha != "unknown":
                for attempt in range(3):
                    try:
                        result_dict = response.model_dump() if hasattr(response, 'model_dump') else response.dict()
                        result_dict["llm_outputs"] = full_state.get("llm_outputs", {})
                        # Internal cache metadata used to support package-scoped cache reuse.
                        result_dict["_cache_package_path"] = req.package_path
                        supabase.table("analysis_cache").insert({
                            "response_id": response.response_id,
                            "repo_url": req.repo_url,
                            "commit_sha": commit_sha,
                            "package_path": req.package_path,
                            "service_name": req.service_name,
                            "result": result_dict
                        }).execute()
                        break
                    except Exception as e:
                        print(f"Failed to cache result in Supabase (attempt {attempt + 1}/3): {e}")
                        if attempt < 2:
                            import time
                            time.sleep(1)
            
            final_dict = response.model_dump() if hasattr(response, 'model_dump') else response.dict()
            log_payload = dict(final_dict)
            log_payload["llm_outputs"] = full_state.get("llm_outputs", {})
            _store_response_log(
                supabase,
                response_id=final_dict.get("response_id") or str(uuid4()),
                endpoint="/analyze/stream",
                repo_url=req.repo_url,
                commit_sha=full_state.get("commit_sha"),
                package_path=req.package_path,
                service_name=req.service_name,
                from_cache=False,
                payload=log_payload,
            )
            yield f"event: complete\ndata: {json.dumps(final_dict)}\n\n"
            
        except Exception as e:
            yield f"event: error\ndata: {json.dumps({'detail': str(e)})}\n\n"

    # Decide auth vs cache BEFORE starting the streaming response. If we raise after
    # the stream begins, Starlette can crash with "response already started."
    _require_auth(authorization)
    if req.commit_sha:
        try:
            _supabase, cached_result = _fetch_cached_analysis_or_404_service_aware(
                repo_url=req.repo_url,
                commit_sha=req.commit_sha,
                package_path=req.package_path,
                service_name=req.service_name,
            )
            cached_payload = dict(cached_result)
            cached_payload.setdefault("commit_sha", req.commit_sha)
            cached_payload.setdefault(
                "token_usage",
                {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0},
            )
            cached_payload.pop("_cache_package_path", None)
            cached_payload.pop("llm_outputs", None)
            return StreamingResponse(cached_event_generator(cached_payload), media_type="text/event-stream")
        except HTTPException as e:
            if e.status_code != 404:
                raise

    return StreamingResponse(live_event_generator(), media_type="text/event-stream")


@app.post("/feedback", response_model=AnalyzeResponse, dependencies=[Depends(require_auth)])
async def improve_with_feedback(req: FeedbackRequest):
    from graph.feedback import run_feedback_improvement

    # 1. Fetch existing cached analysis
    supabase, cached_result = _fetch_cached_analysis_or_404(req.repo_url, req.commit_sha, req.package_path)

    # 2. Regenerate artifacts guided by the feedback
    tracker = TokenTracker()
    try:
        context = {
            "repo_url": req.repo_url,
            "github_token": req.github_token,
            "deployment_failure_summary": req.failure_summary,
            "deployment_failure_logs": req.failure_logs,
            "failed_artifact_scope": req.failed_artifact_scope,
        }
        improved = run_feedback_improvement(cached_result, req.feedback, context=context)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Feedback improvement failed: {e}")

    # 3. Build the response
    # Build files array from legacy structure
    files = _build_files_array(
        improved.get("dockerfiles", {}),
        improved.get("docker_compose"),
        improved.get("nginx_conf"),
        improved.get("services", []),
    )
    
    # Merge hadolint results into risks
    risks = _merge_hadolint_into_risks(
        improved.get("risks", []),
        improved.get("hadolint_results", {}),
    )
    
    response = AnalyzeResponse(
        response_id=str(uuid4()),
        commit_sha=req.commit_sha,
        stack_tokens=improved.get("stack_tokens", []),
        files=files,
        risks=risks,
        confidence=improved.get("confidence", 0.0),
        token_usage=TokenUsage(**tracker.get_usage()),
    )

    # 4. Upsert the improved result back to cache
    result_dict = response.model_dump() if hasattr(response, "model_dump") else response.dict()
    result_dict["llm_outputs"] = improved.get("llm_outputs", {})
    result_dict["_cache_package_path"] = cached_result.get("_cache_package_path", ".")
    try:
        supabase.table("analysis_cache").upsert(
            {
                "response_id": response.response_id,
                "repo_url": req.repo_url,
                "commit_sha": req.commit_sha,
                "package_path": cached_result.get("_cache_package_path", "."),
                "service_name": None,
                "result": result_dict,
            },
            on_conflict="repo_url,commit_sha,package_path,service_name",
        ).execute()
        print(f"Updated feedback-improved cache for {req.repo_url}@{req.commit_sha}")
    except Exception as e:
        print(f"Failed to update cache after feedback improvement: {e}")
    _store_response_log(
        supabase,
        response_id=response.response_id or str(uuid4()),
        endpoint="/feedback",
        repo_url=req.repo_url,
        commit_sha=req.commit_sha,
        package_path=req.package_path,
        service_name=None,
        from_cache=False,
        payload=result_dict,
    )

    return response


@app.post("/feedback/stream", dependencies=[Depends(require_auth)])
async def improve_with_feedback_stream(req: FeedbackRequest):
    async def event_generator():
        from graph.feedback import feedback_graph, build_feedback_initial_state, format_feedback_result

        tracker = TokenTracker()

        try:
            supabase, cached_result = _fetch_cached_analysis_or_404(req.repo_url, req.commit_sha, req.package_path)
        except HTTPException as e:
            yield f"event: error\ndata: {json.dumps({'detail': e.detail})}\n\n"
            return

        initial_state = build_feedback_initial_state(cached_result, req.feedback)
        initial_state.update(
            {
                "repo_url": req.repo_url,
                "github_token": req.github_token,
                "deployment_failure_summary": req.failure_summary,
                "deployment_failure_logs": req.failure_logs,
                "failed_artifact_scope": req.failed_artifact_scope,
            }
        )

        try:
            full_state = dict(initial_state)
            async for output in feedback_graph.astream(initial_state, config={"callbacks": [tracker]}):
                for node_name, state_update in output.items():
                    full_state.update(state_update)
                    progress_data = {
                        "node": node_name,
                        "status": "completed",
                    }
                    yield f"event: progress\ndata: {json.dumps(progress_data)}\n\n"

                    if "error" in state_update:
                        yield f"event: error\ndata: {json.dumps({'detail': state_update['error']})}\n\n"
                        return

            improved = format_feedback_result(full_state)

            # Build files array from legacy structure
            files = _build_files_array(
                improved.get("dockerfiles", {}),
                improved.get("docker_compose"),
                improved.get("nginx_conf"),
                improved.get("services", []),
            )
            
            # Merge hadolint results into risks
            risks = _merge_hadolint_into_risks(
                improved.get("risks", []),
                improved.get("hadolint_results", {}),
            )
            
            response = AnalyzeResponse(
                response_id=str(uuid4()),
                commit_sha=req.commit_sha,
                stack_tokens=improved.get("stack_tokens", []),
                files=files,
                risks=risks,
                confidence=improved.get("confidence", 0.0),
                token_usage=TokenUsage(**tracker.get_usage()),
            )

            result_dict = response.model_dump() if hasattr(response, "model_dump") else response.dict()
            result_dict["llm_outputs"] = improved.get("llm_outputs", {})
            result_dict["_cache_package_path"] = cached_result.get("_cache_package_path", ".")
            try:
                supabase.table("analysis_cache").upsert(
                    {
                        "response_id": response.response_id,
                        "repo_url": req.repo_url,
                        "commit_sha": req.commit_sha,
                        "package_path": cached_result.get("_cache_package_path", "."),
                        "service_name": None,
                        "result": result_dict,
                    },
                    on_conflict="repo_url,commit_sha,package_path,service_name",
                ).execute()
                print(f"Updated feedback-improved cache for {req.repo_url}@{req.commit_sha}")
            except Exception as e:
                print(f"Failed to update cache after feedback improvement: {e}")

            final_dict = response.model_dump() if hasattr(response, "model_dump") else response.dict()
            _store_response_log(
                supabase,
                response_id=response.response_id or str(uuid4()),
                endpoint="/feedback/stream",
                repo_url=req.repo_url,
                commit_sha=req.commit_sha,
                package_path=req.package_path,
                service_name=None,
                from_cache=False,
                payload=result_dict,
            )
            yield f"event: complete\ndata: {json.dumps(final_dict)}\n\n"

        except Exception as e:
            yield f"event: error\ndata: {json.dumps({'detail': str(e)})}\n\n"

    return StreamingResponse(event_generator(), media_type="text/event-stream")


@app.post("/responses/status", response_model=ResponseStatusResponse, dependencies=[Depends(require_auth)])
async def set_response_status(req: ResponseStatusRequest):
    from db import supabase

    if not supabase:
        raise HTTPException(status_code=503, detail="Supabase is not configured")

    try:
        response_row = (
            supabase.table("analysis_responses")
            .select("id,repo_url,commit_sha,package_path,service_name")
            .eq("id", req.response_id)
            .single()
            .execute()
        )
    except Exception:
        raise HTTPException(status_code=404, detail=f"Response id not found: {req.response_id}")

    row = cast(Dict[str, Any], response_row.data) if response_row.data else {}
    if not row:
        raise HTTPException(status_code=404, detail=f"Response id not found: {req.response_id}")

    try:
        supabase.table("analysis_responses").update({"passed": req.passed}).eq("id", req.response_id).execute()
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to update response status: {e}")

    deleted = 0
    if req.passed is False:
        delete_query = (
            supabase.table("analysis_cache")
            .delete()
            .eq("repo_url", row.get("repo_url"))
            .eq("commit_sha", row.get("commit_sha"))
            .eq("package_path", row.get("package_path"))
        )
        if row.get("service_name"):
            delete_query = delete_query.eq("service_name", row.get("service_name"))
        else:
            delete_query = delete_query.is_("service_name", None)
        deleted_rows = delete_query.execute()
        deleted = len(deleted_rows.data or [])

    return ResponseStatusResponse(response_id=req.response_id, passed=req.passed, cache_deleted=deleted)

@app.get("/templates", dependencies=[Depends(require_auth)])
async def get_templates(active_only: bool = True):
    templates = list_templates(active_only=active_only)
    return {"templates": templates}


@app.post("/templates", dependencies=[Depends(require_auth)])
async def create_or_update_template(req: TemplateRequest):
    try:
        result = upsert_template(req.model_dump() if hasattr(req, 'model_dump') else req.dict())
        return result
    except RuntimeError as e:
        raise HTTPException(status_code=503, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.post("/templates/seed", response_model=TemplateSeedResponse, dependencies=[Depends(require_auth)])
async def seed_templates():
    result = seed_default_templates()
    return TemplateSeedResponse(**result)


@app.delete("/templates/{name}", dependencies=[Depends(require_auth)])
async def remove_template(name: str):
    try:
        deleted = delete_template(name)
        if not deleted:
            raise HTTPException(status_code=404, detail=f"Template '{name}' not found")
        return {"deleted": True, "name": name}
    except RuntimeError as e:
        raise HTTPException(status_code=503, detail=str(e))


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", 8080)))
