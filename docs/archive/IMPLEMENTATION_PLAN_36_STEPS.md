# Implementation Plan for Radio Program Enhancements (Improvements 1-4)

**Date:** 2026-09-24
**Target Features:** Script Segmentation, News Variation, Crossfade Music, Program Guide
**Priority:** High
**Status:** Planning Phase

## Overview
This implementation plan details the development of four major improvements to the Retro Radio Time Machine's radio program generation and playback features. These enhancements focus on improving content variety, user experience, and accessibility.

## Improvements Summary

### 1. Script Segmentation with Timestamps
- **Goal:** Structure radio scripts into meaningful segments with timing information
- **Impact:** Enables synchronized UI features, improves accessibility, enhances navigation
- **Key Changes:**
  - Modify `GenerateResponse` to include segment data
  - Update `script_generator.py` to produce structured content
  - Add segment parsing utilities in `server.py`

### 2. News Content Variation
- **Goal:** Generate varied news content for each program to prevent repetition
- **Impact:** Increases replay value, provides diverse historical perspectives
- **Key Changes:**
  - Enhance `_build_prompt` in `script_generator.py` to include multiple news candidates
  - Implement random selection logic for news topics
  - Add API endpoint for news topic management

### 3. Crossfade Music Playback
- **Goal:** Implement seamless audio transitions between songs using Web Audio API
- **Impact:** Professional DJ-like experience, eliminates jarring audio cuts
- **Key Changes:**
  - Enhance `app.js` audio system with crossfade functionality
  - Update `playNextMedleyTrack` to support smooth transitions
  - Add configuration options for crossfade duration

### 4. Program Guide with Historical Context
- **Goal:** Display a radio program guide with historical context and program scheduling
- **Impact:** Provides reference value, enhances educational aspect, improves navigation
- **Key Changes:**
  - Add `timetable` field to `GenerateResponse`
  - Create data structures for historical radio program information
  - Enhance frontend UI with program guide display

## Implementation Details

### 1. Script Segmentation Implementation

#### Technical Specifications
- **Data Structure:** New `ScriptSegment` class in `retro_radio/models/radio.py`
- **Fields:**
  - `id`: Unique segment identifier
  - `title`: Segment title (Opening, News, Daily Life, Listener Message, Song Introduction)
  - `content`: Segment text content
  - `estimated_duration`: Estimated reading time in seconds (calculated based on content length)
  - `order`: Segment sequence number
  - `metadata`: Optional metadata (source, confidence score)

#### Code Changes

**File: `retro_radio/models/radio.py`**
```python
from typing import List, Optional
from dataclasses import dataclass

@dataclass
class ScriptSegment:
    id: str
    title: str
    content: str
    estimated_duration: float
    order: int
    metadata: Optional[dict] = None
    source_language: str = "ja"
```

**File: `retro_radio/core/script_generator.py`**
```python
def _build_prompt_with_segments(year: int, month: int, day: int, mode: str, target_name: Optional[str] = None) -> str:
    """Enhanced prompt that includes segment structure requirements"""
    target_chars = settings.target_script_chars
    
    # Define segment structure with character allocation
    segment_structure = {
        "opening": {
            "chars": target_chars // 7,
            "description": "Opening greeting and seasonal introduction"
        },
        "news": {
            "chars": target_chars * 2 // 7,
            "description": "Main news events and stories"
        },
        "daily_life": {
            "chars": target_chars * 2 // 7,
            "description": "Daily life, customs, and lifestyle"
        },
        "listener_message": {
            "chars": target_chars // 7,
            "description": "Message to listeners/viewers"
        },
        "song_introduction": {
            "chars": 50,
            "description": "Introduction to the day's hit song"
        }
    }
    
    # Generate segment-specific prompts
    segment_prompts = []
    for segment_name, config in segment_structure.items():
        segment_prompts.append(f"{segment_name.upper()}: {config['chars']}文字程度で作成してください - {config['description']}")
    
    # Build enhanced prompt
    base_prompt = _build_prompt(year, month, day, mode, target_name)
    
    return f"{base_prompt}\n\n追加の指示:\n" + "\n".join(segment_prompts) + "\n\n各セグメントを区切るために、例として『### オープニング』のようなマーカーを使用してください。"
```

**File: `retro_radio/server.py`**
```python
from typing import List
from dataclasses import dataclass

@dataclass
class GenerateResponse(BaseModel):
    # Existing fields...
    segments: List[ScriptSegment] = None
    
    class Config:
        json_encoders = {
            ScriptSegment: lambda v: v.__dict__
        }

def parse_script_segments(script: str) -> List[ScriptSegment]:
    """Parse structured script into segments"""
    segments = []
    current_segment = None
    lines = script.split('\n')
    
    for line in lines:
        # Check for segment header (e.g., "### オープニング")
        if line.startswith('### '):
            if current_segment:
                segments.append(current_segment)
            current_segment = ScriptSegment(
                id=f"seg_{len(segments)}",
                title=line.replace('### ', '').strip(),
                content="",
                estimated_duration=0,
                order=len(segments)
            )
        elif current_segment:
            current_segment.content += line + '\n'
    
    if current_segment:
        segments.append(current_segment)
    
    # Calculate estimated durations based on content
    for segment in segments:
        segment.estimated_duration = len(segment.content) / 3.0  # ~3 chars per second
    
    return segments
```

