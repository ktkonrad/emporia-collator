# Emporia Data Collator

This project provides a Python script (`download_data.py`) to connect to the Emporia Energy API, fetch usage data for your devices, and save it to CSV files.

## Features

- Authenticates with the Emporia Energy API using credentials from a configuration file.
- Fetches a list of all devices associated with your account.
- Downloads 1-minute (or hourly, depending on configuration) usage data for each channel of each device for the previous calendar month.
- Saves the collected data into individual CSV files per device.

## Setup

1.  **Install dependencies**:
    If you have `uv` installed, run:
    ```bash
    uv pip install -r requirements.txt
    ```
    Otherwise, use `pip`:
    ```bash
    pip install -r requirements.txt
    ```

2.  **Configure Credentials**:
    Create a file named `config.cfg` in the root directory of this project with your Emporia Energy API credentials. This file is ignored by Git to protect your sensitive information.

    ```ini
    [emporia]
    username = your_emporia_email@example.com
    password = your_emporia_password
    ```
    Replace `your_emporia_email@example.com` and `your_emporia_password` with your actual Emporia account email and password.

## Usage

To download data, simply run the `download_data.py` script:

```bash
uv run download_data.py
```
or if using `pip`:
```bash
python download_data.py
```

The script will:
- Log in to the Emporia API.
- Discover your devices and their channels.
- Fetch historical usage data for the previous calendar month.
- Save CSV files for each device in an `emporia_data/` directory (which will be created if it doesn't exist).

## Troubleshooting

-   **Hanging during login**: If the script hangs indefinitely during login, it might be due to network connectivity issues to AWS Cognito. Ensure your network allows outbound connections to `cognito-idp.us-east-2.amazonaws.com`.
-   **`400 Client Error`**: This can occur if there are issues with the parameters sent to the Emporia API. The script attempts to use appropriate `scale` and `unit` values. If errors persist, it might indicate specific device or channel issues on the Emporia side, or temporary API problems.
-   **`Cannot save file into a non-existent directory`**: This error has been addressed by sanitizing device names that might contain directory separators. Ensure you have the latest version of `download_data.py`.
-   **"Skipping pseudo-channel: None (1,2,3)"**: Some devices may report a pseudo-channel like "1,2,3" which is not directly supported by the API for data fetching. The script will automatically skip these channels.
