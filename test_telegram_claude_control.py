"""Tests for the agent-inbox consumer of telegram-claude-control.py."""
import importlib.util
import os
import pathlib
import tempfile
import unittest

TMP = tempfile.TemporaryDirectory()
CONFIG = pathlib.Path(TMP.name) / "env"
CONFIG.write_text("BOT_TOKEN=x\nPAIR_CODE=y\n")
os.environ.update(TELEGRAM_CLAUDE_CONFIG=str(CONFIG), TELEGRAM_CLAUDE_JOBS_DB=f"{TMP.name}/jobs.db",
                  TELEGRAM_CLAUDE_STATE=f"{TMP.name}/state.json")
MODULE_PATH = pathlib.Path(__file__).with_name("telegram-claude-control.py")
SPEC = importlib.util.spec_from_file_location("telegram_claude_control", MODULE_PATH)
controller = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(controller)
AGENT_INBOX = pathlib.Path(__file__).resolve().parent.parent / "agent-fleet" / "orchestrator" / "agent_inbox.py"


@unittest.skipUnless(AGENT_INBOX.exists(), "agent-fleet's agent_inbox.py is not checked out beside this repo")
class AgentInboxTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        old = os.environ.get("AGENT_INBOX_DIR")
        os.environ["AGENT_INBOX_DIR"] = self.tmp.name + "/inbox"
        self.addCleanup(lambda: os.environ.pop("AGENT_INBOX_DIR") if old is None else os.environ.update(AGENT_INBOX_DIR=old))
        self.inbox = controller.load_agent_inbox(str(AGENT_INBOX))
        self.sent, self.runs = [], []
        saved = (controller.INBOX, controller.INBOX_TOPIC, controller.reply, controller.CLAUDE.run)
        self.addCleanup(self._restore, saved)
        controller.INBOX, controller.INBOX_TOPIC = self.inbox, "claude_pellow"
        controller.reply = lambda chat_id, text, thread_id=None, **kw: self.sent.append((chat_id, text))

        def run(prompt, conversation_id=None, notify=None):
            self.runs.append((prompt, conversation_id))
            return "pong"
        controller.CLAUDE.run = run

    def _restore(self, saved):
        controller.INBOX, controller.INBOX_TOPIC, controller.reply, controller.CLAUDE.run = saved

    def test_message_runs_and_its_answer_goes_to_the_reply_topic(self):
        self.inbox.post("claude_pellow", "reply with the word pong", sender="crystal-clear", reply_to="crystal-clear")
        controller.take_inbox({"chat_id": 42})
        self.assertEqual(self.inbox.pending("claude_pellow"), [])
        ((prompt, conversation),) = self.runs
        self.assertIn("Message from agent crystal-clear (Unix user ", prompt)
        self.assertTrue(prompt.endswith("reply with the word pong"))
        self.assertEqual(conversation, "inbox:crystal-clear")  # not the operator's conversation
        self.assertEqual([chat for chat, _ in self.sent], [42, 42])
        self.assertIn("📬 Answer to crystal-clear:\npong", self.sent[1][1])
        (answer,) = [self.inbox.read(p) for p in self.inbox.pending("crystal-clear")]
        self.assertEqual((answer["text"], answer["sender"], answer["kind"]), ("pong", "claude_pellow", "peer"))

    def test_failure_is_answered_and_unpaired_is_silent(self):
        def boom(prompt, conversation_id=None, notify=None):
            raise RuntimeError("quota")
        controller.CLAUDE.run = boom
        self.inbox.post("claude_pellow", "x", sender="a", reply_to="a")
        controller.take_inbox({})
        self.assertEqual(self.sent, [])
        (answer,) = [self.inbox.read(p) for p in self.inbox.pending("a")]
        self.assertTrue(answer["text"].startswith("Request failed:"))


if __name__ == "__main__":
    unittest.main()
