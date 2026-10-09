"""Probe: edge-tts fallback must not poison the edge cache key."""
import os, sys, io, pathlib, tempfile, uuid
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
os.environ["RETRO_RADIO_TTS_ENGINE"] = "edge"
os.environ["RETRO_RADIO_SECRET_KEY"] = "pytest-secret-key-not-for-production"

from retro_radio import server as sv
from retro_radio.core import tts_engines

sv.settings.tts_engine = "edge"
sv.settings.tts_edge_voice = "ja-JP-NanamiNeural"

print("resolve_engine ->", sv._active_tts_engine())

sv._tts_throttle = lambda: None
sv._tts_circuit_open = lambda: False
sv._tts_is_blocked_by_gtts = lambda: False

TEXT = "キャッシュ取り違えの検証です。" + uuid.uuid4().hex[:8]
CACHE = pathlib.Path(sv._tenant_cache_dir(None))

edge_calls = []

def fake_edge(text, path, options):
    edge_calls.append(text)
    raise tts_engines.EdgeTTSUnavailable("simulated transient failure")

def fake_edge_ok(text, path, options):
    edge_calls.append(text)
    pathlib.Path(path).write_bytes(b"EDGE-NEURAL-AUDIO")

def fake_save(tts, cache_dir=None):
    d = pathlib.Path(cache_dir) if cache_dir else CACHE
    d.mkdir(parents=True, exist_ok=True)
    p = d / "g.mp3"
    p.write_bytes(b"GTTS-ROBOTIC-AUDIO")
    return str(p)

sv._save_tts_to_temp = fake_save

# Simulate the PRE-FIX behaviour: ignore the explicit engine argument and always
# derive the key from the *active* engine (edge), which is what the old code did.
if os.environ.get("SIMULATE_OLD") == "1":
    _orig_name = sv._tts_cache_filename

    def _old_name(text, engine=None):
        return _orig_name(text)

    sv._tts_cache_filename = _old_name
    print(">>> simulating PRE-FIX behaviour (engine arg ignored)")

# --- call 1: edge is broken -------------------------------------------------
tts_engines.synthesize_to_file = fake_edge
name1 = sv.generate_tts_cached(TEXT, tenant_id=None)
print("\n[1] edge failed -> file:", name1[-16:])
print("    stored bytes  :", (CACHE / name1).read_bytes())
print("    edge attempts :", len(edge_calls))

# --- call 2: edge has recovered --------------------------------------------
tts_engines.synthesize_to_file = fake_edge_ok
name2 = sv.generate_tts_cached(TEXT, tenant_id=None)
print("\n[2] edge recovered -> file:", name2[-16:])
print("    edge attempts :", len(edge_calls), "(must be >=2: edge retried)")
print("    stored bytes  :", (CACHE / name2).read_bytes())

print()
if len(edge_calls) >= 2 and (CACHE / name2).read_bytes() == b"EDGE-NEURAL-AUDIO":
    print(">>> OK: edge was retried and the neural audio replaced the fallback.")
else:
    print(">>> STILL BROKEN: the gTTS fallback is pinned under the edge cache key.")
