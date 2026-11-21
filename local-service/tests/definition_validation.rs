use serde_json::{json, Value};
use std::sync::{Arc, Barrier};
use tickdesk_local::contract::validate_definition;

fn position() -> Value {
    json!({
        "symbol":"600000.SH", "name":"fixture", "qty":100, "availableQty":0,
        "cost":"10", "pendingLots":[{"qty":100,"sellableDate":"2024-02-29"}],
        "asOf":1, "positionRevision":1, "reconciliationRequired":false,
        "lastPrice":null, "priceAsOf":null, "priceStatus":"missing",
        "marketValue":null, "unrealizedPnl":null, "unrealizedPnlRatio":null
    })
}

#[test]
fn concurrent_definition_validation_preserves_formats_nested_refs_and_input_isolation() {
    // A separate test process gives these definitions concurrent first use.
    let barrier = Arc::new(Barrier::new(8));
    std::thread::scope(|scope| {
        for index in 0..8 {
            let barrier = barrier.clone();
            scope.spawn(move || {
                barrier.wait();
                for _ in 0..3 {
                    let (name, valid, invalid) = if index % 2 == 0 {
                        (
                            "Lot",
                            json!({"qty":100,"sellableDate":"2024-02-29"}),
                            json!({"qty":100,"sellableDate":"2023-02-29"}),
                        )
                    } else {
                        let valid = position();
                        let mut invalid = valid.clone();
                        invalid["pendingLots"][0]["qty"] = json!(0);
                        ("Position", valid, invalid)
                    };
                    validate_definition(name, &valid).unwrap();
                    assert_eq!(
                        validate_definition(name, &invalid).unwrap_err().to_string(),
                        format!("invalid v2 {name}")
                    );
                    validate_definition(name, &valid).unwrap();
                }
            });
        }
    });
    assert_eq!(
        validate_definition("unknown_definition", &json!({}))
            .unwrap_err()
            .to_string(),
        "unknown schema definition"
    );
}
