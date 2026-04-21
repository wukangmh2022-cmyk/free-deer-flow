from unittest.mock import patch

import pytest

from app.gateway.routers.models import list_models
from deerflow.config.model_config import ModelConfig


def _make_model(name: str, display_name: str) -> ModelConfig:
    return ModelConfig(
        name=name,
        display_name=display_name,
        description=None,
        use="langchain_openai:ChatOpenAI",
        model=name,
        supports_thinking=True,
        supports_reasoning_effort=False,
    )


@pytest.mark.anyio
async def test_list_models_hides_sticky_variants_from_gui():
    app_config = type(
        "Config",
        (),
        {
            "models": [
                _make_model("deepseek-web-deerflow", "DeepSeek Web DeerFlow"),
                _make_model("deepseek-web-deerflow-sticky", "DeepSeek Web DeerFlow Sticky"),
                _make_model("xiaomi-mimo-v2-pro", "Xiaomi MiMo V2 Pro"),
            ]
        },
    )()

    with patch("app.gateway.routers.models.get_app_config", return_value=app_config):
        response = await list_models()

    assert [model.name for model in response.models] == [
        "deepseek-web-deerflow",
        "xiaomi-mimo-v2-pro",
    ]
