import pytest
from unittest.mock import MagicMock, call
import pandas as pd
from datetime import datetime, date, timezone
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

class TestGetDefaultDates:
    def test_get_default_dates_standard(self, monkeypatch):
        """Test logic returns the full previous month (e.g., March 1 to March 31 if today is April 10)."""
        mock_date = MagicMock()
        mock_date.today.return_value = date(2026, 4, 10)
        monkeypatch.setattr(download_data, 'date', mock_date)
        
        s_date, e_date = download_data.get_default_dates()
        assert s_date == date(2026, 3, 1)
        assert e_date == date(2026, 3, 31)

    def test_get_default_dates_year_boundary(self, monkeypatch):
        """Test logic works across year boundaries (e.g., Dec 1 to Dec 31 if today is Jan 5)."""
        mock_date = MagicMock()
        mock_date.today.return_value = date(2026, 1, 5)
        monkeypatch.setattr(download_data, 'date', mock_date)
        
        s_date, e_date = download_data.get_default_dates()
        assert s_date == date(2025, 12, 1)
        assert e_date == date(2025, 12, 31)

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
        """
        config_path = mock_config_file(config_content)
        settings = download_data.load_config(config_file=str(config_path))
        assert settings['email'] == 'user@example.com'

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
        
        self.mock_device_info = {123: self.mock_device1}
        self.mock_get_device_info = MagicMock(return_value=self.mock_device_info)
        monkeypatch.setattr(download_data, 'get_emporia_device_info', self.mock_get_device_info)

        self.mock_fetch_device_data = MagicMock(return_value=[pd.DataFrame({
            'instant': [datetime(2023, 1, 1)], 
            'Test Device: Channel 1 (USD)': [0.1],
            'Test Device: Channel 1 (kWh)': [0.5]
        })])
        monkeypatch.setattr(download_data, 'fetch_device_data', self.mock_fetch_device_data)

        self.mock_save = MagicMock()
        monkeypatch.setattr(download_data, 'save_data', self.mock_save)

        self.mock_get_default_dates = MagicMock(return_value=(date(2023, 1, 1), date(2023, 1, 31)))
        monkeypatch.setattr(download_data, 'get_default_dates', self.mock_get_default_dates)

    def test_basic_download_logic(self):
        """Test that data is fetched and saved correctly."""
        # Device 1 has one channel
        self.mock_fetch_device_data.return_value = [
            pd.DataFrame({'instant': [datetime(2023, 1, 1)], 'Test Device: Ch1 (USD)': [0.1], 'Test Device: Ch1 (kWh)': [0.5]})
        ]
        
        download_data.download_emporia_data(
            email='email', password='pass', start_date_str='2023-01-01', end_date_str='2023-01-31', 
            granularity='DAY'
        )
        
        saved_df = self.mock_save.call_args[0][0]
        assert 'Test Device: Ch1 (USD)' in saved_df.columns
        assert saved_df['Test Device: Ch1 (USD)'].iloc[0] == pytest.approx(0.1)

    def test_output_totals_flag(self):
        """Test that --output_totals includes the raw [Total] columns."""
        self.mock_fetch_device_data.return_value = [
            pd.DataFrame({'instant': [datetime(2023, 1, 1)], 'Test Device: Ch1 (USD)': [0.1], 'Test Device: Ch1 (kWh)': [0.5]}),
            pd.DataFrame({'instant': [datetime(2023, 1, 1)], 'Test Device: [Total] (USD)': [0.1], 'Test Device: [Total] (kWh)': [0.5]})
        ]
        
        # Test without flag (default)
        download_data.download_emporia_data(
            email='email', password='pass', start_date_str='2023-01-01', end_date_str='2023-01-31', 
            granularity='DAY', output_totals=False
        )
        saved_df = self.mock_save.call_args[0][0]
        assert 'Test Device: [Total] (USD)' not in saved_df.columns
        
        # Test with flag
        download_data.download_emporia_data(
            email='email', password='pass', start_date_str='2023-01-01', end_date_str='2023-01-31', 
            granularity='DAY', output_totals=True
        )
        saved_df = self.mock_save.call_args[0][0]
        assert 'Test Device: [Total] (USD)' in saved_df.columns

class TestFetchDeviceData:
    def test_fetch_device_data_naming_and_balance(self, monkeypatch):
        """Test that fetch_device_data uses correct naming rules and computes balance including expansion channels."""
        mock_vue = MagicMock()
        mock_device = MagicMock()
        mock_device.device_name = "MyVue"
        
        ch1 = MagicMock(); ch1.channel_num = '1'; ch1.name = "Mains"
        ch2 = MagicMock(); ch2.channel_num = '2'; ch2.name = None
        ch4 = MagicMock(); ch4.channel_num = '4'; ch4.name = "Kitchen"
        ch5 = MagicMock(); ch5.channel_num = '5'; ch5.name = "Empty"
        ch_total = MagicMock(); ch_total.channel_num = '1,2,3'; ch_total.name = None
        mock_device.channels = [ch1, ch2, ch4, ch_total]
        
        def mock_fetch_ch(vue, channel, start_date, end_date, granularity, target_name=None):
            if target_name == "TOTAL":
                return pd.DataFrame({
                    'instant': [datetime(2023,1,1)], 
                    'TOTAL (USD)': [1.0], 
                    'TOTAL (kWh)': [10.0]
                })
            elif target_name == "MyVue: Mains":
                return pd.DataFrame({
                    'instant': [datetime(2023,1,1)], 
                    'MyVue: Mains (USD)': [0.4], 
                    'MyVue: Mains (kWh)': [4.0]
                })
            elif target_name == "MyVue: Kitchen":
                return pd.DataFrame({
                    'instant': [datetime(2023,1,1)], 
                    'MyVue: Kitchen (USD)': [0.2], 
                    'MyVue: Kitchen (kWh)': [2.0]
                })
            return None

        monkeypatch.setattr(download_data, 'fetch_channel_data', mock_fetch_ch)
        
        dfs = download_data.fetch_device_data(mock_vue, mock_device, date(2023,1,1), date(2023,1,2), "DAY")
        
        # Should return 4 DataFrames: Mains, Kitchen, Balance, and [Total]
        # (Unnamed Channel 2 is skipped)
        assert len(dfs) == 4
        
        # Check names
        all_cols = []
        for df in dfs:
            all_cols.extend([c for c in df.columns if c != 'instant'])
        
        assert "MyVue: Mains (USD)" in all_cols
        assert "MyVue: Kitchen (USD)" in all_cols
        assert "MyVue: Balance (USD)" in all_cols
        assert "MyVue: [Total] (USD)" in all_cols
        
        # Check balance calculation: 1.0 - (0.4 + 0.2) = 0.4
        balance_df = next(df for df in dfs if "MyVue: Balance (USD)" in df.columns)
        assert balance_df["MyVue: Balance (USD)"].iloc[0] == pytest.approx(0.4)
        assert balance_df["MyVue: Balance (kWh)"].iloc[0] == pytest.approx(4.0)

    def test_fetch_device_data_no_balance_if_zero(self, monkeypatch):
        """Test that balance channel is omitted if it is effectively zero."""
        mock_vue = MagicMock()
        mock_device = MagicMock()
        mock_device.device_name = "MyVue"
        
        ch1 = MagicMock(); ch1.channel_num = '1'; ch1.name = "Mains"
        ch_total = MagicMock(); ch_total.channel_num = '1,2,3'; ch_total.name = None
        mock_device.channels = [ch1, ch_total]
        
        def mock_fetch_ch(vue, channel, start_date, end_date, granularity, target_name=None):
            if target_name == "TOTAL":
                return pd.DataFrame({
                    'instant': [datetime(2023,1,1)], 
                    'TOTAL (USD)': [1.0], 
                    'TOTAL (kWh)': [10.0]
                })
            elif target_name == "MyVue: Mains":
                return pd.DataFrame({
                    'instant': [datetime(2023,1,1)], 
                    'MyVue: Mains (USD)': [1.0], 
                    'MyVue: Mains (kWh)': [10.0]
                })
            return None

        monkeypatch.setattr(download_data, 'fetch_channel_data', mock_fetch_ch)
        
        dfs = download_data.fetch_device_data(mock_vue, mock_device, date(2023,1,1), date(2023,1,2), "DAY")
        
        # Should return 2 DataFrames: Mains and [Total] (Balance is 0)
        assert len(dfs) == 2
        assert "MyVue: Mains (USD)" in [c for df in dfs for c in df.columns]
        assert "MyVue: [Total] (USD)" in [c for df in dfs for c in df.columns]
        assert not any("balance" in c.lower() for df in dfs for c in df.columns)

class TestGetEmporiaDeviceInfo:
    def test_get_emporia_device_info_populates_properties(self):
        """Test that get_emporia_device_info calls populate_device_properties."""
        mock_vue = MagicMock()
        mock_device = MagicMock()
        mock_device.device_gid = 123
        mock_device.channels = []
        mock_vue.get_devices.return_value = [mock_device]
        
        info = download_data.get_emporia_device_info(mock_vue)
        
        mock_vue.get_devices.assert_called_once()
        mock_vue.populate_device_properties.assert_called_once_with(mock_device)
        assert 123 in info

class TestSaveToGoogleSheet:
    @pytest.fixture(autouse=True)
    def mocks(self, monkeypatch):
        self.mock_exists = MagicMock(return_value=True)
        monkeypatch.setattr(os.path, 'exists', self.mock_exists)
        
        self.mock_creds = MagicMock()
        monkeypatch.setattr(download_data.Credentials, 'from_service_account_file', MagicMock(return_value=self.mock_creds))
        
        self.mock_client = MagicMock()
        monkeypatch.setattr(download_data.gspread, 'authorize', MagicMock(return_value=self.mock_client))
        
        self.mock_spreadsheet = MagicMock()
        self.mock_client.open_by_url.return_value = self.mock_spreadsheet
        
        self.mock_worksheet = MagicMock()
        self.mock_spreadsheet.get_worksheet.return_value = self.mock_worksheet
        self.mock_spreadsheet.worksheets.return_value = [self.mock_worksheet]
        self.mock_worksheet.id = 141030202

    def test_save_success_headers_match(self, monkeypatch):
        """Test successful append when headers match."""
        df = pd.DataFrame({'col1': [1], 'col2': [2]})
        self.mock_worksheet.get_all_values.return_value = [['col1', 'col2']]
        
        download_data.save_to_google_sheet(df, "http://sheet", "sa.json")
        assert self.mock_worksheet.append_row.call_count == 1

class TestFetchChannelData:
    @pytest.mark.parametrize("granularity, expected_scale", [
        ("MINUTE", Scale.MINUTE.value),
        ("HOUR", Scale.HOUR.value),
        ("DAY", Scale.DAY.value),
    ])
    def test_granularity_mapping(self, monkeypatch, granularity, expected_scale):
        """Test that granularity is correctly mapped."""
        mock_vue = MagicMock()
        mock_channel = MagicMock(); mock_channel.channel_num = '1'; mock_channel.name = "Ch1"
        
        mock_vue.get_chart_usage.side_effect = [
            ([0.1], datetime(2023, 1, 1)),
            ([0.5], datetime(2023, 1, 1))
        ]
        
        df = download_data.fetch_channel_data(mock_vue, mock_channel, date(2023,1,1), date(2023,1,2), granularity)
        assert mock_vue.get_chart_usage.call_args_list[0][1]['scale'] == expected_scale

    def test_timezone_conversion(self, monkeypatch):
        """Test that local dates are converted to UTC for the API call and back for the result."""
        mock_vue = MagicMock()
        mock_channel = MagicMock(); mock_channel.channel_num = '1'; mock_channel.name = "Ch1"
        
        # 2023-01-01 00:00:00 America/Los_Angeles is 2023-01-01 08:00:00 UTC
        # 2023-01-01 23:59:00 America/Los_Angeles is 2023-01-02 07:59:00 UTC
        
        mock_vue.get_chart_usage.side_effect = [
            ([0.1], datetime(2023, 1, 1, 8, 0, 0, tzinfo=timezone.utc)),
            ([0.5], datetime(2023, 1, 1, 8, 0, 0, tzinfo=timezone.utc))
        ]
        
        df = download_data.fetch_channel_data(mock_vue, mock_channel, date(2023,1,1), date(2023,1,2), "DAY")
        
        # Verify API calls used UTC
        args_list = mock_vue.get_chart_usage.call_args_list
        start_utc = args_list[0][1]['start']
        end_utc = args_list[0][1]['end']
        
        assert start_utc.tzinfo == timezone.utc
        assert start_utc.hour == 8
        assert end_utc.tzinfo == timezone.utc
        assert end_utc.hour == 7 # 23:59 local -> 07:59 UTC next day
        
        # Verify result uses local timezone
        assert df['instant'].iloc[0].tzinfo is not None
        # pandas to_datetime might return a Timestamp with tz
        assert df['instant'].iloc[0].hour == 0
        assert df['instant'].iloc[0].day == 1

def test_csv_output_header(tmp_path, monkeypatch):
    """Integration test for output header."""
    mock_auth = MagicMock()
    mock_vue_instance = MagicMock()
    mock_auth.return_value = mock_vue_instance
    monkeypatch.setattr(download_data, 'authenticate', mock_auth)

    mock_device = MagicMock()
    mock_device.device_gid = 123
    mock_device.device_name = "Test Device"
    ch1 = MagicMock(); ch1.channel_num = '1'; ch1.name = "Ch1"
    mock_device.channels = [ch1]
    
    monkeypatch.setattr(download_data, 'get_emporia_device_info', MagicMock(return_value={123: mock_device}))
    
    mock_vue_instance.get_chart_usage.side_effect = [
        ([0.15], datetime(2023, 1, 1)),
        ([0.75], datetime(2023, 1, 1))
    ]
    
    monkeypatch.setattr(download_data, 'get_default_dates', MagicMock(return_value=(date(2023, 1, 1), date(2023, 1, 31))))

    output_folder = tmp_path / "emporia_data"
    download_data.download_emporia_data(
        email='test@example.com', password='pass', start_date_str=None, end_date_str=None, 
        granularity='DAY', output_folder=str(output_folder)
    )

    csv_file_path = output_folder / "emporia_data_2023-01.csv"
    df = pd.read_csv(csv_file_path)
    assert 'Test Device: Ch1 (USD)' in df.columns
