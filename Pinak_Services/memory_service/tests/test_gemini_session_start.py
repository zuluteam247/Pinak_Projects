"""Fixture-level hook output; not proof of a real client's behavior."""
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def run_hook(payload):
    return subprocess.run([sys.executable, str(ROOT / "scripts/gemini_session_start.py")],
                          input=json.dumps(payload), text=True, capture_output=True, timeout=5)


def test_gemini_hook_emits_valid_session_start_context():
    result = run_hook({"hook_event_name": "SessionStart", "source": "startup", "session_id": "fixture"})
    assert result.returncode == 0 and not result.stderr
    output = json.loads(result.stdout)
    assert output["hookSpecificOutput"]["hookEventName"] == "SessionStart"
    assert "call the pinak-memory recall tool once" in output["hookSpecificOutput"]["additionalContext"]
    assert "untrusted data" in output["hookSpecificOutput"]["additionalContext"]
    assert "fixture" not in result.stdout


def test_other_event_and_bad_input_do_not_inject_context():
    for payload in ({"hook_event_name": "BeforeTool", "source": "startup"},
                    {"hook_event_name": "SessionStart", "source": "unrecognized"}):
        result = run_hook(payload)
        assert result.returncode == 0 and json.loads(result.stdout) == {}
    result = subprocess.run([sys.executable, str(ROOT / "scripts/gemini_session_start.py")],
                            input="not-json", text=True, capture_output=True, timeout=5)
    assert result.returncode == 1 and result.stdout == ""


def test_example_is_opt_in_and_syntactically_valid():
    example = json.loads((ROOT / "examples/gemini-cli/settings-session-start.example.json").read_text())
    hooks = example["hooks"]["SessionStart"]
    assert hooks[0]["matcher"] == "startup"
    assert "ABSOLUTE/PATH" in hooks[0]["hooks"][0]["command"]
