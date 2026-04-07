from fastapi import FastAPI, HTTPException, Header, Depends
from fastapi.responses import StreamingResponse
import json
from pydantic import BaseModel, Field
from typing import Optional, List, Dict
from graph.graph import graph
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

class AnalyzeResponse(BaseModel):
    commit_sha: str = "unknown"
    stack_summary: str
    stack_tokens: List[str] = Field(default_factory=list)
    services: List[Dict]
    dockerfiles: Dict[str, str]
    docker_compose: Optional[str] = None
    nginx_conf: Optional[str] = None
    has_existing_dockerfiles: bool = False
    has_existing_compose: bool = False
    risks: List[str]
    confidence: float
    hadolint_results: Dict[str, str] = {}
    commands: Dict = Field(default_factory=dict)
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
    repo_url: str
    commit_sha: Optional[str] = None
    package_path: str = "."
    service_name: Optional[str] = None


class DeleteCacheResponse(BaseModel):
    deleted: int
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


def _fetch_cached_analysis_or_404(repo_url: str, commit_sha: str, package_path: str = "."):
    from db import supabase

    if not supabase:
        raise HTTPException(status_code=503, detail="Supabase is not configured")

    try:
        existing = (
            supabase.table("analysis_cache")
            .select("result")
            .eq("repo_url", repo_url)
            .eq("commit_sha", commit_sha)
            .eq("package_path", package_path)
            .is_("service_name", None)
            .single()
            .execute()
        )
    except Exception:
        raise HTTPException(status_code=404, detail=f"No cached analysis found for {repo_url}@{commit_sha}")

    cached_result = existing.data.get("result") if existing.data else None
    if not cached_result:
        raise HTTPException(status_code=404, detail=f"No cached analysis found for {repo_url}@{commit_sha}")

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
            .select("result")
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

    cached_result = existing.data.get("result") if existing.data else None
    if not cached_result:
        raise HTTPException(status_code=404, detail=f"No cached analysis found for {repo_url}@{commit_sha}")

    return supabase, cached_result


def _require_auth(authorization: Optional[str]) -> None:
    expected = os.getenv("SD_API_BEARER_TOKEN") or os.getenv("API_BEARER_TOKEN") or os.getenv("API_AUTH_TOKEN")
    if not expected:
        raise HTTPException(status_code=503, detail="API authentication is not configured on the server")
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Unauthorized")
    token = authorization.split(" ", 1)[1].strip()
    if token != expected:
        raise HTTPException(status_code=401, detail="Unauthorized")


def require_auth(authorization: Optional[str] = Header(default=None)) -> None:
    _require_auth(authorization)

