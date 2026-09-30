# tests/test_load.py
import pytest
pytest.importorskip("locust")
from locust import HttpUser, task, between

class RetroRadioUser(HttpUser):
    wait_time = between(1, 3)
    
    def on_start(self):
        self.client.get("/")
    
    @task(3)
    def generate_radio(self):
        # 年選択→生成の流れ（Streamlitはstatefulなので簡易版）
        self.client.post("/_stcore/stream", json={
            "widget_id": "slider", "value": 1980
        })
        self.client.post("/_stcore/stream", json={
            "widget_id": "button_generate", "value": True
        })
    
    @task(1)
    def view_history(self):
        self.client.get("/?history=1")

# 実行: locust -f tests/test_load.py --host=http://localhost:8501
# 目標: 10同時ユーザーでエラー率<1%、p95<10秒
