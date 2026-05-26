"""Global application state — single instance shared across modules."""


class State:
    token: str = ""
    openai_token: str = ""
    user: dict | None = None
    mock_mode: bool = False
    rate_limit: dict | None = None


state = State()
