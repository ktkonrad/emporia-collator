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
    """Fetches and consolidates device information."""
    logging.info("Fetching devices...")
    try:
        devices = vue.get_devices()
        device_info: Dict[int, Any] = {}
        for device in devices:
            vue.populate_device_properties(device)
            gid = device.device_gid
            if gid not in device_info:
                device_info[gid] = device
            else:
                # Merge properties if device already exists (e.g., from multiple API calls or nested devices)
                if not device_info[gid].device_name: device_info[gid].device_name = device.device_name
                
                existing_nums = {ch.channel_num for ch in device_info[gid].channels}
                for ch in device.channels:
                    if ch.channel_num not in existing_nums:
                        device_info[gid].channels.append(ch)
                        existing_nums.add(ch.channel_num)
        
        logging.info(f"Found {len(device_info)} devices.")
        return device_info
    except Exception as e:
        logging.error(f"Error getting devices: {e}")
        return None

def fetch_channel_data(vue: PyEmVue, channel: Any, start_date: date, end_date: date, granularity: str, target_name: Optional[str] = None) -> Optional[pd.DataFrame]:
    """
    Fetches usage and cost data for a single channel.
    
    Emporia devices use channel_num '1', '2', and '3' for the main phases.
    Expansion CTs (e.g., for individual circuits) start at channel_num '4' and onwards.
    """
    # Identify if this is a main phase
    is_main = str(channel.channel_num) in ['1', '2', '3']
    ch_name = target_name or channel.name
    
    # Skip multi-channels (like '1,2,4') but allow '1,2,3' for the total
    if ',' in str(channel.channel_num) and str(channel.channel_num) != '1,2,3':
        return None
        
    if not ch_name:
        # For channels that might not have a user-defined name
        if is_main:
            ch_name = f"Main ({channel.channel_num})"
        else:
            ch_name = f"Channel {channel.channel_num}"

    logging.info(f"  Fetching data for channel: {ch_name} ({channel.channel_num})")
    
    if granularity.upper() not in GRANULARITY_MAP:
        logging.warning(f"  Unsupported granularity: {granularity}")
        return None
        
    scale_value, time_unit = GRANULARITY_MAP[granularity.upper()]
    
    # Create local datetimes and convert to UTC
    start_dt_local = datetime.combine(start_date, datetime.min.time()).replace(tzinfo=LOCAL_TZ)
    end_dt_local = datetime.combine(end_date, datetime.max.time()).replace(second=0, microsecond=0, tzinfo=LOCAL_TZ)
    
    start_dt_utc = start_dt_local.astimezone(timezone.utc)
    end_dt_utc = end_dt_local.astimezone(timezone.utc)

    try:
        usage_usd, start_time_usd = vue.get_chart_usage(channel=channel, start=start_dt_utc, end=end_dt_utc, scale=scale_value, unit=Unit.USD.value)
        usage_kwh, _ = vue.get_chart_usage(channel=channel, start=start_dt_utc, end=end_dt_utc, scale=scale_value, unit=Unit.KWH.value)

        if usage_usd and usage_kwh:
            if all(v is None for v in usage_usd) and all(v is None for v in usage_kwh):
                logging.warning(f"  {ch_name} is empty")
                return None

            # Convert returned UTC timestamp to local timezone
            if start_time_usd.tzinfo is None:
                start_time_usd = start_time_usd.replace(tzinfo=timezone.utc)
            start_time_local = start_time_usd.astimezone(LOCAL_TZ)

            timestamps = pd.to_datetime([start_time_local] * len(usage_usd)) + pd.to_timedelta(range(len(usage_usd)), unit=time_unit)
            return pd.DataFrame({
                'instant': timestamps,
                f'{ch_name} (USD)': usage_usd,
                f'{ch_name} (kWh)': usage_kwh
            })
    except Exception as e:
        logging.error(f"  Error fetching data for channel {ch_name}: {e}")
    return None

