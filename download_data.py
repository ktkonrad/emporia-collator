import os
import pandas as pd
from datetime import datetime, date, timedelta
from pyemvue import PyEmVue
from pyemvue.enums import Scale, Unit

import logging
import argparse
import yaml
import gspread
from google.oauth2.service_account import Credentials
from typing import Optional, Tuple, Dict, Any, List

def setup_logging(verbosity: str):
    """
    Configures the logging module based on the provided verbosity level.

    Args:
        verbosity (str): The desired logging level ('DEBUG', 'INFO', 'WARNING', 'ERROR').
    """
    level = getattr(logging, verbosity.upper(), logging.INFO)
    logging.basicConfig(level=level, format='%(levelname)s: %(message)s')

def get_default_dates() -> Tuple[date, date]:
    """
    Helper function to get the default start and end dates: 
    27th of month before last to 26th of most recent month.

    Returns:
        Tuple[date, date]: A tuple containing the start and end dates.
    """
    today = date.today()
    if today.day >= 26:
        # Most recent 26th is in the current month
        e_date = today.replace(day=26)
        first_day_of_current_month = today.replace(day=1)
        last_day_of_last_month = first_day_of_current_month - timedelta(days=1)
        s_date = last_day_of_last_month.replace(day=27)
    else:
        # Most recent 26th was in the previous month
        first_day_of_current_month = today.replace(day=1)
        last_day_of_last_month = first_day_of_current_month - timedelta(days=1)
        e_date = last_day_of_last_month.replace(day=26)
        
        first_day_of_last_month = last_day_of_last_month.replace(day=1)
        last_day_of_month_before_last = first_day_of_last_month - timedelta(days=1)
        s_date = last_day_of_month_before_last.replace(day=27)
    
    return s_date, e_date

def authenticate(email: str, password: str) -> Optional[PyEmVue]:
    """
    Connects to the Emporia API and returns a logged-in PyEmVue object.

    Args:
        email (str): The user's email address.
        password (str): The user's password.

    Returns:
        Optional[PyEmVue]: A logged-in PyEmVue object, or None if login fails.
    """
    logging.info("Attempting to log in to Emporia Energy API...")
    try:
        vue = PyEmVue()
        vue.login(username=email, password=password)
        logging.info("Successfully logged in to Emporia.")
        return vue
    except Exception as e:
        logging.error(f"Error logging in to Emporia: {e}")
        logging.error("Please check your credentials and network connection.")
        return None

def get_emporia_device_info(vue: PyEmVue) -> Optional[Dict[int, Any]]:
    """
    Fetches all devices and consolidates channels for devices with multiple channel sets.

    Args:
        vue (PyEmVue): The logged-in PyEmVue object.

    Returns:
        Optional[Dict[int, Any]]: A dictionary of device information, or None if fetching fails.
    """
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
    """
    Fetches usage data for a single channel with a specified granularity and calculates the cost.

    Args:
        vue (PyEmVue): The logged-in PyEmVue object.
        channel (Any): The channel object to fetch data for.
        start_date (date): The start date for the data fetch.
        end_date (date): The end date for the data fetch.
        granularity (str): The granularity of the data (e.g., 'DAY', 'HOUR', 'MINUTE').

    Returns:
        Optional[pd.DataFrame]: A DataFrame with the channel's usage data (in kWh) and cost (in USD), or None.
    """
    if channel.name is None:
        logging.debug(f"  Skipping channel with no name and channel number {channel.channel_num}")
        return None
    if ',' in str(channel.channel_num):
        logging.debug(f"  Skipping pseudo-channel: {channel.name} ({channel.channel_num})")
        return None

    logging.info(f"  Fetching data for channel: {channel.name} ({channel.channel_num})")
    
    scale_map = {
        'MINUTE': (Scale.MINUTE.value, 'm'),
        'HOUR': (Scale.HOUR.value, 'h'),
        'DAY': (Scale.DAY.value, 'd'),
    }
    
    if granularity.upper() not in scale_map:
        logging.warning(f"  Unsupported granularity: {granularity}")
        return None
        
    scale_value, time_unit = scale_map[granularity.upper()]

    try:
        # Fetch USD data
        usage_usd, start_time_usd = vue.get_chart_usage(
            channel=channel,
            start=datetime.combine(start_date, datetime.min.time()),
            end=datetime.combine(end_date, datetime.max.time()).replace(second=0, microsecond=0),
            scale=scale_value,
            unit=Unit.USD.value
        )

        # Fetch kWh data
        usage_kwh, start_time_kwh = vue.get_chart_usage(
            channel=channel,
            start=datetime.combine(start_date, datetime.min.time()),
            end=datetime.combine(end_date, datetime.max.time()).replace(second=0, microsecond=0),
            scale=scale_value,
            unit=Unit.KWH.value
        )

        if usage_usd and usage_kwh:
            # Check if there is any data (not just None)
            if all(val is None for val in usage_usd) and all(val is None for val in usage_kwh):
                logging.warning(f"  No valid usage data returned for channel {channel.name}")
                return None

            timestamps = pd.to_datetime(start_time_usd) + pd.to_timedelta(range(len(usage_usd)), unit=time_unit)
            return pd.DataFrame({
                'instant': timestamps,
                f'channel_{channel.channel_num}_cost_usd': usage_usd,
                f'channel_{channel.channel_num}_usage_kwh': usage_kwh
            })
        else:
            logging.warning(f"  No data returned for channel {channel.name}")
            return None
    except Exception as e:
        logging.error(f"  Error fetching data for channel {channel.name}: {e}")
        return None

