"""Public API surface of the Python SDK at a source directory.

Prints a JSON object ``{"Name" | "Name.member": normalized text}`` for everything in
``gabriel.__all__`` plus SDK classes reachable from it (attribute and signature annotations).
Used by the release script to decide whether a release changes the public API.

Usage: python api_python.py <src dir containing the gabriel package>
"""

from __future__ import annotations

import ast
import dataclasses
import enum
import inspect
import textwrap
import json
import os
import re
import sys
import typing
from typing import Any

PACKAGE = "gabriel"
IDENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
ADDRESS = re.compile(r" at 0x[0-9a-fA-F]+")


def _text(annotation: Any) -> str:
    if isinstance(annotation, str):
        return annotation
    if isinstance(getattr(annotation, "__forward_arg__", None), str):
        return str(annotation.__forward_arg__)
    if isinstance(annotation, type):
        return annotation.__qualname__
    return repr(annotation).replace("typing.", "")


def _signature(obj: Any) -> str:
    try:
        return ADDRESS.sub("", str(inspect.signature(obj)))
    except (TypeError, ValueError):
        return "(...)"


class Surface:
    def __init__(self) -> None:
        self.out: dict[str, str] = {}
        self.seen: set[int] = set()
        self.queue: list[type] = []

    def own(self, obj: Any) -> bool:
        module = getattr(obj, "__module__", "") or ""
        return module == PACKAGE or module.startswith(PACKAGE + ".")

    def reach(self, text: str, module_name: str) -> None:
        """Queues SDK classes named in an annotation string."""
        namespace = vars(sys.modules[module_name]) if module_name in sys.modules else {}
        for name in IDENT.findall(text):
            target = namespace.get(name)
            if inspect.isclass(target) and self.own(target) and id(target) not in self.seen:
                self.seen.add(id(target))
                self.queue.append(target)

    def describe(self, name: str, obj: Any) -> None:
        if inspect.isclass(obj):
            self.describe_class(name, obj)
        elif callable(obj):
            sig = _signature(obj)
            self.out[name] = f"def {sig}"
            self.reach(sig, getattr(obj, "__module__", PACKAGE))
        else:
            self.out[name] = f"{type(obj).__name__} {obj!r}" if isinstance(obj, (str, int, float, bool, tuple)) else type(obj).__name__

    def describe_class(self, name: str, cls: type) -> None:
        module = cls.__module__
        if typing.is_typeddict(cls):
            self.out[name] = "TypedDict"
            required = getattr(cls, "__required_keys__", frozenset())
            for field, annotation in cls.__annotations__.items():
                text = _text(annotation)
                self.out[f"{name}.{field}{'' if field in required else '?'}"] = text
                self.reach(text, module)
            return
        if issubclass(cls, enum.Enum):
            self.out[name] = "enum " + ",".join(sorted(m.name + "=" + repr(m.value) for m in cls))
            return
        bases = ",".join(b.__qualname__ for b in cls.__bases__ if b is not object)
        self.out[name] = f"class({bases})"
        if dataclasses.is_dataclass(cls):
            for f in dataclasses.fields(cls):
                if f.name.startswith("_"):
                    continue
                optional = "?" if f.default is not dataclasses.MISSING or f.default_factory is not dataclasses.MISSING else ""
                text = _text(f.type)
                self.out[f"{name}.{f.name}{optional}"] = text
                self.reach(text, module)
        else:
            init = cls.__dict__.get("__init__")
            if init is not None:
                sig = _signature(init)
                self.out[f"{name}.__init__"] = sig
                self.reach(sig, module)
        annotated: dict[str, Any] = dict(cls.__dict__.get("__annotations__", {}))
        annotated.update({k: v for k, v in self.instance_attributes(cls).items() if k not in annotated})
        for attr, annotation in annotated.items():
            key = f"{name}.{attr}"
            if attr.startswith("_") or key in self.out or f"{key}?" in self.out:
                continue
            text = _text(annotation)
            self.out[key] = text
            self.reach(text, module)
        for attr, member in inspect.getmembers(cls):
            if attr.startswith("_") or attr in annotated:
                continue
            raw = inspect.getattr_static(cls, attr)
            if isinstance(raw, property):
                text = _text(inspect.signature(raw.fget).return_annotation) if raw.fget else "property"
                self.out[f"{name}.{attr}"] = f"property {text}"
                self.reach(text, module)
            elif inspect.isfunction(member) or inspect.ismethod(member) or isinstance(raw, (staticmethod, classmethod)):
                if not self.own(member):
                    continue
                sig = _signature(member)
                kind = "async def" if inspect.iscoroutinefunction(member) or inspect.isasyncgenfunction(member) else "def"
                self.out[f"{name}.{attr}"] = f"{kind} {sig}"
                self.reach(sig, module)


    @staticmethod
    def instance_attributes(cls: type) -> dict[str, str]:
        """Public ``self.x = Name(...)`` / ``self.x: T = ...`` attributes set in ``__init__``."""
        init = cls.__dict__.get("__init__")
        try:
            tree = ast.parse(textwrap.dedent(inspect.getsource(init))) if init else None
        except (OSError, TypeError, SyntaxError):
            return {}
        found: dict[str, str] = {}
        for node in ast.walk(tree) if tree else []:
            target = node.target if isinstance(node, ast.AnnAssign) else (node.targets[0] if isinstance(node, ast.Assign) else None)
            if not (isinstance(target, ast.Attribute) and isinstance(target.value, ast.Name) and target.value.id == "self"):
                continue
            if target.attr.startswith("_"):
                continue
            if isinstance(node, ast.AnnAssign):
                found[target.attr] = ast.unparse(node.annotation)
            elif isinstance(node.value, ast.Call) and isinstance(node.value.func, ast.Name):
                found.setdefault(target.attr, node.value.func.id)
        return found


def surface(src: str) -> dict[str, str]:
    sys.path.insert(0, src)
    for loaded in [m for m in sys.modules if m == PACKAGE or m.startswith(PACKAGE + ".")]:
        del sys.modules[loaded]
    package = __import__(PACKAGE)
    location = os.path.realpath(getattr(package, "__file__", "") or "")
    if not location.startswith(os.path.realpath(src) + os.sep):
        sys.exit(f"imported {PACKAGE} from {location}, not from {src}")
    s = Surface()
    for name in getattr(package, "__all__", []):
        obj = getattr(package, name)
        if inspect.isclass(obj):
            s.seen.add(id(obj))
        s.describe(name, obj)
    while s.queue:
        cls = s.queue.pop(0)
        if cls.__name__ not in s.out:
            s.describe(cls.__name__, cls)
    return dict(sorted(s.out.items()))


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit("usage: api_python.py <src dir>")
    print(json.dumps(surface(sys.argv[1]), ensure_ascii=False, indent=1))
