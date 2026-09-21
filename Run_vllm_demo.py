"""
run_vllm_demo.py
----------------
用真实的本地 vLLM 服务跑一遍 LangGraph Agent。

前提：
  - 已启动 vLLM api server
  - 端口默认是 127.0.0.1:8000
  - 模型服务名默认是 qwen2.5-coder-7b
"""

from Demo_db import build_demo_db
from Graph import build_graph
from Llm_client import VLLMClient


def main():
    db_path = str(build_demo_db())
    app = build_graph(llm_client=VLLMClient())

    initial_state = {
        "question": "各部门的平均人数是多少？",
        "db_path": db_path,
        "retry_count": 0,
        "max_retries": 3,
        "error_history": [],
    }

    final_state = None
    for step in app.stream(initial_state, stream_mode="values"):
        final_state = step

    print("最终状态:", final_state.get("status"))
    print("最终 SQL:", final_state.get("current_sql"))
    print("最终回答:", final_state.get("final_answer"))
    print("是否转人工:", final_state.get("needs_human"))


if __name__ == "__main__":
    main()
