import os
import pandas as pd
from datetime import datetime, date, timedelta
from pyemvue import PyEmVue
from pyemvue.enums import Scale
import configparser
import logging
import argparse
from typing import Optional, Tuple, Dict, Any, List

def setup_logging(verbosity: str):
    """
    Configures the logging module based on the provided verbosity level.

    Args:
        verbosity (str): The desired logging level ('DEBUG', 'INFO', 'WARNING', 'ERROR').
    """
    level = getattr(logging, verbosity.upper(), logging.INFO)
    logging.basicConfig(level=level, format='%(levelname)s: %(message)s')

def get_last_month_dates() -> Tuple[date, date]:
    """
    Helper function to get the start and end dates for the previous calendar month.

    Returns:
        Tuple[date, date]: A tuple containing the first and last day of the previous month.
    """
    today = date.today()
    first_day_of_current_month = today.replace(day=1)
    last_day_of_last_month = first_day_of_current_month - timedelta(days=1)
    first_day_of_last_month = last_day_of_last_month.replace(day=1)
    return first_day_of_last_month, last_day_of_last_month

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
    Fetches all devices, populates their properties including electricity rate, 
    and consolidates channels for devices with multiple channel sets.

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
            vue.populate_device_properties(device)
            if device.device_gid not in device_info:
                device_info[device.device_gid] = device
            else:
                device_info[device.device_gid].channels.extend(device.channels)
        logging.info(f"Found {len(device_info)} devices.")
        return device_info
    except Exception as e:
        logging.error(f"Error getting devices: {e}")
        return None

def fetch_channel_data(vue: PyEmVue, device: Any, channel: Any, start_date: date, end_date: date, granularity: str) -> Optional[pd.DataFrame]:
    """
    Fetches usage data for a single channel with a specified granularity and calculates the cost.

    Args:
        vue (PyEmVue): The logged-in PyEmVue object.
        device (Any): The device object, containing location information with electricity rate.
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
        usage_data, start_time = vue.get_chart_usage(
            channel=channel,
            start=datetime.combine(start_date, datetime.min.time()),
            end=datetime.combine(end_date, datetime.max.time()).replace(second=0, microsecond=0),
            scale=scale_value
        )

        if usage_data:
            # Filter out None values from usage_data
            filtered_usage_data = [val for val in usage_data if val is not None]
            if not filtered_usage_data:
                logging.warning(f"  No valid usage data returned for channel {channel.name}")
                return None

            rate = device.usage_cent_per_kw_hour / 100 # Convert cents to dollars
            timestamps = pd.to_datetime(start_time) + pd.to_timedelta(range(len(filtered_usage_data)), unit=time_unit)
            usage_kwh = [val / 1000 for val in filtered_usage_data] # Assuming usage from API is in Wh, convert to kWh
            cost = [val * rate for val in usage_kwh]
            return pd.DataFrame({
                'instant': timestamps,
                f'channel_{channel.channel_num}_usage_kwh': usage_kwh,
                f'channel_{channel.channel_num}_cost_usd': cost
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
    channel_dfs = [fetch_channel_data(vue, device, ch, start_date, end_date, granularity) for ch in device.channels]
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

def download_emporia_data(email: str, password: str, start_date: Optional[str], end_date: Optional[str], granularity: str, output_folder: str = 'emporia_data'):
    """
    Orchestrates the download of Emporia data with configurable granularity into a single CSV file.

    Args:
        email (str): The user's email address.
        password (str): The user's password.
        start_date (Optional[str]): The start date for the data fetch.
        end_date (Optional[str]): The end date for the data fetch.
        granularity (str): The granularity of the data.
        output_folder (str, optional): The folder to save data. Defaults to 'emporia_data'.
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
        s_date, e_date = get_last_month_dates()
        logging.info(f"Downloading data from {s_date} to {e_date} with {granularity} granularity.")

    all_device_dfs: List[pd.DataFrame] = []
    for gid, device in device_info.items():
        device_df = fetch_device_data(vue, device, s_date, e_date, granularity)
        if device_df is not None:
            all_device_dfs.append(device_df)

    if all_device_dfs:
        combined_df = pd.concat(all_device_dfs, ignore_index=True)
        save_data(combined_df, s_date, output_folder)
    else:
        logging.warning("No data was downloaded for any device.")

def load_config(config_file: str = 'config.cfg') -> Optional[Dict[str, Any]]:
    """
    Loads Emporia credentials and other configuration from a file.

    Args:
        config_file (str, optional): Path to the config file. Defaults to 'config.cfg'.

    Returns:
        Optional[Dict[str, Any]]: A dictionary of configuration values, or None.
    """
    config = configparser.ConfigParser()
    if not os.path.exists(config_file):
        logging.error(f"Error: Configuration file '{config_file}' not found.")
        logging.error("Create it with:\n[emporia]\nusername = your_email@example.com\npassword = your_password")
        return None
        
    config.read(config_file)

    try:
        settings = {
            'email': config['emporia']['username'],
            'password': config['emporia']['password'],
            'start_date': config['emporia'].get('start_date'),
            'end_date': config['emporia'].get('end_date'),
            'granularity': config['emporia'].get('granularity', 'DAY').upper()
        }
    except (KeyError, configparser.NoSectionError):
        logging.error("Error: 'emporia' section not found in config.cfg.")
        logging.error("Ensure the config file has:\n[emporia]\nusername = your_email@example.com\npassword = your_password")
        return None

    if settings['email'] == "your_email@example.com" or settings['password'] == "your_password":
        logging.error("Please update config.cfg with your Emporia credentials.")
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
            granularity=config['granularity']
        )

if __name__ == '__main__':
    main()
