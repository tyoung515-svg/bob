from __future__ import annotations

import os
import yaml
from typing import Union
from pydantic import ValidationError

from core.modules.schema import ModuleManifest


class ModuleManifestError(ValueError):
    """Raised when a module manifest cannot be loaded or validated."""
    pass


def load_manifest(path: Union[str, "os.PathLike[str]"]) -> ModuleManifest:
    """
    Load and validate a module manifest from a YAML file.

    Args:
        path: Filesystem path to the manifest file.

    Returns:
        A validated ModuleManifest instance.

    Raises:
        ModuleManifestError: If the file does not exist, the YAML content is
            not a dictionary, or the content fails pydantic validation (the
            error message will contain the original validator's message
            substring).
    """
    try:
        with open(path, 'r') as f:
            data = yaml.safe_load(f)
    except FileNotFoundError:
        raise ModuleManifestError(f"Manifest file not found: {path}")
    except yaml.YAMLError as e:
        raise ModuleManifestError(f"YAML parsing error: {e}")

    if not isinstance(data, dict):
        raise ModuleManifestError(
            f"Manifest file {path} must contain a mapping (dict), "
            f"got {type(data).__name__}"
        )

    # Preprocess known fields that might be provided as lists instead of strings
    # to match the expected schema types (e.g., audience).
    if "audience" in data and isinstance(data["audience"], list):
        data["audience"] = ", ".join(data["audience"])

    try:
        manifest = ModuleManifest.model_validate(data)
    except ValidationError as e:
        raise ModuleManifestError(str(e)) from e

    return manifest