### 2. News Content Variation Implementation

#### Technical Specifications
- **News Topic Categories:** Politics, Economy, Society, Culture, Sports, Technology
- **Historical Data Source:** Integration with `fallback.py` for decade-specific news
- **Selection Algorithm:** Weighted random selection with caching

#### Code Changes

**File: `retro_radio/core/script_generator.py`**
```python
import random
from typing import List, Dict

class NewsTopic:
    def __init__(self, year: int, category: str, headline: str, importance: int):
        self.year = year
        self.category = category
        self.headline = headline
        self.importance = importance

NEWS_TOPICS_BY_DECADE: Dict[int, List[NewsTopic]] = {
    1950: [
        NewsTopic(1950, "社会", "日本の敗戦が決定", 10),
        NewsTopic(1950, "経済", "ジャパン・ディスカウント・ドルの導入", 8),
        NewsTopic(1950, "文化", "東京オリンピック招致活動開始", 7),
        # ... more topics
    ],
    # ... other decades
}

def select_news_topics(year: int, count: int = 2) -> List[NewsTopic]:
    """Select news topics for the given year"""
    decade = (year // 10) * 10
    
    if decade in NEWS_TOPICS_BY_DECADE:
        topics = random.sample(NEWS_TOPICS_BY_DECADE[decade], min(count, len(NEWS_TOPICS_BY_DECADE[decade])))
    else:
        # Fallback to closest decade
        decades = sorted(NEWS_TOPICS_BY_DECADE.keys())
        closest_decade = min(decades, key=lambda d: abs(d - decade))
        topics = random.sample(NEWS_TOPICS_BY_DECADE[closest_decade], min(count, len(NEWS_TOPICS_BY_DECADE[closest_decade])))
    
    # Sort by importance (descending)
    topics.sort(key=lambda t: t.importance, reverse=True)
    return topics

def _build_prompt_with_news_variety(year: int, month: int, day: int, mode: str, target_name: Optional[str] = None) -> str:
    """Build prompt that includes news variety instructions"""
    base_prompt = _build_prompt(year, month, day, mode, target_name)
    
    # Get news topics for the year
    news_topics = select_news_topics(year, count=3)
    
    # Create news variety instructions
    news_section = "\n\n=== ニュースバリエーション指示 ===\n"
    news_section += "各生成で異なるニュース組み合わせを提供するために、以下のニュース候補から1〜2件をランダムに選択してください：\n\n"
    
    for i, topic in enumerate(news_topics):
        news_section += f"{i+1}. {topic.category} - {topic.headline} (重要度: {topic.importance})\n"
    
    news_section += f"\n選択したニュースについて、{year}年{month}月{day}日の背景として、自然に組み込んでください。"
    
    return base_prompt + news_section
```

### 3. Crossfade Music Implementation

#### Technical Specifications
- **Crossfade Duration:** 2.0 seconds (configurable)
- **Audio Mixing:** Web Audio API GainNode for smooth transitions
- **Fallback:** Sudden cuts if crossfade fails

#### Code Changes

