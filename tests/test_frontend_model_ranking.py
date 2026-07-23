import json
import subprocess
import tempfile
from pathlib import Path


APP_JS = Path(__file__).parents[1] / "assets" / "app.js"


def test_frontend_model_normalization_handles_provider_prefixes() -> None:
    source = APP_JS.read_text(encoding="utf-8")

    assert 'name = name.replace(/^[A-Za-z][A-Za-z0-9_-]*\\//, "");' in source

    script = f"""
const source = {json.dumps(source)};
const match = source.match(/function normalizeModelKey\\(model\\) {{[\\s\\S]*?\\n}}/);
if (!match) process.exit(2);
eval(match[0]);
console.log(JSON.stringify([
  normalizeModelKey('anthropic.claude-opus-4-8'),
  normalizeModelKey('claude-opus-4-8'),
  normalizeModelKey('bedrock/anthropic.claude-opus-4-8'),
  normalizeModelKey('bedrock/chatgpt-acct-84-anthropic.claude-opus-4-8'),
  normalizeModelKey('bedrock/claude-opus-4-7'),
  normalizeModelKey('')
]));
"""
    with tempfile.NamedTemporaryFile("w", suffix=".js", encoding="utf-8", delete=False) as handle:
        handle.write(script)
        script_path = handle.name
    try:
        result = subprocess.run(["node", script_path], capture_output=True, text=True, check=True)
    finally:
        Path(script_path).unlink(missing_ok=True)
    assert json.loads(result.stdout) == [
        "claude-opus-4-8",
        "claude-opus-4-8",
        "claude-opus-4-8",
        "claude-opus-4-8",
        "claude-opus-4-7",
        "",
    ]


def test_frontend_model_normalization_keeps_model_body_specific() -> None:
    source = APP_JS.read_text(encoding="utf-8")

    # The display helper still shortens only after normalization; model names
    # remain specific and are not matched by fuzzy substrings.
    assert "const separatorIndex = normalized.lastIndexOf(\"/\")" in source
    assert "slice(separatorIndex + 1)" in source
