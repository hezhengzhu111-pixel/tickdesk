mod support;

use serde_json::{json, Value};
use support::{command, deliver, LOCAL_PAIR, PEER};
use tickdesk_local::storage::Storage;
use tickdesk_local::{contract, provider::ProviderError};

fn run(db: &mut Storage, key: &str, name: &str, fields: Value, now: u64) -> Value {
    let input = command(db, &format!("event-{key}"), key, name, fields);
    db.receive(&deliver(db, PEER, key, &input)).unwrap();
    db.process(db.job(key).unwrap().unwrap(), now)
        .unwrap()
        .unwrap()
}

#[test]
fn accepted_chat_persists_user_history_and_replays_original_ack_after_restart() {
    let dir = tempfile::tempdir().unwrap();
    let mut db = Storage::open(dir.path()).unwrap();
    db.bind_peer(LOCAL_PAIR, PEER).unwrap();
    let ack = run(
        &mut db,
        "chat-one",
        "chat",
        json!({"text":"解释未知持仓数量"}),
        100,
    );
    assert_eq!(ack["data"]["ok"], true);
    assert_eq!(
        ack["data"]["result"],
        json!({"accepted":true,"requestId":"chat-one"})
    );
    drop(db);
    let mut db = Storage::open(dir.path()).unwrap();
    let duplicate = command(
        &db,
        "event-chat-one",
        "chat-one",
        "chat",
        json!({"text":"解释未知持仓数量"}),
    );
    db.receive(&deliver(&db, PEER, "new-delivery", &duplicate))
        .unwrap();
    let replay = db
        .process(db.job("new-delivery").unwrap().unwrap(), 200)
        .unwrap()
        .unwrap();
    assert_eq!(replay, ack);
    let history = run(&mut db, "history-one", "get_messages", json!({}), 201);
    let items = history["data"]["result"]["items"].as_array().unwrap();
    assert_eq!(items.len(), 1);
    assert_eq!(items[0]["role"], "user");
    assert_eq!(items[0]["text"], "解释未知持仓数量");
    assert_eq!(items[0]["requestId"], "chat-one");
}

fn accepted() -> (tempfile::TempDir, Storage) {
    let dir = tempfile::tempdir().unwrap();
    let mut db = Storage::open(dir.path()).unwrap();
    db.bind_peer(LOCAL_PAIR, PEER).unwrap();
    assert_eq!(
        run(&mut db, "chat-one", "chat", json!({"text":"解释数据"}), 100)["data"]["ok"],
        true
    );
    (dir, db)
}