@app.post("/analyze", response_model=AnalyzeResponse)
async def analyze_repo(req: AnalyzeRequest, authorization: Optional[str] = Header(default=None)):
    # Allow unauthenticated reads ONLY when the client can be served from cache directly.
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
            cached_payload.pop("_cache_package_path", None)
            return AnalyzeResponse(**cached_payload)
        except HTTPException as e:
            # Only fall back to live compute when authenticated.
            if e.status_code != 404:
                raise

    _require_auth(authorization)
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
        return AnalyzeResponse(**cached_payload)
    
    commit_sha = result.get("commit_sha", "unknown")

    response = AnalyzeResponse(
        commit_sha=commit_sha,
        stack_summary=result.get("detected_stack", "Unknown"),
        stack_tokens=result.get("stack_tokens", []),
        services=result.get("services", []),
        dockerfiles=result.get("dockerfiles", {}),
        docker_compose=result.get("docker_compose"),
        nginx_conf=result.get("nginx_conf"),
        has_existing_dockerfiles=result.get("has_existing_dockerfiles", False),
        has_existing_compose=result.get("has_existing_compose", False),
        risks=result.get("risks", []),
        confidence=result.get("confidence", 0.0),
        hadolint_results=result.get("hadolint_results", {}),
        commands=result.get("commands", {}),
        token_usage=TokenUsage(**tracker.get_usage())
    )
    
    # Save to Supabase cache
    from db import supabase
    if supabase and commit_sha != "unknown":
        for attempt in range(3):
            try:
                result_dict = response.model_dump() if hasattr(response, 'model_dump') else response.dict()
                # Internal cache metadata used to support package-scoped cache reuse.
                result_dict["_cache_package_path"] = req.package_path
                supabase.table("analysis_cache").insert({
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

    return response


@app.post("/examples/seed", response_model=SeedExampleBankResponse)
async def seed_example_bank(req: SeedExampleBankRequest, _auth: None = Depends(require_auth)):
    result = seed_example_bank_from_repos(
        repo_urls=req.repo_urls,
        github_token=req.github_token,
        max_files_per_repo=req.max_files_per_repo,
        permissive_only=req.permissive_only,
    )
    return SeedExampleBankResponse(**result)


@app.post("/examples/seed/popular", response_model=SeedExampleBankResponse)
async def seed_example_bank_popular(github_token: Optional[str] = None, _auth: None = Depends(require_auth)):
    result = seed_example_bank_from_repos(
        repo_urls=POPULAR_EXAMPLE_REPOS,
        github_token=github_token,
        max_files_per_repo=20,
        permissive_only=True,
    )
    return SeedExampleBankResponse(**result)


@app.post("/examples/preview", response_model=PreviewExamplesResponse)
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


@app.delete("/cache", response_model=DeleteCacheResponse)
async def delete_cached_analysis(req: DeleteCacheRequest, _auth: None = Depends(require_auth)):
    from db import supabase

    if not supabase:
        raise HTTPException(status_code=503, detail="Supabase is not configured")

    try:
        query = supabase.table("analysis_cache").select("id").eq("repo_url", req.repo_url)
        if req.commit_sha:
            query = query.eq("commit_sha", req.commit_sha)
        if req.package_path:
            query = query.eq("package_path", req.package_path)
        if req.service_name:
            query = query.eq("service_name", req.service_name)
        else:
            query = query.is_("service_name", None)

        existing = query.execute()
        rows = existing.data or []
        if not rows:
            raise HTTPException(status_code=404, detail="No cached result found for the provided criteria")

        delete_query = supabase.table("analysis_cache").delete().eq("repo_url", req.repo_url)
        if req.commit_sha:
            delete_query = delete_query.eq("commit_sha", req.commit_sha)
        if req.package_path:
            delete_query = delete_query.eq("package_path", req.package_path)
        if req.service_name:
            delete_query = delete_query.eq("service_name", req.service_name)
        else:
            delete_query = delete_query.is_("service_name", None)
        delete_query.execute()

        return DeleteCacheResponse(
            deleted=len(rows),
            repo_url=req.repo_url,
            commit_sha=req.commit_sha,
            package_path=req.package_path,
            service_name=req.service_name,
        )
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to delete cache: {e}")

@app.post("/analyze/stream")
async def analyze_repo_stream(req: AnalyzeRequest, authorization: Optional[str] = Header(default=None)):
    async def cached_event_generator(cached_payload: Dict):
        import asyncio
        import random
        # Cache hits should still look like a full run to clients.
        yield f"event: progress\ndata: {json.dumps({'node': 'scanner', 'status': 'completed'})}\n\n"
        remaining_nodes = ["planner", "commands_gen", "docker_gen", "compose_gen", "nginx_gen", "verifier"]
        if not cached_payload.get("docker_compose"):
            remaining_nodes = [n for n in remaining_nodes if n != "compose_gen"]
        total_delay_s = random.uniform(4.0, 10.0)
        step_delay_s = total_delay_s / max(1, len(remaining_nodes))
        for node in remaining_nodes:
            await asyncio.sleep(step_delay_s)
            yield f"event: progress\ndata: {json.dumps({'node': node, 'status': 'completed'})}\n\n"

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
                        remaining_nodes = ["planner", "commands_gen", "docker_gen", "compose_gen", "nginx_gen", "verifier"]
                        if not cached.get("docker_compose"):
                            remaining_nodes = [n for n in remaining_nodes if n != "compose_gen"]
                        total_delay_s = random.uniform(4.0, 10.0)
                        step_delay_s = total_delay_s / max(1, len(remaining_nodes))
                        for node in remaining_nodes:
                            await asyncio.sleep(step_delay_s)
                            yield f"event: progress\ndata: {json.dumps({'node': node, 'status': 'completed'})}\n\n"

                        cached.pop("_cache_package_path", None)
                        yield f"event: complete\ndata: {json.dumps(cached)}\n\n"
                        return
            
            response = AnalyzeResponse(
                commit_sha=full_state.get("commit_sha", "unknown"),
                stack_summary=full_state.get("detected_stack", "Unknown"),
                stack_tokens=full_state.get("stack_tokens", []),
                services=full_state.get("services", []),
                dockerfiles=full_state.get("dockerfiles", {}),
                docker_compose=full_state.get("docker_compose"),
                nginx_conf=full_state.get("nginx_conf"),
                has_existing_dockerfiles=full_state.get("has_existing_dockerfiles", False),
                has_existing_compose=full_state.get("has_existing_compose", False),
                risks=full_state.get("risks", []),
                confidence=full_state.get("confidence", 0.0),
                hadolint_results=full_state.get("hadolint_results", {}),
                commands=full_state.get("commands", {}),
                token_usage=TokenUsage(**tracker.get_usage())
            )
            
            # Save to Supabase cache
            from db import supabase
            commit_sha = full_state.get("commit_sha", "unknown")
            if supabase and commit_sha != "unknown":
                for attempt in range(3):
                    try:
                        result_dict = response.model_dump() if hasattr(response, 'model_dump') else response.dict()
                        # Internal cache metadata used to support package-scoped cache reuse.
                        result_dict["_cache_package_path"] = req.package_path
                        supabase.table("analysis_cache").insert({
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
            yield f"event: complete\ndata: {json.dumps(final_dict)}\n\n"
            
        except Exception as e:
            yield f"event: error\ndata: {json.dumps({'detail': str(e)})}\n\n"

    # Decide auth vs cache BEFORE starting the streaming response. If we raise after
    # the stream begins, Starlette can crash with "response already started."
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
            return StreamingResponse(cached_event_generator(cached_payload), media_type="text/event-stream")
        except HTTPException as e:
            if e.status_code != 404:
                raise

    _require_auth(authorization)
    return StreamingResponse(live_event_generator(), media_type="text/event-stream")


@app.post("/feedback", response_model=AnalyzeResponse)
async def improve_with_feedback(req: FeedbackRequest, _auth: None = Depends(require_auth)):
    from graph.feedback import run_feedback_improvement

    # 1. Fetch existing cached analysis
    supabase, cached_result = _fetch_cached_analysis_or_404(req.repo_url, req.commit_sha, req.package_path)

    # 2. Regenerate artifacts guided by the feedback
    tracker = TokenTracker()
    try:
        improved = run_feedback_improvement(cached_result, req.feedback)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Feedback improvement failed: {e}")

    # 3. Build the response
    response = AnalyzeResponse(
        commit_sha=req.commit_sha,
        stack_summary=improved["stack_summary"],
        stack_tokens=improved.get("stack_tokens", []),
        services=improved["services"],
        dockerfiles=improved["dockerfiles"],
        docker_compose=improved.get("docker_compose"),
        nginx_conf=improved.get("nginx_conf"),
        has_existing_dockerfiles=improved["has_existing_dockerfiles"],
        has_existing_compose=improved["has_existing_compose"],
        risks=improved["risks"],
        confidence=improved["confidence"],
        hadolint_results=improved["hadolint_results"],
        commands=improved.get("commands", {}),
        token_usage=TokenUsage(**tracker.get_usage()),
    )

    # 4. Upsert the improved result back to cache
    result_dict = response.model_dump() if hasattr(response, "model_dump") else response.dict()
    result_dict["_cache_package_path"] = cached_result.get("_cache_package_path", ".")
    try:
        supabase.table("analysis_cache").upsert(
            {
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

    return response


@app.post("/feedback/stream")
async def improve_with_feedback_stream(req: FeedbackRequest, _auth: None = Depends(require_auth)):
    async def event_generator():
        from graph.feedback import feedback_graph, build_feedback_initial_state, format_feedback_result

        tracker = TokenTracker()

        try:
            supabase, cached_result = _fetch_cached_analysis_or_404(req.repo_url, req.commit_sha, req.package_path)
        except HTTPException as e:
            yield f"event: error\ndata: {json.dumps({'detail': e.detail})}\n\n"
            return

        initial_state = build_feedback_initial_state(cached_result, req.feedback)

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

            response = AnalyzeResponse(
                commit_sha=req.commit_sha,
                stack_summary=improved["stack_summary"],
                stack_tokens=improved.get("stack_tokens", []),
                services=improved["services"],
                dockerfiles=improved["dockerfiles"],
                docker_compose=improved.get("docker_compose"),
                nginx_conf=improved.get("nginx_conf"),
                has_existing_dockerfiles=improved["has_existing_dockerfiles"],
                has_existing_compose=improved["has_existing_compose"],
                risks=improved["risks"],
                confidence=improved["confidence"],
                hadolint_results=improved["hadolint_results"],
                commands=improved.get("commands", {}),
                token_usage=TokenUsage(**tracker.get_usage()),
            )

            result_dict = response.model_dump() if hasattr(response, "model_dump") else response.dict()
            result_dict["_cache_package_path"] = cached_result.get("_cache_package_path", ".")
            try:
                supabase.table("analysis_cache").upsert(
                    {
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
            yield f"event: complete\ndata: {json.dumps(final_dict)}\n\n"

        except Exception as e:
            yield f"event: error\ndata: {json.dumps({'detail': str(e)})}\n\n"

    return StreamingResponse(event_generator(), media_type="text/event-stream")

@app.get("/templates")
async def get_templates(active_only: bool = True):
    templates = list_templates(active_only=active_only)
    return {"templates": templates}


@app.post("/templates")
async def create_or_update_template(req: TemplateRequest, _auth: None = Depends(require_auth)):
    try:
        result = upsert_template(req.model_dump() if hasattr(req, 'model_dump') else req.dict())
        return result
    except RuntimeError as e:
        raise HTTPException(status_code=503, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.post("/templates/seed", response_model=TemplateSeedResponse)
async def seed_templates(_auth: None = Depends(require_auth)):
    result = seed_default_templates()
    return TemplateSeedResponse(**result)


@app.delete("/templates/{name}")
async def remove_template(name: str, _auth: None = Depends(require_auth)):
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
