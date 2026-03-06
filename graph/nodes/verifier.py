from typing import Dict, Any, List
from pydantic import BaseModel, Field, model_validator
import json
import re
from .llm_config import llm_verifier


class VerifierOutput(BaseModel):
    confidence: float = Field(description="Confidence score from 0.0 to 1.0 on the quality of all generated artifacts")
    risks: List[str] = Field(description="List of identified risks, issues, or warnings about the generated artifacts")
    
    @model_validator(mode="before")
    @classmethod
    def coerce_risks_to_list(cls, data):
        """Handle LLM returning risks as a single string instead of a list."""
        if isinstance(data, dict) and isinstance(data.get("risks"), str):
            raw = data["risks"].strip()
            # Split on: bullet points, numbered items, or quoted-newline separators like "\n"
            items = re.split(r'"\s*\n\s*"|\\n|\\"\s*\\n\s*\\"|(?:\n\s*[-*•]\s*)|(?:\n\s*\d+\.\s*)', raw)
            # Clean up and filter empty strings
            data["risks"] = [item.strip().strip('"').strip("'") for item in items if item.strip()]
        return data


def verifier_node(state: Dict[str, Any]) -> Dict[str, Any]:
    """Review all generated artifacts and assign confidence + risks."""
    scan = state.get("repo_scan", {})
    services = state.get("services", [])
    dockerfiles = state.get("dockerfiles", {})
    docker_compose = state.get("docker_compose", "")
    nginx_conf = state.get("nginx_conf", "")

    prompt = f"""
You are a senior DevOps reviewer. Review ALL the generated deployment artifacts below for a repository and assess their quality.

REPO INFO:
- Stack: {state.get('detected_stack', 'unknown')}
- Services: {json.dumps(services, indent=2)}
- Key files found: {list(scan.get('key_files', {}).keys())}
- Directories: {scan.get('dirs', [])}

GENERATED DOCKERFILES:
{json.dumps(dockerfiles, indent=2)}

GENERATED DOCKER-COMPOSE:
{docker_compose}

GENERATED NGINX CONFIG:
{nginx_conf}

Review for:
1. Port consistency — do Dockerfiles EXPOSE the same ports referenced in compose and nginx?
2. Build context accuracy — do compose build contexts match the actual repo directory structure?
3. Security — non-root users, no hardcoded secrets, proper headers in nginx?
4. Completeness — are all services accounted for? Are missing env vars flagged?
5. Best practices — multi-stage builds, health checks, proper caching layers?

Provide a confidence score (0.0 to 1.0) and a list of specific risks or issues found.
Each risk must be a separate string in the list. Do NOT combine multiple risks into one string.
If everything looks good, confidence should be high (0.85+) with an empty or minimal risks list.
"""

    structured_llm = llm_verifier.with_structured_output(VerifierOutput)
    
    try:
        result = structured_llm.invoke(prompt)
        state["confidence"] = result.confidence
        state["risks"] = result.risks
    except Exception as e:
        state["confidence"] = 0.5
        state["risks"] = [f"Verifier failed to run: {e}"]
    
    return state