#[test]
fn a_new_manual_report_refreshes_the_directory_without_requesting_a_notice() {
    let (dir, mut db) = accepted();
    let job = db.claim_chat(101).unwrap().unwrap();
    let fixture = rusqlite::Connection::open(dir.path().join("tickdesk.sqlite3")).unwrap();
    fixture
        .execute(
            "UPDATE chat_jobs SET prompt_version='tickdesk-assistant-v1' WHERE job_id=?1",
            [&job.job_id],
        )
        .unwrap();
    let before = run(&mut db, "directory-before", "list_reports", json!({}), 102);
    assert_eq!(before["data"]["result"]["items"], json!([]));
    let raw = json!({
        "text":"已保存研究观察。","sourceRefs":[],
        "artifact":{"report":{"reportId":job.job_id,"symbols":["300502.SZ"],"asOf":100,
                              "validUntil":10000,"body":{"stance":"watch","summary":{"text":"量化结论不可用"}},
                              "quantOriginal":[{"entry":{"value":null},"exit":{"value":true},"score":{"value":null}},
                                               {"entry":{"value":false},"exit":{"value":false},"score":{"value":"0"}}],
                              "productionQuant":{"status":"succeeded","candidates":[{"direction":"sell"}]}},
                    "context":{"quality":{"business":"partial","quant":"partial"}}}
    }).to_string();
    fixture
        .execute_batch(
            "CREATE TRIGGER report_outbox_fault BEFORE INSERT ON transport_outbox
         WHEN json_extract(NEW.frame,'$.payload.kind')='analysis_report'
         BEGIN SELECT RAISE(ABORT, 'injected publication failure'); END;",
        )
        .unwrap();
    assert!(db.finish_chat(&job, Ok(raw.clone()), 103).is_err());
    assert!(db.outbox(16).unwrap().iter().all(|frame| {
        !matches!(
            frame["payload"]["kind"].as_str(),
            Some("analysis_report" | "reply")
        )
    }));
    fixture
        .execute_batch("DROP TRIGGER report_outbox_fault")
        .unwrap();
    let reply = db.finish_chat(&job, Ok(raw.clone()), 103).unwrap().unwrap();
    assert_eq!(reply["data"]["status"], "completed");
    assert!(db.finish_chat(&job, Ok(raw), 104).unwrap().is_none());
    drop(db);
    let mut db = Storage::open(dir.path()).unwrap();
    let publications: Vec<_> = db
        .outbox(16)
        .unwrap()
        .into_iter()
        .map(|frame| frame["payload"].clone())
        .filter(|payload| payload["kind"] == "analysis_report")
        .collect();
    assert_eq!(
        publications.len(),
        1,
        "manual report needs a durable directory refresh event"
    );
    let publication = &publications[0];
    contract::validate_business(publication).unwrap();
    assert_eq!(publication["data"]["notifyUntil"], Value::Null);
    assert_eq!(publication["data"]["header"]["origin"], "manual");
    assert_eq!(publication["data"]["header"]["summary"],
        "正式候选 · 卖出 1 项\n研究观察 · 入场未触发 1 项 · 入场待核实 1 项 · 退出触发 1 项 · 退出未触发 1 项 · 评分已计算 1 项 · 评分待核实 1 项");
    assert!(publication["snapshotSeq"].as_u64() > reply["snapshotSeq"].as_u64());
    let directory = run(&mut db, "directory-after", "list_reports", json!({}), 105);
    assert_eq!(
        directory["data"]["result"]["items"],
        json!([publication["data"]["header"]])
    );
    let detail = run(
        &mut db,
        "manual-report",
        "get_report",
        json!({"reportId":job.request_id}),
        106,
    );
    assert_eq!(
        detail["data"]["result"]["header"],
        publication["data"]["header"]
    );
    assert_eq!(detail["data"]["result"]["text"], reply["data"]["text"]);
    assert_eq!(
        detail["data"]["result"]["sources"],
        reply["data"]["sources"]
    );
}

#[test]
fn focused_analysis_revalidates_only_the_observations_it_received() {
    let dir = tempfile::tempdir().unwrap();
    let mut db = Storage::open(dir.path()).unwrap();
    db.bind_peer(LOCAL_PAIR, PEER).unwrap();
    let epoch = db.identity.server_epoch.clone();
    let fixture = rusqlite::Connection::open(dir.path().join("tickdesk.sqlite3")).unwrap();

    let row = |symbol: &str, entry| {
        json!({"symbol":symbol,"group":"daily_swing","strategyId":"fixture-daily",
               "timeframe":"1d","admission":"research","entry":{"value":entry}})
    };
    let save_scan = |id: &str, rows: Vec<Value>| {
        fixture
            .execute(
                "INSERT INTO objects(collection,object_id,body) VALUES ('market_quant_observations',?1,?2)",
                rusqlite::params![id,json!({"scanId":id,"serverEpoch":epoch,"batch":{"observations":rows}}).to_string()],
            )
            .unwrap();
        fixture
            .execute(
                "INSERT OR REPLACE INTO objects(collection,object_id,body) VALUES ('market_meta','latest_analysis_scan',?1)",
                [json!({"scanId":id,"serverEpoch":epoch}).to_string()],
            )
            .unwrap();
    };
    save_scan("scan-other-only", vec![row("600519.SH", false)]);
    let submit = |db: &mut Storage, request, at| {
        assert_eq!(
            run(db, request, "chat", json!({"text":"/分析 600000.SH"}), at)["data"]["ok"],
            true
        );
        let job = db.claim_chat(at + 1).unwrap().unwrap();
        fixture
            .execute(
                "UPDATE chat_jobs SET prompt_version='tickdesk-assistant-v1' WHERE job_id=?1",
                [&job.job_id],
            )
            .unwrap();
        job
    };
    let finish = |db: &mut Storage, job: &_, at| {
        db.finish_chat(
            job,
            Ok(json!({"text":"证据已核对。","sourceRefs":[]}).to_string()),
            at,
        )
        .unwrap()
        .unwrap()
    };

    let without_target = submit(&mut db, "without-target", 100);
    assert_eq!(
        without_target.evidence["quant"]["batch"]["observations"],
        json!([])
    );

    save_scan(
        "scan-target-and-other",
        vec![row("600000.SH", false), row("600519.SH", false)],
    );
    let unchanged_target = submit(&mut db, "outside-change", 110);
    save_scan(
        "scan-other-changed",
        vec![row("600000.SH", false), row("600519.SH", true)],
    );
    let outside_change = finish(&mut db, &unchanged_target, 112);

    let changed_target = submit(&mut db, "inside-change", 120);
    save_scan(
        "scan-target-changed",
        vec![row("600000.SH", true), row("600519.SH", true)],
    );
    let inside_change = finish(&mut db, &changed_target, 122);
    assert_eq!(inside_change["data"]["errorCode"], "AI_STALE_CONTEXT");
    assert_eq!(outside_change["data"]["status"], "completed");

    save_scan("scan-no-target-again", vec![row("600519.SH", true)]);
    let outside_pool = finish(&mut db, &without_target, 123);
    assert_eq!(outside_pool["data"]["status"], "completed");
}

