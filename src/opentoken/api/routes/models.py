from fastapi import APIRouter

from opentoken.gateway.model_registry import get_default_model_registry
from opentoken.models.media import media_models_for_logged_in_providers
from opentoken.models.openai_compat import build_openai_model_objects

router = APIRouter()


@router.get('/v1/models')
def list_models() -> dict[str, object]:
    registry = get_default_model_registry()
    entries = registry.list_models()
    data = build_openai_model_objects(entries)
    # Media models (Seedream / Seedance) come from a separate picker in the
    # provider UI, so chat discovery never reports them. Advertise them here —
    # but only for providers with stored credentials, otherwise /v1/models would
    # invite callers to hit an endpoint the gateway can't serve.
    listed = {item['id'] for item in data}
    for model in media_models_for_logged_in_providers():
        if model.id in listed:
            continue
        listed.add(model.id)
        data.append(model.to_openai_dict())
    return {
        'object': 'list',
        'data': data,
    }
