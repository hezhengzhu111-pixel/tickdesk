use super::*;
use crate::{commands::now_ms, contract::digest};
use rcgen::{generate_simple_self_signed, CertifiedKey};
use rustls::pki_types::{PrivateKeyDer, PrivatePkcs8KeyDer};
use tokio::net::TcpListener;
use tokio_rustls::TlsAcceptor;
use tokio_tungstenite::{
    accept_hdr_async,
    tungstenite::handshake::server::{Request, Response},
};
use uuid::Uuid;

#[tokio::test]
#[allow(clippy::result_large_err)]
async fn pinned_websocket_reconnect_retries_original_ack_after_peer_loses_it() {
    let temp = tempfile::tempdir().unwrap();
    let db = Arc::new(Mutex::new(Storage::open(temp.path()).unwrap()));
    let (stop, shutdown) = watch::channel(false);
    let worker = tokio::spawn(process_inbox(db.clone(), None, shutdown));
    let server_id = db.lock().unwrap().identity.clone();
    let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
    let addr = listener.local_addr().unwrap();
    let CertifiedKey { cert, signing_key } =
        generate_simple_self_signed(vec!["127.0.0.1".into()]).unwrap();
    let pin = spki_digest(cert.der()).unwrap();
    let tls = TlsAcceptor::from(Arc::new(
        rustls::ServerConfig::builder_with_provider(Arc::new(
            rustls::crypto::ring::default_provider(),
        ))
        .with_safe_default_protocol_versions()
        .unwrap()
        .with_no_client_auth()
        .with_single_cert(
            vec![cert.der().clone()],
            PrivateKeyDer::Pkcs8(PrivatePkcs8KeyDer::from(signing_key.serialize_der())),
        )
        .unwrap(),
    ));
    let cfg = Pairing {
        endpoint: format!("wss://{addr}/relay/v2/connect"),
        address: addr,
        pair_id: "pair".into(),
        token: "A".repeat(43),
        pins: vec![pin],
    };
    let body = json!({"v":2,"kind":"command","id":"logical-id","dedupeKey":"command:c1","ts":now_ms(),
        "serverEpoch":server_id.server_epoch,"data":{"cmd":"get_recovery","clientMsgId":"c1","serverEpoch":server_id.server_epoch}});
    let command = json!({"v":2,"type":"deliver","deliveryId":"incoming","fromIncarnation":"app","toIncarnation":server_id.incarnation,
        "category":"business","payloadHash":digest(&body).unwrap(),"payload":body});
    let first_server = async {
        let (stream, _) = listener.accept().await.unwrap();
        let tls = tls.accept(stream).await.unwrap();
        let mut ws = accept_hdr_async(tls, |req: &Request, mut res: Response| {
            assert_eq!(
                req.headers()["authorization"],
                format!("Bearer {}", "A".repeat(43))
            );
            assert_eq!(req.headers()["sec-websocket-protocol"], "tickdesk.v2");
            res.headers_mut()
                .insert("sec-websocket-protocol", "tickdesk.v2".parse().unwrap());
            Ok(res)
        })
        .await
        .unwrap();
        let read = |m: Message| parse_frame(m.to_text().unwrap().as_bytes()).unwrap();
        let hello = read(ws.next().await.unwrap().unwrap());
        assert_eq!(hello["incarnation"], server_id.incarnation);
        send_json(&mut ws, &json!({"v":2,"type":"welcome","pairId":"pair","role":"local",
            "relayBootId":"boot","connectionId":Uuid::new_v4().to_string(),"heartbeatIntervalMs":15000,"heartbeatTimeoutMs":45000})).await.unwrap();
        send_json(&mut ws, &json!({"v":2,"type":"presence","peer":{"online":true,"incarnation":"app","serverEpoch":server_id.server_epoch}})).await.unwrap();
        send_json(&mut ws, &command).await.unwrap();
        let receipt = read(ws.next().await.unwrap().unwrap());
        assert_eq!(receipt["type"], "receipt");
        assert_eq!(receipt["deliveryId"], "incoming");
        let frame = read(ws.next().await.unwrap().unwrap());
        assert_eq!(frame["payload"]["data"]["refId"], "logical-id");
        frame
    };
    let client = async {
        let socket = connect(&cfg).await.unwrap();
        let _ = session(socket, &db, &cfg).await;
    };
    tokio::time::timeout(Duration::from_secs(10), async {
        let (first, ()) = tokio::join!(first_server, client);
        let second_server = async {
            let (stream, _) = listener.accept().await.unwrap();
            let tls = tls.accept(stream).await.unwrap();
            let mut ws = accept_hdr_async(tls, |_: &Request, mut res: Response| {
                res.headers_mut().insert("sec-websocket-protocol", "tickdesk.v2".parse().unwrap());
                Ok(res)
            }).await.unwrap();
            let _ = ws.next().await.unwrap().unwrap();
            send_json(&mut ws, &json!({"v":2,"type":"welcome","pairId":"pair","role":"local",
                "relayBootId":"new-boot","connectionId":"second","heartbeatIntervalMs":15000,"heartbeatTimeoutMs":45000})).await.unwrap();
            send_json(&mut ws, &json!({"v":2,"type":"presence","peer":{"online":true,"incarnation":"app","serverEpoch":server_id.server_epoch}})).await.unwrap();
            let replay = parse_frame(ws.next().await.unwrap().unwrap().to_text().unwrap().as_bytes()).unwrap();
            assert_eq!(replay, first);
            let mut receipt = first.clone();
            receipt["type"] = json!("receipt");
            receipt.as_object_mut().unwrap().remove("payload");
            receipt.as_object_mut().unwrap().remove("category");
            send_json(&mut ws, &receipt).await.unwrap();
            ws.close(None).await.unwrap();
        };
        let client = async {
            let socket = connect(&cfg).await.unwrap();
            let _ = session(socket, &db, &cfg).await;
        };
        tokio::join!(second_server, client);
        assert!(db.lock().unwrap().outbox(16).unwrap().is_empty());
    }).await.unwrap();
    stop.send(true).unwrap();
    worker.await.unwrap();
}

#[test]
fn trust_validates_pin_ip_and_expiry() {
    let CertifiedKey { cert, .. } = generate_simple_self_signed(vec!["127.0.0.1".into()]).unwrap();
    let verifier = PinVerifier::new(vec![spki_digest(cert.der()).unwrap()]);
    let expires = parse_x509_certificate(cert.der().as_ref())
        .unwrap()
        .1
        .validity()
        .not_after
        .timestamp() as u64;
    let name = |ip: &str| ServerName::try_from(ip.to_owned()).unwrap();
    assert!(verifier
        .verify_server_cert(cert.der(), &[], &name("127.0.0.1"), &[], UnixTime::now())
        .is_ok());
    assert!(verifier
        .verify_server_cert(cert.der(), &[], &name("127.0.0.2"), &[], UnixTime::now())
        .is_err());
    assert!(verifier
        .verify_server_cert(
            cert.der(),
            &[],
            &name("127.0.0.1"),
            &[],
            UnixTime::since_unix_epoch(Duration::from_secs(expires + 1))
        )
        .is_err());
    assert!(PinVerifier::new(vec![[0; 32]])
        .verify_server_cert(cert.der(), &[], &name("127.0.0.1"), &[], UnixTime::now())
        .is_err());
}
