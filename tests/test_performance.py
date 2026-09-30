import time
from retro_radio.services.export_service import ExportService
from unittest.mock import Mock, patch

def test_export_performance():
    """エクスポート機能のパフォーマンステスト"""
    # 大量のモックデータを作成
    mock_data = []
    for i in range(1000):  # 1000件のデータ
        mock_data.append({
            "year": 1960 + (i % 30),
            "month": (i % 12) + 1,
            "day": (i % 28) + 1,
            "script": f"これはテストスクリプト{i}です。" * 10,  # やや長いスクリプト
            "song_title": f"テスト曲{i}",
            "artist_name": f"テストアーティスト{i % 100}",
            "preview_url": f"http://example.com/song{i}.mp3" if i % 3 == 0 else "",
            "created_at": f"2023-{(i % 12) + 1:02d}-{(i % 28) + 1:02d} 10:30:00"
        })
    
    # モックのGenerationRepositoryを作成
    mock_repo = Mock()
    mock_repo.get_by_user.return_value = mock_data
    
    # パフォーマンステスト
    with patch('retro_radio.services.export_service.get_db_sync'), \
         patch('retro_radio.services.export_service.GenerationRepository', return_value=mock_repo):
        service = ExportService()
        
        start_time = time.time()
        csv_data = service.export_generations_csv("test_user")
        end_time = time.time()
        
        service.close()
        
        # パフォーマンス要件: 1000件のエクスポートが2秒以内
        elapsed_time = end_time - start_time
        assert elapsed_time < 2.0, f"Export took too long: {elapsed_time:.2f} seconds"
        
        # 基本的な内容チェック
        assert "1960" in csv_data
        assert len(csv_data) > 10000  # 十分なサイズがあることを確認
        
        print(f"Export performance test passed: {elapsed_time:.2f} seconds for 1000 records")

if __name__ == "__main__":
    test_export_performance()
    print("All performance tests passed!")
