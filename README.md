# Emporia Data Collator

This project provides a Python script (`download_data.py`) to connect to the Emporia Energy API, fetch usage data for your devices, and save it to CSV files.

## Features

- Authenticates with the Emporia Energy API using credentials from a configuration file.
- Fetches a list of all devices associated with your account.
- Downloads usage data for each channel of each device for the previous calendar month, with configurable granularity.
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
    Create a file named `config.yaml` in the root directory of this project with your Emporia Energy API credentials and desired data parameters. This file is ignored by Git to protect your sensitive information.

    ```yaml
    credentials:
      username: your_emporia_email@example.com
      password: your_emporia_password
    data:
      start_date: YYYY-MM-DD
      end_date: YYYY-MM-DD
      granularity: DAY
    output:
      # Optional: Define custom output columns. If omitted, one column per device will be generated.
      - name: Total Home Usage
        devices:
          - name: Main Panel
            channels: [1, 2, 3]
          - name: Kitchen
            channels: [4, 5]
      - name: Upstairs
        devices:
          - name: Main Panel
            channels: [6, 7]
    ```
    Replace `your_emporia_email@example.com` and `your_emporia_password` with your actual Emporia account email and password.

    **Configuration Options:**
    *   `credentials.username`: Your Emporia Energy account email address. (Required)
    *   `credentials.password`: Your Emporia Energy account password. (Required)
    *   `data.start_date`: (Optional) The start date for data download in `YYYY-MM-DD` format. If left empty, the script defaults to the first day of the previous calendar month.
    *   `data.end_date`: (Optional) The end date for data download in `YYYY-MM-DD` format. If left empty, the script defaults to the last day of the previous calendar month.
    *   `data.granularity`: (Optional) The time interval for the data. Supported values are `MINUTE`, `HOUR`, or `DAY`. Defaults to `DAY`.

## Custom Output Columns

By default, the script will output one column for each of your Emporia devices, containing the sum of all channels on that device. You can customize the output by defining an `output` section in your `config.yaml` file. This allows you to group channels from different devices into a single column.

The `output` section should be a list of dictionaries, where each dictionary defines a custom column:

```yaml
output:
  - name: Total Home Usage
    devices:
      - name: Main Panel
        channels: [1, 2, 3]
      - name: Kitchen
        channels: [4, 5]
  - name: Upstairs
    devices:
      - name: Main Panel
        channels: [6, 7]
```

In this example, the output CSV file will have two columns: `Total Home Usage` and `Upstairs`.

*   The `Total Home Usage` column will be the sum of channels 1, 2, and 3 from the "Main Panel" device and channels 4 and 5 from the "Kitchen" device.
*   The `Upstairs` column will be the sum of channels 6 and 7 from the "Main Panel" device.

If the `output` section is omitted or empty in the `config.yaml` file, the script will revert to the default behavior of creating one column per device.

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
