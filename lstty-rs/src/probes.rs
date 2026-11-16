//! Debug-probe families, saved nicknames and the per-family probe tables.
//! Mirrors `ProbeFamily`, `load_probe_config` and `render_probe_table` in tools/bench/lstty.

use std::{
    collections::{BTreeMap, BTreeSet},
    env, fs,
    path::PathBuf,
};

use serde_json::Value;

use crate::table::{BOLD, CYAN, Col, DIM, GREEN, MAGENTA, Painter, Row, YELLOW, render_columns};

pub struct Family {
    pub key: &'static str,
    pub title: &'static str,
    vid: Option<&'static str>,
    pids: Option<&'static [&'static str]>,
    ids: Option<&'static [(&'static str, &'static str)]>,
    normalize: fn(&str) -> String,
}

impl Family {
    pub fn matches(&self, vid: &str, pid: &str) -> bool {
        if let Some(ids) = self.ids {
            return ids.iter().any(|(v, p)| *v == vid && *p == pid);
        }
        self.vid == Some(vid) && self.pids.is_none_or(|p| p.contains(&pid))
    }
    pub fn normalize(&self, serial: &str) -> String {
        (self.normalize)(serial)
    }
}

fn clean_missing(value: &str) -> &str {
    let v = value.trim();
    if v == "-" { "" } else { v }
}

fn jlink_serial(value: &str) -> String {
    let v = clean_missing(value);
    if v.is_empty() || !v.chars().all(|c| c.is_ascii_digit()) {
        return String::new();
    }
    let stripped = v.trim_start_matches('0');
    if stripped.is_empty() { v.to_string() } else { stripped.to_string() }
}

fn stlink_serial(value: &str) -> String {
    let v = clean_missing(value);
    if v.is_empty() {
        return String::new();
    }
    if v.is_ascii() && v.chars().all(|c| c.is_ascii_alphanumeric()) {
        return v.to_uppercase();
    }
    // Old ST-Link/V2 firmware reports raw bytes; tools show them as hex (latin-1, '?' if wider).
    v.chars()
        .map(|c| if (c as u32) < 256 { c as u32 as u8 } else { b'?' })
        .map(|b| format!("{b:02x}"))
        .collect::<String>()
        .to_uppercase()
}

fn alnum_serial(value: &str) -> String {
    let v = clean_missing(value);
    if v.is_empty() || !v.is_ascii() || !v.chars().all(|c| c.is_ascii_alphanumeric()) {
        return String::new();
    }
    v.to_uppercase()
}

pub static FAMILIES: [Family; 4] = [
    Family { key: "jlink", title: "J-Link", vid: Some("1366"), pids: None, ids: None, normalize: jlink_serial },
    Family {
        key: "stlink",
        title: "ST-Link",
        vid: Some("0483"),
        pids: Some(&["3744", "3748", "374a", "374b", "3752", "374d", "374e", "374f", "3753", "3754", "3757"]),
        ids: None,
        normalize: stlink_serial,
    },
    Family {
        key: "mculink",
        title: "MCU-Link",
        vid: Some("1fc9"),
        pids: Some(&["0143"]),
        ids: None,
        normalize: alnum_serial,
    },
    Family {
        key: "cmsisdap",
        title: "CMSIS-DAP",
        vid: None,
        pids: None,
        ids: Some(&[("d6e7", "3507"), ("42bf", "b210"), ("0d28", "0204"), ("2e8a", "000c")]),
        normalize: alnum_serial,
    },
];

pub fn config_dir() -> PathBuf {
    let base = env::var_os("XDG_CONFIG_HOME")
        .map(PathBuf::from)
        .or_else(|| home().map(|h| h.join(".config")))
        .unwrap_or_else(|| PathBuf::from(".config"));
    base.join("lstty")
}

fn home() -> Option<PathBuf> {
    env::var_os(if cfg!(windows) { "USERPROFILE" } else { "HOME" }).map(PathBuf::from)
}

fn read_json(path: &PathBuf) -> Option<Value> {
    serde_json::from_str(&fs::read_to_string(path).ok()?).ok()
}

/// True when links.json defines serial aliases. The Python front end keeps the
/// symlinks in sync on every listing, so the native one leaves that case to it.
pub fn has_links() -> bool {
    read_json(&config_dir().join("links.json"))
        .and_then(|v| v.get("links")?.as_array().map(|a| a.iter().any(|l| l.is_object())))
        .unwrap_or(false)
}

pub struct Usage {
    pub at: f64,
    pub what: String,
}

#[derive(Default)]
pub struct Config {
    /// serial -> sorted nicknames
    pub names: BTreeMap<String, Vec<String>>,
    /// serial -> device of the first nickname that has one
    pub device: BTreeMap<String, String>,
    pub usage: BTreeMap<String, Usage>,
}

