import os
import sys
import logging
import argparse
import yaml
import gspread
import calendar
import pandas as pd
import urllib.parse
from datetime import datetime, date, timedelta, timezone
from typing import Optional, Tuple, Dict, Any, List
from pyemvue import PyEmVue
from pyemvue.enums import Scale, Unit
from google.oauth2.service_account import Credentials
from zoneinfo import ZoneInfo

# Constants
DEFAULT_CONFIG_FILE = 'config.yaml'
DEFAULT_OUTPUT_FOLDER = 'emporia_data'
DEFAULT_SERVICE_ACCOUNT_FILE = 'service_account.json'
LOCAL_TZ = ZoneInfo('America/Los_Angeles')
GRANULARITY_MAP = {
    'MINUTE': (Scale.MINUTE.value, 'm'),
    'HOUR': (Scale.HOUR.value, 'h'),
    'DAY': (Scale.DAY.value, 'D'),
}

def setup_logging(verbosity: str):
    """Configures the logging module."""
    level = getattr(logging, verbosity.upper(), logging.INFO)
    logging.basicConfig(level=level, format='%(levelname)s: %(message)s')

def get_default_dates() -> Tuple[date, date]:
    """
    Returns the first and last day of the previous calendar month.
    """
    today = date.today()
    # First day of current month
    first_of_current = today.replace(day=1)
    # Last day of previous month
    e_date = first_of_current - timedelta(days=1)
    # First day of previous month
    s_date = e_date.replace(day=1)
    return s_date, e_date

def get_dates_for_month(month_str: str) -> Tuple[date, date]:
    """
    Parses a month string (YYYY-MM or MM) and returns the first and last day of that month.
    If only MM is provided, it assumes the most recent year that month occurred (completed).
    """
    today = date.today()
    try:
        if '-' in month_str:
            dt = datetime.strptime(month_str, '%Y-%m')
            year, month = dt.year, dt.month
        else:
            month = int(month_str)
            if not 1 <= month <= 12:
                raise ValueError("Month must be between 1 and 12")
            # If the requested month is >= current month, it haven't completed this year yet.
            year = today.year - 1 if month >= today.month else today.year
    except ValueError as e:
        if "Month must be between 1 and 12" in str(e):
            raise
        raise ValueError(f"Invalid month format '{month_str}'. Expected 'YYYY-MM' or 'MM'.") from e
            
    s_date = date(year, month, 1)
    _, last_day = calendar.monthrange(year, month)
    e_date = date(year, month, last_day)
    return s_date, e_date

def authenticate(email: str, password: str) -> Optional[PyEmVue]:
    """Authenticates with the Emporia API."""
    logging.info("Attempting to log in to Emporia Energy API...")
    try:
        vue = PyEmVue()
        vue.login(username=email, password=password)
        logging.info("Successfully logged in to Emporia.")
        return vue
    except Exception as e:
        logging.error(f"Error logging in to Emporia: {e}")
        return None

def get_emporia_device_info(vue: PyEmVue) -> Optional[Dict[int, Any]]:
    """
    Fetches all devices and populates their properties.
    Consolidates devices by GID to handle potential duplicates from the API.
    """
    logging.info("Fetching devices and properties...")
    try:
        devices = vue.get_devices()
        device_info: Dict[int, Any] = {}
        
        for device in devices:
            # populate_device_properties is critical for getting names and channel details
            vue.populate_device_properties(device)
            gid = device.device_gid
            
            if gid not in device_info:
                device_info[gid] = device
            else:
                # If we encounter the same GID, merge any new channels found
                existing_nums = {ch.channel_num for ch in device_info[gid].channels}
                for ch in device.channels:
                    if ch.channel_num not in existing_nums:
                        device_info[gid].channels.append(ch)
                        existing_nums.add(ch.channel_num)
        
        logging.info(f"Successfully discovered {len(device_info)} unique devices.")
        return device_info
    except Exception as e:
        logging.error(f"Failed to fetch device info: {e}")
        return None