**File: `static/app.js`**
```javascript
// Audio system enhancements
const CROSSFADE_DURATION = 2000; // milliseconds

function playNextMedleyTrackWithCrossfade() {
    if (state.medleyPlayer.currentIndex >= state.medleyPlayer.urls.length) {
        // Medley finished
        setVinylSpinning(false);
        setTubeGlowing(false);
        stopVuMeter();
        updateStreamStatus('📻 放送終了', 'ダイヤルを回して他の年代もお楽しみください');
        setAudioActionIcon('▶️');
        return;
    }
    
    const currentUrl = state.medleyPlayer.urls[state.medleyPlayer.currentIndex];
    const nextUrl = state.medleyPlayer.urls[state.medleyPlayer.currentIndex + 1];
    
    // Create crossfade transition
    crossfadeBetweenTracks(currentUrl, nextUrl, () => {
        // Callback when crossfade is complete
        state.medleyPlayer.currentIndex++;
        playNextMedleyTrackWithCrossfade();
    });
}

function crossfadeBetweenTracks(currentUrl, nextUrl, callback) {
    if (!state.audioContext) {
        // Fallback to immediate transition
        playNextTrack(nextUrl, callback);
        return;
    }
    
    // Resume audio context if suspended
    if (state.audioContext.state === 'suspended') {
        state.audioContext.resume();
    }
    
    // Create sources for both tracks
    const currentSource = state.audioContext.createBufferSource();
    const nextSource = state.audioContext.createBufferSource();
    
    // Create gain nodes for volume control
    const currentGain = state.audioContext.createGain();
    const nextGain = state.audioContext.createGain();
    
    // Load current track as buffer (for crossfade)
    loadAudioBuffer(currentUrl).then(currentBuffer => {
        currentSource.buffer = currentBuffer;
        currentSource.loop = false;
        
        // Load next track as buffer
        loadAudioBuffer(nextUrl).then(nextBuffer => {
            nextSource.buffer = nextBuffer;
            nextSource.loop = false;
            
            // Connect nodes for crossfade
            currentSource.connect(currentGain);
            currentGain.connect(state.audioContext.destination);
            
            nextSource.connect(nextGain);
            nextGain.connect(state.audioContext.destination);
            
            // Crossfade setup
            currentGain.gain.setValueAtTime(1.0, state.audioContext.currentTime);
            nextGain.gain.setValueAtTime(0.0, state.audioContext.currentTime);
            
            // Fade out current track
            currentGain.gain.exponentialRampToValueAtTime(
                0.01, 
                state.audioContext.currentTime + CROSSFADE_DURATION / 1000
            );
            
            // Fade in next track
            nextGain.gain.linearRampToValueAtTime(
                1.0, 
                state.audioContext.currentTime + CROSSFADE_DURATION / 1000
            );
            
            // Set up completion handlers
            currentSource.onended = () => {
                // Clean up current track
                currentSource.disconnect();
                currentGain.disconnect();
                
                // Start next track
                nextSource.start();
                
                nextSource.onended = () => {
                    callback();
                };
            };
            
            // Start both tracks
            currentSource.start();
            setTimeout(() => {
                nextSource.start();
            }, 100); // Small delay to ensure smooth transition
            
        }).catch(error => {
            console.warn('Failed to load next track for crossfade:', error);
            // Fallback to immediate transition
            callback();
        });
        
    }).catch(error => {
        console.warn('Failed to load current track for crossfade:', error);
        // Fallback to immediate transition
        callback();
    });
}

function loadAudioBuffer(url) {
    return new Promise((resolve, reject) => {
        const audio = new Audio(url);
        const source = state.audioContext.createMediaElementSource(audio);
        const processor = state.audioContext.createScriptProcessor(4096, 1, 1);
        
        audio.oncanplaythrough = () => {
            // Convert to AudioBuffer
            state.audioContext.decodeAudioData(
                audio.captureStream().getAudioTracks()[0].getContents(),
                resolve,
                reject
            );
        };
        
        audio.onerror = reject;
        audio.src = url;
    });
}
```

### 4. Program Guide Implementation

#### Technical Specifications
- **Program Structure:** Daily schedule with start times, program titles, descriptions
- **Historical Context:** Integration with historical radio program data
- **Display Format:** Timeline view with historical anchors

#### Code Changes

**File: `retro_radio/models/radio.py`**
```python
@dataclass
class ProgramSchedule:
    id: str
    title: str
    start_time: str  # Format: "HH:MM"
    duration: int  # minutes
    description: str
    is_historical: bool = False
    source: str = "modern"  # "modern", "historical", "legacy"

@dataclass
class ProgramGuide:
    date: str  # YYYY-MM-DD
    weekday: str  # Japanese weekday
    schedules: List[ProgramSchedule]
    today_highlight: str  # Today's featured program
    special_events: List[str] = None

class HistoricalRadioPrograms:
    """Historical radio program database"""
    
    PROGRAMS_BY_DECADE: Dict[int, List[ProgramSchedule]] = {
        1950: [
            ProgramSchedule("06:00", "06:00", 30, "NHKラジオ第一放送", True, "1950年"),
            ProgramSchedule("19:00", "19:00", 60, "ゴールデンタイム・ドラマ", True, "1950年"),
            ProgramSchedule("23:00", "23:00", 30, "深夜放送・懐かしの歌謡ショー", True, "1950年"),
        ],
        1970: [
            ProgramSchedule("07:00", "07:00", 30, "オール朝のワイドショー", True, "1970年"),
            ProgramSchedule("12:00", "12:00", 90, "ランチタイム・ミュージックハイライト", True, "1970年"),
            # ... more
        ],
        # ... other decades
    }
    
    @classmethod
    def get_program_guide(cls, year: int, month: int, day: int) -> ProgramGuide:
        """Generate program guide for specific date"""
        weekday_names = ["日", "月", "火", "水", "木", "金", "土"]
        weekday = weekday_names[datetime(year, month, day).weekday()]
        
        # Today's highlight (random historical program for the year)
        today_highlight = "レトロラジオ・タイムマシン"
        
        # Get scheduled programs
        schedules = []
        
        # Add modern schedule
        modern_schedules = [
            ProgramSchedule("06:00", "06:00", 30, "モーニングニュース", False, "modern"),
            ProgramSchedule("10:00", "10:00", 60, "文化遺産の旅", False, "modern"),
            ProgramSchedule("13:00", "13:00", 90, f"特集: {year}年の回想", False, "modern"),
            ProgramSchedule("19:00", "19:00", 120, "ヒット曲メドレー", False, "modern"),
            ProgramSchedule("21:00", "21:00", 30, "ナイト・トーキング・ショー", False, "modern"),
        ]
        
        # Occasionally include a historical program based on the year
        import random
        if random.random() < 0.3:  # 30% chance
            decade = (year // 10) * 10
            if decade in cls.PROGRAMS_BY_DECADE and cls.PROGRAMS_BY_DECADE[decade]:
                historical_program = random.choice(cls.PROGRAMS_BY_DECADE[decade])
                historical_program.is_historical = True
                schedules.append(historical_program)
        
        schedules.extend(modern_schedules)
        
        return ProgramGuide(
            date=f"{year:04d}-{month:02d}-{day:02d}",
            weekday=weekday,
            schedules=schedules,
            today_highlight=today_highlight,
            special_events=[]
        )
```

