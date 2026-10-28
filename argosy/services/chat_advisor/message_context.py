"""Persist forwarded content separately from the user's authority, without a migration."""
import json

PREFIX = "ARGOSY_FORWARDED_V1:"


def encode_message(user_text: str, forwarded: list[str]) -> str:
    # Escape literal protocol-looking user text; only the transport creates
    # envelopes. Decode one layer, never reinterpret nested source content.
    if not forwarded and not user_text.startswith(PREFIX):
        return user_text
    return PREFIX + json.dumps({"user_text": user_text,
                                "forwarded": [value[:6000] for value in forwarded[:3]]}, ensure_ascii=False)


def decode_message(value: str) -> tuple[str, str]:
    if value.startswith(PREFIX):
        try:
            data = json.loads(value[len(PREFIX):])
            if isinstance(data["user_text"], str) and isinstance(data["forwarded"], list):
                forwarded = [item[:6000] for item in data["forwarded"][:3] if isinstance(item, str)]
                return data["user_text"], "\n\n".join(forwarded)
        except (ValueError, KeyError, TypeError):
            pass
    return value, ""
