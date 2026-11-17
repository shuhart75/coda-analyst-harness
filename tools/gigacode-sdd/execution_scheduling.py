"""Resource-stage exclusion and explicit selection among verified candidates."""
from contextlib import contextmanager
import threading


class ResourceLane:
    def __init__(self):
        self.condition = threading.Condition()
        self.queue = []
        self.busy = False

    @contextmanager
    def enter(self, stop):
        from controller import Interrupted
        ticket = object()
        with self.condition:
            self.queue.append(ticket)
            try:
                while self.busy or self.queue[0] is not ticket:
                    if stop.is_set():
                        raise Interrupted('Interrupted while waiting for shared resources')
                    self.condition.wait(.1)
                if stop.is_set():
                    raise Interrupted('Interrupted while waiting for shared resources')
                self.busy = True
            finally:
                self.queue.remove(ticket)
                self.condition.notify_all()
        try:
            yield
        finally:
            with self.condition:
                self.busy = False
                self.condition.notify_all()


def quality(candidate):
    # Mandatory checks and independent review already passed. Only validated
    # optional gaps discriminate; code size and model self-ratings do not.
    review = candidate['review']
    return len(review.get('optional_findings', []))


def choose(candidates, models):
    return min(candidates, key=lambda c: (quality(c), models.index(c['model'])))
