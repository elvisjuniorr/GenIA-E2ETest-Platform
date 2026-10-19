import threading
import time

from backend.pipelines.genia_orchestrator import GenIAOrchestrator


def test_only_two_pipelines_can_hold_extraction_refinement_slot():
    orchestrator = GenIAOrchestrator()
    for pipeline_id in ("heavy-a", "heavy-b", "heavy-c"):
        orchestrator._create_session(pipeline_id)

    gate = threading.Event()
    two_entered = threading.Event()
    third_entered = threading.Event()
    counter_lock = threading.Lock()
    active = 0
    max_active = 0

    def hold_slot(pipeline_id, entered):
        nonlocal active, max_active
        orchestrator._acquire_heavy_stage_slot(pipeline_id)
        with counter_lock:
            active += 1
            max_active = max(max_active, active)
            if active == 2:
                two_entered.set()
        entered.set()
        assert gate.wait(timeout=3)
        with counter_lock:
            active -= 1
        orchestrator._heavy_stage_semaphore.release()

    threads = [
        threading.Thread(target=hold_slot, args=("heavy-a", threading.Event())),
        threading.Thread(target=hold_slot, args=("heavy-b", threading.Event())),
    ]
    for thread in threads:
        thread.start()
    assert two_entered.wait(timeout=2)

    third = threading.Thread(target=hold_slot, args=("heavy-c", third_entered))
    third.start()
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        if orchestrator._get_session("heavy-c")["state"] == "WAITING_HEAVY_STAGE":
            break
        time.sleep(0.01)

    assert orchestrator._get_session("heavy-c")["state"] == "WAITING_HEAVY_STAGE"
    assert not third_entered.is_set()
    gate.set()
    for thread in [*threads, third]:
        thread.join(timeout=3)
        assert not thread.is_alive()
    assert max_active == 2
