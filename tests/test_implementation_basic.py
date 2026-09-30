class TestBasicAPI:
    def test_health_check(self, client):
        response = client.get("/health")
        assert response.status_code == 200
        data = response.json()
        assert "status" in data
        assert "Retro Radio" in data["service"]

    def test_generate_endpoint_basic(self, client):
        payload = {
            "year": 1975,
            "month": 9,
            "day": 24,
            "mode": "normal"
        }
        
        response = client.post("/api/generate", json=payload)
        assert response.status_code == 200
        
        data = response.json()
        assert "year" in data
        assert "month" in data
        assert "day" in data
        assert "mode" in data
        assert "script" in data
        assert "audio_url" in data
        assert "songs" in data
        
        # Basic validation
        assert data["year"] == 1975
        assert data["mode"] == "normal"
        assert len(data["script"]) > 50
        assert data["audio_url"] is not None
        assert isinstance(data["songs"], list)
    
    def test_generate_endpoint_with_segments_and_guide(self, client):
        """Test that new segments and program_guide fields are present"""
        payload = {
            "year": 1975,
            "month": 9,
            "day": 24,
            "mode": "normal"
        }
        
        response = client.post("/api/generate", json=payload)
        assert response.status_code == 200
        
        data = response.json()
        
        # New fields should be present (may be None if parsing fails)
        assert "segments" in data
        assert "program_guide" in data
        
        # If segments exist, they should have proper structure
        if data["segments"]:
            segment = data["segments"][0]
            assert "title" in segment
            assert "content" in segment
            assert "estimated_duration" in segment
            assert "order" in segment
        
        # Program guide should have basic structure
        if data["program_guide"]:
            guide = data["program_guide"]
            assert "date" in guide
            assert "weekday" in guide
            assert "schedules" in guide
            
            schedules = guide["schedules"]
            assert isinstance(schedules, list)
            
            if schedules:
                schedule = schedules[0]
                assert "title" in schedule
                assert "start_time" in schedule
                assert "duration" in schedule
                assert "description" in schedule
    
    def test_generate_endpoint_modes(self, client):
        # Test normal mode
        payload = {
            "year": 1975,
            "month": 9,
            "day": 24,
            "mode": "normal"
        }
        response = client.post("/api/generate", json=payload)
        assert response.status_code == 200
        data = response.json()
        assert data["mode"] == "normal"
        
        # Test care recreation mode
        payload = {
            "year": 1960,
            "month": 10,
            "day": 10,
            "mode": "care_recreation"
        }
        response = client.post("/api/generate", json=payload)
        assert response.status_code == 200
        data = response.json()
        assert data["mode"] == "care_recreation"
        assert "reminiscence_quiz" in data
        assert data["reminiscence_quiz"] is not None
        assert len(data["reminiscence_quiz"]) >= 1
        
        # Test anniversary mode
        payload = {
            "year": 1980,
            "month": 5,
            "day": 15,
            "mode": "anniversary",
            "target_name": "お母さん"
        }
        response = client.post("/api/generate", json=payload)
        assert response.status_code == 200
        data = response.json()
        assert data["mode"] == "anniversary"
        assert data["target_name"] == "お母さん"
        assert "お母さん" in data["script"]
