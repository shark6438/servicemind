from collections.abc import Sequence

from langchain_core.language_models import BaseChatModel
from pydantic import BaseModel

from servicemind.model_gateway.contracts import ModelCallContext
from servicemind.model_gateway.gateway import (
    GovernedStructuredRunnable,
    governed_structured_output,
)


def structured_output[SchemaT: BaseModel](
    model: BaseChatModel,
    schema: type[SchemaT],
    *,
    context: ModelCallContext | None = None,
    fallback_models: Sequence[BaseChatModel] = (),
) -> GovernedStructuredRunnable[SchemaT]:
    """Return the only allowed structured model path: the governed Model Gateway."""
    return governed_structured_output(
        model, schema, context=context, fallback_models=fallback_models
    )
