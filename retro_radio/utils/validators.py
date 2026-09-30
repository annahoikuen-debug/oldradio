from pydantic import BaseModel, Field, field_validator
from datetime import date
import html
import re

class GenerationRequest(BaseModel):
    year: int = Field(ge=1950, le=2025, description="生成対象年")
    month: int = Field(ge=1, le=12, description="月")
    day: int = Field(ge=1, le=31, description="日")
    
    @field_validator('day')
    @classmethod
    def validate_day(cls, v: int, info) -> int:
        if 'month' in info.data and 'year' in info.data:
            try:
                date(info.data['year'], info.data['month'], v)
            except ValueError:
                raise ValueError(f'{info.data["year"]}年{info.data["month"]}月に{v}日は存在しません')
        return v

class ApiKeyConfig(BaseModel):
    gemini_key: str = Field(min_length=10, description="Gemini APIキー")
    
    @field_validator('gemini_key')
    @classmethod
    def validate_key_format(cls, v: str) -> str:
        if not re.match(r'^AIza[A-Za-z0-9_-]{35}$', v):
            raise ValueError('無効なAPIキー形式です')
        return v

class SearchParams(BaseModel):
    term: str = Field(min_length=1, max_length=200)
    country: str = Field(pattern='^[A-Z]{2}$', default='JP')
    media: str = Field(pattern='^(music|podcast|audiobook)$', default='music')
    entity: str = Field(pattern='^(musicTrack|album|artist)$', default='musicTrack')
    limit: int = Field(ge=1, le=200, default=50)

def sanitize_text(text: str, max_len: int = 5000) -> str:
    """XSS対策・文字数制限（HTMLエスケープで特殊文字を無害化する）"""
    cleaned = html.escape(text.strip(), quote=True)
    return cleaned[:max_len]

def validate_year_range(year: int, min_y: int = None, max_y: int = None) -> bool:
    if min_y is None:
        from ..config import get_settings
        settings = get_settings()
        min_y = settings.min_year
    if max_y is None:
        from ..config import get_settings
        settings = get_settings()
        max_y = settings.max_year
    return min_y <= year <= max_y
