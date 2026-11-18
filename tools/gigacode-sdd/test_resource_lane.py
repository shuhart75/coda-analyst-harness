from concurrent.futures import ThreadPoolExecutor
import threading
import unittest
from controller import Interrupted
from execution_scheduling import ResourceLane


class ResourceLaneTests(unittest.TestCase):
    def test_resume_restores_fifo_even_when_later_thread_starts_first(self):
        lane = ResourceLane([('first', {'state': 'waiting', 'ticket': 2, 'waiting_since': 10}),
                             ('second', {'state': 'waiting', 'ticket': 3, 'waiting_since': 20})])
        stop = threading.Event(); waiting = threading.Event(); order = []; records = []
        def observe(state):
            records.append(state)
            if state['state'] == 'waiting': waiting.set()
        def second():
            with lane.enter(stop, 'second', observe): order.append('second')
        with ThreadPoolExecutor(1) as pool:
            future = pool.submit(second)
            self.assertTrue(waiting.wait(2))
            with lane.enter(stop, 'first'): order.append('first')
            future.result(timeout=2)
        self.assertEqual(order, ['first', 'second'])
        self.assertEqual(records[0]['waiting_since'], 20)
        self.assertEqual(records[-1]['state'], 'released')

    def test_waiter_observes_holder_and_cancel_does_not_release_holder(self):
        lane = ResourceLane(); stop = threading.Event(); waiting = threading.Event(); records = []
        def observe(state): records.append(state); waiting.set()
        def peer():
            with lane.enter(stop, 'peer', observe): self.fail('cancelled waiter acquired lease')
        with lane.enter(threading.Event(), 'holder'):
            with ThreadPoolExecutor(1) as pool:
                future = pool.submit(peer)
                self.assertTrue(waiting.wait(2)); stop.set()
                with self.assertRaises(Interrupted): future.result(timeout=2)
            self.assertEqual(lane.holder, 'holder')
        self.assertEqual(records[0]['holder'], 'holder')
        self.assertIsNone(lane.holder)
        self.assertEqual(lane.queue, [])

    def test_persistence_failure_releases_lease(self):
        lane = ResourceLane()
        def broken(state): raise OSError('disk')
        with self.assertRaises(OSError):
            with lane.enter(threading.Event(), 'one', broken): pass
        with lane.enter(threading.Event(), 'two'): self.assertEqual(lane.holder, 'two')

    def test_abandoned_seed_does_not_strand_peer(self):
        lane = ResourceLane([('old', {'state': 'held', 'ticket': 1})])
        self.assertIsNone(lane.holder)  # Persistent records never grant ownership.
        lane.withdraw('old')
        with lane.enter(threading.Event(), 'new'): self.assertEqual(lane.holder, 'new')
