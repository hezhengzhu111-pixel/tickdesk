from app.services.tickdesk_ai_outcomes import AiOutcomeStore


def test_outcome_store_replays_within_ttl_and_expires(tmp_path):
    store = AiOutcomeStore(tmp_path / "ai-outcomes.sqlite3")
    assert store.get("chat", "epoch", "job-1", 1000) is None
    store.put("chat", "epoch", "job-1", 1000, 200, '{"ok":true}')
    assert store.get("chat", "epoch", "job-1", 1001) == (200, '{"ok":true}')
    assert store.get("chat", "other", "job-1", 1001) is None
    assert store.get("chat", "epoch", "job-1", 1000 + 60 * 60 * 1000 + 1) is None


def test_outcome_store_replaces_and_prunes_expired(tmp_path):
    store = AiOutcomeStore(tmp_path / "ai-outcomes.sqlite3")
    store.put("chat", "epoch", "job-1", 1000, 422, '{"detail":"AI_INVALID_RESPONSE"}')
    store.put("chat", "epoch", "job-1", 2000, 200, '{"ok":true}')
    assert store.get("chat", "epoch", "job-1", 2001) == (200, '{"ok":true}')
    store.put("chat", "epoch", "job-2", 2000, 200, '{"ok":true}')
    assert store.get("chat", "epoch", "job-2", 2000 + 60 * 60 * 1000 + 1) is None
