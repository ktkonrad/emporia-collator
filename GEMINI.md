# Emporia Data Collator

This project provides a Python script (`download_data.py`) to connect to the Emporia Energy API, fetch usage data for your devices, and save it to CSV files.

## Features

- Authenticates with the Emporia Energy API using credentials from a configuration file.
- Fetches a list of all devices associated with your account.
- Downloads usage data for each channel of each device for a specified period or the previous calendar month.
- Supports multiple granularities: `MINUTE`, `HOUR`, or `DAY`.
- **Timezone Support**: Handles data in the `America/Los_Angeles` timezone, converting to UTC for API requests and back to local time for output.
- **Performance Optimized**: Skips unnamed channels (ports with no CT attached) to speed up data fetching.
- **Multiple Output Formats**: Saves data to local CSV files and can optionally append results to a Google Sheet.

## Key Concepts

### Devices and Channels
-   **Device**: A physical Emporia hardware unit (e.g., a Vue 2 energy monitor).
-   **Channel**: An individual sensor or phase. Channels 1, 2, and 3 are typically the main phases. Channels 4+ represent expansion CTs for individual circuits.
-   **Total (1,2,3) Pseudochannel**: A virtual channel in the API that returns the aggregate usage of the main phases.

### Balance Calculation
The **Balance** represents usage on the main phases that isn't accounted for by ANY individual sensor. It is calculated as:
`Balance = Total (1,2,3) - Sum(All Named Monitored Channels)`

This ensures that the total usage reported always matches the actual energy consumed, even if not all circuits are monitored.

### Timezone Handling
All timestamps and date ranges are treated as **America/Los_Angeles** local time. The script automatically handles the conversion to UTC for Emporia API requests and localizes the returned data for the final output.

### Mapping to Web & API
-   **Web Interface**: Shows a "Total" usage (from the `1,2,3` pseudochannel), each individual channel's usage, and a "Balance" which is calculated as `Total - sum(all monitored channels)`.
-   **API**: Provides data for individual channels (1, 2, 3, 4+) and the aggregate `1,2,3` pseudochannel.
-   **This Script**: For each device, always fetches the `1,2,3` pseudochannel data and computes the "balance" channel locally. This ensures that the output reflects the same data model seen in the web interface and accurately captures all energy usage.

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
    Create a file named `config.yaml` in the root directory:

    ```yaml
    credentials:
      username: your_emporia_email@example.com
      password: your_emporia_password
    data:
      start_date: YYYY-MM-DD
      end_date: YYYY-MM-DD
      granularity: DAY
    output:
      google_sheet_url: "https://docs.google.com/spreadsheets/d/your_sheet_id/edit#gid=your_gid"
      service_account_file: "service_account.json"
    ```

## Usage

Run the script using `uv` or `python`:

```bash
uv run download_data.py [options]
```

### Options
-   `--output_totals`: Include raw `[Total]` columns for each device in the output (useful for verification).
-   `-v`, `--verbose`: Enables detailed debug logging.
-   `-q`, `--quiet`: Only logs warnings and errors.

## Output Files
Data is saved in the `emporia_data/` directory:
- `emporia_data_YYYY-MM.csv`: The output file containing usage data and totals.

### Column Naming Convention
The first column is always **Period**, formatted as `YYYY-MM`. Other columns are named using the format: `{Device Name}: {Channel Name} ({Unit})`
- **Example**: `Kitchen Vue: Refrigerator (kWh)`
- **Calculated Balance**: `Kitchen Vue: Balance (USD)`
- **Raw Totals** (if `--output_totals` is used): `Kitchen Vue: [Total] (kWh)`

## Troubleshooting
- **Empty Channels**: The script only fetches data for **named** channels. If a circuit is missing, ensure it has a name assigned in the Emporia mobile app.
- **Login Issues**: Ensure outbound connections to `cognito-idp.us-east-2.amazonaws.com` are allowed.
