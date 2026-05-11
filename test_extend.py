"""
Tests for /extend command in tech-support-bot.

Verifies that the admin can subtract subscription time by passing negative
days (e.g. /extend 681325220 base -30) while preserving the original positive-
days flow. Runs without installing real telebot/requests/dotenv via sys.modules
injection.

Run: python3 test_extend.py
"""
import os
import sys
import unittest
from unittest.mock import MagicMock

# --- Required env BEFORE importing main ---
os.environ['BOT_TOKEN_SUPPORT'] = 'test_token'
os.environ['ADMIN_IDS'] = '111,222'
os.environ['API_URL_SUPPORT'] = 'http://test/api'
os.environ['SUPPORT_API_URL'] = 'http://test/support'

# --- Mock third-party libs that aren't installed in this venv ---
def _identity_decorator(*args, **kwargs):
    def wrapper(fn):
        return fn
    return wrapper

_bot_instance = MagicMock()
_bot_instance.message_handler = _identity_decorator
_bot_instance.callback_query_handler = _identity_decorator

_telebot_mock = MagicMock()
_telebot_mock.TeleBot.return_value = _bot_instance
sys.modules['telebot'] = _telebot_mock
sys.modules['telebot.types'] = MagicMock()

sys.modules['dotenv'] = MagicMock()
sys.modules['requests'] = MagicMock()

# Now safe to import main
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import main  # noqa: E402


def make_message(text, user_id=111):
    msg = MagicMock()
    msg.text = text
    msg.from_user.id = user_id
    msg.chat.id = user_id
    return msg


def reply_text(mock_reply):
    """Extract the text argument from the most recent bot.reply_to call."""
    return mock_reply.call_args.args[1]


class TestExtendCommand(unittest.TestCase):

    def setUp(self):
        # Reset call history on shared mocks
        main.bot.reset_mock()
        main.requests.reset_mock()

        # Default: API PATCH succeeds
        ok_resp = MagicMock()
        ok_resp.status_code = 200
        ok_resp.text = "ok"
        main.requests.patch.return_value = ok_resp

        # Default: payments-log POST succeeds
        post_resp = MagicMock()
        post_resp.status_code = 200
        main.requests.post.return_value = post_resp

    # ------------ Positive days ------------

    def test_positive_30_days_calls_api_with_positive_days(self):
        msg = make_message("/extend 681325220 base 30")
        main.handle_extend(msg)

        main.requests.patch.assert_called_once()
        url = main.requests.patch.call_args.args[0]
        body = main.requests.patch.call_args.kwargs['json']
        self.assertIn('/681325220/extend', url)
        self.assertEqual(body, {"days": 30, "plan": "base"})

    def test_positive_days_reply_says_prolonged(self):
        msg = make_message("/extend 681325220 base 30")
        main.handle_extend(msg)
        text = reply_text(main.bot.reply_to)
        self.assertIn("продлена", text)
        self.assertIn("+30", text)

    # ------------ Negative days (NEW BEHAVIOR) ------------

    def test_negative_30_days_calls_api_with_negative_days(self):
        """Главный тест: /extend ... base -30 шлёт {days: -30} в API."""
        msg = make_message("/extend 681325220 base -30")
        main.handle_extend(msg)

        main.requests.patch.assert_called_once()
        body = main.requests.patch.call_args.kwargs['json']
        self.assertEqual(body, {"days": -30, "plan": "base"})

    def test_negative_days_reply_says_shortened(self):
        msg = make_message("/extend 681325220 base -30")
        main.handle_extend(msg)
        text = reply_text(main.bot.reply_to)
        self.assertIn("сокращена", text)
        self.assertIn("-30", text)

    def test_negative_days_payments_log_propagates_sign(self):
        """Лог в /internal/payments должен иметь days_added: -30."""
        msg = make_message("/extend 681325220 base -30")
        main.handle_extend(msg)

        main.requests.post.assert_called_once()
        body = main.requests.post.call_args.kwargs['json']
        self.assertEqual(body['days_added'], -30)
        self.assertEqual(body['source'], 'admin_extend')
        self.assertEqual(body['telegram_id'], 681325220)
        self.assertEqual(body['plan'], 'base')

    def test_negative_days_works_for_bs_plan(self):
        """Минус дни работают для плана bsfamily."""
        msg = make_message("/extend 681325220 bsfamily -7")
        main.handle_extend(msg)
        body = main.requests.patch.call_args.kwargs['json']
        self.assertEqual(body, {"days": -7, "plan": "bsfamily"})

    # ------------ Zero is rejected ------------

    def test_zero_days_is_rejected_without_api_call(self):
        msg = make_message("/extend 681325220 base 0")
        main.handle_extend(msg)
        main.requests.patch.assert_not_called()
        text = reply_text(main.bot.reply_to)
        self.assertIn("0", text)
        self.assertIn("Ошибка", text)

    # ------------ Argument validation ------------

    def test_wrong_arg_count_shows_usage_with_negative_example(self):
        msg = make_message("/extend 681325220 base")  # missing days
        main.handle_extend(msg)
        main.requests.patch.assert_not_called()
        text = reply_text(main.bot.reply_to)
        # Usage hint must now mention negative-days example
        self.assertIn("снять 30 дней", text)
        self.assertIn("-30", text)

    def test_invalid_plan_rejected(self):
        msg = make_message("/extend 681325220 garbage 10")
        main.handle_extend(msg)
        main.requests.patch.assert_not_called()
        text = reply_text(main.bot.reply_to)
        self.assertIn("Неизвестный план", text)

    def test_invalid_tg_id_rejected(self):
        msg = make_message("/extend abc base 10")
        main.handle_extend(msg)
        main.requests.patch.assert_not_called()
        text = reply_text(main.bot.reply_to)
        self.assertIn("Telegram ID", text)

    def test_non_integer_days_rejected(self):
        msg = make_message("/extend 681325220 base abc")
        main.handle_extend(msg)
        main.requests.patch.assert_not_called()
        text = reply_text(main.bot.reply_to)
        # int("abc") raises ValueError → caught at the except branch
        self.assertIn("Ошибка", text)

    # ------------ API error paths ------------

    def test_api_404_user_not_found(self):
        not_found = MagicMock()
        not_found.status_code = 404
        not_found.text = "User not found"
        main.requests.patch.return_value = not_found

        msg = make_message("/extend 999999 base -30")
        main.handle_extend(msg)
        main.requests.patch.assert_called_once()
        text = reply_text(main.bot.reply_to)
        self.assertIn("не найден", text)

    def test_api_500_other_error(self):
        err = MagicMock()
        err.status_code = 500
        err.text = "boom"
        main.requests.patch.return_value = err

        msg = make_message("/extend 681325220 base -30")
        main.handle_extend(msg)
        text = reply_text(main.bot.reply_to)
        self.assertIn("Ошибка", text)
        self.assertIn("boom", text)


if __name__ == '__main__':
    unittest.main(verbosity=2)