def fetch_device_data(vue: PyEmVue, device: Any, start_date: date, end_date: date, granularity: str) -> Optional[pd.DataFrame]:
    """
    Fetches data for all channels in a device and merges them into a single DataFrame.

    Args:
        vue (PyEmVue): The logged-in PyEmVue object.
        device (Any): The device to fetch data for.
        start_date (date): The start date for the data fetch.
        end_date (date): The end date for the data fetch.
        granularity (str): The granularity of the data.

    Returns:
        Optional[pd.DataFrame]: A merged DataFrame of all channel data for the device.
    """
    logging.info(f"Fetching data for device: {device.device_name} (gid: {device.device_gid})")
    channel_dfs = [fetch_channel_data(vue, ch, start_date, end_date, granularity) for ch in device.channels]
    channel_dfs = [df for df in channel_dfs if df is not None]

    if not channel_dfs:
        logging.warning(f"No data returned for any channels in {device.device_name}")
        return None

    # Merge all channel DataFrames for the device
    df = channel_dfs[0]
    for i in range(1, len(channel_dfs)):
        df = pd.merge(df, channel_dfs[i], on='instant', how='outer')

    df['device_gid'] = device.device_gid
    df['device_name'] = device.device_name
    return df

def save_to_google_sheet(df: pd.DataFrame, sheet_url: str, service_account_file: str):
    """
    Appends the provided DataFrame as a new row to a Google Sheet.

    Args:
        df (pd.DataFrame): The single-row DataFrame (totals) to append.
        sheet_url (str): The URL of the Google Sheet.
        service_account_file (str): Path to the Google service account JSON file.
    """
    if not os.path.exists(service_account_file):
        logging.error(f"Google service account file not found: {service_account_file}")
        return

    try:
        scope = ['https://www.googleapis.com/auth/spreadsheets']
        creds = Credentials.from_service_account_file(service_account_file, scopes=scope)
        client = gspread.authorize(creds)
        
        spreadsheet = client.open_by_url(sheet_url)
        
        # Parse the gid if present in the fragment
        import urllib.parse
        parsed_url = urllib.parse.urlparse(sheet_url)
        fragment = parsed_url.fragment
        query = urllib.parse.parse_qs(fragment)
        gid = query.get('gid', [None])[0]
        
        if gid:
            worksheet = None
            for sheet in spreadsheet.worksheets():
                if str(sheet.id) == gid:
                    worksheet = sheet
                    break
            if not worksheet:
                logging.warning(f"Worksheet with gid {gid} not found. Using first worksheet.")
                worksheet = spreadsheet.get_worksheet(0)
        else:
            worksheet = spreadsheet.get_worksheet(0)

        # Handle header if the sheet is empty
        values = worksheet.get_all_values()
        df_headers = df.columns.tolist()
        
        if not values:
            worksheet.append_row(df_headers)
        else:
            existing_headers = values[0]
            if existing_headers != df_headers:
                # Find the specific mismatches
                missing_in_sheet = set(df_headers) - set(existing_headers)
                extra_in_sheet = set(existing_headers) - set(df_headers)
                
                error_msg = "Google Sheet headers do not match. Aborting.\n"
                if missing_in_sheet:
                    error_msg += f"  - Missing in Google Sheet: {sorted(list(missing_in_sheet))}\n"
                if extra_in_sheet:
                    error_msg += f"  - Extra in Google Sheet: {sorted(list(extra_in_sheet))}\n"
                
                # Also check for order mismatch if sets are same
                if not missing_in_sheet and not extra_in_sheet:
                    error_msg += "  - Headers are the same but in different order.\n"
                    for i, (h1, h2) in enumerate(zip(df_headers, existing_headers)):
                        if h1 != h2:
                            error_msg += f"    - At index {i}: expected '{h1}', found '{h2}'\n"
                
                logging.error(error_msg)
                return
        
        # Append the row
        row_to_append = df.iloc[0].tolist()
        worksheet.append_row([str(val) if not isinstance(val, (int, float)) else val for val in row_to_append])
        logging.info("Successfully appended data to Google Sheet.")
        
    except Exception as e:
        logging.error(f"Error saving to Google Sheet: {e}")


