import json
from pathlib import Path


def _merge(base, child):
    result = dict(base)
    for key, value in child.items():
        if isinstance(result.get(key), dict) and isinstance(value, dict):
            value = _merge(result[key], value)
        result[key] = value
    return result


def _read(path, ancestors):
    path = path.resolve()
    if path in ancestors:
        raise ValueError(f"Configuration inheritance cycle at {path}")
    content = path.read_text(encoding="utf-8-sig")
    if path.suffix == ".json":
        values = json.loads(content)
    elif path.suffix in {".yaml", ".yml"}:
        try:
            import yaml
        except ModuleNotFoundError as exc:
            raise RuntimeError("PyYAML is required to read a YAML configuration") from exc
        values = yaml.safe_load(content)
    else:
        raise ValueError(f"Unsupported configuration file: {path}")
    if not isinstance(values, dict):
        raise ValueError(f"Expected configuration mapping in {path}")
    if "extends" not in values:
        return values
    parent = values.pop("extends")
    if not isinstance(parent, str) or not parent:
        raise ValueError(f"Expected nonempty extends path in {path}")
    return _merge(_read(path.parent / parent, ancestors | {path}), values)


def load_config(path, overrides=None):
    return _merge(_read(Path(path), set()), overrides or {})
