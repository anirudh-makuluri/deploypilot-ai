from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from typing import Optional, List, Dict
from graph.graph import graph
from fastapi.middleware.cors import CORSMiddleware
import os

app = FastAPI(title="DeployPilot-AI Repo Analyzer")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

class AnalyzeRequest(BaseModel):
    repo_url: str
    github_token: str
    max_files: Optional[int] = 50
    package_path: str = "."

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

@app.post("/analyze", response_model=AnalyzeResponse)
async def analyze_repo(req: AnalyzeRequest):
    initial_state = {
        "repo_url": req.repo_url,
        "github_token": req.github_token,
        "max_files": req.max_files,
        "package_path": req.package_path,
    }
    result = graph.invoke(initial_state)
    
    # Check for errors from scanner or planner
    if "error" in result:
        raise HTTPException(status_code=400, detail=result["error"])
    
    return AnalyzeResponse(
        stack_summary=result.get("detected_stack", "Unknown"),
        services=result.get("services", []),
        dockerfiles=result.get("dockerfiles", {}),
        docker_compose=result.get("docker_compose"),
        nginx_conf=result.get("nginx_conf"),
        has_existing_dockerfiles=result.get("has_existing_dockerfiles", False),
        has_existing_compose=result.get("has_existing_compose", False),
        risks=result.get("risks", []),
        confidence=result.get("confidence", 0.0)
    )

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", 8080)))
