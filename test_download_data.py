import pytest
from unittest.mock import MagicMock, call
import pandas as pd
from datetime import datetime, date
import download_data
import os
from pyemvue.enums import Scale


@pytest.fixture
def mock_config_file(tmp_path):
    """Fixture to create a temporary config file for testing."""
    def _create_config(content):
        config_path = tmp_path / "config.yaml"
        with open(config_path, "w") as f:
            f.write(content)
        return config_path
    return _create_config

class TestLoadConfig:
    def test_load_full_config(self, mock_config_file):
        """Test loading a complete config file."""
        config_content = """
        credentials:
          username: user@example.com
          password: pass
        data:
          start_date: 2023-01-01
          end_date: 2023-01-31
          granularity: HOURS
        aggregate_devices:
          - Device 1
          - Device 2
        """
        config_path = mock_config_file(config_content)
        settings = download_data.load_config(config_file=str(config_path))
        assert settings['email'] == 'user@example.com'
        assert settings['password'] == 'pass'
        assert settings['start_date'] == '2023-01-01'
        assert settings['end_date'] == '2023-01-31'
        assert settings['granularity'] == 'HOURS'
        assert settings['aggregate_devices'] == ['Device 1', 'Device 2']

    def test_load_default_config(self, mock_config_file):
        """Test loading a config with empty dates and no granularity."""
        config_content = """
        credentials:
          username: user@example.com
          password: pass
        data:
          start_date:
          end_date:
        """
        config_path = mock_config_file(config_content)
        settings = download_data.load_config(config_file=str(config_path))
        assert settings['start_date'] is None
        assert settings['end_date'] is None
        assert settings['granularity'] == 'DAY'
        assert settings['aggregate_devices'] == []

    def test_load_missing_credentials(self, mock_config_file):
        """Test that loading a config with missing credentials returns None."""
        config_content = """
        credentials:
          # username: user@example.com
          # password: pass
        data:
          start_date: 2023-01-01
          end_date: 2023-01-31
        """
        config_path = mock_config_file(config_content)
        settings = download_data.load_config(config_file=str(config_path))
        assert settings is None

    def test_load_missing_config_file(self):
        """Test that a missing config file returns None."""
        settings = download_data.load_config(config_file="non_existent_file.yaml")
        assert settings is None

class TestDownloadEmporiaData:
    @pytest.fixture(autouse=True)
    def mocks(self, monkeypatch):
        """Set up mocks for all tests in this class."""
        self.mock_auth = MagicMock()
        self.mock_vue_instance = MagicMock()
        self.mock_auth.return_value = self.mock_vue_instance
        monkeypatch.setattr(download_data, 'authenticate', self.mock_auth)

        self.mock_device1 = MagicMock()
        self.mock_device1.device_gid = 123
        self.mock_device1.device_name = "Test Device"
        self.mock_channel1 = MagicMock()
        self.mock_channel1.channel_num = '1'
        self.mock_channel1.name = "Channel 1"
        self.mock_device1.channels = [self.mock_channel1]
        
        self.mock_device_info = {123: self.mock_device1}
        self.mock_get_device_info = MagicMock(return_value=self.mock_device_info)
        monkeypatch.setattr(download_data, 'get_emporia_device_info', self.mock_get_device_info)

        self.mock_fetch_device_data = MagicMock(return_value=pd.DataFrame({
            'instant': [datetime(2023, 1, 1)], 
            'channel_1_cost_usd': [0.1]
        }))
        monkeypatch.setattr(download_data, 'fetch_device_data', self.mock_fetch_device_data)

        self.mock_fetch_channel_data = MagicMock(return_value=pd.DataFrame({
            'instant': [datetime(2023, 1, 1)], 
            'channel_1_cost_usd': [0.1]
        }))
        monkeypatch.setattr(download_data, 'fetch_channel_data', self.mock_fetch_channel_data)

        self.mock_save = MagicMock()
        monkeypatch.setattr(download_data, 'save_data', self.mock_save)

        self.mock_get_last_month = MagicMock(return_value=(date(2023, 1, 1), date(2023, 1, 31)))
        monkeypatch.setattr(download_data, 'get_last_month_dates', self.mock_get_last_month)

    def test_with_provided_dates_per_channel(self):
        """Test that provided start and end dates are used with per-channel default."""
        download_data.download_emporia_data(
            'email', 'pass', '2023-02-01', '2023-02-28', 'HOUR', []
        )
        self.mock_get_last_month.assert_not_called()
        expected_start = date(2023, 2, 1)
        expected_end = date(2023, 2, 28)
        self.mock_fetch_channel_data.assert_called_once_with(
            self.mock_vue_instance, self.mock_channel1, expected_start, expected_end, 'HOUR'
        )

    def test_with_aggregate_devices(self):
        """Test that aggregation is used for devices in aggregate_devices list."""
        download_data.download_emporia_data(
            'email', 'pass', '2023-01-01', '2023-01-31', 'DAY', ["Test Device"]
        )
        expected_start = date(2023, 1, 1)
        expected_end = date(2023, 1, 31)
        self.mock_fetch_device_data.assert_called_once_with(
            self.mock_vue_instance, self.mock_device1, expected_start, expected_end, 'DAY'
        )
        self.mock_fetch_channel_data.assert_not_called()

        saved_df = self.mock_save.call_args[0][0]
        assert 'Test Device_cost_usd' in saved_df.columns
        assert saved_df['Test Device_cost_usd'].iloc[0] == 0.1

    def test_per_channel_output(self):
        """Test that per-channel output uses channel names."""
        download_data.download_emporia_data(
            'email', 'pass', '2023-01-01', '2023-01-31', 'DAY', []
        )
        saved_df = self.mock_save.call_args[0][0]
        assert 'Channel 1_cost_usd' in saved_df.columns
        assert saved_df['Channel 1_cost_usd'].iloc[0] == 0.1

