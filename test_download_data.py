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
            'channel_1_cost_usd': [0.1],
            'channel_1_usage_kwh': [0.5]
        }))
        monkeypatch.setattr(download_data, 'fetch_device_data', self.mock_fetch_device_data)

        self.mock_fetch_channel_data = MagicMock(return_value=pd.DataFrame({
            'instant': [datetime(2023, 1, 1)], 
            'channel_1_cost_usd': [0.1],
            'channel_1_usage_kwh': [0.5]
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
        assert 'period' in saved_df.columns
        assert 'Test Device (USD)' in saved_df.columns
        assert 'Test Device (kWh)' in saved_df.columns
        assert len(saved_df) == 1
        assert saved_df['Test Device (USD)'].iloc[0] == 0.1
        assert saved_df['Test Device (kWh)'].iloc[0] == 0.5

    def test_per_channel_output(self):
        """Test that per-channel output uses channel names."""
        download_data.download_emporia_data(
            'email', 'pass', '2023-01-01', '2023-01-31', 'DAY', []
        )
        saved_df = self.mock_save.call_args[0][0]
        assert 'period' in saved_df.columns
        assert 'Channel 1 (USD)' in saved_df.columns
        assert 'Channel 1 (kWh)' in saved_df.columns
        assert len(saved_df) == 1
        assert saved_df['Channel 1 (USD)'].iloc[0] == 0.1
        assert saved_df['Channel 1 (kWh)'].iloc[0] == 0.5

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
        mock_vue.get_chart_usage.side_effect = [
            ([0.15], datetime(2023, 1, 1)), # USD
            ([0.75], datetime(2023, 1, 1))  # kWh
        ]
        
        df = download_data.fetch_channel_data(mock_vue, mock_channel, start_date, end_date, granularity)

        assert mock_vue.get_chart_usage.call_count == 2
        # Check that the scale argument is correct
        assert mock_vue.get_chart_usage.call_args_list[0][1]['scale'] == expected_scale
        assert mock_vue.get_chart_usage.call_args_list[1][1]['scale'] == expected_scale
        # Check that the returned dataframe is not empty
        assert df is not None
        assert not df.empty
        assert f'channel_1_cost_usd' in df.columns
        assert f'channel_1_usage_kwh' in df.columns
        assert df[f'channel_1_cost_usd'][0] == 0.15
        assert df[f'channel_1_usage_kwh'][0] == 0.75

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

    def test_missing_data_no_collapse(self, monkeypatch):
        """Test that missing data (None values) doesn't cause the output to collapse."""
        mock_vue = MagicMock()
        mock_channel = MagicMock()
        mock_channel.channel_num = '1'
        mock_channel.name = "Test Channel"
        start_date = date(2026, 2, 1)
        end_date = date(2026, 2, 28)
        
        usage_usd = [0.1, None, 0.2]
        usage_kwh = [0.5, None, 1.0]
        start_time = datetime(2026, 2, 1)
        mock_vue.get_chart_usage.side_effect = [
            (usage_usd, start_time),
            (usage_kwh, start_time)
        ]
        
        df = download_data.fetch_channel_data(mock_vue, mock_channel, start_date, end_date, "DAY")
        
        assert df is not None
        assert len(df) == 3
        assert df['instant'].iloc[0] == pd.Timestamp('2026-02-01')
        assert df['instant'].iloc[1] == pd.Timestamp('2026-02-02')
        assert df['instant'].iloc[2] == pd.Timestamp('2026-02-03')
        assert df['channel_1_cost_usd'].iloc[0] == 0.1
        assert pd.isna(df['channel_1_cost_usd'].iloc[1])
        assert df['channel_1_cost_usd'].iloc[2] == 0.2
        assert df['channel_1_usage_kwh'].iloc[0] == 0.5
        assert pd.isna(df['channel_1_usage_kwh'].iloc[1])
        assert df['channel_1_usage_kwh'].iloc[2] == 1.0

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

    mock_vue_instance.get_chart_usage.side_effect = [
        ([0.15, 0.30], datetime(2023, 1, 1)), # USD
        ([0.75, 1.50], datetime(2023, 1, 1))  # kWh
    ]
    
    # Mock get_last_month_dates to return a consistent date range
    mock_get_last_month = MagicMock(return_value=(date(2023, 1, 1), date(2023, 1, 31)))
    monkeypatch.setattr(download_data, 'get_last_month_dates', mock_get_last_month)

    output_folder = tmp_path / "emporia_data"
    download_data.download_emporia_data('test@example.com', 'password', None, None, 'DAY', ["Test Device"], output_folder=str(output_folder))

    csv_file_path = output_folder / "emporia_data_2023-01.csv"
    assert csv_file_path.exists()

    df = pd.read_csv(csv_file_path)
    expected_headers = ['period', 'Test Device (USD)', 'Test Device (kWh)']
    assert list(df.columns) == expected_headers
    assert len(df) == 1
    assert df['period'].iloc[0] == "2023-01-01 to 2023-01-31"
    assert df['Test Device (USD)'].iloc[0] == pytest.approx(0.45)
    assert df['Test Device (kWh)'].iloc[0] == pytest.approx(2.25)

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

    mock_vue_instance.get_chart_usage.side_effect = [
        ([0.15, 0.30], datetime(2023, 1, 1)), # USD
        ([0.75, 1.50], datetime(2023, 1, 1))  # kWh
    ]
    
    # Mock get_last_month_dates to return a consistent date range
    mock_get_last_month = MagicMock(return_value=(date(2023, 1, 1), date(2023, 1, 31)))
    monkeypatch.setattr(download_data, 'get_last_month_dates', mock_get_last_month)

    output_folder = tmp_path / "emporia_data"
    download_data.download_emporia_data('test@example.com', 'password', None, None, 'DAY', [], output_folder=str(output_folder))

    csv_file_path = output_folder / "emporia_data_2023-01.csv"
    assert csv_file_path.exists()

    df = pd.read_csv(csv_file_path)
    expected_headers = ['period', 'Channel 1 (USD)', 'Channel 1 (kWh)']
    assert list(df.columns) == expected_headers
    assert len(df) == 1
    assert df['period'].iloc[0] == "2023-01-01 to 2023-01-31"
    assert df['Channel 1 (USD)'].iloc[0] == pytest.approx(0.45)
    assert df['Channel 1 (kWh)'].iloc[0] == pytest.approx(2.25)
