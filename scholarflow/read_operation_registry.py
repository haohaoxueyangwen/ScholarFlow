"""Composition of capability-owned operation declarations, not domain branches."""
import importlib.util
from pathlib import Path

from .read_operations import ReadFacade

ROOT = Path(__file__).resolve().parents[1] / "packages" / "scholarflow-pi"
MODULES = {"project-evidence": ROOT / "profiles/minimal/operations.py", "paper-read": ROOT / "capabilities/paper-read/operations.py"}


def build_read_facade(data_dir, audit_path=None, capabilities=None):
    operations = []
    selected = list(MODULES) if capabilities is None else sorted(capabilities)
    if set(selected) - MODULES.keys():
        raise ValueError("Unknown trusted capability")
    for index, capability in enumerate(selected):
        path = MODULES[capability]
        spec = importlib.util.spec_from_file_location(f"scholarflow_read_capability_{index}", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        if any(operation.capability != capability for operation in module.OPERATIONS):
            raise ValueError("Operation capability does not match its declaration")
        operations.extend(module.OPERATIONS)
    return ReadFacade(operations, data_dir, audit_path)
