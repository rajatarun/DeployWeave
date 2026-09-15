"""Pin the resource-naming rules in deployment.yaml.

Four DynamoDB tables and two SQS queues were named globally rather than per
environment, so a sandbox stack tried to create the ones the dev stack already
owned and every sandbox changeset failed EarlyValidation
ResourceExistenceCheck. A ResourceSuffix parameter makes them unique per
stack.

The dangerous part is the default. Renaming a DynamoDB table does not rename
it -- CloudFormation replaces it, and the data goes with the old one. So the
default has to be the empty string, and the resolved names under that default
have to be byte-identical to the literals that were there before the parameter
existed. These tests pin exactly that, because a plausible-looking "tidy-up"
(defaulting the suffix to "-${Environment}", say) would silently destroy the
prod tables on the next deploy.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

TEMPLATE_PATH = Path(__file__).resolve().parent / "deployment.yaml"

# The literal names as they stood before ResourceSuffix existed. These are the
# live physical resources in dev and prod; a change here is a replacement.
NAMES_BEFORE_SUFFIX = {
    "AdapterCatalogTable.TableName": "deployweave-adapter-catalog",
    "ModelMetricsTable.TableName": "deployweave-model-metrics",
    "AgentRegistryTable.TableName": "deployweave-agent-registry",
    "ContractsTable.TableName": "deployweave-contracts",
    "CleanupDLQ.QueueName": "deployweave-cleanup-dlq",
    "CleanupQueue.QueueName": "deployweave-cleanup-queue",
}

NAME_PROPS = ("TableName", "QueueName", "TopicName", "RoleName", "FunctionName", "AlarmName")


class _CfnLoader(yaml.SafeLoader):
    """SafeLoader that keeps !Sub/!Ref/!GetAtt as data instead of rejecting them."""


def _tag(loader, suffix, node):
    if isinstance(node, yaml.ScalarNode):
        return {"__tag__": suffix, "v": loader.construct_scalar(node)}
    if isinstance(node, yaml.SequenceNode):
        return {"__tag__": suffix, "v": loader.construct_sequence(node, deep=True)}
    return {"__tag__": suffix, "v": loader.construct_mapping(node, deep=True)}


_CfnLoader.add_multi_constructor("!", _tag)


@pytest.fixture(scope="module")
def template() -> dict:
    with open(TEMPLATE_PATH, encoding="utf-8") as fh:
        return yaml.load(fh, Loader=_CfnLoader)


def _resolve(value, params: dict) -> str:
    """Resolve a name property to a literal under the given parameter values."""
    if isinstance(value, str):
        return value
    if isinstance(value, dict) and value.get("__tag__") == "Sub":
        raw = value["v"] if isinstance(value["v"], str) else value["v"][0]
        return re.sub(r"\$\{([A-Za-z0-9:]+)\}",
                      lambda m: params.get(m.group(1), "<" + m.group(1) + ">"), raw)
    return repr(value)


def _named_resources(template: dict, params: dict) -> dict:
    out = {}
    for logical_id, resource in (template.get("Resources") or {}).items():
        props = resource.get("Properties") or {}
        for prop in NAME_PROPS:
            if prop in props:
                out[f"{logical_id}.{prop}"] = _resolve(props[prop], params)
    return out


def test_resource_suffix_defaults_to_empty(template):
    """A non-empty default would rename -- and therefore replace -- live tables."""
    param = (template.get("Parameters") or {}).get("ResourceSuffix")
    assert param is not None, "deployment.yaml has no ResourceSuffix parameter"
    assert param.get("Default") == "", (
        f"ResourceSuffix defaults to {param.get('Default')!r}, not \"\". Any other "
        f"default renames the live DynamoDB tables, and CloudFormation renames a "
        f"table by replacing it -- the data goes with the old one."
    )


def test_empty_suffix_reproduces_the_original_names(template):
    """The whole safety argument: default in, nothing changes."""
    resolved = _named_resources(template, {"Environment": "dev", "ResourceSuffix": ""})
    for key, expected in NAMES_BEFORE_SUFFIX.items():
        assert resolved.get(key) == expected, (
            f"{key} resolves to {resolved.get(key)!r} with ResourceSuffix=\"\", but the "
            f"live resource is named {expected!r}. This deploy would replace it."
        )


def test_a_suffix_makes_every_global_name_unique(template):
    """And the suffix has to actually reach all six, or the sandbox still collides."""
    resolved = _named_resources(template, {"Environment": "dev", "ResourceSuffix": "-sb-x"})
    for key, base in NAMES_BEFORE_SUFFIX.items():
        assert resolved.get(key) == f"{base}-sb-x", (
            f"{key} is {resolved.get(key)!r} with a suffix set; it does not carry "
            f"ResourceSuffix, so a sandbox stack would still collide with dev on it."
        )


def test_every_named_resource_is_scoped_by_environment_or_suffix(template):
    """Catch the next globally-named resource before it breaks the sandbox again.

    A new table or queue with a bare literal name reintroduces exactly the bug
    this parameter exists to fix, and it would only show up as a failed
    changeset on a branch nobody is watching.
    """
    offenders = []
    for logical_id, resource in (template.get("Resources") or {}).items():
        props = resource.get("Properties") or {}
        for prop in NAME_PROPS:
            if prop not in props:
                continue
            raw = props[prop]
            text = raw if isinstance(raw, str) else str(raw.get("v", ""))
            if "${Environment}" not in text and "${ResourceSuffix}" not in text:
                offenders.append(f"{logical_id}.{prop} = {text!r}")
    assert not offenders, (
        "these resources have one global name per account, so a second stack in "
        "the same account cannot create them:\n  " + "\n  ".join(offenders)
        + "\nInterpolate ${Environment} or ${ResourceSuffix} into the name."
    )
