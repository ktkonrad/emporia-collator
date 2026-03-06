import pytest
import download_data
import sys
from unittest.mock import MagicMock

def test_main_exits_1_on_config_failure(monkeypatch):
    """Test that main() exits with code 1 if load_config returns None."""
    monkeypatch.setattr(download_data, 'load_config', MagicMock(return_value=None))
    
    with pytest.raises(SystemExit) as excinfo:
        download_data.main([])
    assert excinfo.value.code == 1

def test_main_exits_1_on_download_failure(monkeypatch):
    """Test that main() exits with code 1 if download_emporia_data returns False."""
    mock_config = {
        'email': 'test@example.com',
        'password': 'pass',
        'start_date': '2023-01-01',
        'end_date': '2023-01-31',
        'granularity': 'DAY',
        'aggregate_devices': []
    }
    monkeypatch.setattr(download_data, 'load_config', MagicMock(return_value=mock_config))
    monkeypatch.setattr(download_data, 'download_emporia_data', MagicMock(return_value=False))
    
    with pytest.raises(SystemExit) as excinfo:
        download_data.main([])
    assert excinfo.value.code == 1

def test_main_exits_0_on_success(monkeypatch):
    """Test that main() exits with code 0 (implicitly) on success."""
    mock_config = {
        'email': 'test@example.com',
        'password': 'pass',
        'start_date': '2023-01-01',
        'end_date': '2023-01-31',
        'granularity': 'DAY',
        'aggregate_devices': []
    }
    monkeypatch.setattr(download_data, 'load_config', MagicMock(return_value=mock_config))
    monkeypatch.setattr(download_data, 'download_emporia_data', MagicMock(return_value=True))
    
    # main() shouldn't raise SystemExit(1) here. 
    # If it finishes normally, it's a success (exit code 0).
    download_data.main([])

def test_download_emporia_data_returns_false_on_auth_failure(monkeypatch):
    """Test that download_emporia_data returns False if authentication fails."""
    monkeypatch.setattr(download_data, 'authenticate', MagicMock(return_value=None))
    
    result = download_data.download_emporia_data(
        'email', 'pass', '2023-01-01', '2023-01-31', 'DAY', []
    )
    assert result is False

def test_download_emporia_data_returns_false_on_no_data(monkeypatch):
    """Test that download_emporia_data returns False if no data is downloaded."""
    monkeypatch.setattr(download_data, 'authenticate', MagicMock(return_value=MagicMock()))
    monkeypatch.setattr(download_data, 'get_emporia_device_info', MagicMock(return_value={}))
    
    result = download_data.download_emporia_data(
        'email', 'pass', '2023-01-01', '2023-01-31', 'DAY', []
    )
    assert result is False
