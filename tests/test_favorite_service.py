from retro_radio.services.favorite_service import FavoriteService
from unittest.mock import Mock, patch

def test_favorite_service():
    """お気に入り追加・削除・取得確認"""
    # モックのリポジトリを作成
    mock_fav_repo = Mock()
    mock_gen_repo = Mock()
    
    # FavoriteServiceのdbとリポジトリをモックに置き換える
    with patch('retro_radio.services.favorite_service.get_db_sync'), \
         patch('retro_radio.services.favorite_service.FavoriteRepository', return_value=mock_fav_repo), \
         patch('retro_radio.services.favorite_service.GenerationRepository', return_value=mock_gen_repo):
        service = FavoriteService()
        
        # お気に入り追加
        mock_fav_repo.add.return_value = True
        result = service.add_favorite("user1", "gen1")
        assert result is True
        mock_fav_repo.add.assert_called_once_with("user1", "gen1")
        
        # お気に入り削除
        mock_fav_repo.remove.return_value = True
        result = service.remove_favorite("user1", "gen1")
        assert result is True
        mock_fav_repo.remove.assert_called_once_with("user1", "gen1")
        
        # お気に入り判定
        mock_fav_repo.is_favorite.return_value = True
        result = service.is_favorite("user1", "gen1")
        assert result is True
        mock_fav_repo.is_favorite.assert_called_once_with("user1", "gen1")
        
        # お気に入り取得
        mock_fav_repo.get_user_favorites.return_value = ["gen1"]
        mock_gen_repo.get_by_id.return_value = {
            "year": 1960,
            "month": 7,
            "day": 15,
            "script": "テストスクリプト",
            "song_title": "テスト曲",
            "artist_name": "テストアーティスト",
            "preview_url": "http://example.com/preview.mp3",
            "created_at": "2023-07-15 10:30:00"
        }
        favorites = service.get_favorites("user1")
        assert len(favorites) == 1
        assert favorites[0]["year"] == 1960
        assert favorites[0]["song_title"] =="テスト曲"
        service.close()