#[test]
fn evidence_stops_reading_after_the_item_budget_and_marks_truncation() {
    let dir = tempfile::tempdir().unwrap();
    let mut db = Storage::open(dir.path()).unwrap();
    db.bind_peer(LOCAL_PAIR, PEER).unwrap();
    let connection = rusqlite::Connection::open(dir.path().join("tickdesk.sqlite3")).unwrap();
    for index in 0..20 {
        let symbol = format!("{index:06}.SZ");
        let record = json!({"symbol":symbol,"name":"测试","addedAt":1,"dataAsOf":null,"riskStatus":"unknown"});
        connection
            .execute(
                "INSERT INTO objects VALUES ('watchlist',?1,?2)",
                rusqlite::params![symbol, record.to_string()],
            )
            .unwrap();
    }
    // This tail is outside the evidence budget and must not be loaded/decoded.
    connection
        .execute(
            "INSERT INTO objects VALUES ('watchlist','999999.SZ','not-json')",
            [],
        )
        .unwrap();
    let ack = run(
        &mut db,
        "bounded-chat",
        "chat",
        json!({"text":"解释关注列表"}),
        100,
    );
    assert_eq!(ack["data"]["ok"], true);
    let job = db.claim_chat(101).unwrap().unwrap();
    assert_eq!(job.evidence["items"].as_array().unwrap().len(), 20);
    assert_eq!(job.evidence["truncated"], true);
    assert_eq!(job.evidence["items"][0]["record"]["symbol"], "000000.SZ");
    assert_eq!(job.evidence["items"][19]["record"]["symbol"], "000019.SZ");
}

#[test]
fn final_reply_is_durable_unverified_and_duplicate_completion_has_no_effect() {
    let (dir, mut db) = accepted();
    let job = db.claim_chat(101).unwrap().unwrap();
    assert!(db.claim_chat(102).unwrap().is_none());
    let raw = json!({"text":"目前资料不足，现金余额未知。","sourceRefs":[]}).to_string();
    let reply = db.finish_chat(&job, Ok(raw.clone()), 103).unwrap().unwrap();
    assert_eq!(reply["kind"], "reply");
    assert_eq!(reply["data"]["status"], "completed");
    assert_eq!(reply["data"]["unverified"], true);
    assert!(db.finish_chat(&job, Ok(raw), 104).unwrap().is_none());
    drop(db);
    let mut db = Storage::open(dir.path()).unwrap();
    let history = run(&mut db, "history", "get_messages", json!({}), 105);
    let messages = history["data"]["result"]["items"].as_array().unwrap();
    assert_eq!(messages.len(), 2);
    assert_eq!(messages[0]["reply"], reply["data"]);
    assert_eq!(messages[1]["role"], "user");
    assert!(db.claim_chat(106).unwrap().is_none());
}

