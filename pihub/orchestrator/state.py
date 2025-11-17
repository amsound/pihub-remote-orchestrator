
from __future__ import annotations
from dataclasses import dataclass, asdict, field
from typing import Literal, Optional, Dict, Any

Activity = Literal["OFF", "WATCH", "LISTEN"]

@dataclass
class TVState:
    power: Literal["on","off"] = "off"

@dataclass
class KEFState:
    source: Literal["Opt","Wifi"] = "Opt"
    volume: int = 20
    mute: bool = False

@dataclass
class MAState:
    state: Literal["off","idle","playing","paused"] = "off"
    player_id: Optional[str] = None

@dataclass
class OrchestratorState:
    activity: Activity = "OFF"
    tv: TVState = field(default_factory=TVState)
    kef: KEFState = field(default_factory=KEFState)
    ma: MAState = field(default_factory=MAState)
    radio_index: int = -1

    def to_dict(self) -> Dict[str, Any]:
        return {
            "activity": self.activity,
            "tv": asdict(self.tv),
            "kef": asdict(self.kef),
            "ma": asdict(self.ma),
            "radio_index": self.radio_index,
        }