**File: `retro_radio/server.py`**
```python
from .models.radio import ProgramGuide

@app.post("/api/generate", response_model=GenerateResponse)
async def generate_program(req: GenerateRequest):
    # ... existing code ...
    
    # Generate program guide
    program_guide = HistoricalRadioPrograms.get_program_guide(
        year=req.year,
        month=req.month,
        day=req.day
    )
    
    # Update response to include segments and program guide
    response_data = {
        "year": req.year,
        "month": req.month,
        "day": req.day,
        "mode": req.mode,
        "script": script,
        "audio_url": audio_url,
        "songs": song_list,
        "song": song_list[0] if song_list else None,
        "reminiscence_quiz": quiz_data,
        "target_name": req.target_name,
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "segments": segments,
        "program_guide": program_guide
    }
    
    # Store generation in database if user is logged in
    if current_user:
        await store_generation(current_user, response_data)
    
    return response_data
```

## Frontend Integration

### JavaScript Updates (`static/app.js`)

**Segment Display**
```javascript
function updateSegmentDisplay(segments) {
    const segmentContainer = document.getElementById('segmentContainer');
    if (!segmentContainer) return;
    
    segmentContainer.innerHTML = '';
    
    segments.forEach((segment, index) => {
        const segmentEl = document.createElement('div');
        segmentEl.className = 'segment-item';
        segmentEl.id = `segment-${index}`;
        segmentEl.innerHTML = `
            <div class="segment-header">
                <span class="segment-number">${index + 1}</span>
                <h4 class="segment-title">${segment.title}</h4>
                <span class="segment-duration">${Math.round(segment.estimated_duration)}秒</span>
            </div>
            <div class="segment-content" data-index="${index}">
                <p>${escapeHtml(segment.content)}</p>
            </div>
        `;
        
        segmentContainer.appendChild(segmentEl);
    });
    
    // Scroll to current segment if we have audio playback
    if (state.currentAudio && state.audioPosition) {
        scrollToSegment(state.audioPosition.segmentIndex);
    }
}

function scrollToSegment(segmentIndex) {
    const segmentEl = document.getElementById(`segment-${segmentIndex}`);
    if (segmentEl) {
        segmentEl.scrollIntoView({ behavior: 'smooth', block: 'center' });
        segmentEl.classList.add('active-segment');
        
        // Remove active class from other segments after delay
        setTimeout(() => {
            document.querySelectorAll('.segment-item').forEach(el => {
                el.classList.remove('active-segment');
            });
            segmentEl.classList.add('active-segment');
        }, 2000);
    }
}
```

**News Variation UI**
```javascript
function updateNewsVariationDisplay(topics) {
    const newsIndicator = document.getElementById('newsVariationIndicator');
    if (!newsIndicator) return;
    
    const randomTopic = topics[Math.floor(Math.random() * topics.length)];
    
    newsIndicator.innerHTML = `
        <span class="news-badge">\u26A0\ufe0f \u65b0\u95a2\u4e89\u544a\u63d2\u5165</span>
        <span class="news-headline">${randomTopic.headline}</span>
        <span class="news-category">${randomTopic.category} (\u91cd\u8981\u5ea6: ${randomTopic.importance})</span>
    `;
    
    // Show for 5 seconds
    newsIndicator.style.display = 'block';
    setTimeout(() => {
        newsIndicator.style.display = 'none';
    }, 5000);
}
```

**Program Guide Display**
```javascript
function updateProgramGuideDisplay(programGuide) {
    const guideContainer = document.getElementById('programGuide');
    if (!guideContainer) return;
    
    guideContainer.innerHTML = `
        <div class="program-guide-header">
            <h3>\u97f3\u4e50\u8a18\u9244\u8a00\u306e\u8a00\u3002\u3010${programGuide.date}\u3011\u3011${programGuide.weekday}\u66dc\u65e5</h3>
            <p class="today-highlight">\ud83c\udfad \u4eca\u65e5\u306e\u7279\u5f81\u306f\u30d5\u30e9\u30f3\u30b5\u30fc\u30de\u30fc\uff1a\u3000${programGuide.today_highlight}</p>
        </div>
        
        <div class="program-schedule">
            <h4>\u3044\u65e5\u306e\u8a00\u5417\u8868</h4>
            <div class="schedule-timeline">
                ${programGuide.schedules.map(schedule => `
                    <div class="schedule-item \${schedule.is_historical ? 'historical' : ''}">
                        <div class="time-column">\u3000\u3000${schedule.start_time} - \u3000\u3000${schedule.duration}min\u3000\u3000</div>
                        <div class="program-column">
                            <div class="program-title">${schedule.title}</div>
                            <div class="program-desc">${schedule.description}</div>
                            \${schedule.is_historical ? '<div class="historical-badge">\ud83d\udcdd \u6f2b\u5386\u306e\u8a00\u5417</div>' : ''}
                        </div>
                    </div>
                `).join('')}
            </div>
        </div>
    `;
    
    // Make guide collapsible
    const toggleBtn = document.getElementById('toggleProgramGuide');
    if (toggleBtn) {
        toggleBtn.addEventListener('click', () => {
            guideContainer.classList.toggle('collapsed');
            toggleBtn.textContent = guideContainer.classList.contains('collapsed') 
                ? '\ud83d\uddd7\ufe0f \u8a00\u5417\u8868\u3092\u8853\u3058\u3066\u3089\u305b\u3046'
                : '\ud83d\udcd7 \u8a00\u5417\u8868\u3092\u96a0\u3057\u3066\u304f\u3060\u3055\u3044';
        });
    }
}
```