def save_data(df: pd.DataFrame, start_date: date, output_folder: str):
    """
    Saves the combined DataFrame to a single CSV file.

    Args:
        df (pd.DataFrame): The combined DataFrame to save.
        start_date (date): The start date of the data period (for filename).
        output_folder (str): The folder to save the CSV in.
    """
    if not os.path.exists(output_folder):
        os.makedirs(output_folder)
        logging.info(f"Created output directory: {output_folder}")

    filename = f"{output_folder}/emporia_data_{start_date.strftime('%Y-%m')}.csv"
    df.to_csv(filename, index=False)
    logging.info(f"Successfully saved device data to {filename}")


def download_emporia_data(email: str, password: str, start_date: Optional[str], end_date: Optional[str], granularity: str, aggregate_devices: List[str], output_folder: str = 'emporia_data', google_sheet_url: Optional[str] = None, service_account_file: Optional[str] = None):
    """
    Orchestrates the download of Emporia data with configurable granularity into a single CSV file and optionally a Google Sheet.

    Args:
        email (str): The user's email address.
        password (str): The user's password.
        start_date (Optional[str]): The start date for the data fetch.
        end_date (Optional[str]): The end date for the data fetch.
        granularity (str): The granularity of the data.
        aggregate_devices (List[str]): List of device names to aggregate channels for.
        output_folder (str, optional): The folder to save data. Defaults to 'emporia_data'.
        google_sheet_url (str, optional): The URL of the Google Sheet to append to.
        service_account_file (str, optional): Path to the Google service account file.
    """
    vue = authenticate(email, password)
    if not vue:
        return

    device_info = get_emporia_device_info(vue)
    if not device_info:
        return

    if start_date and end_date:
        s_date = datetime.strptime(start_date, '%Y-%m-%d').date()
        e_date = datetime.strptime(end_date, '%Y-%m-%d').date()
        logging.info(f"Downloading data from {s_date} to {e_date} with {granularity} granularity.")
    else:
        s_date, e_date = get_default_dates()
        logging.info(f"Downloading data from {s_date} to {e_date} with {granularity} granularity.")

    all_column_dfs = []

    for device in device_info.values():
        if device.device_name in aggregate_devices:
            logging.info(f"Processing device (aggregated): {device.device_name}")
            device_df = fetch_device_data(vue, device, s_date, e_date, granularity)
            if device_df is not None:
                cost_cols = [col for col in device_df.columns if 'cost_usd' in col]
                usage_cols = [col for col in device_df.columns if 'usage_kwh' in col]
                device_df.set_index('instant', inplace=True)
                device_df[f"{device.device_name} (USD)"] = device_df[cost_cols].sum(axis=1)
                device_df[f"{device.device_name} (kWh)"] = device_df[usage_cols].sum(axis=1)
                aggregated_df = device_df[[f"{device.device_name} (USD)", f"{device.device_name} (kWh)"]].reset_index()
                all_column_dfs.append(aggregated_df)
        else:
            logging.info(f"Processing device (per-channel): {device.device_name}")
            for channel in device.channels:
                channel_df = fetch_channel_data(vue, channel, s_date, e_date, granularity)
                if channel_df is not None:
                    usd_col = next((col for col in channel_df.columns if 'cost_usd' in col), None)
                    kwh_col = next((col for col in channel_df.columns if 'usage_kwh' in col), None)
                    rename_dict = {}
                    if usd_col:
                        rename_dict[usd_col] = f"{channel.name} (USD)"
                    if kwh_col:
                        rename_dict[kwh_col] = f"{channel.name} (kWh)"
                    
                    if rename_dict:
                        channel_df.rename(columns=rename_dict, inplace=True)
                        all_column_dfs.append(channel_df)

    if all_column_dfs:
        # Merge all DataFrames into a single DataFrame
        final_df = all_column_dfs[0]
        for i in range(1, len(all_column_dfs)):
            final_df = pd.merge(final_df, all_column_dfs[i], on='instant', how='outer')
        
        # Calculate totals for the entire period
        # Exclude 'instant' from the sum by selecting only numeric columns
        numeric_cols = [col for col in final_df.columns if col != 'instant']
        totals = final_df[numeric_cols].sum()
        
        # Create a single-row DataFrame for the totals
        totals_df = pd.DataFrame([totals])
        # Add the period as the first column
        period_str = f"{s_date.strftime('%Y-%m-%d')} to {e_date.strftime('%Y-%m-%d')}"
        totals_df.insert(0, 'period', period_str)
        
        save_data(totals_df, s_date, output_folder)
        
        if google_sheet_url and service_account_file:
            save_to_google_sheet(totals_df, google_sheet_url, service_account_file)
    else:
        logging.warning("No data was downloaded for any device.")


