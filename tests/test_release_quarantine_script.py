"""release_quarantine.py must refuse to touch the state file while the bot
holds its exclusive lock (L7, Nov 2026 audit)."""

import importlib.util
from pathlib import Path

from alphalive.state import INTENT_UNCERTAIN, BotState

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "release_quarantine.py"


def _script():
    spec = importlib.util.spec_from_file_location("release_quarantine", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _quarantined_state(path):
    st = BotState(str(path))
    intent = st.create_submission_intent("AAPL", "BUY", 1.0, "market")
    st.update_submission_intent(
        intent["intent_id"], status=INTENT_UNCERTAIN, is_quarantine=True
    )
    st.save()
    return intent["intent_id"]


def test_refuses_while_the_bot_holds_the_lock(tmp_path, capsys):
    sf = tmp_path / "state.json"
    iid = _quarantined_state(sf)
    bot = BotState(str(sf), exclusive=True)  # the running bot
    try:
        rc = _script().main(
            [
                "--state-file",
                str(sf),
                "--release",
                iid,
                "--operator",
                "me",
                "--note",
                "checked",
            ]
        )
        assert rc == 3
        assert "locked" in capsys.readouterr().err
    finally:
        bot.close()
    assert BotState(str(sf)).list_open_intents()[0]["status"] == INTENT_UNCERTAIN


def test_releases_when_the_bot_is_stopped(tmp_path):
    sf = tmp_path / "state.json"
    iid = _quarantined_state(sf)
    rc = _script().main(
        [
            "--state-file",
            str(sf),
            "--release",
            iid,
            "--operator",
            "me",
            "--note",
            "checked",
        ]
    )
    assert rc == 0
    assert BotState(str(sf)).list_open_intents() == []
