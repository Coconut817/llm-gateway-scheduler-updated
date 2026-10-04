"""从 stdin/JSONL 逐行接收请求，立即写出一行结果；可注入发送适配器。"""
import argparse
from contextlib import ExitStack
import importlib
import json
from pathlib import Path
import sys

from ..common.paths import ARTIFACTS, stage_results
from ..common.tokenizer import load_tokenizer
from .output_heavy_policy import OutputHeavyPolicy, validate_threshold
from .percentile_reference import PercentileReference
from .stream import RequestProcessor, RequestProcessingError


def import_sender(spec):
    module, separator, attribute = spec.partition(":")
    if not separator or not module or not attribute:
        raise ValueError("--sender must be importable.module:callable")
    sender = getattr(importlib.import_module(module), attribute)
    if not callable(sender):
        raise TypeError("Imported sender must be callable")
    return sender


def threshold_argument(value):
    try:
        return validate_threshold(float(value))
    except (ValueError, TypeError) as error:
        raise argparse.ArgumentTypeError(str(error)) from None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, help="Omit to read stdin; each line is ONE current request")
    parser.add_argument("--output", type=Path, help="Omit to write stdout, flush after EVERY request")
    parser.add_argument("--sender", help="User-supplied synchronous adapter, importable.module:callable")
    parser.add_argument("--lengths-only", action="store_true", help="Do not load frozen Output-Heavy policy")
    parser.add_argument("--threshold", type=threshold_argument, help="Manual override for this process, strictly between 0 and 1")
    parser.add_argument("--on-error", choices=["raise", "yield"], default="raise")
    parser.add_argument("--include-response", action="store_true", help="Explicitly include response text in results")
    args = parser.parse_args()
    if args.lengths_only and args.threshold is not None:
        parser.error("--threshold needs an Output-Heavy policy; omit --lengths-only")
    if args.input and args.output and args.input.resolve() == args.output.resolve():
        parser.error("Input and output files must be different")
    tokenizer, _ = load_tokenizer()
    policy = None
    if not args.lengths_only:
        results = stage_results("stage2_1")
        reference = PercentileReference.load(ARTIFACTS / "output_percentile_reference.parquet",
                                             results / "percentile_reference_metadata.json")
        policy = OutputHeavyPolicy(reference, config_path=results / "runtime_policy_config.json")
        if args.threshold is not None:
            policy.set_threshold(args.threshold)
    processor = RequestProcessor(tokenizer, output_policy=policy,
                                 sender=import_sender(args.sender) if args.sender else None,
                                 include_response=args.include_response)
    failures = 0
    with ExitStack() as stack:
        source = stack.enter_context(args.input.open(encoding="utf-8-sig")) if args.input else sys.stdin
        if args.output:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            target = stack.enter_context(args.output.open("w", encoding="utf-8"))
        else:
            target = sys.stdout
        # 不读 list(source)，也不加载整个历史数据集。处理一行才读取下一行。
        for index, line in enumerate(source):
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                if args.on_error == "raise":
                    raise ValueError(f"Invalid JSON at stream_index={index}") from None
                result = {"tokenization_status": "error", "error_stage": "input", "error_code": "invalid_json"}
            else:
                try:
                    result = next(processor.process_stream([record], on_error=args.on_error))
                except RequestProcessingError as error:
                    raise SystemExit(f"stream_index={index}: {error}") from None
            result["stream_index"] = index
            failures += result["tokenization_status"] == "error"
            target.write(json.dumps(result, ensure_ascii=False, allow_nan=False) + "\n")
            target.flush()
    if failures:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