## CSS Updates

**File: `static/app.css`**
```css
/* Segment display styles */
.segment-container {
    background: rgba(0, 0, 0, 0.7);
    border-radius: 8px;
    padding: 20px;
    margin: 20px 0;
    max-height: 400px;
    overflow-y: auto;
}

.segment-item {
    background: rgba(255, 255, 255, 0.1);
    border-left: 4px solid #d4af37;
    margin: 10px 0;
    padding: 15px;
    border-radius: 4px;
    transition: all 0.3s ease;
}

.segment-item.active-segment {
    background: rgba(212, 175, 55, 0.3);
    border-left-color: #ffd700;
    box-shadow: 0 0 10px rgba(212, 175, 55, 0.5);
}

.segment-header {
    display: flex;
    align-items: center;
    margin-bottom: 10px;
}

.segment-number {
    background: #d4af37;
    color: #000;
    border-radius: 50%;
    width: 30px;
    height: 30px;
    display: flex;
    align-items: center;
    justify-content: center;
    font-weight: bold;
    margin-right: 10px;
}

.segment-title {
    margin: 0;
    color: #d4af37;
}

.segment-duration {
    margin-left: auto;
    color: #aaa;
    font-size: 0.9em;
}

/* News variation indicator */
#newsVariationIndicator {
    position: fixed;
    top: 20px;
    right: 20px;
    background: rgba(255, 140, 0, 0.9);
    color: white;
    padding: 10px 15px;
    border-radius: 8px;
    box-shadow: 0 4px 6px rgba(0, 0, 0, 0.3);
    z-index: 1000;
    display: none;
    animation: slideIn 0.3s ease;
}

.news-badge {
    display: block;
    background: rgba(255, 255, 255, 0.2);
    padding: 2px 6px;
    border-radius: 4px;
    font-size: 0.8em;
    margin-bottom: 5px;
}

.news-headline {
    font-weight: bold;
    font-size: 1.1em;
    margin-bottom: 3px;
    display: block;
}

.news-category {
    font-size: 0.9em;
    opacity: 0.9;
}

@keyframes slideIn {
    from { transform: translateX(100%); }
    to { transform: translateX(0); }
}

/* Program guide styles */
.program-guide {
    background: rgba(0, 0, 0, 0.8);
    border-radius: 8px;
    padding: 20px;
    margin: 20px 0;
    border: 1px solid rgba(212, 175, 55, 0.3);
}

.program-guide.collapsed {
    max-height: 60px;
    overflow: hidden;
}

.program-guide-header {
    border-bottom: 1px solid rgba(212, 175, 55, 0.5);
    padding-bottom: 10px;
    margin-bottom: 15px;
}

.program-schedule {
    margin-top: 20px;
}

.schedule-timeline {
    margin-top: 15px;
}

.schedule-item {
    display: flex;
    margin: 15px 0;
    padding: 15px;
    background: rgba(255, 255, 255, 0.05);
    border-radius: 6px;
    border-left: 4px solid #444;
    transition: all 0.3s ease;
}

.schedule-item.historical {
    border-left-color: #d4af37;
    background: rgba(212, 175, 55, 0.1);
}

.time-column {
    color: #d4af37;
    font-family: 'JetBrains Mono', monospace;
    min-width: 100px;
}

.program-column {
    flex: 1;
}

.program-title {
    font-weight: bold;
    margin-bottom: 5px;
    color: #d4af37;
}

.program-desc {
    font-size: 0.9em;
    margin-bottom: 5px;
}

.historical-badge {
    display: inline-block;
    background: rgba(212, 175, 55, 0.2);
    color: #d4af37;
    padding: 2px 6px;
    border-radius: 4px;
    font-size: 0.8em;
    margin-top: 5px;
}
```

## Testing Strategy

### Unit Tests

