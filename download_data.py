import os
import pandas as pd
from datetime import datetime, date, timedelta
from pyemvue import PyEmVue
from pyemvue.enums import Scale
import configparser
from typing import Optional, Tuple, Dict, Any, List

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
    print("Attempting to log in to Emporia Energy API...")
    try:
        vue = PyEmVue()
        vue.login(username=email, password=password)
        print("Successfully logged in to Emporia.")
        return vue
    except Exception as e:
        print(f"Error logging in to Emporia: {e}")
        print("Please check your credentials and network connection.")
        return None

def get_emporia_devices(vue: PyEmVue) -> Optional[Dict[int, Any]]:
    """
    Fetches all devices and consolidates channels for devices with multiple channel sets.

    Args:
        vue (PyEmVue): The logged-in PyEmVue object.

    Returns:
        Optional[Dict[int, Any]]: A dictionary of device information, or None if fetching fails.
    """
    print("Fetching devices...")
    try:
        devices = vue.get_devices()
        device_info: Dict[int, Any] = {}
        for device in devices:
            if device.device_gid not in device_info:
                device_info[device.device_gid] = device
            else:
                device_info[device.device_gid].channels.extend(device.channels)
        print(f"Found {len(device_info)} devices.")
        return device_info
    except Exception as e:
        print(f"Error getting devices: {e}")
        return None

def fetch_channel_data(vue: PyEmVue, channel: Any, start_date: date, end_date: date) -> Optional[pd.DataFrame]:
    """
    Fetches hourly usage data for a single channel.

    Args:
        vue (PyEmVue): The logged-in PyEmVue object.
        channel (Any): The channel object to fetch data for.
        start_date (date): The start date for the data fetch.
        end_date (date): The end date for the data fetch.

    Returns:
        Optional[pd.DataFrame]: A DataFrame with the channel's usage data, or None.
    """
    if ',' in str(channel.channel_num):
        print(f"  Skipping pseudo-channel: {channel.name} ({channel.channel_num})")
        return None

    print(f"  Fetching data for channel: {channel.name} ({channel.channel_num})")
    try:
        usage_data, start_time = vue.get_chart_usage(
            channel=channel,
            start=datetime.combine(start_date, datetime.min.time()),
            end=datetime.combine(end_date, datetime.max.time()).replace(second=0, microsecond=0),
            scale=Scale.HOUR.value
        )

        if usage_data:
            timestamps = pd.to_datetime(start_time) + pd.to_timedelta(range(len(usage_data)), unit='h')
            return pd.DataFrame({
                'instant': timestamps,
                f'channel_{channel.channel_num}_usage': usage_data
            })
        else:
            print(f"  No data returned for channel {channel.name}")
            return None
    except Exception as e:
        print(f"  Error fetching data for channel {channel.name}: {e}")
        return None

def fetch_device_data(vue: PyEmVue, device: Any, start_date: date, end_date: date) -> Optional[pd.DataFrame]:
    """
    Fetches data for all channels in a device and merges them into a single DataFrame.

    Args:
        vue (PyEmVue): The logged-in PyEmVue object.
        device (Any): The device to fetch data for.
        start_date (date): The start date for the data fetch.
        end_date (date): The end date for the data fetch.

    Returns:
        Optional[pd.DataFrame]: A merged DataFrame of all channel data for the device.
    """
    print(f"Fetching data for device: {device.device_name} (gid: {device.device_gid})")
    channel_dfs = [fetch_channel_data(vue, ch, start_date, end_date) for ch in device.channels]
    channel_dfs = [df for df in channel_dfs if df is not None]

    if not channel_dfs:
        print(f"No data returned for any channels in {device.device_name}")
        return None

    # Merge all channel DataFrames for the device
    df = channel_dfs[0]
    for i in range(1, len(channel_dfs)):
        df = pd.merge(df, channel_dfs[i], on='instant', how='outer')

    df['device_gid'] = device.device_gid
    df['device_name'] = device.device_name
    return df

def save_all_data(df: pd.DataFrame, start_date: date, output_folder: str):
    """
    Saves the combined DataFrame to a single CSV file.

    Args:
        df (pd.DataFrame): The combined DataFrame to save.
        start_date (date): The start date of the data period (for filename).
        output_folder (str): The folder to save the CSV in.
    """
    if not os.path.exists(output_folder):
        os.makedirs(output_folder)
        print(f"Created output directory: {output_folder}")

    filename = f"{output_folder}/emporia_data_{start_date.strftime('%Y-%m')}.csv"
    df.to_csv(filename, index=False)
    print(f"Successfully saved all device data to {filename}")

def download_emporia_data(email: str, password: str, output_folder: str = 'emporia_data'):
    """
    Orchestrates the download of Emporia data into a single CSV file.

    Args:
        email (str): The user's email address.
        password (str): The user's password.
        output_folder (str, optional): The folder to save data. Defaults to 'emporia_data'.
    """
    vue = authenticate(email, password)
    if not vue:
        return

    device_info = get_emporia_devices(vue)
    if not device_info:
        return

    start_date, end_date = get_last_month_dates()
    print(f"Downloading data from {start_date} to {end_date}.")

    all_device_dfs: List[pd.DataFrame] = []
    for gid, device in device_info.items():
        device_df = fetch_device_data(vue, device, start_date, end_date)
        if device_df is not None:
            all_device_dfs.append(device_df)

    if all_device_dfs:
        combined_df = pd.concat(all_device_dfs, ignore_index=True)
        save_all_data(combined_df, start_date, output_folder)
    else:
        print("No data was downloaded for any device.")

def load_credentials(config_file: str = 'config.cfg') -> Tuple[Optional[str], Optional[str]]:
    """
    Loads Emporia credentials from a configuration file.

    Args:
        config_file (str, optional): Path to the config file. Defaults to 'config.cfg'.

    Returns:
        Tuple[Optional[str], Optional[str]]: A tuple of (email, password), or (None, None).
    """
    config = configparser.ConfigParser()
    if not os.path.exists(config_file):
        print(f"Error: Configuration file '{config_file}' not found.")
        print("Create it with:\n[emporia]\nusername = your_email@example.com\npassword = your_password")
        return None, None
        
    config.read(config_file)

    try:
        email = config['emporia']['username']
        password = config['emporia']['password']
    except (KeyError, configparser.NoSectionError):
        print("Error: 'emporia' section not found in config.cfg.")
        print("Ensure the config file has:\n[emporia]\nusername = your_email@example.com\npassword = your_password")
        return None, None

    if email == "your_email@example.com" or password == "your_password":
        print("Please update config.cfg with your Emporia credentials.")
        return None, None

    return email, password

def main():
    """Main function to run the data download process."""
    email, password = load_credentials()
    if email and password:
        download_emporia_data(email, password)

if __name__ == '__main__':
    main()
