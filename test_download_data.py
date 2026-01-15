import pytest
from unittest.mock import MagicMock, call
import pandas as pd
from datetime import datetime, date
import download_data

def test_download_emporia_data_success(monkeypatch):
    """
    Test the successful execution of download_emporia_data, ensuring data is
    concatenated and saved to a single file.
    """
    # --- Mock Authentication and Device Fetching ---
    mock_auth = MagicMock()
    mock_vue_instance = MagicMock()
    mock_auth.return_value = mock_vue_instance
    monkeypatch.setattr(download_data, 'authenticate', mock_auth)

    mock_device1 = MagicMock()
    mock_device1.device_gid = 12345
    mock_device1.device_name = 'Test Device 1'
    mock_device2 = MagicMock()
    mock_device2.device_gid = 67890
    mock_device2.device_name = 'Test Device 2'
    mock_device_info = {12345: mock_device1, 67890: mock_device2}
    mock_get_devices = MagicMock(return_value=mock_device_info)
    monkeypatch.setattr(download_data, 'get_emporia_devices', mock_get_devices)

    # --- Mock Data Fetching ---
    df1 = pd.DataFrame({'device_gid': [12345]})
    df2 = pd.DataFrame({'device_gid': [67890]})
    mock_fetch_data = MagicMock(side_effect=[df1, df2])
    monkeypatch.setattr(download_data, 'fetch_device_data', mock_fetch_data)

    # --- Mock Saving ---
    mock_save = MagicMock()
    monkeypatch.setattr(download_data, 'save_all_data', mock_save)

    # --- Run the function ---
    download_data.download_emporia_data('test@example.com', 'password123')

    # --- Assertions ---
    # Assert that authentication was called
    mock_auth.assert_called_once_with('test@example.com', 'password123')

    # Assert that devices were fetched
    mock_get_devices.assert_called_once_with(mock_vue_instance)

    # Assert that fetch_device_data was called for each device
    assert mock_fetch_data.call_count == 2

    # Assert that the save function was called with the concatenated data
    mock_save.assert_called_once()
    saved_df = mock_save.call_args[0][0]
    assert len(saved_df) == 2
    # Use a set to check for presence of gids regardless of order
    assert set(saved_df['device_gid']) == {12345, 67890}