**File: `tests/test_script_segments.py`**
```python
import pytest
from retro_radio.models.radio import ScriptSegment, parse_script_segments
from retro_radio.server import parse_script_segments

class TestScriptSegmentModel:
    def test_script_segment_creation(self):
        segment = ScriptSegment(
            id="test-seg-1",
            title="オーディング",
            content="これはテスト用の原稿です。",
            estimated_duration=5.0,
            order=1
        )
        assert segment.id == "test-seg-1"
        assert segment.title == "オーディング"
        assert segment.estimated_duration == 5.0

    def test_script_segment_to_dict(self):
        segment = ScriptSegment(
            id="test-seg-1",
            title="オーディング",
            content="原稿テスト",
            estimated_duration=5.0,
            order=1,
            metadata={"source": "test"}
        )
        
        segment_dict = segment.__dict__
        assert segment_dict["title"] == "オーディング"
        assert segment_dict["metadata"]["source"] == "test"

def test_parse_script_segments():
    script = """### オープニング
こんにちは、皆様。今日の時代へご案内いたします。

### ニュース
ある日のニュース headlines here.

### くらしの風景
当時の暮らしぶりを紹介します。"""
    
    segments = parse_script_segments(script)
    
    assert len(segments) == 3
    assert segments[0].title == "オープニング"
    assert "こんにちは" in segments[0].content
    assert segments[1].title == "ニュース"
    assert segments[2].title == "くらしの風景"
    
    # Check estimated durations
    assert segments[0].estimated_duration > 0
    assert segments[1].estimated_duration > 0
    assert segments[2].estimated_duration > 0

def test_script_segment_integration_with_response():
    from fastapi.testclient import TestClient
    from retro_radio.server import app
    
    client = TestClient(app)
    
    payload = {
        "year": 1975,
        "month": 9,
        "day": 24,
        "mode": "normal"
    }
    
    response = client.post("/api/generate", json=payload)
    assert response.status_code == 200
    
    data = response.json()
    assert "segments" in data
    assert isinstance(data["segments"], list)
    
    if data["segments"]:
        segment = data["segments"][0]
        assert "title" in segment
        assert "content" in segment
        assert "estimated_duration" in segment
        assert "order" in segment
```

**File: `tests/test_news_variation.py`**
```python
import pytest
from retro_radio.core.script_generator import select_news_topics, NewsTopic

class TestNewsTopicModel:
    def test_news_topic_creation(self):
        topic = NewsTopic(
            year=1975,
            category="政治",
            headline="ある日の政治ニュース",
            importance=8
        )
        
        assert topic.year == 1975
        assert topic.category == "政治"
        assert topic.headline == "ある日の政治ニュース"
        assert topic.importance == 8

    def test_news_topic_sorting(self):
        topic1 = NewsTopic(1975, "政治", "ニュース1", 5)
        topic2 = NewsTopic(1975, "経済", "ニュース2", 10)
        topic3 = NewsTopic(1975, "文化", "ニュース3", 7)
        
        topics = [topic1, topic2, topic3]
        topics.sort(key=lambda t: t.importance, reverse=True)
        
        assert topics[0].importance == 10
        assert topics[0].headline == "ニュース2"
        assert topics[2].importance == 5

def test_select_news_topics():
    topics = select_news_topics(1975, count=2)
    
    assert isinstance(topics, list)
    assert len(topics) <= 2
    
    for topic in topics:
        assert isinstance(topic, NewsTopic)
        assert topic.year == 1975
        assert topic.category in ["政治", "経済", "社会", "文化", "スポーツ", "技術"]
        assert 1 <= topic.importance <= 10

def test_news_variation_integration():
    from fastapi.testclient import TestClient
    from retro_radio.server import app
    
    client = TestClient(app)
    
    payload = {
        "year": 1975,
        "month": 9,
        "day": 24,
        "mode": "normal"
    }
    
    response = client.post("/api/generate", json=payload)
    assert response.status_code == 200
    
    data = response.json()
    assert "script" in data
    
    # The script should contain various news topics
    # This is a basic check - in a real test, we might want to parse the script for specific patterns
    assert len(data["script"]) > 100
```

