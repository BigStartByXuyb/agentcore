"""Tests for TaskStore."""

from src.task_store import TaskStore


class TestCreate:
    def test_creates_task_with_defaults(self):
        store = TaskStore()
        task = store.create("Write unit tests")
        assert task.id == 1
        assert task.content == "Write unit tests"
        assert task.status == "pending"
        assert task.active_form == ""

    def test_creates_with_active_form(self):
        store = TaskStore()
        task = store.create("Write tests", active_form="Writing tests")
        assert task.active_form == "Writing tests"

    def test_auto_increments_ids(self):
        store = TaskStore()
        t1 = store.create("Task 1")
        t2 = store.create("Task 2")
        t3 = store.create("Task 3")
        assert t1.id == 1
        assert t2.id == 2
        assert t3.id == 3


class TestUpdate:
    def test_update_status(self):
        store = TaskStore()
        store.create("Task A")
        updated = store.update(1, status="in_progress")
        assert updated is not None
        assert updated.status == "in_progress"

    def test_update_content(self):
        store = TaskStore()
        store.create("Old content")
        updated = store.update(1, content="New content")
        assert updated is not None
        assert updated.content == "New content"

    def test_update_active_form(self):
        store = TaskStore()
        store.create("Task")
        updated = store.update(1, active_form="Working on task")
        assert updated is not None
        assert updated.active_form == "Working on task"

    def test_update_nonexistent_returns_none(self):
        store = TaskStore()
        assert store.update(999, status="completed") is None

    def test_update_multiple_fields(self):
        store = TaskStore()
        store.create("Task")
        updated = store.update(1, status="completed", content="Done task")
        assert updated is not None
        assert updated.status == "completed"
        assert updated.content == "Done task"


class TestDelete:
    def test_delete_existing(self):
        store = TaskStore()
        store.create("To delete")
        assert store.delete(1) is True
        assert store.get(1) is None

    def test_delete_nonexistent(self):
        store = TaskStore()
        assert store.delete(999) is False


class TestGet:
    def test_get_existing(self):
        store = TaskStore()
        store.create("My task")
        task = store.get(1)
        assert task is not None
        assert task.content == "My task"

    def test_get_nonexistent(self):
        store = TaskStore()
        assert store.get(42) is None


class TestListAll:
    def test_empty_store(self):
        store = TaskStore()
        assert store.list_all() == []

    def test_returns_all_tasks(self):
        store = TaskStore()
        store.create("A")
        store.create("B")
        store.create("C")
        tasks = store.list_all()
        assert len(tasks) == 3
        contents = [t.content for t in tasks]
        assert "A" in contents
        assert "B" in contents
        assert "C" in contents


class TestHasActive:
    def test_empty_store(self):
        store = TaskStore()
        assert store.has_active() is False

    def test_all_completed(self):
        store = TaskStore()
        store.create("Done")
        store.update(1, status="completed")
        assert store.has_active() is False

    def test_has_pending(self):
        store = TaskStore()
        store.create("Pending")
        assert store.has_active() is True

    def test_has_in_progress(self):
        store = TaskStore()
        store.create("WIP")
        store.update(1, status="in_progress")
        assert store.has_active() is True

    def test_mixed(self):
        store = TaskStore()
        store.create("Done")
        store.create("Still going")
        store.update(1, status="completed")
        assert store.has_active() is True


class TestClear:
    def test_clears_all_tasks(self):
        store = TaskStore()
        store.create("A")
        store.create("B")
        store.clear()
        assert store.list_all() == []
        assert store.has_active() is False

    def test_resets_id_counter(self):
        store = TaskStore()
        store.create("A")
        store.create("B")
        store.clear()
        new_task = store.create("C")
        assert new_task.id == 1
