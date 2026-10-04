"""Stage 1 的描述统计、图和报告；与执行入口分离。"""
import json

def describe_lengths(frame) -> dict:
    import numpy as np
    from scipy.stats import pearsonr, spearmanr

    # 统计对象仅为成功计算长度的 request；分位数统一使用 linear 插值，仅作描述统计。
    distributions = {}
    for column in ("input_tokens", "output_tokens", "total_tokens"):
        values = frame[column].to_numpy()
        distributions[column] = {
            "count": int(len(values)), "mean": float(np.mean(values)),
            "median": float(np.median(values)),
            **{name: float(np.quantile(values, q, method="linear"))
               for name, q in (("P90", .90), ("P95", .95), ("P99", .99))},
            "max": int(np.max(values)),
        }
    x, y = frame["input_tokens"].to_numpy(), frame["output_tokens"].to_numpy()
    # 样本不足或任一侧为常量时，相关系数没有定义，保存为 None 而不是误填 0。
    defined = len(frame) >= 2 and len(set(x)) > 1 and len(set(y)) > 1
    correlations = {
        "pearson": float(pearsonr(x, y).statistic) if defined else None,
        "spearman": float(spearmanr(x, y).statistic) if defined else None,
        "count": len(frame), "status": "ok" if defined else "insufficient_or_constant_data",
    }
    return {"distributions": distributions, "correlations": correlations,
            "quantile_method": "linear", "population": "successfully tokenized expanded requests"}


def make_plots(frame, results):
    import matplotlib
    # 使用无窗口的绘图后端，命令行运行时可直接保存 PNG。
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import numpy as np

    plt.rcParams.update({"figure.dpi": 140, "font.size": 11, "axes.spines.top": False,
                         "axes.spines.right": False})
    for prefix in ("input", "output"):
        values = frame[prefix + "_tokens"].to_numpy()
        # 对数横轴无法表示 0；直方图只画正值，零长度数量单独标注，不从数据中删除。
        positive = values[values > 0]
        fig, ax = plt.subplots(figsize=(8, 4.8), layout="constrained")
        if len(positive):
            lower, upper = max(.5, positive.min() * .9), max(positive.max() * 1.1, positive.min() * 1.2)
            ax.hist(positive, bins=np.geomspace(lower, upper, 51), color="#356a99", edgecolor="white", linewidth=.4)
            ax.set_xscale("log")
        ax.set(title=f"{prefix.title()} token length distribution (n={len(values):,})",
               xlabel=f"{prefix.title()} tokens (log scale)", ylabel="Request count")
        ax.text(.98, .95, f"Zero-token requests: {(values == 0).sum()}",
                transform=ax.transAxes, va="top", ha="right", fontsize=9)
        ax.grid(axis="y", alpha=.2)
        fig.savefig(results / f"{prefix}_length_histogram.png")
        plt.close(fig)

        # 经验 CCDF：从大到小累计频数，得到每个观测长度 x 对应的 P(length >= x)。
        # 重复长度先合并计数，避免把同一个长度的概率画成多个不同值。
        unique, counts = np.unique(values, return_counts=True)
        survival = np.cumsum(counts[::-1])[::-1] / len(values)
        fig, ax = plt.subplots(figsize=(8, 4.8), layout="constrained")
        # +1 只用于对数坐标展示，原始 token 长度及统计值保持不变。
        ax.step(unique + 1, survival, where="pre", color="#356a99", linewidth=1.8)
        ax.set_xscale("log")
        ax.set_yscale("log")
        ax.set(title=f"{prefix.title()} token length CCDF (n={len(values):,})",
               xlabel=f"{prefix.title()} tokens + 1 (log scale)", ylabel="P(length >= x)")
        ax.grid(which="both", alpha=.2)
        fig.savefig(results / f"{prefix}_length_ccdf.png")
        plt.close(fig)

    fig, ax = plt.subplots(figsize=(8, 5.4), layout="constrained")
    # 散点图使用全部样本，降低透明度以展示重叠；同样只在绘图坐标上加 1。
    ax.scatter(frame["input_tokens"] + 1, frame["output_tokens"] + 1,
               s=9, alpha=.28, color="#356a99", edgecolors="none", rasterized=True)
    ax.set_xscale("log")
    ax.set_yscale("log")
    ax.set(title=f"Input vs. output token lengths (n={len(frame):,})",
           xlabel="Input tokens + 1 (log scale)", ylabel="Output tokens + 1 (log scale)")
    ax.grid(which="both", alpha=.2)
    fig.savefig(results / "input_vs_output_scatter.png")
    plt.close(fig)


