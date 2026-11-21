//! TLS server configuration.
//!
//! TLS 1.3 session tickets are deliberately disabled. The post-handshake
//! ticket flight could race with the first application read on a loopback
//! connection that arrived while the other paired endpoint was reconnecting;
//! the upgrade request then stayed unread until the client timed out. No
//! TickDesk peer depends on ticket resumption, and full handshakes keep role
//! fencing and heartbeat deadlines deterministic after a restart.
use axum_server::tls_rustls::RustlsConfig;
use rustls::pki_types::pem::PemObject;
use rustls::pki_types::{CertificateDer, PrivateKeyDer};
use std::{io, path::Path, sync::Arc};

pub fn server_config(
    cert: impl AsRef<Path>,
    key: impl AsRef<Path>,
) -> io::Result<Arc<rustls::ServerConfig>> {
    let certs = CertificateDer::pem_file_iter(cert)
        .map_err(io::Error::other)?
        .collect::<Result<Vec<_>, _>>()
        .map_err(io::Error::other)?;
    let key = PrivateKeyDer::from_pem_file(key).map_err(io::Error::other)?;
    let mut config = rustls::ServerConfig::builder()
        .with_no_client_auth()
        .with_single_cert(certs, key)
        .map_err(io::Error::other)?;
    config.send_tls13_tickets = 0;
    Ok(Arc::new(config))
}

pub fn rustls_config(cert: impl AsRef<Path>, key: impl AsRef<Path>) -> io::Result<RustlsConfig> {
    Ok(RustlsConfig::from_config(server_config(cert, key)?))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn tls13_session_tickets_are_disabled() {
        let dir = tempfile::tempdir().unwrap();
        let cert = rcgen::generate_simple_self_signed(vec!["localhost".into()]).unwrap();
        let cert_path = dir.path().join("cert.pem");
        let key_path = dir.path().join("key.pem");
        std::fs::write(&cert_path, cert.cert.pem()).unwrap();
        std::fs::write(&key_path, cert.signing_key.serialize_pem()).unwrap();
        let config = server_config(&cert_path, &key_path).unwrap();
        assert_eq!(config.send_tls13_tickets, 0);
    }
}
