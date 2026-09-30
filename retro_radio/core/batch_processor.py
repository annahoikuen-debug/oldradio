from typing import List, Dict
from retro_radio.core.pipeline import generate_all_parallel
from retro_radio.utils.async_runner import AsyncProgress

class BatchProcessor:
    def __init__(self):
        self.progress = AsyncProgress()
    
    async def process_batch(self, years: List[int], month: int, day: int) -> List[Dict]:
        """複数年の一括生成"""
        results = []
        total = len(years)
        
        for i, year in enumerate(years):
            self.progress.update(
                int((i / total) * 100) if total else 0,
                f"処理中: {year}年 ({i+1}/{total})"
            )
            
            try:
                result = await generate_all_parallel(year, month, day, self.progress)
                results.append({
                    "year": year,
                    "success": True,
                    "data": result
                })
            except Exception as e:
                results.append({
                    "year": year,
                    "success": False,
                    "error": str(e)
                })
        
        self.progress.complete("バッチ処理完了")
        return results