#[test]
fn failed_outbox_commit_rolls_back_history_and_can_retry_same_result() {
    let (dir, mut db) = accepted();
    let job = db.claim_chat(101).unwrap().unwrap();
    let fault = rusqlite::Connection::open(dir.path().join("tickdesk.sqlite3")).unwrap();
    fault.execute_batch("CREATE TRIGGER block_reply BEFORE INSERT ON transport_outbox BEGIN SELECT RAISE(ABORT,'disk'); END;").unwrap();
    assert!(db
        .finish_chat(&job, Err(ProviderError::NotConfigured), 102)
        .is_err());
    let count: i64 = fault
        .query_row(
            "SELECT COUNT(*) FROM objects WHERE collection='messages'",
            [],
            |r| r.get(0),
        )
        .unwrap();
    assert_eq!(count, 1);
    fault.execute_batch("DROP TRIGGER block_reply").unwrap();
    let reply = db
        .finish_chat(&job, Err(ProviderError::NotConfigured), 103)
        .unwrap()
        .unwrap();
    assert_eq!(reply["data"]["status"], "failed");
    assert_eq!(reply["data"]["errorCode"], "AI_NOT_CONFIGURED");
}

#[test]
fn restart_requeues_inflight_chat_for_cached_replay_and_fences_replaced_peer() {
    let (dir, mut db) = accepted();
    let job = db.claim_chat(101).unwrap().unwrap();
    drop(db);
    let mut db = Storage::open(dir.path()).unwrap();
    assert_eq!(db.recover_interrupted_chats(102).unwrap(), 0);
    let retried = db.claim_chat(103).unwrap().unwrap();
    assert_eq!(retried.job_id, job.job_id);
    let history = run(&mut db, "history", "get_messages", json!({}), 104);
    let items = history["data"]["result"]["items"].as_array().unwrap();
    assert_eq!(items.len(), 1);
    assert!(items[0].get("reply").is_none());
    db.bind_peer(LOCAL_PAIR, "replacement-app").unwrap();
    assert!(db
        .finish_chat(&retried, Err(ProviderError::Unavailable), 105)
        .unwrap()
        .is_none());
    assert!(db.claim_chat(106).unwrap().is_none());
}

#[test]
fn restart_finishes_overdue_inflight_chat_as_interrupted() {
    let (dir, mut db) = accepted();
    let job = db.claim_chat(101).unwrap().unwrap();
    drop(db);
    let mut db = Storage::open(dir.path()).unwrap();
    assert_eq!(db.recover_interrupted_chats(job.deadline + 1).unwrap(), 1);
    assert!(db.claim_chat(job.deadline + 2).unwrap().is_none());
    let history = run(
        &mut db,
        "history",
        "get_messages",
        json!({}),
        job.deadline + 3,
    );
    assert_eq!(
        history["data"]["result"]["items"][0]["reply"]["errorCode"],
        "AI_TIMEOUT"
    );
}

#[test]
fn invented_sources_fail_and_expired_results_never_publish_success() {
    let (_dir, mut db) = accepted();
    let job = db.claim_chat(101).unwrap().unwrap();
    let reply = db
        .finish_chat(
            &job,
            Ok(json!({
                "text":"可信公告","sourceRefs":["https://invented.invalid"],
                "extraEvidence":[{"source":{"name":"伪造公告","ref":"https://invented.invalid","asOf":100}}]
            }).to_string()),
            102,
        )
        .unwrap()
        .unwrap();
    assert_eq!(reply["data"]["status"], "failed");
    assert_eq!(reply["data"]["errorCode"], "AI_UNVERIFIED_SOURCE");
    assert_eq!(reply["data"]["sources"], json!([]));
    run(&mut db, "chat-two", "chat", json!({"text":"新问题"}), 103);
    let job = db.claim_chat(104).unwrap().unwrap();
    let reply = db
        .finish_chat(
            &job,
            Ok(json!({"text":"late","sourceRefs":[]}).to_string()),
            job.deadline,
        )
        .unwrap()
        .unwrap();
    assert_eq!(reply["data"]["errorCode"], "AI_TIMEOUT");
}

