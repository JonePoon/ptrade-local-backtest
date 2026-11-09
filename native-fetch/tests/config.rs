use ptrade_fetch::config::Config;
use ptrade_fetch::runner::{doctor, jobs};
use serde_json::json;
fn base() -> serde_json::Value {
    json!({"lake":"temporary","start":"2026-11-05","end":"2026-11-06","symbols":["000001.SZ","600000.SS"],"datasets":["daily_bars"],"extra_reference_config":{"secrets":"not exported"}})
}
#[test]
fn unknown_dictionary_keys_and_host_forms_are_supported() {
    for host in [
        json!(["127.0.0.1", 7709]),
        json!({"host":"127.0.0.1","port":7709}),
        json!("127.0.0.1:7709"),
    ] {
        let mut value = base();
        value["tdx_hosts"] = json!([host]);
        let mut config: Config = serde_json::from_value(value).unwrap();
        config.validate().unwrap();
        assert_eq!(config.symbols, vec!["000001.SZ", "600000.SH"]);
        assert_eq!(jobs(&config).unwrap().len(), 2);
    }
}
#[test]
fn invalid_page_rate_dates_and_dataset_configuration_fail() {
    for (key, value) in [
        ("page_size", json!(801)),
        ("max_pages", json!(0)),
        ("requests_per_second", json!(0)),
        ("concurrency", json!(0)),
        ("provider", json!("python")),
        ("datasets", json!(["unknown"])),
    ] {
        let mut raw = base();
        raw[key] = value;
        let mut config: Config = serde_json::from_value(raw).unwrap();
        assert!(config.validate().is_err(), "{key}");
    }
}
#[test]
fn doctor_has_no_config_no_lake_no_python_and_no_network() {
    let report = doctor();
    assert_eq!(report.status, "succeeded");
    assert_eq!(report.capabilities["native"], true);
    assert_eq!(report.capabilities["python_subprocess"], false);
    assert_eq!(report.capabilities["network_contacted"], false);
    assert_eq!(report.capabilities["writes_lake"], false);
}