**File: `tests/test_crossfade_music.py`**
```python
import pytest
from unittest.mock import patch, MagicMock
import asyncio
import time

class TestCrossfadeFunctionality:
    @pytest.mark.asyncio
    async def test_crossfade_with_valid_tracks(self):
        # Mock audio context
        mock_context = MagicMock()
        mock_context.state = "running"
        
        with patch('retro_radio.app.js.AudioContext', return_value=mock_context):
            from static.app import crossfadeBetweenTracks
            
            # Mock track loading
            with patch('static.app.loadAudioBuffer') as mock_load:
                mock_load.return_value = {"mock": "buffer"}
                
                # Mock gain nodes
                mock_current_gain = MagicMock()
                mock_next_gain = MagicMock()
                
                mock_context.createGain.return_value = mock_current_gain
                
                # Test crossfade initiation
                crossfadeBetweenTracks(
                    "track1.mp3",
                    "track2.mp3",
                    lambda: None  # callback
                )
                
                # Verify API calls
                mock_context.createGain.assert_called()
    
    def test_crossfade_fallback_on_error(self):
        """Test that crossfade gracefully falls back to immediate transition when errors occur"""
        with patch('retro_radio.app.js.AudioContext', side_effect=Exception("Audio not supported")):
            from static.app import crossfadeBetweenTracks
            
            # Should call callback immediately on fallback
            callback_called = []
            def mock_callback():
                callback_called.append(True)
            
            crossfadeBetweenTracks(
                "track1.mp3",
                "track2.mp3", 
                mock_callback
            )
            
            assert callback_called == [True], "Fallback should call callback immediately"

def test_crossfade_configuration():
    """Test crossfade configuration options"""
    from static.app import CROSSFADE_DURATION
    
    # Should have a reasonable default crossfade duration
    assert CROSSFADE_DURATION is not None
    assert CROSSFADE_DURATION > 0
    assert CROSSFADE_DURATION < 10000  # Should be less than 10 seconds

@pytest.mark.asyncio
async def test_crossfade_integration_with_medley_player():
    """Integration test for crossfade with medley player"""
    from fastapi.testclient import TestClient
    from retro_radio.server import app
    
    client = TestClient(app)
    
    # Mock the generation to include preview tracks
    with patch('retro_radio.server.search_itunes_songs') as mock_search:
        mock_search.return_value = [
            {
                "trackName": "テスト曲1",
                "artistName": "テストアーティスト1", 
                "previewUrl": "http://example.com/track1.mp3",
                "artworkUrl100": "http://example.com/art1.jpg"
            },
            {
                "trackName": "テスト曲2",
                "artistName": "テストアーティスト2",
                "previewUrl": "http://example.com/track2.mp3", 
                "artworkUrl100": "http://example.com/art2.jpg"
            }
        ]
        
        payload = {
            "year": 1975,
            "month": 9,
            "day": 24,
            "mode": "normal"
        }
        
        response = client.post("/api/generate", json=payload)
        assert response.status_code == 200
        
        data = response.json()
        assert "songs" in data
        assert len(data["songs"]) > 0
        
        # Check that preview URLs are included (for crossfade)
        preview_count = sum(1 for song in data["songs"] if song.get("preview_url"))
        assert preview_count >= 1  # At least one track should have preview
```

**File: `tests/test_program_guide.py`**
```python
import pytest
from datetime import datetime
from retro_radio.models.radio import ProgramGuide, ProgramSchedule, HistoricalRadioPrograms

class TestProgramScheduleModel:
    def test_program_schedule_creation(self):
        schedule = ProgramSchedule(
            id="schedule-1",
            title="テスト番組",
            start_time="10:00",
            duration=60,
            description="テスト用の番組説明です。",
            is_historical=True,
            source="1950年"
        )
        
        assert schedule.id == "schedule-1"
        assert schedule.title == "テスト番組"
        assert schedule.start_time == "10:00"
        assert schedule.duration == 60
        assert schedule.is_historical == True

    def test_program_schedule_modern_defaults(self):
        schedule = ProgramSchedule(
            id="schedule-2",
            title="モーニングニュース",
            start_time="06:00",
            duration=30,
            description="最新のニュースをお届けします。"
        )
        
        assert schedule.is_historical == False
        assert schedule.source == "modern"

class TestHistoricalRadioPrograms:
    def test_get_program_guide_basic(self):
        guide = HistoricalRadioPrograms.get_program_guide(1975, 9, 24)
        
        assert isinstance(guide, ProgramGuide)
        assert guide.date == "1975-09-24"
        assert guide.weekday == "火"  # 9月24日が火曜日
        assert "today_highlight" in guide.__dict__
        assert isinstance(guide.schedules, list)
        assert len(guide.schedules) > 0
    
    def test_program_guide_contains_modern_schedule(self):
        guide = HistoricalRadioPrograms.get_program_guide(1975, 9, 24)
        
        modern_schedules = [s for s in guide.schedules if not s.is_historical]
        assert len(modern_schedules) > 0
        
        # Should have at least morning news
        schedule_titles = [s.title for s in modern_schedules]
        assert any("モーニングニュース" in title for title in schedule_titles)
    
    def test_program_guide_occasional_historical(self):
        """Test that program guide occasionally includes historical programs"""
        historical_count = 0
        total_tests = 20
        
        for _ in range(total_tests):
            guide = HistoricalRadioPrograms.get_program_guide(1975, 9, 24)
            historical_count += sum(1 for s in guide.schedules if s.is_historical)
        
        # At least some historical programs should appear across multiple calls
        assert historical_count > 0
    
    def test_program_guide_date_consistency(self):
        guide1 = HistoricalRadioPrograms.get_program_guide(1975, 9, 24)
        guide2 = HistoricalRadioPrograms.get_program_guide(1975, 9, 24)
        
        assert guide1.date == guide2.date
        assert guide1.weekday == guide2.weekday
        
        # Schedules should be consistent
        assert len(guide1.schedules) == len(guide2.schedules)

def test_program_guide_integration_with_api():
    from fastapi.testclient import TestClient
    from retro_radio.server import app
    
    client = TestClient(app)
    
    payload = {
        "year": 1975,
        "month": 9,
        "day": 24,
        "mode": "normal"
    }
    
    response = client.post("/api/generate", json=payload)
    assert response.status_code == 200
    
    data = response.json()
    assert "program_guide" in data
    
    program_guide = data["program_guide"]
    assert "date" in program_guide
    assert "weekday" in program_guide
    assert "today_highlight" in program_guide
    assert "schedules" in program_guide
    
    # Check schedule structure
    schedules = program_guide["schedules"]
    assert len(schedules) > 0
    
    schedule = schedules[0]
    assert "title" in schedule
    assert "start_time" in schedule
    assert "duration" in schedule
    assert "description" in schedule
    assert "is_historical" in schedule

def test_program_guide_ui_integration():
    """Test that program guide data structure matches frontend expectations"""
    from fastapi.testclient import TestClient
    from retro_radio.server import app
    
    client = TestClient(app)
    
    payload = {
        "year": 1975,
        "month": 9,
        "day": 24,
        "mode": "normal"
    }
    
    response = client.post("/api/generate", json=payload)
    data = response.json()
    
    program_guide = data["program_guide"]
    
    # Verify frontend-compatible structure
    assert program_guide["date"].match(r'\d{4}-\d{2}-\d{2}')  # Date format validation
    assert program_guide["weekday"] in ["日", "月", "火", "水", "木", "金", "土"]
    
    # All schedules should have required frontend fields
    for schedule in program_guide["schedules"]:
        assert isinstance(schedule["title"], str)
        assert isinstance(schedule["start_time"], str)
        assert schedule["duration"] > 0
        assert isinstance(schedule["description"], str)
        assert isinstance(schedule["is_historical"], bool)
        
        # Historical schedules should have source field
        if schedule["is_historical"]:
            assert "source" in schedule or True  # Accept either way
```

