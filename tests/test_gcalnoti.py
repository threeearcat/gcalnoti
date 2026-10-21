import datetime
import logging
import unittest
from unittest import mock

import gi
import httplib2
from google.auth.exceptions import RefreshError, TransportError
from googleapiclient.errors import HttpError

gi.require_version("Notify", "0.7")
from gi.repository import Notify

# Import without opening the user's log or initializing desktop notifications.
with (
    mock.patch.object(Notify, "init"),
    mock.patch("os.makedirs"),
    mock.patch("logging.FileHandler", return_value=logging.NullHandler()),
    mock.patch("logging.basicConfig"),
):
    import gcalnoti


def http_error(status):
    return HttpError(httplib2.Response({"status": str(status)}), b"{}")


class StopPolling(Exception):
    pass


class AuthErrorTests(unittest.TestCase):
    def test_only_authentication_failures_require_reauthentication(self):
        cases = [
            (http_error(401), True),
            (RefreshError("invalid_grant"), True),
            (RefreshError("temporarily_unavailable", retryable=True), False),
            (TransportError("connection timed out"), False),
            (BrokenPipeError(32, "Broken pipe"), False),
            (ConnectionResetError(104, "Connection reset by peer"), False),
            (TimeoutError("The read operation timed out"), False),
            (http_error(403), False),
            (http_error(429), False),
            (http_error(503), False),
        ]
        for error, expected in cases:
            with self.subTest(error=repr(error)):
                self.assertEqual(gcalnoti.is_auth_error(error), expected)


class PollingTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.notifier = mock.Mock(spec=gcalnoti.Notifier)
        self.notifier_patch = mock.patch.object(gcalnoti, "notifier", self.notifier)
        self.notifier_patch.start()
        self.addCleanup(self.notifier_patch.stop)
        logger_patch = mock.patch.object(gcalnoti, "logger")
        logger_patch.start()
        self.addCleanup(logger_patch.stop)

    async def poll_events(self, outcomes):
        delays = []

        async def sleep(delay):
            delays.append(delay)
            if len(delays) == len(outcomes):
                raise StopPolling

        now = datetime.datetime(2026, 10, 21, 11, 11, 30, tzinfo=datetime.UTC)
        with (
            mock.patch.object(gcalnoti, "fetch_events", side_effect=outcomes),
            mock.patch.object(gcalnoti.asyncio, "sleep", side_effect=sleep),
            mock.patch.object(gcalnoti, "datetime", wraps=datetime) as clock,
        ):
            clock.datetime.now.return_value = now
            with self.assertRaises(StopPolling):
                await gcalnoti.notify_upcoming_events(mock.Mock(), {})
        return delays

    async def test_network_failure_retries_in_one_minute_and_recovers(self):
        delays = await self.poll_events([
            BrokenPipeError(32, "Broken pipe"),
            ConnectionResetError(104, "Connection reset by peer"),
            TimeoutError("The read operation timed out"),
            None,
        ])
        self.assertEqual(delays, [60, 60, 60, 210])
        self.notifier._notify_raw.assert_not_called()
        # Failed fetches must not consume the morning/evening notification flags.
        self.notifier.notify.assert_called_once_with()

    async def test_temporary_google_errors_do_not_prompt_reauthentication(self):
        delays = await self.poll_events([
            RefreshError("temporarily_unavailable", retryable=True),
            TransportError("connection timed out"),
            http_error(403),
            http_error(429),
            http_error(503),
        ])
        self.assertEqual(delays, [60] * 5)
        self.notifier._notify_raw.assert_not_called()
        self.notifier.notify.assert_not_called()

    async def test_auth_alert_is_once_per_failure_until_recovery(self):
        delays = await self.poll_events([
            RefreshError("invalid_grant"),
            http_error(401),
            None,
            http_error(401),
        ])
        self.assertEqual(delays, [300, 300, 210, 300])
        self.assertEqual(self.notifier._notify_raw.call_count, 2)
        self.notifier._notify_raw.assert_called_with(
            gcalnoti.app_name, gcalnoti.auth_error
        )
        self.notifier.notify.assert_called_once_with()

    async def test_calendar_list_failure_retries_before_normal_interval(self):
        delays = []
        calendars = [{"id": "calendar-id", "summary": "Calendar"}]

        async def sleep(delay):
            delays.append(delay)
            if len(delays) == 2:
                raise StopPolling

        with (
            mock.patch.object(gcalnoti, "calendars", calendars),
            mock.patch.object(gcalnoti, "fetch_calendar_list", side_effect=[
                TimeoutError("timed out"), calendars,
            ]),
            mock.patch.object(gcalnoti.asyncio, "sleep", side_effect=sleep),
        ):
            with self.assertRaises(StopPolling):
                await gcalnoti.update_calendar_list_loop(mock.Mock(), {})
            self.assertEqual(gcalnoti.calendars, calendars)
        self.assertEqual(delays, [60, 1800])


class StartupTests(unittest.TestCase):
    def run_main(self, refresh_error):
        creds = mock.Mock(valid=False, expired=True, refresh_token="test-token")
        creds.refresh.side_effect = refresh_error
        with (
            mock.patch.object(gcalnoti, "logger"),
            mock.patch.object(gcalnoti, "conf"),
            mock.patch.object(gcalnoti, "conf_path"),
            mock.patch.object(gcalnoti, "load_conf", return_value={}),
            mock.patch.object(gcalnoti.sys, "argv", ["gcalnoti.py"]),
            mock.patch.object(gcalnoti.os.path, "exists", return_value=True),
            mock.patch.object(gcalnoti.Credentials, "from_authorized_user_file",
                              return_value=creds),
            mock.patch.object(gcalnoti, "Request"),
            mock.patch.object(gcalnoti.Notify.Notification, "new") as notify,
            mock.patch.object(gcalnoti, "build") as build,
            mock.patch.object(gcalnoti, "notification_loop") as loop,
            mock.patch.object(gcalnoti, "open", create=True) as token_file,
        ):
            if gcalnoti.is_auth_error(refresh_error):
                with self.assertRaises(SystemExit):
                    gcalnoti.main()
                notify.assert_called_once_with(gcalnoti.app_name, gcalnoti.auth_error)
                build.assert_not_called()
                loop.assert_not_called()
            else:
                gcalnoti.main()
                notify.assert_not_called()
                build.assert_called_once_with("calendar", "v3", credentials=creds)
                loop.assert_called_once_with(build.return_value, {})
            token_file.assert_not_called()

    def test_temporary_refresh_failure_keeps_polling_without_overwriting_token(self):
        for error in (
            TransportError("connection timed out"),
            RefreshError("temporarily_unavailable", retryable=True),
        ):
            with self.subTest(error=repr(error)):
                self.run_main(error)

    def test_permanent_refresh_failure_prompts_reauthentication(self):
        self.run_main(RefreshError("invalid_grant"))


if __name__ == "__main__":
    unittest.main()