#[test]
fn answer_longer_than_previous_text_limit_is_delivered_in_full_when_it_fits_the_frame() {
    let (_dir, mut db) = accepted();
    let job = db.claim_chat(101).unwrap().unwrap();
    let text = "甲".repeat(18_000);
    let raw = json!({"text":text,"sourceRefs":[]}).to_string();
    let reply = db.finish_chat(&job, Ok(raw), 102).unwrap().unwrap();
    contract::validate_business(&reply).unwrap();
    assert_eq!(reply["data"]["status"], "completed");
    let delivered = reply["data"]["text"].as_str().unwrap();
    assert!(delivered.ends_with(&text));
    assert!(!delivered.contains("节选"));
    let history = run(&mut db, "history", "get_messages", json!({}), 103);
    assert_eq!(
        history["data"]["result"]["items"][0]["reply"],
        reply["data"]
    );
}

#[test]
fn large_unicode_reply_is_summarized_before_publication_and_history_is_identical() {
    let (dir, mut db) = accepted();
    let job = db.claim_chat(101).unwrap().unwrap();
    let raw = json!({"text":"甲".repeat(23999),"sourceRefs":[]}).to_string();
    let reply = db.finish_chat(&job, Ok(raw.clone()), 102).unwrap().unwrap();
    contract::validate_business(&reply).unwrap();
    assert!(contract::canonical(&reply).unwrap().len() < contract::BUSINESS_LIMIT);
    assert!(reply["data"]["text"].as_str().unwrap().contains("节选"));
    let history = run(&mut db, "history", "get_messages", json!({}), 103);
    assert!(contract::canonical(&history).unwrap().len() < contract::BUSINESS_LIMIT);
    assert_eq!(
        history["data"]["result"]["items"][0]["reply"],
        reply["data"]
    );
    let check = rusqlite::Connection::open(dir.path().join("tickdesk.sqlite3")).unwrap();
    let artifact: String = check
        .query_row("SELECT raw_output FROM chat_jobs", [], |r| r.get(0))
        .unwrap();
    assert_eq!(artifact, raw);
}

#[test]
fn accepted_full_reply_pages_reconstruct_unicode_and_keep_evidence_private() {
    let (dir, mut db) = accepted();
    let job = db.claim_chat(101).unwrap().unwrap();
    let original = "中文🙂e\u{301}\n\"\\\u{1}".repeat(9000);
    let raw = json!({
        "text": original, "sourceRefs": [],
        "artifact": {"context":{"prompt":"PRIVATE_PROMPT"}, "reasoning":"PRIVATE_REASONING"}
    });
    let published = db
        .finish_chat(&job, Ok(raw.to_string()), 102)
        .unwrap()
        .unwrap();
    assert!(published["data"]["text"].as_str().unwrap().contains("节选"));
    drop(db);
    let mut db = Storage::open(dir.path()).unwrap();
    let mut collected = String::new();
    let mut offset = 0;
    let mut checksum = None;
    loop {
        let mut args = json!({"requestId":"chat-one"});
        if let Some(checksum) = &checksum {
            args["offset"] = json!(offset);
            args["sha256"] = json!(checksum);
        }
        let ack = run(
            &mut db,
            &format!("page-{offset}"),
            "get_reply_page",
            args,
            103,
        );
        contract::validate_business(&ack).unwrap();
        assert!(contract::canonical(&ack).unwrap().len() <= contract::BUSINESS_LIMIT);
        assert_eq!(ack["data"]["ok"], true, "{ack}");
        let result = &ack["data"]["result"];
        let text = result["text"].as_str().unwrap();
        assert!(!text.contains("PRIVATE_PROMPT") && !text.contains("PRIVATE_REASONING"));
        assert!(text.chars().count() <= 2048 && !text.is_empty());
        assert_eq!(result["requestId"], "chat-one");
        assert_eq!(result["offset"], offset);
        let digest = result["sha256"].as_str().unwrap();
        assert_eq!(checksum.get_or_insert_with(|| digest.to_owned()), digest);
        collected.push_str(text);
        let Some(next) = result["nextOffset"].as_u64() else {
            assert_eq!(result["totalChars"], collected.chars().count());
            break;
        };
        assert_eq!(next - offset, text.chars().count() as u64);
        offset = next;
    }
    assert!(collected.ends_with(&original));
    assert_eq!(checksum.unwrap(), contract::sha256(collected.as_bytes()));
    for (key, args) in [
        ("no-digest", json!({"requestId":"chat-one","offset":1})),
        (
            "bad-digest",
            json!({"requestId":"chat-one","sha256":"0".repeat(64)}),
        ),
        (
            "out-of-range",
            json!({"requestId":"chat-one","offset":9007199254740991u64,
                                 "sha256":contract::sha256(collected.as_bytes())}),
        ),
    ] {
        assert_eq!(
            run(&mut db, key, "get_reply_page", args, 104)["data"]["error"]["code"],
            "INVALID_PARAMS"
        );
    }
}

