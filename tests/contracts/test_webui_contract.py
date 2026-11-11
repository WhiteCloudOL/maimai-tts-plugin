"""工作台操作声明与后台分发保持一致，覆盖宿主调用前的参数边界。"""

from ast import Compare, Constant, Eq, In, List, Name, Set, Tuple, parse, walk
from inspect import getsource
from pathlib import Path
from textwrap import dedent

import json

from maimai_tts_contract.services.studio import Studio


def test_declared_launch_operations_match_backend_dispatch():
    declaration = json.loads((Path(__file__).resolve().parents[2] / "webui.json").read_text(encoding="utf-8"))
    studio = next(page for page in declaration["pages"] if page["id"] == "studio")
    launch = studio["actions"]["launch"]
    operations = set()
    for node in walk(parse(dedent(getsource(Studio.launch)))):
        if not isinstance(node, Compare) or not isinstance(node.left, Name) or node.left.id != "operation":
            continue
        for operator, value in zip(node.ops, node.comparators, strict=True):
            if isinstance(operator, Eq) and isinstance(value, Constant):
                operations.add(value.value)
            elif isinstance(operator, In) and isinstance(value, (List, Set, Tuple)):
                operations.update(item.value for item in value.elts if isinstance(item, Constant))
    assert launch["api"] == "studio_launch"
    assert set(launch["parameters"]["operation"]["choices"]) == operations