class TestFetchChannelData:
    @pytest.mark.parametrize("granularity, expected_scale, expected_unit", [
        ("MINUTE", Scale.MINUTE.value, 'm'),
        ("HOUR", Scale.HOUR.value, 'h'),
        ("DAY", Scale.DAY.value, 'd'),
    ])
    def test_granularity_mapping(self, monkeypatch, granularity, expected_scale, expected_unit):
        """Test that granularity is correctly mapped to scale and time unit."""
        mock_vue = MagicMock()
        mock_channel = MagicMock()
        mock_channel.channel_num = '1'
        mock_channel.name = "Channel 1"
        start_date = date(2023, 1, 1)
        end_date = date(2023, 1, 31)

        # Mock get_chart_usage to return some data
        mock_vue.get_chart_usage.return_value = ([0.15], datetime(2023, 1, 1))
        
        df = download_data.fetch_channel_data(mock_vue, mock_channel, start_date, end_date, granularity)

        mock_vue.get_chart_usage.assert_called_once()
        # Check that the scale argument is correct
        assert mock_vue.get_chart_usage.call_args[1]['scale'] == expected_scale
        # Check that the returned dataframe is not empty
        assert df is not None
        assert not df.empty
        assert f'channel_1_cost_usd' in df.columns
        assert df[f'channel_1_cost_usd'][0] == 0.15

    def test_unsupported_granularity(self, monkeypatch):
        """Test that unsupported granularity returns None."""
        mock_vue = MagicMock()
        mock_channel = MagicMock()
        mock_channel.channel_num = '1'
        mock_channel.name = "Channel 1"
        start_date = date(2023, 1, 1)
        end_date = date(2023, 1, 31)
        
        df = download_data.fetch_channel_data(mock_vue, mock_channel, start_date, end_date, "WEEKS")

        assert df is None
        mock_vue.get_chart_usage.assert_not_called()

def test_csv_output_header_aggregated(tmp_path, monkeypatch):
    """Test that the output CSV file has the correct header when aggregated."""
    mock_auth = MagicMock()
    mock_vue_instance = MagicMock()
    mock_auth.return_value = mock_vue_instance
    monkeypatch.setattr(download_data, 'authenticate', mock_auth)

    mock_device = MagicMock()
    mock_device.device_gid = 123
    mock_device.device_name = "Test Device"
    mock_channel = MagicMock()
    mock_channel.channel_num = '1'
    mock_channel.name = "Channel 1"
    mock_device.channels = [mock_channel]
    mock_get_device_info = MagicMock(return_value={123: mock_device})
    monkeypatch.setattr(download_data, 'get_emporia_device_info', mock_get_device_info)

    mock_vue_instance.get_chart_usage.return_value = ([0.15, 0.30], datetime(2023, 1, 1))
    
    # Mock get_last_month_dates to return a consistent date range
    mock_get_last_month = MagicMock(return_value=(date(2023, 1, 1), date(2023, 1, 31)))
    monkeypatch.setattr(download_data, 'get_last_month_dates', mock_get_last_month)

    output_folder = tmp_path / "emporia_data"
    download_data.download_emporia_data('test@example.com', 'password', None, None, 'DAY', ["Test Device"], output_folder=str(output_folder))

    csv_file_path = output_folder / "emporia_data_2023-01.csv"
    assert csv_file_path.exists()

    df = pd.read_csv(csv_file_path)
    expected_headers = ['instant', 'Test Device_cost_usd']
    assert list(df.columns) == expected_headers

def test_csv_output_header_per_channel(tmp_path, monkeypatch):
    """Test that the output CSV file has the correct header when per-channel."""
    mock_auth = MagicMock()
    mock_vue_instance = MagicMock()
    mock_auth.return_value = mock_vue_instance
    monkeypatch.setattr(download_data, 'authenticate', mock_auth)

    mock_device = MagicMock()
    mock_device.device_gid = 123
    mock_device.device_name = "Test Device"
    mock_channel = MagicMock()
    mock_channel.channel_num = '1'
    mock_channel.name = "Channel 1"
    mock_device.channels = [mock_channel]
    mock_get_device_info = MagicMock(return_value={123: mock_device})
    monkeypatch.setattr(download_data, 'get_emporia_device_info', mock_get_device_info)

    mock_vue_instance.get_chart_usage.return_value = ([0.15, 0.30], datetime(2023, 1, 1))
    
    # Mock get_last_month_dates to return a consistent date range
    mock_get_last_month = MagicMock(return_value=(date(2023, 1, 1), date(2023, 1, 31)))
    monkeypatch.setattr(download_data, 'get_last_month_dates', mock_get_last_month)

    output_folder = tmp_path / "emporia_data"
    download_data.download_emporia_data('test@example.com', 'password', None, None, 'DAY', [], output_folder=str(output_folder))

    csv_file_path = output_folder / "emporia_data_2023-01.csv"
    assert csv_file_path.exists()

    df = pd.read_csv(csv_file_path)
    expected_headers = ['instant', 'Channel 1_cost_usd']
    assert list(df.columns) == expected_headers
