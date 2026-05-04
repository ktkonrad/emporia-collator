import os
import sys
import logging
import argparse
import yaml
import gspread
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
    Returns the most recent billing cycle dates (27th to 26th).
    """
    today = date.today()
    if today.day >= 26:
        e_date = today.replace(day=26)
    else:
        # Previous month's 26th
        e_date = (today.replace(day=1) - timedelta(days=1)).replace(day=26)
    
    # s_date is the 27th of the month before e_date
    s_date = (e_date.replace(day=1) - timedelta(days=1)).replace(day=27)
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

        if not usage_usd or not usage_kwh:
            return None
            
        if all(v is None for v in usage_usd) and all(v is None for v in usage_kwh):
            logging.debug(f"  No data returned for {ch_display_name}")
            return None

        # The API returns the exact UTC start time of the first data point
        if start_time_utc.tzinfo is None:
            start_time_utc = start_time_utc.replace(tzinfo=timezone.utc)
        
        # Localize the start time for the output DataFrame
        start_time_local = start_time_utc.astimezone(LOCAL_TZ)

        # Generate the timestamp series for each returned data point
        timestamps = pd.to_datetime([start_time_local] * len(usage_usd)) + pd.to_timedelta(range(len(usage_usd)), unit=pandas_freq)
        
        return pd.DataFrame({
            'instant': timestamps,
            f'{ch_display_name} (USD)': usage_usd,
            f'{ch_display_name} (kWh)': usage_kwh
        })
    except Exception as e:
        logging.error(f"  API Error for {ch_display_name}: {e}")
    return None

def compute_balance(device_name: str, total_df: pd.DataFrame, monitored_dfs: List[pd.DataFrame]) -> Optional[pd.DataFrame]:
    """
    Computes the 'Balance' channel.
    Balance = [Total (1,2,3)] - sum(All other named/monitored channels).
    This represents unmonitored load on the main phases.
    """
    if total_df is None:
        return None
        
    balance_name = f"{device_name}: Balance"
    
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
    
    # Only return the balance if it's statistically significant (non-zero)
    if (balance_df[f'{balance_name} (USD)'].abs() > 1e-6).any() or \
       (balance_df[f'{balance_name} (kWh)'].abs() > 1e-6).any():
        return balance_df
    return None

def fetch_device_data(vue: PyEmVue, device: Any, s_date: date, e_date: date, granularity: str) -> List[pd.DataFrame]:
    """
    Orchestrates the fetching of all data for a single device.
    Includes named channels, calculated balance, and raw total.
    """
    logging.info(f"Processing Device: {device.device_name}")
    
    # 1. Identify the 'Total' pseudochannel (1,2,3)
    total_channel = next((ch for ch in device.channels if str(ch.channel_num) == '1,2,3'), None)
    
    # 2. Filter for individual channels that have a name.
    # We skip pseudochannels (those with commas in the channel_num) here 
    # because they represent aggregates already covered by individual channels or the Total.
    monitored_channels = [
        ch for ch in device.channels 
        if ch.name and ',' not in str(ch.channel_num)
    ]

    # 3. Fetch the absolute total (for balance calculation and verification)
    total_df = fetch_channel_data(vue, total_channel, s_date, e_date, granularity, target_name="TOTAL") if total_channel else None
    
    # 4. Fetch usage for all individual monitored channels
    component_dfs = []
    for ch in monitored_channels:
        display_name = f"{device.device_name}: {ch.name}"
        df = fetch_channel_data(vue, ch, s_date, e_date, granularity, target_name=display_name)
        if df is not None:
            component_dfs.append(df)
            
    # 5. Calculate the 'Balance' (Total - sum(monitored components))
    balance_df = compute_balance(device.device_name, total_df, component_dfs)
    
    # 6. Build the final set of DataFrames for this device
    results = component_dfs
    if balance_df is not None:
        results.append(balance_df)
        
    if total_df is not None:
        # Include a raw 'Total' column for verification (prefixed to avoid accidental aggregation)
        raw_total_df = total_df.copy()
        raw_total_df.columns = ['instant', f"{device.device_name}: [Total] (USD)", f"{device.device_name}: [Total] (kWh)"]
        results.append(raw_total_df)
            
    return results

def save_to_google_sheet(df: pd.DataFrame, sheet_url: str, service_account_file: str) -> bool:
    """
    Appends the summary row to a Google Sheet.
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
        
        # Append the first row of the summary (usually the totals row)
        worksheet.append_row([str(val) if not isinstance(val, (int, float)) else val for val in df.iloc[0]])
        logging.info("Successfully updated Google Sheet.")
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

