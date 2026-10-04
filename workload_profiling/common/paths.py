"""路径只在此处定义；所有默认路径相对代码位置，不依赖启动目录。"""
from pathlib import Path

PACKAGE = Path(__file__).resolve().parents[1]
ROOT = PACKAGE.parent
DATA = PACKAGE / "data"
PROCESSED = DATA / "processed"
ARTIFACTS = DATA / "artifacts"
RESULTS = PACKAGE / "results"
CACHE = PACKAGE / "cache"
TOKENIZER_DIR = DATA / "tokenizer" / "Qwen3-8B"
POLICY_CONFIG = PACKAGE / "config" / "runtime_policy.json"
DEFAULT_SOURCE = ROOT / "prompt数据" / "prompt回答数据包_3168条" / "prompt回答.jsonl"


def stage_results(stage: str, smoke: bool = False) -> Path:
    if stage not in {"stage1", "stage2", "stage2_1"}:
        raise ValueError(f"Unknown stage: {stage}")
    path = RESULTS / stage
    return CACHE / "smoke" / stage / "results" if smoke else path


def processed_dir(smoke: bool = False, *, stage="stage1") -> Path:
    if stage not in {"stage1", "stage2", "stage2_1"}:
        raise ValueError(f"Unknown stage: {stage}")
    return CACHE / "smoke" / stage / "data" if smoke else PROCESSED


def relative(path) -> str:
    path = Path(path).resolve()
    return path.relative_to(ROOT).as_posix() if path.is_relative_to(ROOT) else str(path)
