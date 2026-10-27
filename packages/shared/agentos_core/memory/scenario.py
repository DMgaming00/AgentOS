"""A deterministic two-session scenario with known answers for comparing memory strategies.

Session 1 teaches the agent facts about projects (owner, region, deadline). Between the sessions some facts are
updated, some expire, and two look-alike entities ("Atlas Pro", "Cobalt Lite") get only partial records, so a sloppy
agent can reuse the wrong project's value. Session 2 asks questions whose correct answers follow from the final state:

* ``current``     the fact never changed;
* ``updated``     the fact changed after session 1 (the old value is a *stale* answer);
* ``expired``     the fact expired (correct answer: ``unknown``; the old value is a stale answer);
* ``inline``      the fact was only stated earlier in the same conversation (short-term context), never stored;
* ``missing``     nobody ever said it (correct answer: ``unknown``);
* ``near_match``  a look-alike entity has no record of that attribute but its sibling does (correct answer: ``unknown``;
                  the sibling's value is an *incorrect reuse*).

Nothing here depends on a strategy; strategies only decide what part of this state the agent gets to see.
"""

from __future__ import annotations

import random
import re
from dataclasses import dataclass, field

OWNERS = ["Priya", "Mateo", "Hana", "Liam", "Zoya", "Omar", "Chen", "Ines"]
REGIONS = ["euwest", "useast", "apsouth", "uswest", "eucentral", "apnorth"]
DEADLINES = ["2026-11-14", "2026-12-01", "2027-01-20", "2027-02-09", "2027-03-30"]
PRIORITIES = ["P0", "P1", "P2"]
VALUES = {"owner": OWNERS, "region": REGIONS, "deadline": DEADLINES, "priority": PRIORITIES}
BASE_ENTITIES = ["Atlas", "Borealis", "Cobalt", "Delta", "Ember", "Fjord", "Granite", "Harbor"]
LOOKALIKES = {"Atlas Pro": "Atlas", "Cobalt Lite": "Cobalt"}
STORED_ATTRIBUTES = ["owner", "region", "deadline"]
UNKNOWN = "unknown"


def slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


@dataclass
class Fact:
    entity: str
    attribute: str
    value: str
    updated_value: str | None = None
    expires: bool = False

    @property
    def key(self) -> str:
        return f"{slug(self.entity)}.{self.attribute}"

    @property
    def final_value(self) -> str | None:
        if self.expires:
            return None
        return self.updated_value or self.value

    def statement(self, value: str | None = None) -> str:
        return f"{self.entity} {self.attribute}: {value or self.value}"


@dataclass
class Task:
    id: str
    kind: str
    entity: str
    attribute: str
    expected: str
    stale: list[str] = field(default_factory=list)
    lookalike_values: list[str] = field(default_factory=list)
    history: list[str] = field(
        default_factory=list
    )  # turns earlier in the same conversation (short-term context)
    relevant_keys: list[str] = field(
        default_factory=list
    )  # records that contain the answer (for retrieval precision)

    @property
    def question(self) -> str:
        return f"What is the {self.attribute} of {self.entity}?"


@dataclass
class Scenario:
    seed: int
    facts: list[Fact]
    tasks: list[Task]

    def fact(self, entity: str, attribute: str) -> Fact | None:
        return next((f for f in self.facts if f.entity == entity and f.attribute == attribute), None)


def build_scenario(seed: int = 7, *, updated: int = 8, expired: int = 3) -> Scenario:
    rng = random.Random(seed)
    facts: list[Fact] = []
    for entity in BASE_ENTITIES:
        for attribute in STORED_ATTRIBUTES:
            facts.append(Fact(entity, attribute, rng.choice(VALUES[attribute])))
    for variant in LOOKALIKES:
        facts.append(Fact(variant, "owner", rng.choice([o for o in OWNERS if o != facts[0].value])))
    # Choose which facts change or expire (only base-entity facts), disjointly.
    base = [f for f in facts if f.entity in BASE_ENTITIES]
    order = list(range(len(base)))
    rng.shuffle(order)
    if updated + expired > len(order):
        raise ValueError(f"updated + expired must be at most {len(order)}")
    to_update, to_expire = order[:updated], order[updated : updated + expired]
    for i in to_update:
        f = base[i]
        f.updated_value = rng.choice([v for v in VALUES[f.attribute] if v != f.value])
    for i in to_expire:
        base[i].expires = True

    tasks: list[Task] = []
    for f in facts:
        if f.expires:
            kind, expected = "expired", UNKNOWN
        elif f.updated_value:
            kind, expected = "updated", f.updated_value
        else:
            kind, expected = "current", f.value
        stale = [f.value] if kind in ("updated", "expired") else []
        tasks.append(
            Task(
                f"{kind}-{slug(f.entity)}-{f.attribute}",
                kind,
                f.entity,
                f.attribute,
                expected,
                stale,
                relevant_keys=[f.key] if kind != "expired" else [],
            )
        )
    # Facts stated only in the conversation: priority of a few entities (no record anywhere).
    for entity in rng.sample(BASE_ENTITIES, 4):
        value = rng.choice(PRIORITIES)
        tasks.append(
            Task(
                f"inline-{slug(entity)}-priority",
                "inline",
                entity,
                "priority",
                value,
                history=[f"Earlier today the team said: {entity} priority is {value}."],
            )
        )
    # Nobody ever said these.
    for entity in rng.sample(BASE_ENTITIES, 4):
        tasks.append(Task(f"missing-{slug(entity)}-budget", "missing", entity, "budget", UNKNOWN))
    # Look-alikes: the sibling has region/deadline records, the look-alike does not.
    for variant, sibling in LOOKALIKES.items():
        for attribute in ("region", "deadline"):
            sib = next(f for f in facts if f.entity == sibling and f.attribute == attribute)
            tasks.append(
                Task(
                    f"near_match-{slug(variant)}-{attribute}",
                    "near_match",
                    variant,
                    attribute,
                    UNKNOWN,
                    lookalike_values=[v for v in {sib.value, sib.updated_value} if v],
                )
            )
    tasks.sort(key=lambda t: t.id)
    return Scenario(seed, facts, tasks)
