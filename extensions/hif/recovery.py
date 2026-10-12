"""HIF task-local recovery budgets; only confirmed business progress resets them."""
import time


class RecoveryState:
    def __init__(self):
        self.reset(None)

    def reset(self, task_id):
        self.task_id = task_id
        self.step = None
        self.deadline = None
        self.pending = None
        self.entry_recovered = False
        self.entry_scene = None
        self.paused = 0.0

    def bind(self, argv):
        task_id = getattr(getattr(argv, 'task_detail', None), 'task_id', None)
        if task_id is not None and task_id != self.task_id:
            self.reset(task_id)
        return self

    def now(self):
        return time.monotonic() - self.paused

    def progress(self, step):
        if step != self.step and self.pending is None:
            self.step = step
            self.deadline = None
            self.entry_recovered = False

    def defer(self, seconds=30):
        if self.deadline is None:
            self.deadline = self.now() + seconds
        return self.now() < self.deadline


state = RecoveryState()