def fetch_channel_data(vue: PyEmVue, channel: Any, start_date: date, end_date: date, granularity: str, target_name: Optional[str] = None) -> Optional[pd.DataFrame]:
    """
    Fetches usage (kWh) and cost (USD) data for a specific channel.
    Handles the conversion between the local timezone (America/Los_Angeles) and UTC required by the API.
    If no data is returned from the API, returns a zero-filled DataFrame to ensure column consistency.
    """
    # Skip multi-channel pseudochannels (e.g., '1,2,4') which are redundant, 
    # but keep '1,2,3' as it represents the device total.
    ch_num_str = str(channel.channel_num)
    if ',' in ch_num_str and ch_num_str != '1,2,3':
        return None
        
    # Determine the display name for the channel
    ch_display_name = target_name or channel.name or f"Channel {ch_num_str}"

    logging.info(f"  Fetching: {ch_display_name} (Channel {ch_num_str})")
    
    if granularity.upper() not in GRANULARITY_MAP:
        logging.error(f"  Invalid granularity: {granularity}")
        return None
        
    scale_value, pandas_freq = GRANULARITY_MAP[granularity.upper()]
    
    # Define local time window: from 00:00:00 on start_date to 23:59:00 on end_date
    start_dt_local = datetime.combine(start_date, datetime.min.time()).replace(tzinfo=LOCAL_TZ)
    end_dt_local = datetime.combine(end_date, datetime.max.time()).replace(second=0, microsecond=0, tzinfo=LOCAL_TZ)
    
    # Convert local window to UTC for the API request
    start_dt_utc = start_dt_local.astimezone(timezone.utc)
    end_dt_utc = end_dt_local.astimezone(timezone.utc)

    try:
        # Fetch both cost and usage. Note: Unit.USD and Unit.KWH are required.
        usage_usd, start_time_utc = vue.get_chart_usage(channel=channel, start=start_dt_utc, end=end_dt_utc, scale=scale_value, unit=Unit.USD.value)
        usage_kwh, _ = vue.get_chart_usage(channel=channel, start=start_dt_utc, end=end_dt_utc, scale=scale_value, unit=Unit.KWH.value)

        # The API returns the exact UTC start time of the first data point if available, 
        # otherwise we fallback to the requested start time.
        if start_time_utc:
            if start_time_utc.tzinfo is None:
                start_time_utc = start_time_utc.replace(tzinfo=timezone.utc)
        else:
            start_time_utc = start_dt_utc
            
        # Localize the start time for the output DataFrame
        start_time_local = start_time_utc.astimezone(LOCAL_TZ)

        # If data is missing or empty, generate a single-row zeroed DataFrame 
        # (or more if we wanted to match the full requested period, but for 'Totals' one zero row suffices).
        if not usage_usd or not usage_kwh or (all(v is None for v in usage_usd) and all(v is None for v in usage_kwh)):
            logging.debug(f"  No data returned for {ch_display_name}, returning zero.")
            return pd.DataFrame({
                'instant': [start_time_local],
                f'{ch_display_name} (USD)': [0.0],
                f'{ch_display_name} (kWh)': [0.0]
            })

        # Generate the timestamp series for each returned data point
        timestamps = pd.to_datetime([start_time_local] * len(usage_usd)) + pd.to_timedelta(range(len(usage_usd)), unit=pandas_freq)
        
        return pd.DataFrame({
            'instant': timestamps,
            f'{ch_display_name} (USD)': [v if v is not None else 0.0 for v in usage_usd],
            f'{ch_display_name} (kWh)': [v if v is not None else 0.0 for v in usage_kwh]
        })
    except Exception as e:
        logging.error(f"  API Error for {ch_display_name}: {e}")
        # Return a zero-row placeholder on error to keep the column structure
        return pd.DataFrame({
            'instant': [start_dt_local],
            f'{ch_display_name} (USD)': [0.0],
            f'{ch_display_name} (kWh)': [0.0]
        })

def compute_balance(device_name: str, total_df: pd.DataFrame, monitored_dfs: List[pd.DataFrame]) -> pd.DataFrame:
    """
    Computes the 'Balance' channel.
    Balance = [Total (1,2,3)] - sum(All other named/monitored channels).
    Always returns a DataFrame (even if zero) to ensure column consistency.
    """
    balance_name = f"{device_name}: Balance"
    
    if total_df is None:
         return pd.DataFrame(columns=['instant', f'{balance_name} (USD)', f'{balance_name} (kWh)'])
        
    # Start with the total as the baseline
    combined_df = total_df[['instant']].copy()
    
    # Consolidate all monitored channels to subtract them from the total
    usd_cols = []
    kwh_cols = []
    for df in monitored_dfs:
        combined_df = pd.merge(combined_df, df, on='instant', how='left')
        usd_cols.extend([c for c in df.columns if '(USD)' in c])
        kwh_cols.extend([c for c in df.columns if '(kWh)' in c])
        
    combined_df = combined_df.fillna(0)
    
    # Sum up all monitored usage
    sum_usd = combined_df[usd_cols].sum(axis=1) if usd_cols else 0
    sum_kwh = combined_df[kwh_cols].sum(axis=1) if kwh_cols else 0
    
    # Subtract monitored usage from the absolute total (1,2,3)
    balance_df = pd.DataFrame({
        'instant': total_df['instant'],
        f'{balance_name} (USD)': total_df['TOTAL (USD)'] - sum_usd,
        f'{balance_name} (kWh)': total_df['TOTAL (kWh)'] - sum_kwh
    })
    
    return balance_df

