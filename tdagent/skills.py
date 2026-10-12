"""Skill library: markdown knowledge files in `skills/` with a small header.

    ---
    name: join-tuning
    description: one line shown to the model in the skill list
    category: 조인
    findings: [PRODUCT_JOIN, DUPLICATE_LARGE]   # tuning findings that pull this skill in
    always: true          # always in the SQL-writing agent's system prompt
    always_tuning: true   # always attached to tuning prompts
    ---
    ## TP-JOIN-02 OR 조인     <- tuning point: id + title, then "- 신호: ..." etc.

Small models are bad at deciding which document to read, so the tuning loop
attaches skills deterministically from the detected findings; the chat agent
also gets a `load_skill` tool for anything else.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

_FRONT = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL)
_TP = re.compile(r"^##+ (TP-[A-Z]+-\d+)\s+(.+)$", re.MULTILINE)
_NEXT_HEADING = re.compile(r"^##? ", re.MULTILINE)
_SIGNAL = re.compile(r"^- 신호:\s*(.+)$", re.MULTILINE)


@dataclass
class TuningPoint:
    id: str
    title: str
    skill: str
    category: str
    signal: str = ""
    text: str = ""


@dataclass
class Skill:
    name: str
    description: str
    body: str
    category: str = ""
    findings: list[str] = field(default_factory=list)
    always: bool = False
    always_tuning: bool = False

    def tuning_points(self) -> list[TuningPoint]:
        points = []
        for m in _TP.finditer(self.body):
            end = _NEXT_HEADING.search(self.body, m.end())
            text = self.body[m.start(): end.start() if end else len(self.body)].strip()
            signal = _SIGNAL.search(text)
            points.append(TuningPoint(id=m.group(1), title=m.group(2).strip(), skill=self.name,
                                      category=self.category, signal=signal.group(1).strip() if signal else "",
                                      text=text))
        return points


def _parse_value(raw: str):
    raw = raw.strip()
    if raw.startswith("[") and raw.endswith("]"):
        return [v.strip().strip("'\"") for v in raw[1:-1].split(",") if v.strip()]
    if raw.lower() in ("true", "false"):
        return raw.lower() == "true"
    return raw.strip("'\"")


def parse_skill(text: str, fallback_name: str) -> Skill:
    meta: dict = {}
    m = _FRONT.match(text)
    body = text
    if m:
        body = text[m.end():]
        for line in m.group(1).splitlines():
            if ":" in line:
                k, v = line.split(":", 1)
                meta[k.strip()] = _parse_value(v)
    return Skill(
        name=meta.get("name") or fallback_name,
        description=meta.get("description", ""),
        body=body.strip(),
        category=meta.get("category", ""),
        findings=list(meta.get("findings") or []),
        always=bool(meta.get("always", False)),
        always_tuning=bool(meta.get("always_tuning", False)),
    )


class SkillLibrary:
    def __init__(self, skills: list[Skill] | None = None):
        self.skills: dict[str, Skill] = {s.name: s for s in skills or []}

    @classmethod
    def load_dir(cls, directory: str | Path) -> "SkillLibrary":
        d = Path(directory)
        skills = []
        if d.exists():
            for p in sorted(d.glob("*.md")):
                skills.append(parse_skill(p.read_text(encoding="utf-8"), p.stem))
        return cls(skills)

    def __len__(self) -> int:
        return len(self.skills)

    def get(self, name: str) -> Skill | None:
        return self.skills.get(name.strip())

    def tuning_points(self) -> dict[str, TuningPoint]:
        return {tp.id: tp for s in self.skills.values() for tp in s.tuning_points()}

    def catalog(self) -> str:
        return "\n".join(f"- {s.name}: {s.description}" for s in self.skills.values())

    def always(self) -> list[Skill]:
        return [s for s in self.skills.values() if s.always]

    def for_findings(self, codes: list[str]) -> list[Skill]:
        """Skills relevant to the given finding codes, in order of first match,
        after the always-on tuning skills."""
        picked = [s for s in self.skills.values() if s.always_tuning]
        for code in codes:
            for s in self.skills.values():
                if code in s.findings and s not in picked:
                    picked.append(s)
        return picked

    def render(self, skills: list[Skill], max_chars: int = 12000) -> str:
        """Concatenate skill bodies within a character budget (later ones are cut first)."""
        out, used = [], 0
        for s in skills:
            block = f"### SKILL: {s.name}\n{s.body}\n"
            if used + len(block) > max_chars:
                remaining = max_chars - used
                if remaining > 400:
                    out.append(block[:remaining] + "\n...(생략)\n")
                break
            out.append(block)
            used += len(block)
        return "\n".join(out)