def compute_balance(device_name: str, total_df: pd.DataFrame, other_dfs: List[pd.DataFrame]) -> Optional[pd.DataFrame]:
    """Computes the balance channel: Total (1,2,3) - sum(all other monitored channels)."""
    if total_df is None:
        return None
        
    balance_name = f"{device_name} balance"
    combined_df = total_df[['instant']].copy()
    
    usd_cols = []
    kwh_cols = []
    
    for df in other_dfs:
        combined_df = pd.merge(combined_df, df, on='instant', how='left')
        usd_cols.extend([c for c in df.columns if '(USD)' in c])
        kwh_cols.extend([c for c in df.columns if '(kWh)' in c])
        
    combined_df = combined_df.fillna(0)
    
    sum_usd = combined_df[usd_cols].sum(axis=1) if usd_cols else 0
    sum_kwh = combined_df[kwh_cols].sum(axis=1) if kwh_cols else 0
    
    balance_df = pd.DataFrame({
        'instant': total_df['instant'],
        f'{balance_name} (USD)': total_df['TOTAL (USD)'] - sum_usd,
        f'{balance_name} (kWh)': total_df['TOTAL (kWh)'] - sum_kwh
    })
    
    # Only return if balance is non-zero
    if (balance_df[f'{balance_name} (USD)'].abs() > 1e-6).any() or \
       (balance_df[f'{balance_name} (kWh)'].abs() > 1e-6).any():
        return balance_df
    return None

def fetch_device_data(vue: PyEmVue, device: Any, s_date: date, e_date: date, granularity: str) -> List[pd.DataFrame]:
    """Fetches all individual channels for a device as separate DataFrames."""
    logging.info(f"Fetching data for device: {device.device_name}")
    
    total_channel = next((ch for ch in device.channels if str(ch.channel_num) == '1,2,3'), None)
    
    # Filter for channels that have a name
    # We require ch.name to be non-empty to fetch it individually
    named_channels = [ch for ch in device.channels if ch.name]
    
    main_channels = [ch for ch in named_channels if str(ch.channel_num) in ['1', '2', '3']]
    expansion_channels = [ch for ch in named_channels if ',' not in str(ch.channel_num) and str(ch.channel_num) not in ['1', '2', '3']]

    total_df = fetch_channel_data(vue, total_channel, s_date, e_date, granularity, target_name="TOTAL") if total_channel else None
    
    main_dfs = []
    for ch in main_channels:
        df = fetch_channel_data(vue, ch, s_date, e_date, granularity, target_name=ch.name)
        if df is not None: main_dfs.append(df)
    
    expansion_dfs = []
    for ch in expansion_channels:
        df = fetch_channel_data(vue, ch, s_date, e_date, granularity, target_name=ch.name)
        if df is not None: expansion_dfs.append(df)
            
    balance_df = compute_balance(device.device_name, total_df, main_dfs + expansion_dfs)
    
    results = main_dfs + expansion_dfs
    if balance_df is not None:
        results.append(balance_df)
        
    if total_df is not None:
        # Include raw total for debugging/verification
        raw_total_df = total_df.copy()
        raw_total_df.columns = ['instant', f'[Total] {device.device_name} (USD)', f'[Total] {device.device_name} (kWh)']
        results.append(raw_total_df)
            
    return results

