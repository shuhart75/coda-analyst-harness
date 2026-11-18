"""Resource-stage exclusion and explicit selection among verified candidates."""
from contextlib import contextmanager
import threading
import time


class ResourceLane:
    """FIFO within one task's resource reservation; checkpoint facts are not locks.

    The controller run lock and process cleanup checks remain authoritative.
    Saved tickets restore order only, never ownership of a surviving process.
    """
    def __init__(self, saved=()):
        self.condition = threading.Condition()
        self.queue = []
        self.holder = None
        self.tickets = {}
        self.since = {}
        for owner, state in saved:
            if state and state.get('state') in ('waiting', 'held'):
                self.tickets[owner] = state['ticket']
                self.since[owner] = state.get('waiting_since', time.time())
        self.queue = sorted(self.tickets, key=lambda owner: (self.tickets[owner], owner))
        self.sequence = max(self.tickets.values(), default=0)

    @contextmanager
    def enter(self, stop, owner=None, observe=None):
        from controller import Interrupted
        owner = owner or str(id(threading.current_thread()))
        observe = observe or (lambda state: None)
        acquired = False
        with self.condition:
            if owner not in self.queue:
                self.sequence += 1
                self.tickets[owner] = self.sequence
                self.since[owner] = time.time()
                self.queue.append(owner)
            ticket = self.tickets[owner]
            last = None
            try:
                while self.holder is not None or self.queue[0] != owner:
                    if stop.is_set():
                        raise Interrupted('Interrupted while waiting for shared resources')
                    state = {'state': 'waiting', 'ticket': ticket, 'holder': self.holder,
                             'position': self.queue.index(owner) + 1, 'waiting_since': self.since[owner]}
                    if state != last:
                        observe(state)
                        last = state
                    self.condition.wait(.1)
                if stop.is_set():
                    raise Interrupted('Interrupted while waiting for shared resources')
                self.holder = owner
                acquired = True
                observe({'state': 'held', 'ticket': ticket, 'holder': owner, 'position': 0})
            except BaseException:
                if acquired:
                    self.holder = None
                # Retain the waiting checkpoint for restart, but unblock peers now.
                raise
            finally:
                self.queue.remove(owner)
                self.tickets.pop(owner, None)
                self.condition.notify_all()
        try:
            yield
        finally:
            with self.condition:
                self.holder = None
                self.condition.notify_all()
                observe({'state': 'released', 'ticket': ticket, 'holder': None, 'position': 0})

    def withdraw(self, owner):
        """A seeded waiter may stop before it reaches enter (e.g. retry delay)."""
        with self.condition:
            if owner in self.queue:
                self.queue.remove(owner)
                self.tickets.pop(owner, None)
                self.condition.notify_all()

def quality(candidate):
    # Mandatory checks and independent review already passed. Only validated
    # optional gaps discriminate; code size and model self-ratings do not.
    review = candidate['review']
    return len(review.get('optional_findings', []))


def choose(candidates, models):
    return min(candidates, key=lambda c: (quality(c), models.index(c['model'])))
