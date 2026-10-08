import tomllib
from pathlib import Path
from types import SimpleNamespace

# Everything the compactor owns lives here: the settings for this bucket and
# the archives it produces, mirroring each source prefix underneath.
ARCHIVE_ROOT = "bucket-archive/"

CONFIG_KEY = ARCHIVE_ROOT + "config.toml"

default_settings = tomllib.loads(Path(__file__).parent.parent.joinpath("config.toml").read_text())
required_settings = default_settings.keys()


def load_settings(s3) -> SimpleNamespace:
    """Read this bucket's settings from s3://<bucket>/bucket-archive/config.toml."""
    settings = default_settings | tomllib.loads(s3.get_body(CONFIG_KEY).decode())
    bad_settings = [
        key for key in required_settings
        if not (v := settings.get(key)) and not isinstance(v, (int, float))
    ]
    if bad_settings:
        raise TypeError(f"All settings must be populated. Found empty: {bad_settings}")
    return SimpleNamespace(**settings)
