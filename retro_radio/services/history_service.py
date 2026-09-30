from retro_radio.db.session import get_db_sync
from retro_radio.db.repository import GenerationRepository

def save_generation_result(entry: dict, user_id: str):
    """生成履歴を保存する（リポジトリは flush のみなので必ず commit する）"""
    db = get_db_sync()
    try:
        repo = GenerationRepository(db)
        repo.create(user_id, entry)
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()
