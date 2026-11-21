use anyhow::{bail, ensure, Context};
use std::{
    path::PathBuf,
    sync::{Arc, Mutex},
};
use tickdesk_local::{backup, config::Config, contract, http, kernel, relay, storage::Storage};

enum Action {
    Serve,
    Check,
    EnterRecovery,
    Backup(PathBuf),
    Restore(PathBuf),
    ReviewOriginal(String, &'static str),
}

#[tokio::main]
async fn main() -> anyhow::Result<()> {
    tracing_subscriber::fmt()
        .with_env_filter(
            tracing_subscriber::EnvFilter::try_from_default_env().unwrap_or_else(|_| "info".into()),
        )
        .with_target(false)
        .init();
    let mut path = PathBuf::from("config/dev.toml");
    let mut action = Action::Serve;
    let mut args = std::env::args().skip(1);
    while let Some(arg) = args.next() {
        let next = match arg.as_str() {
            "--config" => {
                path = args.next().context("--config requires a path")?.into();
                continue;
            }
            "--check-config" => Action::Check,
            "--enter-recovery" => Action::EnterRecovery,
            "--backup-to" => Action::Backup(
                args.next()
                    .context("--backup-to requires a new directory")?
                    .into(),
            ),
            "--restore-from" => Action::Restore(
                args.next()
                    .context("--restore-from requires the backup directory")?
                    .into(),
            ),
            "--keep-restored-original" => {
                Action::ReviewOriginal(args.next().context("file ID required")?, "keep")
            }
            "--mark-restored-original-missing" => {
                Action::ReviewOriginal(args.next().context("file ID required")?, "missing")
            }
            "--delete-restored-original" => {
                Action::ReviewOriginal(args.next().context("file ID required")?, "deleted")
            }
            "--help" | "-h" => {
                println!("tickdesk-local [--config PATH] [--check-config | --enter-recovery | --backup-to NEW_DIR | --restore-from DIR | --keep-restored-original FILE_ID | --mark-restored-original-missing FILE_ID | --delete-restored-original FILE_ID]");
                return Ok(());
            }
            _ => bail!("unknown argument: {arg}"),
        };
        ensure!(
            matches!(action, Action::Serve),
            "select only one management action"
        );
        action = next;
    }
    let config = Config::load(&path)?;
    contract::verify_catalog()?;
    let budget_config = config
        .budget_file
        .as_ref()
        .map(|file| tickdesk_local::budget::BudgetConfig::load(file))
        .transpose()?;
    let (chat_provider, vision_provider) = if !matches!(action, Action::Serve | Action::Check) {
        (None, None)
    } else if let Some(assistant) = &config.assistant {
        let provider = tickdesk_local::provider::AiProvider::bridge(assistant)?;
        (Some(provider.clone()), Some(provider))
    } else {
        (
            config
                .chat
                .as_ref()
                .map(tickdesk_local::provider::AiProvider::new)
                .transpose()?,
            config
                .vision
                .as_ref()
                .map(tickdesk_local::provider::AiProvider::new)
                .transpose()?,
        )
    };
    let pairing = if matches!(action, Action::Serve | Action::Check) {
        config
            .relay
            .map(|relay| relay.load(path.parent().unwrap_or(std::path::Path::new("."))))
            .transpose()?
    } else {
        None
    };
    if matches!(action, Action::Check) {
        println!("configuration and v2 validators valid; no storage or network started");
        return Ok(());
    }
    if let Action::Restore(backup) = action {
        Storage::restore_from(&backup, &config.data_dir)?;
        println!("backup verified and prepared in a new directory; recovery gate active");
        return Ok(());
    }
    if !matches!(action, Action::Serve) {
        ensure!(
            config.data_dir.join("tickdesk.sqlite3").is_file(),
            "management action requires an existing database"
        );
    }
    let mut storage = Storage::open(&config.data_dir)?;
    if let Some(pairing) = &pairing {
        relay::ensure_pair(&storage, pairing)?;
    }
    match action {
        Action::EnterRecovery => {
            storage.enter_recovery()?;
            println!("new endpoint incarnation and epoch committed; recovery gate active");
            return Ok(());
        }
        Action::Backup(path) => {
            storage.backup_to(&path)?;
            println!("local backup completed");
            return Ok(());
        }
        Action::ReviewOriginal(id, decision) => {
            storage.resolve_restored_original(&id, decision)?;
            println!("original review recorded; obtain a new recovery sequence before sealing");
            return Ok(());
        }
        _ => {}
    }

    // Resolve interrupted external work before accepting any new file or command activity.
    storage.recover_vision(tickdesk_local::commands::now_ms())?;
    storage.recover_interrupted_chats(tickdesk_local::commands::now_ms())?;
    storage.configure_budget(budget_config.as_ref(), tickdesk_local::commands::now_ms())?;
    let listener = tokio::net::TcpListener::bind(config.listen).await?;
    storage.record_startup()?;
    let (stop, shutdown) = tokio::sync::watch::channel(false);
    let storage = Arc::new(Mutex::new(storage));
    let assistant_authorization = chat_provider
        .as_ref()
        .and_then(|provider| provider.bridge_authorization());
    let assistant_health = tokio::spawn(tickdesk_local::assistant_health::run(
        chat_provider.clone(),
        storage.clone(),
        shutdown.clone(),
    ));
    let reminders = tokio::spawn(tickdesk_local::reminders::run(
        storage.clone(),
        shutdown.clone(),
    ));
    let backup = tokio::spawn(backup::daily_worker(
        storage.clone(),
        config.data_dir.join("backups"),
        shutdown.clone(),
    ));
    let chat = tokio::spawn(tickdesk_local::chat::run(
        storage.clone(),
        chat_provider,
        shutdown.clone(),
    ));
    let vision = tokio::spawn(tickdesk_local::vision::run_with_kernel(
        storage.clone(),
        vision_provider,
        config.kernel.clone(),
        shutdown.clone(),
    ));
    let market = tokio::spawn(tickdesk_local::market::run(
        storage.clone(),
        config.kernel.clone(),
        shutdown.clone(),
    ));
    let relay = pairing.map(|pairing| {
        tokio::spawn(relay::run_with_kernel(
            pairing,
            storage.clone(),
            config.kernel.clone(),
            shutdown.clone(),
        ))
    });
    let kernel = config
        .kernel
        .map(|config| tokio::spawn(kernel::run(config, storage.clone(), shutdown.clone())));
    tracing::info!(listen=%listener.local_addr()?, "TickDesk local service listening");
    let result = axum::serve(
        listener,
        http::router_with_assistant(storage.clone(), assistant_authorization),
    )
    .with_graceful_shutdown(tickdesk_local::lifecycle::shutdown_signal())
    .await;
    let _ = stop.send(true);
    backup.await?;
    reminders.await??;
    chat.await??;
    vision.await??;
    market.await??;
    assistant_health.await??;
    if let Some(relay) = relay {
        relay.await?;
    }
    if let Some(kernel) = kernel {
        kernel.await??;
    }
    result?;
    Ok(())
}
