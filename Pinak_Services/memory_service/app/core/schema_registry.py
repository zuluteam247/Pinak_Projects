import json
import os
from pathlib import Path
from typing import Any, Dict, List, Optional

from jsonschema import Draft7Validator


class SchemaRegistry:
    def __init__(self, schema_dir: Optional[str] = None):
        self.schema_dir = Path(
            schema_dir
            or os.getenv("PINAK_SCHEMA_DIR")
            or os.path.expanduser("~/pinak-memory/schemas")
        )
        self.fallback_dir = Path(__file__).resolve().parent.parent.parent / "schemas"

    def _schema_path(self, layer: str) -> Optional[Path]:
        filename = f"{layer}.schema.json"
        primary = self.schema_dir / filename
        if primary.exists():
            return primary
        fallback = self.fallback_dir / filename
        if fallback.exists():
            return fallback
        return None

    def load_schema(self, layer: str) -> Optional[Dict[str, Any]]:
        path = self._schema_path(layer)
        if not path:
            return None
        return json.loads(path.read_text())

    def validate_payload(self, layer: str, payload: Dict[str, Any]) -> List[str]:
        schema = self.load_schema(layer)
        if not schema:
            return [f"Schema not found for {layer}"]
        validator = Draft7Validator(schema)
        errors = []
        for error in validator.iter_errors(payload):
            errors.append(error.message)
        return errors
