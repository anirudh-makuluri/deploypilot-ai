from langchain_aws import ChatBedrock
import os
from dotenv import load_dotenv
from graph.llm_retry import RetryConfig

load_dotenv()

BEDROCK_MODEL_ID = os.getenv("BEDROCK_MODEL_ID", "anthropic.claude-3-haiku-20240307-v1:0")

_LLM_TEMPERATURES = {
    "llm_planner": 0.1,
    "llm_docker": 0.0,
    "llm_compose": 0.0,
    "llm_nginx": 0.0,
    "llm_verifier": 0.0,
    "llm_coordinator": 0.2,
}

def __getattr__(name: str):
    if name in _LLM_TEMPERATURES:
        instance = ChatBedrock(
            model_id=BEDROCK_MODEL_ID,
            model_kwargs={"temperature": _LLM_TEMPERATURES[name], "max_tokens": 4096}
        )
        globals()[name] = instance
        return instance
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


RETRY_CONFIGS = {
        "planner": RetryConfig(max_attempts=3, timeout_seconds=90.0, fallback_after_attempt=2),
        "docker": RetryConfig(max_attempts=3, timeout_seconds=120.0, fallback_after_attempt=2),
        "compose": RetryConfig(max_attempts=3, timeout_seconds=120.0, fallback_after_attempt=2),
        "nginx": RetryConfig(max_attempts=3, timeout_seconds=90.0, fallback_after_attempt=2),
        "verifier": RetryConfig(max_attempts=3, timeout_seconds=90.0, fallback_after_attempt=2),
    "coordinator": RetryConfig(max_attempts=3, timeout_seconds=90.0, fallback_after_attempt=2),
}


FALLBACK_PROMPTS = {
        "planner": """
You are analyzing repository metadata for deployability.

Return ONLY raw JSON with this exact schema and no markdown:
{
    "is_deployable": boolean,
    "error_reason": "string",
    "stack_tokens": ["string"],
    "services": [
        {
            "name": "string",
            "build_context": "string",
            "port": integer,
            "dockerfile_path": "string"
        }
    ],
    "has_existing_dockerfiles": boolean,
    "has_existing_compose": boolean
}

If unsure, set is_deployable=false and explain in error_reason.
""".strip(),
        "docker": """
Generate ONLY a valid production Dockerfile.

Rules:
- Use multi-stage build when applicable.
- Use slim/alpine base images.
- Run as non-root user.
- Include EXPOSE and HEALTHCHECK.
- Output raw Dockerfile only.
""".strip(),
        "compose": """
Generate ONLY a valid docker-compose YAML.

Rules:
- Include all application services.
- Add required dependency services when implied.
- Use env vars for credentials placeholders.
- Output raw YAML only.
""".strip(),
        "nginx": """
Generate ONLY a valid nginx.conf for reverse proxy.

Rules:
- Listen on port 80.
- Add upstream routing for listed services.
- Include security and proxy headers.
- Output raw nginx config only.
""".strip(),
        "verifier": """
Return ONLY JSON matching:
{
    "confidence": number,
    "risks": ["string"]
}

Every risk must be a separate list item.
""".strip(),
        "coordinator": """
Return ONLY raw JSON with this exact schema and no markdown:
{
    "change_plan": [
        {
            "artifact_type": "dockerfile" | "compose" | "nginx",
            "service_name": "string",
            "should_change": boolean,
            "instructions": "string"
        }
    ],
    "summary": "string"
}

Include one entry per Dockerfile service (using its service_name), plus one entry each for compose and nginx.
For artifact_type compose and nginx, service_name must be an empty string.
Set should_change=true only for artifacts that need changes based on feedback and findings.
""".strip(),
}


from langchain_core.callbacks import BaseCallbackHandler

class TokenTracker(BaseCallbackHandler):
    """Callback handler that tracks token usage across all LLM calls."""
    
    def __init__(self):
        self.input_tokens = 0
        self.output_tokens = 0
        self.total_tokens = 0
    
    def on_llm_end(self, response, **kwargs):
        # Bedrock puts usage in response.llm_output, not generation_info
        usage = (response.llm_output or {}).get("usage", {})
        self.input_tokens += usage.get("prompt_tokens", 0)
        self.output_tokens += usage.get("completion_tokens", 0)
        self.total_tokens += usage.get("total_tokens", 0)
    
    def get_usage(self):
        return {
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "total_tokens": self.total_tokens or (self.input_tokens + self.output_tokens)
        }


import re

def strip_markdown_wrapper(content: str, lang: str = "docker") -> str:
    """Strip markdown code block wrappers and LLM preamble from output."""
    content = content.strip()
    
    # If the LLM wrapped content in a markdown code block, extract it
    code_block_pattern = rf"```(?:{lang}|dockerfile|yaml|nginx|conf)?\s*\n(.*?)```"
    match = re.search(code_block_pattern, content, re.DOTALL | re.IGNORECASE)
    if match:
        content = match.group(1).strip()
        return content
    
    # Strip leading backticks
    content = content.strip("`").strip()
    if content.startswith(f"{lang}\n"):
        content = content[len(lang) + 1:]
    
    # Strip common LLM preambles like "IMPROVED Dockerfile:\n\n"
    preamble_pattern = r"^(?:IMPROVED|REVIEWED|GENERATED|UPDATED|HERE(?:'S| IS))[\s\S]*?:\s*\n+"
    content = re.sub(preamble_pattern, "", content, count=1, flags=re.IGNORECASE)
    
    return content.strip()
