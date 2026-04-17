LORA_COMPATIBLE_MODELS = [
    "amazon.titan-",
    "meta.llama",
]


def validate_lora_compatibility(model: str, lora_adapter_id) -> bool:
    """Return True if model supports LoRA, raise ValueError if not. No-op when adapter is None."""
    if not lora_adapter_id:
        return True
    for prefix in LORA_COMPATIBLE_MODELS:
        if model.startswith(prefix):
            return True
    raise ValueError(
        f"Model '{model}' does not support custom LoRA adapters via Bedrock. "
        f"Compatible model prefixes: {LORA_COMPATIBLE_MODELS}"
    )
