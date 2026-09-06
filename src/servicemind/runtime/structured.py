from typing import Any, cast

from langchain_core.language_models import BaseChatModel
from langchain_core.runnables import Runnable
from pydantic import BaseModel

from schema.models import DeepseekModelName


def structured_output[SchemaT: BaseModel](
    model: BaseChatModel, schema: type[SchemaT]
) -> Runnable[Any, SchemaT]:
    """Return the provider-compatible structured-output adapter.

    DeepSeek V4 currently supports JSON Object mode but rejects the JSON Schema
    response-format variant emitted by LangChain's default strategy.
    """
    model_name = str(getattr(model, "model_name", getattr(model, "model", "")))
    if model_name in {item.value for item in DeepseekModelName}:
        return cast(
            Runnable[Any, SchemaT],
            model.with_structured_output(schema, method="json_mode"),
        )
    return cast(Runnable[Any, SchemaT], model.with_structured_output(schema))
