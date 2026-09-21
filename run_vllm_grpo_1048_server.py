from __future__ import annotations

import os
import runpy
import sys

import vllm.platforms as platforms
from vllm.platforms.cuda import NonNvmlCudaPlatform


def main() -> None:
    platforms.current_platform = NonNvmlCudaPlatform()
    os.environ.setdefault("CUDA_VISIBLE_DEVICES", "2")
    os.environ.setdefault("VLLM_USE_FLASHINFER_SAMPLER", "0")

    sys.argv = [
        "vllm.entrypoints.openai.api_server",
        "--model",
        "/data1/tiantian/VeriSQL-Agent/models/Qwen2.5-Coder-7B-Instruct-BIRD-GRPO-1048",
        "--served-model-name",
        "qwen2.5-coder-7b-grpo-1048",
        "--host",
        "127.0.0.1",
        "--port",
        "8002",
        "--dtype",
        "bfloat16",
        "--max-model-len",
        "4096",
        "--gpu-memory-utilization",
        "0.80",
        "--enforce-eager",
    ]
    runpy.run_module("vllm.entrypoints.openai.api_server", run_name="__main__")


if __name__ == "__main__":
    main()
