from __future__ import annotations

from importlib.resources import files

from codexia_manual_agent.domain.errors import PromptVersionError


_PROMPT_ASSETS = {
    "v0.3": "v0.3.txt",
}


def available_prompt_versions() -> tuple[str, ...]:
    return tuple(sorted(_PROMPT_ASSETS))


def load_prompt(version: str) -> str:
    asset_name = _PROMPT_ASSETS.get(version)
    if asset_name is None:
        available = ", ".join(available_prompt_versions())
        raise PromptVersionError(
            f"Unknown prompt version {version!r}. Available: {available}"
        )
    return (
        files("codexia_manual_agent.prompt_assets")
        .joinpath(asset_name)
        .read_text(encoding="utf-8")
    )
