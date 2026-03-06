from fastapi import FastAPI
from pydantic import BaseModel
from typing import Optional, List
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

class AnalyzeResponse(BaseModel):
    stack_summary: str
    services_needed: List[str]
    entry_port: int
    dockerfile: Optional[str] = None
    docker_compose: Optional[str] = None
    nginx_conf: Optional[str] = None
    risks: List[str]
    confidence: float

@app.post("/analyze", response_model=AnalyzeResponse)
async def analyze_repo(req: AnalyzeRequest):
    initial_state = {
        "repo_url": req.repo_url,
        "github_token": req.github_token,
        "max_files": req.max_files,
    }
    result = graph.invoke(initial_state)
    
    return AnalyzeResponse(
        stack_summary=result.get("detected_stack", "Unknown"),
        services_needed=result.get("services_needed", []),
        entry_port=result.get("entry_port", 3000),
        dockerfile=result.get("dockerfile"),
        docker_compose=result.get("docker_compose"),
        nginx_conf=result.get("nginx_conf"),
        risks=result.get("risks", []),
        confidence=result.get("confidence", 0.0)
    )

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", 8080)))