#[test]
fn full_reply_and_cached_command_results_are_scoped_to_original_fence() {
    let (_dir, mut db) = accepted();
    let job = db.claim_chat(101).unwrap().unwrap();
    let original = "仅对原配对端可见";
    db.finish_chat(
        &job,
        Ok(json!({"text":original,"sourceRefs":[]}).to_string()),
        102,
    )
    .unwrap();
    let fields = json!({"requestId":"chat-one"});
    let page_command = command(&db, "event-page", "page", "get_reply_page", fields.clone());
    let ack = run(&mut db, "page", "get_reply_page", fields.clone(), 103);
    assert!(ack["data"]["result"]["text"]
        .as_str()
        .unwrap()
        .contains(original));
    let epoch = db.identity.server_epoch.clone();
    assert_eq!(
        support::status_query(&mut db, PEER, "old-status", &epoch, &page_command, None)["data"]
            ["result"]["state"],
        "found"
    );
    let replacement = "replacement-app";
    db.bind_peer(LOCAL_PAIR, replacement).unwrap();
    for (delivery, payload) in [
        ("duplicate-page", page_command.clone()),
        (
            "fresh-page",
            command(&db, "event-fresh", "fresh", "get_reply_page", fields),
        ),
    ] {
        db.receive(&deliver(&db, replacement, delivery, &payload))
            .unwrap();
        let ack = db
            .process(db.job(delivery).unwrap().unwrap(), 104)
            .unwrap()
            .unwrap();
        assert_eq!(ack["data"]["error"]["code"], "AI_REPLY_UNAVAILABLE");
        assert!(!ack.to_string().contains(original));
    }
    let lookup = support::status_query(
        &mut db,
        replacement,
        "new-status",
        &epoch,
        &page_command,
        None,
    );
    assert_eq!(lookup["data"]["result"]["state"], "not_found");
    let replay = support::status_query(
        &mut db,
        replacement,
        "old-status",
        &epoch,
        &page_command,
        None,
    );
    assert_eq!(replay["data"]["error"]["code"], "AI_REPLY_UNAVAILABLE");
}