def fetch_device_data(vue: PyEmVue, device: Any, s_date: date, e_date: date, granularity: str) -> List[pd.DataFrame]:
    """
    Orchestrates the fetching of all data for a single device.
    Includes named channels, calculated balance, and raw total.
    """
    logging.info(f"Processing Device: {device.device_name}")
    
    # Identify the 'Total' pseudochannel (1,2,3)
    total_channel = next((ch for ch in device.channels if str(ch.channel_num) == '1,2,3'), None)
    
    # Filter for individual channels that have a name and are not pseudochannels.
    monitored_channels = [
        ch for ch in device.channels 
        if ch.name and ',' not in str(ch.channel_num)
    ]

    # Fetch usage for all individual monitored channels
    results = []
    circuit_dfs = []
    for ch in monitored_channels:
        display_name = f"{device.device_name}: {ch.name}"
        df = fetch_channel_data(vue, ch, s_date, e_date, granularity, target_name=display_name)
        if df is not None:
            results.append(df)
            # Only subtract expansion channels from the total to compute balance.
            # Channels 1, 2, and 3 are the 'Mains' and are already included in the '1,2,3' total.
            ch_num_str = str(ch.channel_num)
            if ch_num_str not in ['1', '2', '3']:
                circuit_dfs.append(df)
            
    # Fetch and compute balance if total channel exists
    total_df = fetch_channel_data(vue, total_channel, s_date, e_date, granularity, target_name="TOTAL") if total_channel else None
    if total_df is not None:
        results.append(compute_balance(device.device_name, total_df, circuit_dfs))
        
        # Keep a copy of the raw total for output (if requested)
        raw_total_df = total_df.copy()
        raw_total_df.columns = ['instant', f"{device.device_name}: [Total] (USD)", f"{device.device_name}: [Total] (kWh)"]
        results.append(raw_total_df)
            
    return results

def save_to_google_sheet(df: pd.DataFrame, sheet_url: str, service_account_file: str) -> bool:
    """
    Appends all rows from the DataFrame to a Google Sheet.
    Verifies that headers match exactly before appending to prevent data corruption.
    """
    if not os.path.exists(service_account_file):
        logging.error(f"Google Service Account file missing: {service_account_file}")
        return False

    try:
        # Authenticate with Google
        creds = Credentials.from_service_account_file(service_account_file, scopes=['https://www.googleapis.com/auth/spreadsheets'])
        client = gspread.authorize(creds)
        spreadsheet = client.open_by_url(sheet_url)
        
        # Extract GID from URL or default to the first worksheet
        parsed_url = urllib.parse.urlparse(sheet_url)
        gid = urllib.parse.parse_qs(parsed_url.fragment).get('gid', [None])[0]
        worksheet = next((s for s in spreadsheet.worksheets() if str(s.id) == gid), spreadsheet.get_worksheet(0)) if gid else spreadsheet.get_worksheet(0)

        # Safety Check: Compare headers
        values = worksheet.get_all_values()
        df_headers = df.columns.tolist()
        
        if not values:
            # Initialize empty sheet with headers
            worksheet.append_row(df_headers)
        elif values[0] != df_headers:
            # Report detailed header mismatches
            existing_headers = values[0]
            missing_in_sheet = set(df_headers) - set(existing_headers)
            extra_in_sheet = set(existing_headers) - set(df_headers)
            
            error_msg = f"Header mismatch in sheet '{spreadsheet.title}'! Aborting update.\n"
            if missing_in_sheet: error_msg += f"  - Missing: {sorted(list(missing_in_sheet))}\n"
            if extra_in_sheet: error_msg += f"  - Extra: {sorted(list(extra_in_sheet))}\n"
            
            logging.error(error_msg)
            return False
        
        # Append all rows
        rows_to_append = []
        for _, row in df.iterrows():
            rows_to_append.append([str(val) if not isinstance(val, (int, float)) else val for val in row])
        
        worksheet.append_rows(rows_to_append)
        logging.info(f"Successfully appended {len(rows_to_append)} rows to Google Sheet.")
        return True
    except Exception as e:
        logging.error(f"Failed to update Google Sheet: {e}")
        return False

def save_data(df: pd.DataFrame, reference_date: date, output_folder: str, suffix: str = ""):
    """Saves the processed DataFrame to a local CSV file."""
    if not os.path.exists(output_folder):
        os.makedirs(output_folder)
        
    filename = f"{output_folder}/emporia_data_{reference_date.strftime('%Y-%m')}{suffix}.csv"
    df.to_csv(filename, index=False)
    logging.info(f"Saved: {filename}")