pub fn load_config(fam: &Family) -> Config {
    let mut cfg = Config::default();
    let Some(data) = read_json(&config_dir().join(format!("{}.json", fam.key))) else { return cfg };
    if let Some(probes) = data.get("probes").and_then(Value::as_object) {
        let mut ordered: Vec<(&String, &Value)> = probes.iter().collect();
        ordered.sort_by(|a, b| a.0.cmp(b.0));
        for (name, probe) in ordered {
            let Some(serial) = probe.get("serial").and_then(Value::as_str) else { continue };
            cfg.names.entry(serial.to_string()).or_default().push(name.clone());
            if let Some(dev) = probe.get("device").and_then(Value::as_str).filter(|d| !d.is_empty()) {
                cfg.device.entry(serial.to_string()).or_insert_with(|| dev.to_string());
            }
        }
    }
    if let Some(usage) = data.get("usage").and_then(Value::as_object) {
        for (serial, entry) in usage {
            cfg.usage.insert(
                serial.clone(),
                Usage {
                    at: entry.get("at").and_then(Value::as_f64).unwrap_or(0.0),
                    what: entry.get("what").and_then(Value::as_str).unwrap_or("").to_string(),
                },
            );
        }
    }
    cfg
}

pub fn format_ago(epoch: f64, now: f64) -> String {
    if epoch == 0.0 {
        return "-".into();
    }
    let delta = (now - epoch).max(0.0) as u64;
    for (unit, size) in [("d", 86400), ("h", 3600), ("m", 60)] {
        if delta >= size {
            return format!("{}{unit} ago", delta / size);
        }
    }
    format!("{delta}s ago")
}

/// A USB device from the live inventory (with or without a COM port).
pub struct UsbDevice {
    pub vid: String,
    pub pid: String,
    pub serial: String,
    pub product: String,
}

/// Print one family's probe table under its heading. Prints nothing when the
/// family has no connected (or, with -a, known) probe, exactly like the Python heading mode.
pub fn render_probe_table(
    out: &mut String,
    p: &Painter,
    fam: &Family,
    cfg: &Config,
    usb: &[UsbDevice],
    ttys: &BTreeMap<String, Vec<String>>,
    show_all: bool,
    now: f64,
    width: usize,
) {
    let mut connected: BTreeMap<String, String> = BTreeMap::new();
    for d in usb {
        if fam.matches(&d.vid, &d.pid) {
            let serial = fam.normalize(&d.serial);
            if !serial.is_empty() {
                let product = if d.product.is_empty() { fam.title.to_string() } else { d.product.clone() };
                connected.insert(serial, product);
            }
        }
    }
    let mut serials: Vec<String> = connected.keys().cloned().collect();
    if show_all {
        let known: BTreeSet<&String> = cfg.names.keys().chain(cfg.usage.keys()).collect();
        serials.extend(known.into_iter().filter(|s| !connected.contains_key(*s)).cloned());
    }
    if serials.is_empty() {
        return;
    }

    let rows: Vec<Row> = serials
        .iter()
        .map(|serial| {
            let online = connected.contains_key(serial);
            let usage = cfg.usage.get(serial);
            let mut r = Row::default();
            r.set("serial", serial.clone());
            r.set("nick", cfg.names.get(serial).map(|n| n.join(", ")).unwrap_or_default());
            r.set("status", if online { "ONLINE" } else { "OFFLINE" });
            let port = if online {
                ttys.get(serial)
                    .map(|t| t.iter().map(|x| x.strip_prefix("/dev/").unwrap_or(x)).collect::<Vec<_>>().join(","))
                    .unwrap_or_default()
            } else {
                String::new()
            };
            r.set("port", port);
            r.set("device", cfg.device.get(serial).cloned().unwrap_or_default());
            r.set("ago", usage.map(|u| format_ago(u.at, now)).unwrap_or_default());
            r.set("what", usage.map(|u| u.what.clone()).unwrap_or_default());
            r.dim = !online;
            r
        })
        .collect();
    let cols = [
        Col::new("serial", "SERIAL", GREEN, 8).mid(),
        Col::new("nick", "BOARD/PROBE", MAGENTA, 11),
        Col::new("status", "STATUS", GREEN, 8),
        Col::new("port", "PORT", CYAN, 4).drop(1).mid(),
        Col::new("device", "DEVICE", "", 6).drop(2),
        Col::new("ago", "AGO", YELLOW, 7),
        Col::new("what", "LAST USE", "", 8).flex(),
    ];
    out.push('\n');
    out.push_str(&p.paint(&format!("{} probes", fam.title), BOLD));
    out.push('\n');
    render_columns(out, p, &cols, &rows, width);
    let _ = DIM;
}
