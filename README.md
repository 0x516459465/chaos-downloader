# Chaos Downloader

Chaos Downloader is a command-line tool designed to interface with the [Chaos API](https://projectdiscovery.io) provided by ProjectDiscovery. It streamlines subdomain extraction and filtering making it an effective asset for your domain enumeration workflow.

## Installation

Clone the repository and install the required dependencies:

```bash
git clone https://github.com/ali-0x11/chaos-downloader/
cd chaos-downloader
pip3 install -r requirements.txt
```

## Usage

Make the script executable (on Linux/macOS) and run the tool:

```bash
# Linux/macOS
chmod +x ./chaos-downloader.py
./chaos-downloader.py

# Windows
python chaos-downloader.py
```

![Example Output](https://github.com/ali-0x11/chaos-downloader/blob/main/info.jpg?raw=true)

## Features

- **Cross-Platform:** Works on Windows, Linux, and macOS.
- **Subdomain Extraction:** Automatically identifies and extracts new subdomains from API responses.
- **Advanced Filtering:** Leverages enhanced filters when interacting with the Chaos API for precise data retrieval.
- **Automated Decompression:** Unzips downloaded folders automatically to streamline your workflow.
- **Domain Aggregation:** Consolidates all domains into a single file for easy management and further analysis.
- **Export Functionality:** Export domains from the database for specific programs.

## Notes

- **Initial Run Behavior:** The tool may not display new subdomains on the first run due to its dependency on an existing database for comparisons.
