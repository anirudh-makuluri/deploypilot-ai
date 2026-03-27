from graph.graph import graph
from graph.nodes.llm_config import TokenTracker

def test_tracker():
    tracker = TokenTracker()
    initial_state = {
        "repo_url": "https://github.com/fastapi/fastapi", 
        "max_files": 5, 
        "package_path": ".",
        "repo_scan": {"key_files": {"package.json": "{}"}}  # skip actual scan
    }
    
    # We will just invoke it, it might fail or succeed, but we only care about tokens
    try:
        result = graph.invoke(initial_state, config={"callbacks": [tracker]})
    except Exception as e:
        print("Graph failed:", e)
        
    usage = tracker.get_usage()
    print("USAGE RECORDED:", usage)
    if usage["input_tokens"] == 0 and usage["output_tokens"] == 0:
        print("FAIL: Tracker counted 0 tokens.")
    else:
        print("PASS: Tracker successfully recorded tokens.")

if __name__ == "__main__":
    test_tracker()
