from app.metadata import tmdb, tvdb
from app.settings import load_config, save_config


class MetadataProviderError(Exception):
    pass


PROVIDERS = {
    "tmdb": {
        "id": "tmdb",
        "name": "TMDB",
    },
    "tvdb": {
        "id": "tvdb",
        "name": "TheTVDB",
    },
}


def available_providers():
    return [entry.copy() for entry in PROVIDERS.values()]


def get_active_provider_id():
    config = load_config()
    provider_id = (
        config.get("metadata", {})
        .get("provider", "tmdb")
        .strip()
        .lower()
    )
    return provider_id if provider_id in PROVIDERS else "tmdb"


def get_active_provider_name():
    return PROVIDERS[get_active_provider_id()]["name"]


def set_active_provider(provider_id):
    provider_id = str(provider_id).strip().lower()
    if provider_id not in PROVIDERS:
        raise MetadataProviderError(f"Unknown metadata provider: {provider_id}")

    config = load_config()
    metadata = config.setdefault("metadata", {})
    metadata["provider"] = provider_id
    save_config(config)


def _call(function_name, *args, **kwargs):
    provider_id = get_active_provider_id()

    try:
        if provider_id == "tmdb":
            return getattr(tmdb, function_name)(*args, **kwargs)
        if provider_id == "tvdb":
            return getattr(tvdb, function_name)(*args, **kwargs)
    except (tmdb.TMDBError, tvdb.TVDBError) as error:
        raise MetadataProviderError(str(error)) from error

    raise MetadataProviderError(
        f'Metadata provider "{provider_id}" does not implement {function_name}.'
    )


def match_media(parsed):
    return _call("match_media", parsed)


def get_candidates(parsed):
    return _call("get_candidates", parsed)


def get_tv_season(series_id, season_number):
    return _call("get_tv_season", series_id, season_number)


def get_tv_episode(series_id, season_number, episode_number):
    return _call("get_tv_episode", series_id, season_number, episode_number)
