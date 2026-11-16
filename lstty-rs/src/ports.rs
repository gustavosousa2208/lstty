//! Shared by the Unix listings: the serial port row and the serial-alias links (`~/.serial`).
//! Mirrors `row_matches_link`, `bound_names_for_row` and `sync_links` in tools/bench/lstty.

use std::{
    env, fs,
    os::unix::fs::symlink,
    path::{Path, PathBuf},
};

use serde_json::Value;

use crate::probes::config_dir;

pub struct SerialRow {
    pub tty: String,
    pub bus: String,
    pub dev: String,
    pub icon: &'static str,
    pub vid: String,
    pub pid: String,
    pub product: String,
    pub serial: String,
    pub driver: String,
    pub speed: String,
    pub match_vid: String,
    pub match_pid: String,
    pub match_serial: String,
    pub match_product: String,
}

pub fn clean_missing(value: &str) -> &str {
    let v = value.trim();
    if v == "-" { "" } else { v }
}

pub fn icon_for(product: &str, driver: &str) -> &'static str {
    let p = product.to_lowercase();
    if p.contains("mcu-link") || p.contains("mculink") {
        return "\u{f188}";
    }
    if p.contains("stlink") || p.contains("st-link") {
        return "\u{f188}";
    }
    if p.contains("j-link") || p.contains("jlink") {
        return "\u{f188}";
    }
    if ["cdc_acm", "ch341", "cp210x", "ftdi_sio", "pl2303"].contains(&driver) {
        return "\u{f2db}";
    }
    "\u{f1e6}"
}

#[derive(Default)]
pub struct Link {
    pub name: String,
    vid: String,
    pid: String,
    serial: String,
    product: String,
}

pub struct LinkConfig {
    pub dir: PathBuf,
    pub links: Vec<Link>,
}

fn text(v: &Value, key: &str) -> String {
    match v.get(key) {
        Some(Value::String(s)) => s.clone(),
        Some(Value::Number(n)) => n.to_string(),
        _ => String::new(),
    }
}

fn expand_user(p: &str) -> PathBuf {
    match (p.strip_prefix("~/"), env::var_os("HOME")) {
        (Some(rest), Some(home)) => PathBuf::from(home).join(rest),
        _ if p == "~" => env::var_os("HOME").map(PathBuf::from).unwrap_or_else(|| PathBuf::from(p)),
        _ => PathBuf::from(p),
    }
}

pub fn load_links() -> LinkConfig {
    let home = env::var_os("HOME").map(PathBuf::from).unwrap_or_default();
    let default_dir = env::var_os("LSTTY_LINK_DIR").map(PathBuf::from).unwrap_or_else(|| home.join(".serial"));
    let data: Option<Value> =
        fs::read_to_string(config_dir().join("links.json")).ok().and_then(|s| serde_json::from_str(&s).ok());
    let Some(data) = data.filter(Value::is_object) else {
        return LinkConfig { dir: default_dir, links: Vec::new() };
    };
    let dir = match data.get("link_dir").and_then(Value::as_str).filter(|s| !s.is_empty()) {
        Some(d) => expand_user(d),
        None => default_dir,
    };
    let links = data
        .get("links")
        .and_then(Value::as_array)
        .map(|a| {
            a.iter()
                .filter(|l| l.is_object())
                .map(|l| Link {
                    name: text(l, "name"),
                    vid: text(l, "vid"),
                    pid: text(l, "pid"),
                    serial: text(l, "serial"),
                    product: text(l, "product"),
                })
                .collect()
        })
        .unwrap_or_default();
    LinkConfig { dir, links }
}

pub fn normalize_usb_id(value: &str) -> String {
    let v = value.trim().to_lowercase();
    let v = v.strip_prefix("0x").unwrap_or(&v).to_string();
    if (1..=4).contains(&v.len()) && v.chars().all(|c| c.is_ascii_hexdigit()) {
        format!("{v:0>4}")
    } else {
        v
    }
}

fn row_matches_link(row: &SerialRow, link: &Link) -> bool {
    if !link.vid.is_empty() && normalize_usb_id(&row.match_vid) != normalize_usb_id(&link.vid) {
        return false;
    }
    if !link.pid.is_empty() && normalize_usb_id(&row.match_pid) != normalize_usb_id(&link.pid) {
        return false;
    }
    if !link.serial.is_empty() && clean_missing(&row.match_serial) != clean_missing(&link.serial) {
        return false;
    }
    if !link.product.is_empty() && !clean_missing(&row.match_product).to_lowercase().contains(&link.product.to_lowercase())
    {
        return false;
    }
    true
}

/// "name1, name2" of every link bound to this port, sorted, or "-".
pub fn bound_names(row: &SerialRow, cfg: &LinkConfig) -> String {
    let mut names: Vec<&str> =
        cfg.links.iter().filter(|l| !l.name.is_empty() && row_matches_link(row, l)).map(|l| l.name.as_str()).collect();
    names.sort();
    if names.is_empty() { "-".into() } else { names.join(", ") }
}

fn valid_link_name(name: &str) -> bool {
    !name.is_empty() && name.chars().all(|c| c.is_ascii_alphanumeric() || matches!(c, '.' | '_' | '-'))
}

fn replace_symlink(path: &Path, target: &str) -> std::io::Result<()> {
    if let Some(parent) = path.parent() {
        fs::create_dir_all(parent)?;
    }
    match fs::symlink_metadata(path) {
        Ok(meta) if meta.file_type().is_symlink() => {
            if fs::read_link(path)?.to_str() == Some(target) {
                return Ok(());
            }
            fs::remove_file(path)?;
        }
        Ok(_) => return Err(std::io::Error::other("exists and is not a symlink")),
        Err(_) => {}
    }
    symlink(target, path)
}

/// Keep the `~/.serial` names pointing at the current port for the matching USB identity.
/// Silent like `sync_links(verbose=False)`: problems are skipped, never fatal to the listing.
pub fn sync_links(cfg: &LinkConfig, rows: &[SerialRow]) {
    for link in &cfg.links {
        if !valid_link_name(&link.name) {
            continue;
        }
        let path = cfg.dir.join(&link.name);
        let matches: Vec<&SerialRow> = rows.iter().filter(|r| row_matches_link(r, link)).collect();
        if let [row] = matches.as_slice() {
            let _ = replace_symlink(&path, &row.tty);
        } else if fs::symlink_metadata(&path).map(|m| m.file_type().is_symlink()).unwrap_or(false) {
            let _ = fs::remove_file(&path);
        }
    }
}
