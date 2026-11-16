# lstty

List serial ports and USB debug probes (J-Link, ST-Link, MCU-Link, CMSIS-DAP) with stable symlinks, JSON output (`--json`), and a fast native listing front end (`lstty-rs`).

## Install on Windows

Requires Python 3.8+ on `PATH` (for subcommands such as `update`, `reassign`, `bind`, `--json`).

```powershell
irm https://raw.githubusercontent.com/gustavosousa2208/lstty/main/install.ps1 | iex
```

## Updating

`lstty` checks GitHub Releases in the background at most once every 24 hours and prints a notice on `stderr` when a newer version is available. To update:

```powershell
lstty update
```

## Build from source

```bash
cargo build --release --locked --manifest-path lstty-rs/Cargo.toml
```
