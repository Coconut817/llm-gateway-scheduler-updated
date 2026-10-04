"""Synchronous sender contract example; returns demo text, without a network call."""


def send_one(prompt):
    messages = prompt["messages"]
    return {"role": "assistant", "content": f"演示适配器已收到完整上下文，共 {len(messages)} 条消息。这段回复没有调用真实模型。"}
