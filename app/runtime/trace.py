"""事件溯源：每步落 JSONL 可回放；会话状态落 state.json 可恢复。"""
import json
import os
from pathlib import Path
from .models import StepRecord

ROOT = Path(__file__).resolve().parents[2]


class Trace:
    def __init__(self, session_id: str):
        safe = "".join(c for c in session_id if c.isalnum() or c in "-_")[:60] or "session"
        self.session_id = safe
        # 评测等批量场景可用环境变量把轨迹写到独立目录，避免污染演示 traces/
        base = Path(os.getenv("MINI_HARNESS_TRACE_DIR", str(ROOT / "traces")))
        self.path = base / f"{safe}.jsonl"
        self.state_path = base / f"{safe}.state.json"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.steps: list[StepRecord] = []

    def append(self, rec: StepRecord):
        # 展示编号用累计轨迹条数，而非会被重规划重置的 step_idx。
        # 否则失败重试后轨迹出现 step2→step1→step2，编号重复、不可读。
        # append 在 emit 之前调用，前端 SSE 拿到的也是这个单调编号。
        rec.step = len(self.steps) + 1
        self.steps.append(rec)
        with self.path.open("a", encoding="utf-8") as f:
            f.write(rec.model_dump_json() + "\n")

    def save_state(self, state: dict):
        """原子替换保存会话状态（tmp + os.replace），避免半写。"""
        tmp = self.state_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(state, ensure_ascii=False, default=str), encoding="utf-8")
        os.replace(tmp, self.state_path)

    def load_state(self) -> dict | None:
        if not self.state_path.exists():
            return None
        try:
            return json.loads(self.state_path.read_text(encoding="utf-8"))
        except Exception:
            return None

    def tail_summary(self, n: int = 3) -> str:
        lines = []
        for s in self.steps[-n:]:
            ok = s.verdict.get("ok") if isinstance(s.verdict, dict) else None
            lines.append(f"step{s.step}:{s.decision}->{s.tool} ok={ok}")
        return "\n".join(lines)

    def replay(self) -> list[StepRecord]:
        with self.path.open(encoding="utf-8") as f:
            return [StepRecord.model_validate_json(line) for line in f if line.strip()]
