"""Load + normalize the LoCoMo dataset (snap-research/locomo, data/locomo10.json).

Raw shape (per conversation):
  {
    "sample_id": "...",
    "qa": [{"question","answer","evidence":[dia_id...],"category":int}, ...],
    "conversation": {
        "speaker_a": "...", "speaker_b": "...",
        "session_1": [{"speaker","dia_id","text", ...}, ...],
        "session_1_date_time": "...",
        "session_2": [...], ...
    },
    ...
  }

We flatten it into Turn / QA / Conversation so the harness never touches raw JSON.
Category codes are the standard LoCoMo taxonomy (see CATEGORY_NAMES).
"""

from __future__ import annotations

import json
import re
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

DATA_URL = "https://raw.githubusercontent.com/snap-research/locomo/main/data/locomo10.json"
DEFAULT_PATH = Path(__file__).parent / "data" / "locomo10.json"

# LoCoMo question-category codes → human names (per the paper/repo).
CATEGORY_NAMES = {
    1: "multi-hop",
    2: "temporal",
    3: "open-domain",
    4: "single-hop",
    5: "adversarial",
}


@dataclass
class Turn:
    session: str           # "session_1"
    dia_id: str            # "D1:3" — matches qa.evidence ids
    speaker: str
    text: str
    date: str = ""         # session datetime, if present

    def as_line(self) -> str:
        d = f" ({self.date})" if self.date else ""
        return f"[{self.dia_id}]{d} {self.speaker}: {self.text}"


@dataclass
class QA:
    question: str
    answer: str            # gold; may be int/str in raw → always coerced to str here
    category: int
    evidence: list = field(default_factory=list)

    @property
    def category_name(self) -> str:
        return CATEGORY_NAMES.get(self.category, f"cat{self.category}")

    @property
    def is_adversarial(self) -> bool:
        # Adversarial questions have no answer in the conversation; gold is often the
        # literal phrase signalling "not answerable". Kept separate in scoring.
        return self.category == 5


@dataclass
class Conversation:
    sample_id: str
    turns: list           # list[Turn] in chronological order
    qa: list              # list[QA]

    def transcript(self) -> str:
        return "\n".join(t.as_line() for t in self.turns)


def _coerce_answer(a) -> str:
    if a is None:
        return ""
    return str(a).strip()


def load(path: Optional[Path] = None) -> list:
    """Load + normalize all conversations. Raises a clear error if the data file is
    missing (run ``python -m benchmarks.locomo.dataset --fetch`` first)."""
    p = Path(path) if path else DEFAULT_PATH
    if not p.exists():
        raise FileNotFoundError(
            f"LoCoMo data not found at {p}. Fetch it with:\n"
            f"  python -m benchmarks.locomo.dataset --fetch"
        )
    raw = json.loads(p.read_text(encoding="utf-8"))
    convos = []
    for i, c in enumerate(raw):
        conv = c.get("conversation", {}) or {}
        # session keys are 'session_1', 'session_2', ... (not the *_date_time siblings)
        sess_keys = sorted(
            [k for k in conv if re.fullmatch(r"session_\d+", k)],
            key=lambda k: int(k.split("_")[1]),
        )
        turns = []
        for sk in sess_keys:
            date = conv.get(f"{sk}_date_time", "") or ""
            for t in (conv.get(sk) or []):
                turns.append(Turn(
                    session=sk,
                    dia_id=str(t.get("dia_id", "")),
                    speaker=str(t.get("speaker", "")),
                    text=str(t.get("text", "")),
                    date=date,
                ))
        qa = [QA(question=str(q.get("question", "")),
                 answer=_coerce_answer(q.get("answer")),
                 category=int(q.get("category", 0) or 0),
                 evidence=list(q.get("evidence", []) or []))
              for q in (c.get("qa") or [])
              if q.get("question")]
        convos.append(Conversation(
            sample_id=str(c.get("sample_id", f"conv{i}")),
            turns=turns, qa=qa,
        ))
    return convos


def fetch(dest: Optional[Path] = None) -> Path:
    """Download locomo10.json from the official repo. Returns the path written."""
    p = Path(dest) if dest else DEFAULT_PATH
    p.parent.mkdir(parents=True, exist_ok=True)
    with urllib.request.urlopen(DATA_URL, timeout=60) as r:  # noqa: S310 (known host)
        data = r.read()
    # validate it parses before committing to disk
    json.loads(data.decode("utf-8"))
    p.write_bytes(data)
    return p


if __name__ == "__main__":
    import sys
    if "--fetch" in sys.argv:
        out = fetch()
        convos = load(out)
        n_qa = sum(len(c.qa) for c in convos)
        n_turns = sum(len(c.turns) for c in convos)
        print(f"fetched {out} — {len(convos)} conversations, {n_turns} turns, {n_qa} QA pairs")
    else:
        convos = load()
        print(f"{len(convos)} conversations loaded")
        for c in convos[:1]:
            print(f"  {c.sample_id}: {len(c.turns)} turns, {len(c.qa)} QA")
