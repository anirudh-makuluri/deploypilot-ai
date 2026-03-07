from fastapi import FastAPI, HTTPException
from fastapi.responses import StreamingResponse
import json
from pydantic import BaseModel
from typing import Optional, List, Dict
from graph.graph import graph
from graph.nodes.llm_config import TokenTracker
from fastapi.middleware.cors import CORSMiddleware
import os

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

class TokenUsage(BaseModel):
    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0

class AnalyzeResponse(BaseModel):
    stack_summary: str
    services: List[Dict]
    dockerfiles: Dict[str, str]
    docker_compose: Optional[str] = None
    nginx_conf: Optional[str] = None
    has_existing_dockerfiles: bool = False
    has_existing_compose: bool = False
    risks: List[str]
    confidence: float
    hadolint_results: Dict[str, str] = {}
    token_usage: TokenUsage = TokenUsage()

@app.post("/analyze", response_model=AnalyzeResponse)
async def analyze_repo(req: AnalyzeRequest):
    tracker = TokenTracker()
    
    initial_state = {
        "repo_url": req.repo_url,
        "github_token": req.github_token,
        "max_files": req.max_files,
        "package_path": req.package_path,
    }
    result = graph.invoke(initial_state, config={"callbacks": [tracker]})
    
    # Check for errors from scanner or planner
    if "error" in result:
        raise HTTPException(status_code=400, detail=result["error"])
        
    if "cached_response" in result:
        print(f"Returning cached analysis for {req.repo_url}")
        return AnalyzeResponse(**result["cached_response"])
    
    response = AnalyzeResponse(
        stack_summary=result.get("detected_stack", "Unknown"),
        services=result.get("services", []),
        dockerfiles=result.get("dockerfiles", {}),
        docker_compose=result.get("docker_compose"),
        nginx_conf=result.get("nginx_conf"),
        has_existing_dockerfiles=result.get("has_existing_dockerfiles", False),
        has_existing_compose=result.get("has_existing_compose", False),
        risks=result.get("risks", []),
        confidence=result.get("confidence", 0.0),
        hadolint_results=result.get("hadolint_results", {}),
        token_usage=TokenUsage(**tracker.get_usage())
    )
    
    # Save to Supabase cache
    from db import supabase
    commit_sha = result.get("commit_sha", "unknown")
    if supabase and commit_sha != "unknown":
        for attempt in range(3):
            try:
                result_dict = response.model_dump() if hasattr(response, 'model_dump') else response.dict()
                supabase.table("analysis_cache").insert({
                    "repo_url": req.repo_url,
                    "commit_sha": commit_sha,
                    "result": result_dict
                }).execute()
                print(f"Cached new analysis for {req.repo_url} at {commit_sha}")
                break
            except Exception as e:
                print(f"Failed to cache result in Supabase (attempt {attempt + 1}/3): {e}")
                if attempt < 2:
                    import time
                    time.sleep(1)

    return response

@app.post("/analyze/stream")
async def analyze_repo_stream(req: AnalyzeRequest):
    async def event_generator():
        tracker = TokenTracker()
        
        initial_state = {
            "repo_url": req.repo_url,
            "github_token": req.github_token,
            "max_files": req.max_files,
            "package_path": req.package_path,
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
                            cached["token_usage"] = TokenUsage(**tracker.get_usage()).dict()
                            
                        # Ensure fields conform
                        yield f"event: complete\ndata: {json.dumps(cached)}\n\n"
                        return
            
            response = AnalyzeResponse(
                stack_summary=full_state.get("detected_stack", "Unknown"),
                services=full_state.get("services", []),
                dockerfiles=full_state.get("dockerfiles", {}),
                docker_compose=full_state.get("docker_compose"),
                nginx_conf=full_state.get("nginx_conf"),
                has_existing_dockerfiles=full_state.get("has_existing_dockerfiles", False),
                has_existing_compose=full_state.get("has_existing_compose", False),
                risks=full_state.get("risks", []),
                confidence=full_state.get("confidence", 0.0),
                hadolint_results=full_state.get("hadolint_results", {}),
                token_usage=TokenUsage(**tracker.get_usage())
            )
            
            # Save to Supabase cache
            from db import supabase
            commit_sha = full_state.get("commit_sha", "unknown")
            if supabase and commit_sha != "unknown":
                for attempt in range(3):
                    try:
                        result_dict = response.model_dump() if hasattr(response, 'model_dump') else response.dict()
                        supabase.table("analysis_cache").insert({
                            "repo_url": req.repo_url,
                            "commit_sha": commit_sha,
                            "result": result_dict
                        }).execute()
                        print(f"Cached new analysis for {req.repo_url} at {commit_sha}")
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

    return StreamingResponse(event_generator(), media_type="text/event-stream")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", 8080)))
