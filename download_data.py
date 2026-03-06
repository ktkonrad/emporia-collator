import os
import sys
import logging
import argparse
import yaml
import gspread
import pandas as pd
import urllib.parse
from datetime import datetime, date, timedelta
from typing import Optional, Tuple, Dict, Any, List
from pyemvue import PyEmVue
from pyemvue.enums import Scale, Unit
from google.oauth2.service_account import Credentials

# Constants
DEFAULT_CONFIG_FILE = 'config.yaml'
DEFAULT_OUTPUT_FOLDER = 'emporia_data'
DEFAULT_SERVICE_ACCOUNT_FILE = 'service_account.json'
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
            if device.device_gid not in device_info:
                device_info[device.device_gid] = device
            else:
                device_info[device.device_gid].channels.extend(device.channels)
        logging.info(f"Found {len(device_info)} devices.")
        return device_info
    except Exception as e:
        logging.error(f"Error getting devices: {e}")
        return None

def fetch_channel_data(vue: PyEmVue, channel: Any, start_date: date, end_date: date, granularity: str) -> Optional[pd.DataFrame]:
    """Fetches usage and cost data for a single channel."""
    if not channel.name or ',' in str(channel.channel_num):
        return None

    logging.info(f"  Fetching data for channel: {channel.name} ({channel.channel_num})")
    
    if granularity.upper() not in GRANULARITY_MAP:
        logging.warning(f"  Unsupported granularity: {granularity}")
        return None
        
    scale_value, time_unit = GRANULARITY_MAP[granularity.upper()]
    start_dt = datetime.combine(start_date, datetime.min.time())
    end_dt = datetime.combine(end_date, datetime.max.time()).replace(second=0, microsecond=0)

    try:
        usage_usd, start_time_usd = vue.get_chart_usage(channel=channel, start=start_dt, end=end_dt, scale=scale_value, unit=Unit.USD.value)
        usage_kwh, _ = vue.get_chart_usage(channel=channel, start=start_dt, end=end_dt, scale=scale_value, unit=Unit.KWH.value)

        if usage_usd and usage_kwh:
            if all(v is None for v in usage_usd) and all(v is None for v in usage_kwh):
                logging.warning(f"  No valid usage data returned for channel {channel.name}")
                return None

            timestamps = pd.to_datetime(start_time_usd) + pd.to_timedelta(range(len(usage_usd)), unit=time_unit)
            return pd.DataFrame({
                'instant': timestamps,
                f'channel_{channel.channel_num}_cost_usd': usage_usd,
                f'channel_{channel.channel_num}_usage_kwh': usage_kwh
            })
    except Exception as e:
        logging.error(f"  Error fetching data for channel {channel.name}: {e}")
    return None

def process_aggregated_device(vue: PyEmVue, device: Any, s_date: date, e_date: date, granularity: str) -> Optional[pd.DataFrame]:
    """Processes a device by summing all its channels."""
    logging.info(f"Processing device (aggregated): {device.device_name}")
    channel_dfs = [fetch_channel_data(vue, ch, s_date, e_date, granularity) for ch in device.channels]
    channel_dfs = [df for df in channel_dfs if df is not None]
    
    if not channel_dfs:
        return None
        
    df = channel_dfs[0]
    for i in range(1, len(channel_dfs)):
        df = pd.merge(df, channel_dfs[i], on='instant', how='outer')
        
    cost_cols = [col for col in df.columns if 'cost_usd' in col]
    usage_cols = [col for col in df.columns if 'usage_kwh' in col]
    
    res = pd.DataFrame({'instant': df['instant']})
    res[f"{device.device_name} (USD)"] = df[cost_cols].sum(axis=1)
    res[f"{device.device_name} (kWh)"] = df[usage_cols].sum(axis=1)
    return res

def process_per_channel_device(vue: PyEmVue, device: Any, s_date: date, e_date: date, granularity: str) -> List[pd.DataFrame]:
    """Processes a device by keeping channels separate."""
    logging.info(f"Processing device (per-channel): {device.device_name}")
    dfs = []
    for channel in device.channels:
        df = fetch_channel_data(vue, channel, s_date, e_date, granularity)
        if df is not None:
            rename_dict = {
                next(c for c in df.columns if 'cost_usd' in c): f"{channel.name} (USD)",
                next(c for c in df.columns if 'usage_kwh' in c): f"{channel.name} (kWh)"
            }
            dfs.append(df.rename(columns=rename_dict))
    return dfs

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

def save_data(df: pd.DataFrame, reference_date: date, output_folder: str):
    """Saves data to a CSV file."""
    if not os.path.exists(output_folder):
        os.makedirs(output_folder)
    filename = f"{output_folder}/emporia_data_{reference_date.strftime('%Y-%m')}.csv"
    df.to_csv(filename, index=False)
    logging.info(f"Successfully saved device data to {filename}")

def download_emporia_data(email: str, password: str, start_date: Optional[str], end_date: Optional[str], granularity: str, aggregate_devices: List[str], output_folder: str = DEFAULT_OUTPUT_FOLDER, google_sheet_url: Optional[str] = None, service_account_file: Optional[str] = DEFAULT_SERVICE_ACCOUNT_FILE) -> bool:
    """Main orchestration function for downloading and saving data."""
    vue = authenticate(email, password)
    if not vue: return False

    device_info = get_emporia_device_info(vue)
    if not device_info: return False

    s_date, e_date = (datetime.strptime(start_date, '%Y-%m-%d').date(), datetime.strptime(end_date, '%Y-%m-%d').date()) if start_date and end_date else get_default_dates()
    logging.info(f"Period: {s_date} to {e_date} ({granularity})")

    all_dfs = []
    for device in device_info.values():
        if device.device_name in aggregate_devices:
            df = process_aggregated_device(vue, device, s_date, e_date, granularity)
            if df is not None: all_dfs.append(df)
        else:
            all_dfs.extend(process_per_channel_device(vue, device, s_date, e_date, granularity))

    if not all_dfs:
        logging.warning("No data downloaded.")
        return False

    final_df = all_dfs[0]
    for i in range(1, len(all_dfs)):
        final_df = pd.merge(final_df, all_dfs[i], on='instant', how='outer')
    
    totals = final_df[[c for c in final_df.columns if c != 'instant']].sum()
    totals_df = pd.DataFrame([totals])
    totals_df.insert(0, 'Period', f"{s_date} to {e_date}")
    
    save_data(totals_df, e_date, output_folder)
    if google_sheet_url:
        return save_to_google_sheet(totals_df, google_sheet_url, service_account_file)
    return True

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
    parsed_args = parser.parse_args(args)

    setup_logging(parsed_args.verbosity or 'INFO')
    config = load_config()
    if not config or not download_emporia_data(**config):
        sys.exit(1)

if __name__ == '__main__':
    main()
