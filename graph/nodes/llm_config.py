from langchain_aws import ChatBedrock
import os
from dotenv import load_dotenv

load_dotenv()

BEDROCK_MODEL_ID = os.getenv("BEDROCK_MODEL_ID", "anthropic.claude-3-haiku-20240307-v1:0")

llm_planner = ChatBedrock(
    model_id=BEDROCK_MODEL_ID,
    model_kwargs={"temperature": 0.1, "max_tokens": 4096}
)
llm_docker = ChatBedrock(
    model_id=BEDROCK_MODEL_ID,
    model_kwargs={"temperature": 0.0, "max_tokens": 4096}
)
llm_compose = ChatBedrock(
    model_id=BEDROCK_MODEL_ID,
    model_kwargs={"temperature": 0.0, "max_tokens": 4096}
)
llm_nginx = ChatBedrock(
    model_id=BEDROCK_MODEL_ID,
    model_kwargs={"temperature": 0.0, "max_tokens": 4096}
)
llm_verifier = ChatBedrock(
    model_id=BEDROCK_MODEL_ID,
    model_kwargs={"temperature": 0.0, "max_tokens": 4096}
)


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
