"""Python API example using portable token lengths; no tokenizer is loaded."""
from dataclasses import replace
import json
from pathlib import Path

from workload_profiling.baseline import load_config
from workload_profiling.baseline.run import execute
from workload_profiling.common.paths import RESULTS


def main():
    source = Path(__file__).with_name("length_requests.jsonl")
    config = replace(load_config(), batch_size=2, batch_wait_ms=3)
    result, _ = execute(config, source=source, source_format="lengths",
                        output=RESULTS / "baseline_example")
    print(json.dumps(result.summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