def save_to_google_sheet(df: pd.DataFrame, sheet_url: str, service_account_file: str) -> bool:
    """Appends data to a Google Sheet."""
    if not os.path.exists(service_account_file):
        logging.error(f"Google service account file not found: {service_account_file}")
        return False

    try:
        creds = Credentials.from_service_account_file(service_account_file, scopes=['https://www.googleapis.com/auth/spreadsheets'])
        client = gspread.authorize(creds)
        spreadsheet = client.open_by_url(sheet_url)
        
        # Determine worksheet
        parsed_url = urllib.parse.urlparse(sheet_url)
        gid = urllib.parse.parse_qs(parsed_url.fragment).get('gid', [None])[0]
        worksheet = next((s for s in spreadsheet.worksheets() if str(s.id) == gid), spreadsheet.get_worksheet(0)) if gid else spreadsheet.get_worksheet(0)

        values = worksheet.get_all_values()
        df_headers = df.columns.tolist()
        
        if not values:
            worksheet.append_row(df_headers)
        elif values[0] != df_headers:
            existing_headers = values[0]
            missing_in_sheet = set(df_headers) - set(existing_headers)
            extra_in_sheet = set(existing_headers) - set(df_headers)
            
            error_msg = "Google Sheet headers do not match. Aborting.\n"
            if missing_in_sheet:
                error_msg += f"  - Missing in Google Sheet: {sorted(list(missing_in_sheet))}\n"
            if extra_in_sheet:
                error_msg += f"  - Extra in Google Sheet: {sorted(list(extra_in_sheet))}\n"
            
            if not missing_in_sheet and not extra_in_sheet:
                error_msg += "  - Order mismatch:\n"
                for i, (h1, h2) in enumerate(zip(df_headers, existing_headers)):
                    if h1 != h2:
                        error_msg += f"    - Index {i}: expected '{h1}', found '{h2}'\n"
            
            logging.error(error_msg)
            return False
        
        worksheet.append_row([str(val) if not isinstance(val, (int, float)) else val for val in df.iloc[0]])
        logging.info("Successfully appended data to Google Sheet.")
        return True
    except Exception as e:
        logging.error(f"Error saving to Google Sheet: {e}")
        return False

def save_data(df: pd.DataFrame, reference_date: date, output_folder: str, suffix: str = ""):
    """Saves data to a CSV file."""
    if not os.path.exists(output_folder):
        os.makedirs(output_folder)
    filename = f"{output_folder}/emporia_data_{reference_date.strftime('%Y-%m')}{suffix}.csv"
    df.to_csv(filename, index=False)
    logging.info(f"Successfully saved device data to {filename}")

def aggregate_data(df: pd.DataFrame, aggregation_map: Dict[str, List[str]], keep_sub_channels: bool = False) -> pd.DataFrame:
    """Aggregates columns based on the provided map."""
    processed_df = pd.DataFrame({'instant': df['instant']})
    consumed_cols = set()
    
    for output_name, input_cols in aggregation_map.items():
        usd_cols = [c for c in input_cols if '(USD)' in c]
        kwh_cols = [c for c in input_cols if '(kWh)' in c]
        
        if usd_cols: processed_df[f"{output_name} (USD)"] = df[usd_cols].sum(axis=1)
        if kwh_cols: processed_df[f"{output_name} (kWh)"] = df[kwh_cols].sum(axis=1)
        
        if keep_sub_channels:
            for col in input_cols:
                processed_df[f"[sub] {col}"] = df[col]
                
        consumed_cols.update(input_cols)
            
    remaining_cols = [c for c in df.columns if c != 'instant' and c not in consumed_cols]
    for col in remaining_cols:
        processed_df[col] = df[col]
    return processed_df

