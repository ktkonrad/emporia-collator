# Emporia Data Collator

This project provides a Python script (`download_data.py`) to connect to the Emporia Energy API, fetch usage data for your devices, and save it to CSV files.

## Features

- Authenticates with the Emporia Energy API using credentials from a configuration file.
- Fetches a list of all devices associated with your account.
- Downloads usage data for each channel of each device for the previous calendar month, with configurable granularity.
- Saves the collected data into a single CSV file.

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
    Create a file named `config.yaml` in the root directory of this project with your Emporia Energy API credentials. This file is ignored by Git to protect your sensitive information.

    ```yaml
    credentials:
      username: your_emporia_email@example.com
      password: your_emporia_password
    data:
      start_date: YYYY-MM-DD
      end_date: YYYY-MM-DD
      granularity: DAY
    aggregate_devices:
      - "Device Name 1"
      - "Device Name 2"
    output:
      google_sheet_url: "https://docs.google.com/spreadsheets/d/your_sheet_id/edit#gid=your_gid"
      service_account_file: "service_account.json"
    ```
    Replace `your_emporia_email@example.com` and `your_emporia_password` with your actual Emporia account email and password.

    **Configuration Options:**
    *   `credentials.username`: Your Emporia Energy account email address. (Required)
    *   `credentials.password`: Your Emporia Energy account password. (Required)
    *   `data.start_date`: (Optional) The start date for data download in `YYYY-MM-DD` format. If left empty, the script defaults to the most recent billing cycle ending on the 26th.
    *   `data.end_date`: (Optional) The end date for data download in `YYYY-MM-DD` format. If left empty, the script defaults to the most recent billing cycle ending on the 26th.
    *   `data.granularity`: (Optional) The time interval for the data. Supported values are `MINUTE`, `HOUR`, or `DAY`. Defaults to `DAY`.
    *   `aggregate_devices`: (Optional) A list of device names to aggregate. For these devices, a single column will be created with the sum of all channels.
    *   `output.google_sheet_url`: (Optional) The URL of a Google Sheet to append data to.
    *   `output.service_account_file`: (Optional) The path to your Google Service Account JSON file. Defaults to `service_account.json`.

## Output Columns

By default, the script will output one column for each channel of your Emporia devices, using the channel name as the column name. 

If a device name is listed in the `aggregate_devices` section of `config.yaml`, the script will instead output a single column for that device, containing the sum of all its channels, and using the device name as the column name.

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
- Save a single CSV file in an `emporia_data/` directory (which will be created if it doesn't exist).

## Troubleshooting

-   **Hanging during login**: If the script hangs indefinitely during login, it might be due to network connectivity issues to AWS Cognito. Ensure your network allows outbound connections to `cognito-idp.us-east-2.amazonaws.com`.
-   **`400 Client Error`**: This can occur if there are issues with the parameters sent to the Emporia API. The script attempts to use appropriate `scale` and `unit` values. If errors persist, it might indicate specific device or channel issues on the Emporia side, or temporary API problems.
-   **"Skipping pseudo-channel: None (1,2,3)"**: Some devices may report a pseudo-channel like "1,2,3" which is not directly supported by the API for data fetching. The script will automatically skip these channels.
