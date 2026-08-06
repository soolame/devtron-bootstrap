#!/usr/bin/env python3
import sys
import yaml


def is_name_value_list(obj):
    return bool(obj) and all(
        isinstance(i, dict) and set(i.keys()) <= {"name", "value"} for i in obj
    )


def flatten(obj, prefix=""):
    if isinstance(obj, dict):
        for key, value in obj.items():
            flatten(value, f"{prefix}.{key}" if prefix else str(key))
    elif isinstance(obj, list):
        if is_name_value_list(obj):
            pairs = ", ".join(f'{i.get("name")}={i.get("value")}' for i in obj)
            print(f"{prefix}: {pairs}")
        else:
            for idx, item in enumerate(obj):
                flatten(item, f"{prefix}[{idx}]")
    else:
        print(f"{prefix}: {obj}")


if __name__ == "__main__":
    path = sys.argv[1]
    with open(path) as f:
        data = yaml.safe_load(f)
    flatten(data)
