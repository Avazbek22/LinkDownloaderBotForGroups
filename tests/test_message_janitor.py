from __future__ import annotations

from app.message_janitor import MessageJanitor


class Clock:
    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        return self.now


def test_messages_are_deleted_only_once_their_delay_has_passed() -> None:
    clock = Clock()
    deleted: list[tuple[int, int]] = []
    janitor = MessageJanitor(lambda chat_id, message_id: deleted.append((chat_id, message_id)), clock)
    janitor.schedule(-100, 1, 10)
    janitor.schedule(-100, 2, 60)

    clock.now += 9
    janitor.run_due()
    assert deleted == []

    clock.now += 1
    janitor.run_due()
    janitor.run_due()
    assert deleted == [(-100, 1)]


def test_rescheduling_replaces_the_earlier_deadline() -> None:
    clock = Clock()
    deleted: list[tuple[int, int]] = []
    janitor = MessageJanitor(lambda chat_id, message_id: deleted.append((chat_id, message_id)), clock)
    janitor.schedule(-100, 1, 60)
    janitor.schedule(-100, 1, 10)

    clock.now += 10
    janitor.run_due()
    clock.now += 60
    janitor.run_due()

    assert deleted == [(-100, 1)]


def test_immediate_delete_cancels_the_scheduled_one() -> None:
    clock = Clock()
    deleted: list[tuple[int, int]] = []
    janitor = MessageJanitor(lambda chat_id, message_id: deleted.append((chat_id, message_id)), clock)
    janitor.schedule(-100, 1, 60)

    janitor.delete_now(-100, 1)
    clock.now += 60
    janitor.run_due()

    assert deleted == [(-100, 1)]


def test_flush_deletes_everything_left_and_failures_do_not_escape() -> None:
    deleted: list[tuple[int, int]] = []

    def delete(chat_id: int, message_id: int) -> None:
        if message_id == 1:
            raise RuntimeError("message to delete not found")
        deleted.append((chat_id, message_id))

    janitor = MessageJanitor(delete, Clock())
    janitor.schedule(-100, 1, 60)
    janitor.schedule(-100, 2, 60)

    janitor.flush()
    janitor.flush()

    assert deleted == [(-100, 2)]