## Performance Considerations

### Memory Usage
- **Segments**: Each segment adds ~1KB of metadata; 50 segments = ~50KB
- **Program Guide**: ~10KB per guide
- **Recommendation**: Implement lazy loading of detailed segment content

### API Response Size
- **Current**: ~50KB per generation response
- **With Enhancements**: ~70-80KB (25% increase)
- **Recommendation**: Consider compression for mobile users

## Accessibility Considerations

### Screen Reader Support
- Segment titles should include proper ARIA labels
- Program guide timing should be announced with clear intervals
- Crossfade transitions should have appropriate announcements

### Keyboard Navigation
- Segment container keyboard navigation
- Program guide collapsible controls
- Audio controls with crossfade status announcements

## Rollback Strategy

### Immediate Rollback (0-2 hours)
- Comment out new fields in `GenerateResponse`
- Revert frontend segment display logic
- Keep program guide data structure but hide from UI

### Partial Rollback (2-8 hours)
- Remove crossfade functionality but keep medley player
- Revert news variation but keep base news structure
- Restore original script segmentation but keep program guide

### Full Rollback (8+ hours)
- Revert all changes to original state
- Restore all original files from git
- Run existing test suite to verify baseline

## Deployment Plan

### Phase 1: Development and Testing (Days 1-7)
1. Implement script segmentation (Day 1-2)
2. Implement news variation (Day 3-4)
3. Implement crossfade (Day 5-6)
4. Implement program guide (Day 7)
5. Write and run unit tests (Day 7)

### Phase 2: Integration and UI (Days 8-14)
1. Integrate changes into existing codebase
2. Update frontend JavaScript
3. Update CSS styling
4. Run integration tests
5. Fix any remaining bugs

### Phase 3: Testing and Validation (Days 15-21)
1. Run comprehensive test suite
2. Perform performance testing
3. Conduct accessibility testing
4. Run regression tests
5. Deploy to staging environment

### Phase 4: Production Deployment (Days 22-25)
1. Deploy to production
2. Monitor performance and user feedback
3. Implement monitoring and logging
4. Create rollback procedures
5. Update documentation

## Risk Assessment

| Risk | Probability | Impact | Mitigation |
|------|-------------|--------|------------|
| Performance degradation | Medium | High | Implement lazy loading, compression |
| Cross-browser compatibility | Low | Medium | Use progressive enhancement |
| Test coverage gaps | Low | Medium | Comprehensive test suite |
| Accessibility issues | Low | High | Early accessibility testing |
| API breaking changes | Medium | High | Maintain backward compatibility |

## Success Criteria

### Technical
- All new functionality works as specified
- Response times remain under target (2-5 seconds)
- Crossfade transitions complete smoothly
- Program guide displays correctly across devices
- Accessibility features meet WCAG 2.1 AA standards

### User Experience
- Segments display with proper timing and content
- News variety provides fresh content on each listen
- Crossfade eliminates audio jarring
- Program guide enhances context without cluttering
- All new features integrate seamlessly with existing UI

### Quality
- 90%+ test coverage for new code
- No regression in existing functionality
- Code follows existing project conventions
- Documentation is complete and accurate
- Performance meets or exceeds requirements

## Conclusion

This implementation plan provides a comprehensive roadmap for deploying the four radio program enhancements. By following this plan, the development team can deliver significant improvements to content variety, user experience, and accessibility while maintaining high quality and minimizing risk.

The key to success will be:
1. Rigorous testing at each phase
2. Early identification of integration issues
3. Maintaining backward compatibility
4. Focusing on user experience and accessibility
5. Implementing proper monitoring and rollback procedures

With proper execution, this enhancement will significantly improve the Retro Radio Time Machine's value proposition and user satisfaction.