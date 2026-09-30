from retro_radio.services.export_service import ExportService
from unittest.mock import Mock, patch
from datetime import datetime

def test_export_service():
    """CSVエクスポート内容確認"""
    # モックのGenerationRepositoryを作成
    mock_repo = Mock()
    mock_repo.get_by_user.return_value = [
        {
            "year": 1960,
            "month": 7,
            "day": 15,
            "script": "テストスクリプト",
            "song_title": "テスト曲",
            "artist_name": "テストアーティスト",
            "preview_url": "http://example.com/preview.mp3",
            "created_at": datetime(2023, 7, 15, 10, 30, 0)
        }
    ]
    
    # ExportServiceのdbとgen_repoをモックに置き換える
    with patch('retro_radio.services.export_service.get_db_sync'), \
         patch('retro_radio.services.export_service.GenerationRepository', return_value=mock_repo):
        service = ExportService()
        csv_data = service.export_generations_csv("test_user")
        service.close()
        
        # CSVデータに期待される内容が含まれているか確認
        assert "1960" in csv_data
        assert "7月15日" in csv_data
        assert "テストスクリプト" in csv_data
        assert "テスト曲" in csv_data
        assert "テストアーティスト" in csv_data
        assert "http://example.com/preview.mp3" in csv_data
        assert "2023-07-15 10:30:00" in csv_data
