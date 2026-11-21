"""Exercise reminder delivery using two real services and isolated synthetic data.

Never uses data/app-integration or an installed Android application's database.
Build both Cargo projects first; dependencies match requirements-integration.txt.
"""
import json
from pathlib import Path
import sqlite3
import tempfile
import time

from integration_services import AppPeer, Harness, ROOT, query, require, wait_for


def seed_tasks(harness):
    fixtures = json.loads((ROOT / 'contracts/v2/fixtures.json').read_text(encoding='utf-8'))
    example = next(case['payload']['data'] for case in fixtures['cases'] if case['name'] == 'task')
    now = int(time.time() * 1000)
    with sqlite3.connect(harness.database) as db:
        for key, due, expiry in [
            ('ws_due', now + 1500, now + 600_000),
            ('ws_missed', now - 70_000, now + 600_000),
            ('ws_expired', now - 70_000, now - 1000),
        ]:
            task = dict(example, taskId=key, level='warn', status='pending', version=1,
                        actionVersion=1, remindedCount=1, createdAt=due-300_000,
                        lastRemindedAt=due-300_000, nextReminderAt=due,
                        currentReminder=None, snoozeUntil=None, validUntil=expiry)
            db.execute("INSERT INTO objects VALUES ('tasks',?,?)", (key, json.dumps(task)))


def reminder_count(harness, key):
    return query(harness.database, """SELECT COUNT(*) FROM transport_outbox
        WHERE json_extract(frame,'$.payload.kind')='reminder'
        AND json_extract(frame,'$.payload.data.taskId')=?""", (key,))[0][0]


def run(harness):
    harness.start('relay')
    harness.start('local')
    app = AppPeer(harness)
    app.connect()
    identity = harness.identity()
    app.close_socket()
    harness.stop('local')
    seed_tasks(harness)  # Service is stopped; synthetic tasks stay in this test directory.
    harness.start('local')
    app.hold_receipts = True
    app.connect()
    first = app.wait(lambda frame: frame['type'] == 'deliver' and
                     frame['payload'].get('kind') == 'reminder' and
                     frame['payload']['data']['taskId'] == 'ws_due', seconds=20)
    task = first['payload']['data']
    require(task['version'] == 2 and task['actionVersion'] == 1, 'reminder must preserve action version')
    require(task['remindedCount'] == 2 and task['nextReminderAt'] is None, 'warning has only two rounds')
    require(0 < task['currentReminder']['displayUntil'] - task['currentReminder']['scheduledAt'] <= 60_000,
            'bounded reminder display window')
    print('PASS: actual scheduler -> durable outbox -> TLS relay -> App peer, schema/hash verified', flush=True)

    app.close_socket()
    harness.stop('local')
    harness.start('local')
    app.connect()
    replay = app.wait(lambda frame: frame['type'] == 'deliver' and
                      frame['deliveryId'] == first['deliveryId'], seconds=20)
    require(replay == first, 'restart must replay exact immutable reminder after lost receipt')
    require(harness.identity() == identity, 'ordinary restart preserves identity')
    require(reminder_count(harness, 'ws_due') == 1, 'restart must not generate a second reminder')
    app.hold_receipts = False
    for frame in list(app.frames):
        if frame['type'] == 'deliver':
            app.receipt(frame)
    wait_for(lambda: query(harness.database, 'SELECT state FROM transport_outbox WHERE delivery_id=?',
                           (first['deliveryId'],)) == [('delivered',)], 'durable reminder receipt')
    print('PASS: lost receipt/restart reuses reminder and delivery identity; no repeated scheduling', flush=True)

    current = app.call('get_task', {'taskId': 'ws_due'})
    require(current['task']['currentReminder'] == task['currentReminder'], 'App can verify current reminder')
    require(current['serverEpoch'] == identity['server_epoch'] and not current['recoveryMode'],
            'verification includes epoch and recovery gate')
    require(app.call('get_task', {'taskId': 'ws_expired'})['task']['status'] == 'expired', 'expiry becomes a fact')
    require(app.call('get_task', {'taskId': 'ws_missed'})['task']['currentReminder'] is None, 'missed round stays silent')
    require(reminder_count(harness, 'ws_missed') == 0 and reminder_count(harness, 'ws_expired') == 0,
            'no stale catch-up reminders')
    app.call('pause')
    require(app.call('get_task', {'taskId': 'ws_due'})['task']['currentReminder'] is None,
            'pause makes earlier delivered reminder unverifiable')
    print('PASS: current-task verification, expiry, missed-window suppression and pause invalidation', flush=True)
    require(harness.health('local', 'ready') == 503, 'unfinished capabilities remain visibly unready')


def main():
    with tempfile.TemporaryDirectory(prefix='tickdesk-ws-reminders-') as directory:
        harness = Harness(Path(directory))
        try:
            run(harness)
        finally:
            harness.close()
    print('PASS: isolated WS reminder integration complete; deployed workspace data untouched')


if __name__ == '__main__':
    main()
