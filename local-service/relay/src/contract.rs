pub const PROTOCOL_MAJOR: u32 = 2;
pub const CONTRACT_VERSION: &str = "2.0.0";
pub const MANIFEST: &str = include_str!("../contracts/v2/manifest.json");
pub fn verify_contract() -> anyhow::Result<()> {
    let manifest: serde_json::Value = serde_json::from_str(MANIFEST)?;
    anyhow::ensure!(
        manifest["protocolMajor"] == PROTOCOL_MAJOR
            && manifest["contractVersion"] == CONTRACT_VERSION,
        "unsupported contract snapshot"
    );
    Ok(())
}