def make_report(metadata, stats, results):
    # 报告读取同一次运行的 metadata/统计，集中说明计数、异常原因和 tokenizer 口径。
    audit = metadata["audit"]
    counts = audit["counts"]
    n = metadata["successful_requests"]
    c = counts["processed_conversations"]
    tokenizer = metadata["tokenizer"]
    distributions = stats["distributions"]
    correlation = stats["correlations"]
    # 这里只检查下一阶段的数据条件，既不执行下一阶段，也不将分位数转为标签。
    ready = metadata["mode"] == "full" and n > 0 and counts.get("tokenization_failures", 0) == 0 and counts.get("invalid_conversations", 0) == 0
    lines = [
        "# Stage 1：Request 长度数据集报告", "",
        f"生成时间：{metadata['created_at']}；运行模式：`{metadata['mode']}`。", "",
        "## 数据来源与展开结果", "",
        f"- 原始 JSONL conversation/内部行单元数：**{metadata['source_row_count']:,}**。",
        f"- 本次处理 conversation 数：**{c:,}**；成功展开 request 数：**{counts['expanded_requests']:,}**；成功计算长度并保存数：**{n:,}**。",
        f"- 每个已处理 conversation 平均成功产生 **{n / c:.6f}** 个 request（分母包含零样本 conversation）。",
        f"- assistant 候选数：{counts['assistant_candidates']:,}；跳过 request 数：**{counts.get('skipped_requests', 0):,}**；无样本 conversation：{counts.get('conversations_without_successful_requests', 0):,}。",
        f"- 无效 conversation：{counts.get('invalid_conversations', 0)}；tokenization 失败：{counts.get('tokenization_failures', 0)}。", "",
        f"成功样本来源：历史 assistant {metadata['response_origin_counts'].get('history', 0):,} 个；顶层 response {metadata['response_origin_counts'].get('top_level_response', 0):,} 个。", "",
        "真实 schema 为每行 `prompt.messages + response`。将顶层 `response` 接为最后一条 assistant，",
        "再枚举所有 assistant；保留当前 assistant 之前的完整历史，前缀必须已有 user 且最后角色是 user 或 tool。",
        "`conversation_id` 为从 0 开始的原始物理行号；`request_index` 为包含跳过候选的 assistant 序号，因此可以有间隙。",
        "`source_message_index` 为补上顶层 response 后的消息位置；`response_origin` 区分 history/top_level_response。", "",
        "说明文件将行称为非流式请求/回答配对，不能据此证明不同行之间互不重叠或代表独立业务会话。",
        "本阶段遵照按原始行建立内部 conversation 的约定，不虚构业务 request_id，不跨行去重。",
        f"{metadata['source_rows_ending_in_assistant']} 行原始历史以 assistant 结尾，顶层 response 是连续 assistant；其与最近 user 的语义配对不明确，按固定规则跳过。", "",
        "## 异常、跳过与保留", "",
        "跳过原因以 request 为单位，逐条定位见 `skipped_events.jsonl`：", "",
        "| 原因 | 数量 |", "|---|---:|",
    ]
    lines += [f"| {reason} | {number} |" for reason, number in audit["skipped_request_reasons"].items()] or ["| 无 | 0 |"]
    lines += ["", "结构观察次数（按消息/相邻消息统计，可重叠；不等于额外丢弃样本）：", "",
              "| 观察项 | 次数 |", "|---|---:|"]
    lines += [f"| {reason} | {number} |" for reason, number in audit["observations"].items()] or ["| 无 | 0 |"]
    lines += [
        "", "连续 user 全部保留。空/null assistant 的工具调用保留在后续历史，但不生成空文本 output 样本；",
        "连续 assistant 不生成当前样本，也不删除已存在的历史。末尾没有 assistant 时只记录未回答尾部，不伪造 output。",
        "list content 仅接受真实观察到的 type=text 块，按原序无分隔拼接；未知非文本块拒绝处理，不静默丢失。",
        "工具 schema、tool_calls、tool_call_id、reasoning_content 在规范化消息中保留；异常历史会使依赖它的后续样本被跳过。", "",
        "## Tokenizer 与固定配置", "",
        f"Tokenizer：`{tokenizer['tokenizer_id']}`；固定 revision：`{tokenizer['revision']}`。",
        f"加载类：`{tokenizer['tokenizer_class']}`，is_fast={tokenizer['is_fast']}。",
        "仅下载显式白名单中的 tokenizer/config/chat template 文件，未下载模型权重。",
        "用途为 **offline length construction tokenizer**，这些长度不声称等于原生产模型的真实 token 数。", "",
        "Input 使用原版 Qwen chat template，固定参数：", "", "```json",
        json.dumps(tokenizer["input_chat_template_parameters"], ensure_ascii=False, indent=2), "```", "",
        "每行原始 `prompt.tools` 原样传入；存在时也传入 `tool_choice`、`parallel_tool_calls`。",
        "这些是源行级配置，统一用于该行展开出的请求；源数据不提供每个历史回合独立的 tools 配置。",
        "后两项为 API 控制，官方模板不渲染它们；tool_call_id 等字段是否进入模板由官方模板决定。",
        "官方模板按自身规则渲染 assistant 历史 reasoning，早于最后 user 的独立 reasoning 不会全部进入序列。",
        "未修改模板来人为补加不渲染的字段。`enable_thinking=False` 固定了新回答的 generation prompt，不更改历史文本。", "",
        "Output 单独计算 `len(tokenizer.encode(response_text, add_special_tokens=False, truncation=False))`；",
        "只计当前 assistant content 文本，不加角色标记/EOS，不拼入独立 reasoning_content 或结构化 tool_calls。",
        f"其中 {metadata['response_has_tool_calls_count']} 个成功样本的 assistant 同时包含 tool_calls，已用 `response_has_tool_calls` 标记；其 output 是文本部分长度。",
        "Input/Output 使用同一 tokenizer；不截断、不填充；`total_tokens = input_tokens + output_tokens`。",
        f"Tokenizer 配置的 model_max_length={tokenizer['model_max_length']:,}；有 {metadata['input_exceeds_tokenizer_model_max_length_count']} 个 input 超过该值，仍完整计数。本阶段不运行模型，不将其作为无效或截断样本。",
        "`message_count` 及各角色计数只统计 input context，不含当前 output，也不把 tools schema 算作消息。",
        "全部保存样本的 `tokenization_status=ok`；失败不填 0 或估计长度，而单独写入审计。",
        "完整参数、源文件 SHA256、tokenizer/template SHA256 与依赖版本见 `stage1_metadata.json`。", "",
        "## 长度描述统计", "",
        "分位数使用 linear 插值，P95 仅为描述统计。", "",
        "| 指标 | input_tokens | output_tokens | total_tokens |", "|---|---:|---:|---:|",
    ]
    for key in ("count", "mean", "median", "P90", "P95", "P99", "max"):
        values = [distributions[column][key] for column in ("input_tokens", "output_tokens", "total_tokens")]
        lines.append("| " + key + " | " + " | ".join(f"{v:,.3f}" if key not in {"count", "max"} else f"{v:,}" for v in values) + " |")
    def format_corr(value):
        return "未定义（样本不足或常量）" if value is None else f"{value:.6f}"
    lines += ["", f"Input/Output Pearson：**{format_corr(correlation['pearson'])}**；",
              f"Spearman：**{format_corr(correlation['spearman'])}**，使用全部 {n:,} 个成功样本。", "",
              "同一 conversation 的 request 存在历史依赖，因此相关系数这里只作描述，不报告独立样本显著性推断。", "",
              "## 分布观察", ""]
    for column in ("input_tokens", "output_tokens"):
        d = distributions[column]
        if d["median"] > 0:
            lines.append(f"- {column}：均值/中位数={d['mean'] / d['median']:.3f}；P99/中位数={d['P99'] / d['median']:.3f}；最大值/中位数={d['max'] / d['median']:.3f}。")
    lines += ["", "长尾观察以这些描述统计和经验分布图为依据；本报告不作幂律/EVT 拟合，也不建立任何阈值或标签。", "",
              "![Input histogram](input_length_histogram.png)", "![Output histogram](output_length_histogram.png)",
              "![Input CCDF](input_length_ccdf.png)", "![Output CCDF](output_length_ccdf.png)",
              "![Input vs output](input_vs_output_scatter.png)", "",
              "直方图使用 token 的对数横轴；CCDF 为 P(length >= x)，横轴使用 length+1，纵轴使用对数；scatter 展示全部样本，两个横纵坐标均使用 length+1。", "",
              "## 下一阶段的数据条件", "",
              ("**已具备进入下一阶段 Tail Detection 的长度数据条件。**" if ready else "**本次尚不作为全量 Tail Detection 数据验收。**"),
              "数据具有可追溯 conversation/request 键、统一 tokenizer 的 input/output/total 长度、异常审计和基础描述统计。",
              "尾部观察对象是这批展开后的文本长度代理；含工具调用 output 的结构化负载不包含在 output_tokens 内。",
              "如后续训练/评估，必须先按 conversation 划分，再展开或使用 request；若确认存在跨行重叠，还须先处理关联，防止泄漏。",
              "本阶段没有训练/验证/测试划分，没有 Heavy 标签、P95 阈值、EVT/POT、分类器、Type-Heavy 或路由/调度实现。", "",
              "## 复现与来源", "", "```powershell", "$py = '.\\.venv\\Scripts\\python.exe'",
              "& $py -m workload_profiling.stages.stage1.run --smoke", "& $py -m workload_profiling.stages.stage1.run", "```", "",
              "Smoke 使用少量但覆盖真实 schema 的 conversation，临时输出到 cache/smoke/stage1。首次下载后，固定本地 tokenizer 可离线复跑。",
              "默认 Parquet 不保存完整 Prompt/Response；通过源文件、conversation_id、request_index 和 reconstruct_request 回溯。", "",
              "官方参考：[Qwen tokenizer 配置与模板](https://huggingface.co/Qwen/Qwen3-8B/blob/" + tokenizer['revision'] + "/tokenizer_config.json)、",
              "[Transformers chat templating](https://huggingface.co/docs/transformers/main/en/chat_templating)。", ""]
    # 长尾观察来自人工核验记录；没有记录时明确说明，不自动构造判断阈值。
    review = metadata.get("visual_review")
    if review:
        insertion = lines.index("## 分布观察") + 2
        lines[insertion:insertion] = [review["tail_observation"], ""]
    else:
        insertion = lines.index("## 分布观察") + 2
        lines[insertion:insertion] = ["本次自动生成描述统计与图，尚未记录人工长尾观察结论；不自动建立长尾判定阈值。", ""]
    (results / "stage1_report.md").write_text("\n".join(lines), encoding="utf-8")


