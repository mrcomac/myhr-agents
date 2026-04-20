MODEL_ID="global.anthropic.claude-opus-4-6-v1"
MODEL_PRICING = {
    "global.anthropic.claude-opus-4-6-v1": {"input": 5.0, "output": 25.0}
}

def calculate_cost_for_model(model, input_tokens, output_tokens):
    pricing = MODEL_PRICING[model]
    cost = (
        (input_tokens / 1_000_000) * pricing["input"] +
        (output_tokens / 1_000_000) * pricing["output"]
    )

    return round(cost, 6)
    