import argparse

def load_config(config_file: str = 'config.yaml') -> Optional[Dict[str, Any]]:
    """
    Loads Emporia credentials and other configuration from a YAML file.
    Args:
        config_file (str, optional): Path to the config file. Defaults to 'config.yaml'.
    Returns:
        Optional[Dict[str, Any]]: A dictionary of configuration values, or None.
    """
    if not os.path.exists(config_file):
        logging.error(f"Error: Configuration file '{config_file}' not found.")
        return None

    with open(config_file, 'r') as f:
        try:
            config = yaml.safe_load(f)
        except yaml.YAMLError as e:
            logging.error(f"Error parsing YAML file: {e}")
            return None

    try:
        credentials = config.get('credentials')
        if credentials is None:
            credentials = {}
        
        data_config = config.get('data', {})

        start_date_obj = data_config.get('start_date')
        end_date_obj = data_config.get('end_date')

        settings = {
            'email': credentials.get('username'),
            'password': credentials.get('password'),
            'start_date': start_date_obj.strftime('%Y-%m-%d') if isinstance(start_date_obj, date) else start_date_obj,
            'end_date': end_date_obj.strftime('%Y-%m-%d') if isinstance(end_date_obj, date) else end_date_obj,
            'granularity': data_config.get('granularity', 'DAY').upper(),
            'aggregate_devices': config.get('aggregate_devices', []),
            'google_sheet_url': config.get('output', {}).get('google_sheet_url'),
            'service_account_file': config.get('output', {}).get('service_account_file', 'service_account.json')
        }
    except KeyError:
        logging.error("Error: 'credentials' section with 'username' and 'password' not found in config.yaml.")
        return None

    if not settings['email'] or not settings['password'] or settings['email'] == 'your_emporia_email@example.com':
        logging.error("Please update config.yaml with your Emporia credentials.")
        return None

    return settings

def main():
    """Main function to run the data download process."""
    parser = argparse.ArgumentParser(description='Download Emporia Energy data.')
    parser.add_argument('-v', '--verbose', action='store_const', dest='verbosity', const='DEBUG',
                        help='Enable verbose logging (DEBUG level).')
    parser.add_argument('-q', '--quiet', action='store_const', dest='verbosity', const='WARNING',
                        help='Enable quiet logging (WARNING level).')
    args = parser.parse_args()

    # Default to INFO level if no verbosity flag is set
    setup_logging(args.verbosity or 'INFO')

    config = load_config()
    if config:
        download_emporia_data(
            email=config['email'],
            password=config['password'],
            start_date=config['start_date'],
            end_date=config['end_date'],
            granularity=config['granularity'],
            aggregate_devices=config['aggregate_devices'],
            google_sheet_url=config.get('google_sheet_url'),
            service_account_file=config.get('service_account_file')
        )

if __name__ == '__main__':
    main()
