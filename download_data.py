
import os
import pandas as pd
from datetime import datetime, date, timedelta
from pyemvue import PyEmVue
from pyemvue.enums import Scale
import configparser

def get_last_month_dates():
    """Helper function to get the start and end dates for the previous calendar month."""
    today = date.today()
    first_day_of_current_month = today.replace(day=1)
    last_day_of_last_month = first_day_of_current_month - timedelta(days=1)
    first_day_of_last_month = last_day_of_last_month.replace(day=1)
    return first_day_of_last_month, last_day_of_last_month

def download_emporia_data(email, password, output_folder='emporia_data'):
    """
    Connects to Emporia API, fetches all devices, and downloads 1-minute data
    for the previous calendar month for each device, saving it to a CSV file.
    """
    print("Attempting to log in to Emporia Energy API...")
    try:
        vue = PyEmVue()
        vue.login(username=email, password=password)
        print("Successfully logged in to Emporia.")
    except Exception as e:
        print(f"Error logging in to Emporia: {e}")
        print("Please check your credentials in config.cfg and ensure your network allows connection to Emporia's API.")
        return

    print("Fetching devices...")
    try:
        devices = vue.get_devices()
        device_gids = []
        device_info = {}
        for device in devices:
            if not device.device_gid in device_gids:
                device_gids.append(device.device_gid)
                device_info[device.device_gid] = device
            else:
                # This logic is from the pyemvue README to handle devices with multiple channel sets.
                device_info[device.device_gid].channels += device.channels
        print(f"Found {len(device_info)} devices.")
    except Exception as e:
        print(f"Error getting devices: {e}")
        return

    if not os.path.exists(output_folder):
        os.makedirs(output_folder)
        print(f"Created output directory: {output_folder}")

    start_date, end_date = get_last_month_dates()
    print(f"Downloading data from {start_date} to {end_date}.")

    for gid, device in device_info.items():
        device_name = device.device_name.replace(' ', '_').replace('/', '_').lower()
        print(f"Fetching data for device: {device.device_name} (gid: {gid})")

        try:
            channel_dfs = []
            for channel in device.channels:
                # Skip pseudo-channels with comma-separated channel numbers
                if ',' in str(channel.channel_num):
                    print(f"  Skipping pseudo-channel: {channel.name} ({channel.channel_num})")
                    continue

                print(f"  Fetching data for channel: {channel.name} ({channel.channel_num})")
                usage_data, start_time = vue.get_chart_usage(
                    channel=channel,
                    start=datetime.combine(start_date, datetime.min.time()),
                    end=datetime.combine(end_date, datetime.max.time()).replace(second=0, microsecond=0), # Removed microseconds
                    scale=Scale.HOUR.value # Changed to hourly scale
                )

                if usage_data:
                    # Create a DataFrame for the current channel
                    # Timestamps should reflect the new hourly scale
                    timestamps = pd.to_datetime(start_time) + pd.to_timedelta(range(len(usage_data)), unit='h')
                    df_channel = pd.DataFrame({
                        'instant': timestamps,
                        f'channel_{channel.channel_num}_usage': usage_data
                    })
                    channel_dfs.append(df_channel)
                else:
                    print(f"  No data returned for channel {channel.name}")

            if channel_dfs:
                # Merge all channel DataFrames for the device
                df = channel_dfs[0]
                for i in range(1, len(channel_dfs)):
                    df = pd.merge(df, channel_dfs[i], on='instant', how='outer')

                # Add device gid and name for context
                df['device_gid'] = device.device_gid
                df['device_name'] = device.device_name
                
                filename = f"{output_folder}/{device_name}_{start_date.strftime('%Y-%m')}.csv"
                df.to_csv(filename, index=False)
                print(f"Successfully saved data for {device.device_name} to {filename}")
            else:
                print(f"No data returned for any channels in {device.device_name}")

        except Exception as e:
            print(f"Error getting usage for {device.device_name}: {e}")

if __name__ == '__main__':
    config = configparser.ConfigParser()
    config.read('config.cfg')

    try:
        email = config['emporia']['username']
        password = config['emporia']['password']
    except (KeyError, configparser.NoSectionError):
        print("Error: 'emporia' section not found in config.cfg.")
        print("Please ensure your config.cfg file has the following format:")
        print("\n[emporia]\nusername = your_email@example.com\npassword = your_password\n")
        exit(1)


    if email == "your_email@example.com" or password == "your_password":
        print("Please update the config.cfg with your Emporia credentials.")
    else:
        download_emporia_data(email, password)