def download_emporia_data(email: str, password: str, start_date: Optional[str], end_date: Optional[str], granularity: str, aggregate_devices: List[str], skip_aggregation: bool = False, all_channels: bool = False, output_folder: str = DEFAULT_OUTPUT_FOLDER, google_sheet_url: Optional[str] = None, service_account_file: Optional[str] = DEFAULT_SERVICE_ACCOUNT_FILE) -> bool:
    """Main orchestration function for downloading and saving data."""
    vue = authenticate(email, password)
    if not vue: return False

    device_info = get_emporia_device_info(vue)
    if not device_info: return False

    s_date, e_date = (datetime.strptime(start_date, '%Y-%m-%d').date(), datetime.strptime(end_date, '%Y-%m-%d').date()) if start_date and end_date else get_default_dates()
    logging.info(f"Period: {s_date} to {e_date} ({granularity})")

    all_dfs = []
    aggregation_map: Dict[str, List[str]] = {}

    for device in device_info.values():
        device_dfs = fetch_device_data(vue, device, s_date, e_date, granularity)
        if not device_dfs: continue
            
        all_dfs.extend(device_dfs)
        if device.device_name in aggregate_devices:
            # Exclude [Total] columns from aggregation to avoid double-counting
            aggregation_map[device.device_name] = [c for df in device_dfs for c in df.columns if c != 'instant' and not c.startswith('[Total]')]

    if not all_dfs:
        logging.warning("No data downloaded.")
        return False

    final_df = all_dfs[0]
    for i in range(1, len(all_dfs)):
        final_df = pd.merge(final_df, all_dfs[i], on='instant', how='outer')
    
    if all_channels:
        logging.info("Creating all channels output...")
        all_channels_df = aggregate_data(final_df, aggregation_map, keep_sub_channels=True)
        all_channels_totals = all_channels_df[[c for c in all_channels_df.columns if c != 'instant']].sum()
        all_channels_totals_df = pd.DataFrame([all_channels_totals])
        all_channels_totals_df.insert(0, 'Period', f"{s_date} to {e_date}")
        save_data(all_channels_totals_df, e_date, output_folder, suffix='_all_channels')

    if not skip_aggregation:
        logging.info("Aggregating data as configured...")
        final_df = aggregate_data(final_df, aggregation_map)
        
    # Remove [Total] columns from final output unless specifically requested
    if not (all_channels or skip_aggregation):
        cols_to_keep = [c for c in final_df.columns if not c.startswith('[Total]')]
        final_df = final_df[cols_to_keep]

    totals = final_df[[c for c in final_df.columns if c != 'instant']].sum()
    totals_df = pd.DataFrame([totals])
    totals_df.insert(0, 'Period', f"{s_date} to {e_date}")
    
    save_data(totals_df, e_date, output_folder)
    return save_to_google_sheet(totals_df, google_sheet_url, service_account_file) if google_sheet_url else True

def load_config(config_file: str = DEFAULT_CONFIG_FILE) -> Optional[Dict[str, Any]]:
    """Loads and validates the configuration file."""
    if not os.path.exists(config_file):
        logging.error(f"Config file '{config_file}' not found.")
        return None

    try:
        with open(config_file, 'r') as f:
            config = yaml.safe_load(f)
        
        creds = config.get('credentials', {})
        data_cfg = config.get('data', {})
        
        def fmt_date(d): return d.strftime('%Y-%m-%d') if isinstance(d, date) else d

        settings = {
            'email': creds.get('username'),
            'password': creds.get('password'),
            'start_date': fmt_date(data_cfg.get('start_date')),
            'end_date': fmt_date(data_cfg.get('end_date')),
            'granularity': data_cfg.get('granularity', 'DAY').upper(),
            'aggregate_devices': config.get('aggregate_devices', []),
            'google_sheet_url': config.get('output', {}).get('google_sheet_url'),
            'service_account_file': config.get('output', {}).get('service_account_file', DEFAULT_SERVICE_ACCOUNT_FILE)
        }
        
        if not settings['email'] or not settings['password'] or settings['email'] == 'your_emporia_email@example.com':
            logging.error("Update config.yaml with valid credentials.")
            return None
        return settings
    except Exception as e:
        logging.error(f"Error loading config: {e}")
        return None

def main(args=None):
    """Main entry point."""
    parser = argparse.ArgumentParser(description='Download Emporia Energy data.')
    parser.add_argument('-v', '--verbose', action='store_const', dest='verbosity', const='DEBUG', help='Verbose logging.')
    parser.add_argument('-q', '--quiet', action='store_const', dest='verbosity', const='WARNING', help='Quiet logging.')
    parser.add_argument('--skip_aggregation', action='store_true', help='Output all individual channels, ignoring aggregation config.')
    parser.add_argument('--all_channels', action='store_true', help='Output all individual channels as a separate CSV, with aggregated columns.')
    parsed_args = parser.parse_args(args)

    setup_logging(parsed_args.verbosity or 'INFO')
    config = load_config()
    if not config:
        sys.exit(1)
        
    config['skip_aggregation'] = parsed_args.skip_aggregation
    config['all_channels'] = parsed_args.all_channels
    if not download_emporia_data(**config):
        sys.exit(1)

if __name__ == '__main__':
    main()
