# Chaos Downloader

Chaos Downloader is a command-line tool that interfaces with the [Chaos API](https://projectdiscovery.io) from ProjectDiscovery. It streamlines subdomain extraction and filtering, making it a useful asset for domain enumeration workflows.

## Requirements

- Python 3.9 or newer
- Works on **Windows**, **Linux**, and **macOS**

## Installation

```bash
git clone https://github.com/0x516459465/chaos-downloader/
cd chaos-downloader
python -m pip install -r requirements.txt
```

## Usage

```bash
# Linux / macOS
chmod +x ./chaos-downloader.py
./chaos-downloader.py

# Windows
python chaos-downloader.py
```

![Example Output](https://github.com/0x516459465/chaos-downloader/blob/main/info.jpg?raw=true)

## Features

- **Cross-Platform:** Windows, Linux, and macOS.
- **Async Downloads:** Uses `httpx` + `asyncio` for fast concurrent downloads.
- **Modern UI:** Rich progress bars, tables, and colored output via `rich`.
- **Interactive Menus:** Driven by `questionary`.
- **Advanced Filtering:** By platform, bounty, new subdomains, or combinations.
- **Automated Decompression:** Unzips downloaded archives automatically.
- **Domain Aggregation:** Consolidates subdomains into per-filter text files.
- **SQLite Persistence:** Programs and subdomains stored in `chaos.db`.
- **Export Functionality:** Export previously-seen subdomains from the DB.

## Tech Stack

- [`httpx`](https://www.python-httpx.org/) — async HTTP client
- [`rich`](https://rich.readthedocs.io/) — progress, tables, colors
- [`questionary`](https://questionary.readthedocs.io/) — interactive prompts

## Notes

- **Initial Run Behavior:** New-subdomain detection depends on an existing local database, so the first run may not show "new" results until the DB is populated.