def download_emporia_data(email: str, password: str, date_ranges: List[Tuple[date, date]], granularity: str, output_folder: str = DEFAULT_OUTPUT_FOLDER, google_sheet_url: Optional[str] = None, service_account_file: Optional[str] = DEFAULT_SERVICE_ACCOUNT_FILE, output_totals: bool = False) -> bool:
    """
    The main orchestration function.
    1. Authenticates
    2. Discovers Devices
    3. For each date range:
       a. Fetches Historical Data
       b. Processes/Aggregates
       c. Saves locally
    4. Optionally appends all results to Google Sheets.
    """
    # 1. API Login
    vue = authenticate(email, password)
    if not vue: return False

    # 2. Device Discovery
    device_info = get_emporia_device_info(vue)
    if not device_info: return False

    all_month_totals = []

    for s_date, e_date in date_ranges:
        logging.info(f"Target Period: {s_date} to {e_date} (Granularity: {granularity})")

        # 4. Fetch data for all discovered devices
        all_dfs = []
        for device in device_info.values():
            device_dfs = fetch_device_data(vue, device, s_date, e_date, granularity)
            if not device_dfs: continue
            all_dfs.extend(device_dfs)

        if not all_dfs:
            logging.warning(f"No data was retrieved for {s_date} to {e_date}.")
            continue

        # 5. Merge all individual channel DataFrames into one large table
        final_df = all_dfs[0]
        for i in range(1, len(all_dfs)):
            final_df = pd.merge(final_df, all_dfs[i], on='instant', how='outer')
        
        # 6. Remove internal [Total] debug columns from final output unless requested
        if not output_totals:
            cols_to_keep = [c for c in final_df.columns if '[Total]' not in c]
            final_df = final_df[cols_to_keep]

        # 7. Calculate and Save Totals for this month
        totals = final_df[[c for c in final_df.columns if c != 'instant']].sum()
        totals_df = pd.DataFrame([totals])
        totals_df.insert(0, 'Period', s_date.strftime('%Y-%m'))
        
        save_data(totals_df, e_date, output_folder)
        all_month_totals.append(totals_df)

    if not all_month_totals:
        return False

    # 8. Optional Google Sheets Upload
    if google_sheet_url:
        combined_totals_df = pd.concat(all_month_totals, ignore_index=True)
        return save_to_google_sheet(combined_totals_df, google_sheet_url, service_account_file)
    return True

def load_config(config_file: str = DEFAULT_CONFIG_FILE) -> Optional[Dict[str, Any]]:
    """Loads and validates the YAML configuration file."""
    if not os.path.exists(config_file):
        logging.error(f"Configuration file '{config_file}' not found.")
        return None

    try:
        with open(config_file, 'r') as f:
            config = yaml.safe_load(f)
        
        creds = config.get('credentials', {})
        data_cfg = config.get('data', {})
        
        settings = {
            'email': creds.get('username'),
            'password': creds.get('password'),
            'granularity': data_cfg.get('granularity', 'DAY').upper(),
            'google_sheet_url': config.get('output', {}).get('google_sheet_url'),
            'service_account_file': config.get('output', {}).get('service_account_file', DEFAULT_SERVICE_ACCOUNT_FILE)
        }
        
        if not settings['email'] or not settings['password'] or settings['email'] == 'your_emporia_email@example.com':
            logging.error("Missing or default credentials found in config.yaml. Please update with your account info.")
            return None
            
        return settings
    except Exception as e:
        logging.error(f"Failed to parse config file: {e}")
        return None

def main(args=None):
    """Main CLI entry point."""
    parser = argparse.ArgumentParser(description='Download and process Emporia Energy usage data.')
    parser.add_argument('-v', '--verbose', action='store_const', dest='verbosity', const='DEBUG', help='Enable debug logging.')
    parser.add_argument('-q', '--quiet', action='store_const', dest='verbosity', const='WARNING', help='Only log warnings and errors.')
    parser.add_argument('--month', type=str, nargs='+', help='Target month(s) in YYYY-MM or MM format. If MM, uses the most recent completed year.')
    parser.add_argument('--output_totals', action='store_true', help='Include raw [Total] columns for each device in the output.')
    parsed_args = parser.parse_args(args)

    setup_logging(parsed_args.verbosity or 'INFO')
    
    config = load_config()
    if not config:
        sys.exit(1)
        
    # Determine dates
    date_ranges = []
    try:
        if parsed_args.month:
            for m in parsed_args.month:
                date_ranges.append(get_dates_for_month(m))
        else:
            date_ranges.append(get_default_dates())
    except ValueError as e:
        logging.error(e)
        sys.exit(1)

    # Apply settings and flags
    config['date_ranges'] = date_ranges
    config['output_totals'] = parsed_args.output_totals
    
    if not download_emporia_data(**config):
        sys.exit(1)

if __name__ == '__main__':
    main()