def download_emporia_data(email: str, password: str, start_date_str: Optional[str], end_date_str: Optional[str], granularity: str, output_folder: str = DEFAULT_OUTPUT_FOLDER, google_sheet_url: Optional[str] = None, service_account_file: Optional[str] = DEFAULT_SERVICE_ACCOUNT_FILE, output_totals: bool = False) -> bool:
    """
    The main orchestration function.
    1. Authenticates
    2. Discovers Devices
    3. Fetches Historical Data
    4. Processes/Aggregates
    5. Saves locally and optionally to Google Sheets.
    """
    # 1. API Login
    vue = authenticate(email, password)
    if not vue: return False

    # 2. Device Discovery
    device_info = get_emporia_device_info(vue)
    if not device_info: return False

    # 3. Determine Date Range
    try:
        if start_date_str and end_date_str:
            s_date = datetime.strptime(start_date_str, '%Y-%m-%d').date()
            e_date = datetime.strptime(end_date_str, '%Y-%m-%d').date()
        else:
            s_date, e_date = get_default_dates()
    except ValueError as e:
        logging.error(f"Invalid date format in config: {e}")
        return False
        
    logging.info(f"Target Period: {s_date} to {e_date} (Granularity: {granularity})")

    # 4. Fetch data for all discovered devices
    all_dfs = []

    for device in device_info.values():
        device_dfs = fetch_device_data(vue, device, s_date, e_date, granularity)
        if not device_dfs: continue
            
        all_dfs.extend(device_dfs)

    if not all_dfs:
        logging.warning("No data was retrieved for the specified period.")
        return False

    # 5. Merge all individual channel DataFrames into one large table
    # We use an 'outer' merge on 'instant' to ensure no timestamps are lost.
    final_df = all_dfs[0]
    for i in range(1, len(all_dfs)):
        final_df = pd.merge(final_df, all_dfs[i], on='instant', how='outer')
    
    # 6. Remove internal [Total] debug columns from final output unless requested
    if not output_totals:
        cols_to_keep = [c for c in final_df.columns if '[Total]' not in c]
        final_df = final_df[cols_to_keep]

    # 7. Calculate and Save Totals
    totals = final_df[[c for c in final_df.columns if c != 'instant']].sum()
    totals_df = pd.DataFrame([totals])
    totals_df.insert(0, 'Period', f"{s_date} to {e_date}")
    
    save_data(totals_df, e_date, output_folder)
    
    # 8. Optional Google Sheets Upload
    if google_sheet_url:
        return save_to_google_sheet(totals_df, google_sheet_url, service_account_file)
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
        
        # Helper to ensure dates are strings for easier downstream processing
        def fmt_date(d): return d.strftime('%Y-%m-%d') if isinstance(d, date) else d

        settings = {
            'email': creds.get('username'),
            'password': creds.get('password'),
            'start_date_str': fmt_date(data_cfg.get('start_date')),
            'end_date_str': fmt_date(data_cfg.get('end_date')),
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
    parser.add_argument('--output_totals', action='store_true', help='Include raw [Total] columns for each device in the output.')
    parsed_args = parser.parse_args(args)

    setup_logging(parsed_args.verbosity or 'INFO')
    
    config = load_config()
    if not config:
        sys.exit(1)
        
    # Apply CLI flags
    config['output_totals'] = parsed_args.output_totals
    
    if not download_emporia_data(**config):
        sys.exit(1)

if __name__ == '__main__':
    main()