#[test]
fn failed_and_stale_chat_outputs_never_return_reply_text() {
    let (dir, mut db) = accepted();
    assert_eq!(
        run(
            &mut db,
            "pending",
            "get_reply_page",
            json!({"requestId":"chat-one"}),
            101
        )["data"]["error"]["code"],
        "AI_REPLY_UNAVAILABLE"
    );
    let job = db.claim_chat(102).unwrap().unwrap();
    let fault = rusqlite::Connection::open(dir.path().join("tickdesk.sqlite3")).unwrap();
    fault
        .execute(
            "UPDATE chat_jobs SET prompt_version='tickdesk-assistant-v1' WHERE job_id=?1",
            [&job.job_id],
        )
        .unwrap();
    let raw = json!({"text":"STALE_PAYLOAD","sourceRefs":[],"artifact":{"staleReason":"ANALYSIS_EVIDENCE_CHANGED"}});
    let reply = db
        .finish_chat(&job, Ok(raw.to_string()), 103)
        .unwrap()
        .unwrap();
    assert_eq!(reply["data"]["errorCode"], "AI_STALE_CONTEXT");
    let ack = run(
        &mut db,
        "stale",
        "get_reply_page",
        json!({"requestId":"chat-one"}),
        104,
    );
    assert_eq!(ack["data"]["error"]["code"], "AI_REPLY_UNAVAILABLE");
    assert!(!ack.to_string().contains("STALE_PAYLOAD"));
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn worker_retries_persistence_without_repeating_http_and_never_holds_storage_over_network() {
    use axum::{routing::post, Json, Router};
    use std::sync::{
        atomic::{AtomicUsize, Ordering},
        Arc, Mutex,
    };
    use tickdesk_local::{
        chat,
        commands::now_ms,
        provider::{AiProvider, AiProviderConfig},
    };
    let count = Arc::new(AtomicUsize::new(0));
    let released = Arc::new(tokio::sync::Notify::new());
    let calls = count.clone();
    let gate = released.clone();
    let router = Router::new().route("/v1/chat/completions", post(move || {
        let calls = calls.clone();
        let gate = gate.clone();
        async move {
            calls.fetch_add(1, Ordering::SeqCst);
            gate.notified().await;
            Json(json!({"choices":[{"finish_reason":"stop","message":{"content":"{\"text\":\"来自测试服务的说明\",\"sourceRefs\":[]}"}}]}))
        }
    }));
    let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
    let provider = AiProvider::new(&AiProviderConfig {
        endpoint: format!(
            "http://{}/v1/chat/completions",
            listener.local_addr().unwrap()
        ),
        model: "test".into(),
        key_file: None,
        timeout_ms: 5000,
        max_response_bytes: 4096,
    })
    .unwrap();
    let server = tokio::spawn(async move {
        axum::serve(listener, router).await.unwrap();
    });
    let dir = tempfile::tempdir().unwrap();
    let mut db = Storage::open(dir.path()).unwrap();
    db.bind_peer(LOCAL_PAIR, PEER).unwrap();
    run(
        &mut db,
        "chat-live",
        "chat",
        json!({"text":"测试"}),
        now_ms(),
    );
    let storage = Arc::new(Mutex::new(db));
    let (stop, shutdown) = tokio::sync::watch::channel(false);
    let worker = tokio::spawn(chat::run(storage.clone(), Some(provider), shutdown));
    tokio::time::timeout(std::time::Duration::from_secs(3), async {
        while count.load(Ordering::SeqCst) == 0 {
            tokio::time::sleep(std::time::Duration::from_millis(10)).await;
        }
    })
    .await
    .unwrap();
    {
        let mut db = storage
            .try_lock()
            .expect("network call must release storage mutex");
        let ack = run(&mut db, "while-ai", "get_messages", json!({}), now_ms());
        assert_eq!(ack["data"]["result"]["items"].as_array().unwrap().len(), 1);
    }
    let fault = rusqlite::Connection::open(dir.path().join("tickdesk.sqlite3")).unwrap();
    fault.execute_batch("CREATE TRIGGER block_reply BEFORE INSERT ON transport_outbox BEGIN SELECT RAISE(ABORT,'disk'); END;").unwrap();
    released.notify_one();
    tokio::time::sleep(std::time::Duration::from_millis(300)).await;
    fault.execute_batch("DROP TRIGGER block_reply").unwrap();
    let finished = tokio::time::timeout(std::time::Duration::from_secs(3), async {
        loop {
            let complete: bool = fault
                .query_row(
                    "SELECT EXISTS(SELECT 1 FROM chat_jobs WHERE state='completed')",
                    [],
                    |r| r.get(0),
                )
                .unwrap();
            if complete {
                break;
            }
            tokio::time::sleep(std::time::Duration::from_millis(25)).await;
        }
    })
    .await;
    let _ = stop.send(true);
    let worker_result = worker.await.unwrap();
    server.abort();
    assert!(
        finished.is_ok(),
        "transient persistence failure must not lose completed AI output: {worker_result:?}"
    );
    worker_result.unwrap();
    assert_eq!(count.load(Ordering::SeqCst), 1);
    let mut db = storage.lock().unwrap();
    let history = run(&mut db, "final", "get_messages", json!({}), now_ms());
    assert_eq!(
        history["data"]["result"]["items"][0]["reply"]["status"],
        "completed"
    );
}
