#!/usr/bin/env python
import gi

gi.require_version("Notify", "0.7")
from gi.repository import Notify

Notify.init("gcalnoti")

import os.path
import datetime
import asyncio
import signal
import logging
import sys
import re
import math
import json
import argparse
import socket
import errno

# Setup logging
LOG_PATH = os.path.join(os.environ["HOME"], ".local", "log", "gcalnoti.log")
os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_PATH),
        logging.StreamHandler(),
    ],
)
logger = logging.getLogger(__name__)

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

# If modifying these scopes, delete the file TOKEN_PATH.
SCOPES = ["https://www.googleapis.com/auth/calendar.readonly"]
TOKEN_PATH = os.path.join(os.environ["HOME"], ".credentials", "gcalnoti.json")

calendars = []

app_name = "GCalNoti"
auth_error = "Failed to fetch calendars. Need to re-authenticate."
exit_message = "Exiting..."

notifier = None
service = None
conf = {}
conf_path = None


def filter_calendar(calendar, regexps):
    summary = calendar["summary"]
    return any(re.match(regexp, summary) for regexp in regexps)


def filter_calendars(calendars, regexps):
    return [
        calendar for calendar in calendars if not filter_calendar(calendar, regexps)
    ]


def fetch_calendar_list(service, conf):
    page_token = None
    new_calendars = []
    while True:
        calendar_list = service.calendarList().list(pageToken=page_token).execute()
        new_calendars.extend(calendar_list["items"])
        page_token = calendar_list.get("nextPageToken")
        if not page_token:
            break
    return new_calendars


def update_calendar_list(service, conf):
    global calendars

    try:
        new_calendars = fetch_calendar_list(service, conf)
    except Exception as e:
        # Failed to fetch the calendar list maybe due to the token
        # expiration. notify_upcoming_events() will notify, so we just
        # return in this function
        logger.error("Failed to fetch calendar list: %s", e)
        return

    ignore_patterns = conf.get("ignore", [])
    if ignore_patterns:
        new_calendars = filter_calendars(new_calendars, ignore_patterns)

    calendars = new_calendars


async def update_calendar_list_loop(service, conf):
    while True:
        update_calendar_list(service, conf)
        MIN = 60
        await asyncio.sleep(30 * MIN)


class Notifier:
    class Entry:
        def __init__(self, calendar, event):
            self.calendar = calendar
            self.event = event

    def __init__(self, conf):
        self.time = None
        self.notified_upcoming_events = {}
        self.evening_notify_done = False
        self.morning_notify_done = False
        self.update_conf(conf)
        self.reinit()

    def update_conf(self, conf):
        self.conf = conf
        self.morning_notify_hour = conf.get("morning_notify", 10)
        self.evening_notify_hour = conf.get("evening_notify", 21)
        self.morning_threshold = conf.get("morning_threshold", 15)
        # notify_before: list of minutes before event to notify (default: [5, 30, 60])
        self.notify_before = sorted(conf.get("notify_before", [5, 30, 60]))
        # ignore_events: list of regex patterns to ignore event titles
        self.ignore_events = conf.get("ignore_events", [])

    def reinit(self):
        self.events = []
        self.prev = self.time
        self.time = datetime.datetime.now().astimezone()
        if self.prev is None or self.time.day != self.prev.day:
            self.notified_upcoming_events = {}
            self.evening_notify_done = False
            self.morning_notify_done = False

    def __do_evening_notify(self):
        if self.evening_notify_done:
            return False
        if self.time.hour >= self.evening_notify_hour:
            logger.info("do evening notify")
            self.evening_notify_done = True
            return True
        return False

    def __do_morning_notify(self):
        if self.morning_notify_done:
            return False
        if self.time.hour >= self.morning_notify_hour:
            logger.info("do morning notify")
            self.morning_notify_done = True
            return True
        return False

    def __is_morning_event(self, event):
        start = event.event["start"]
        if "date" in start:
            # All day event
            return True
        elif "dateTime" in start:
            dateTime = datetime.datetime.fromisoformat(start["dateTime"])
            return dateTime.hour < self.morning_threshold
        else:
            return False

    def __is_today_event(self, event):
        start = event.event["start"]
        end = event.event["end"]
        today = datetime.date.today()
        if "date" in start:
            start_date = datetime.datetime.fromisoformat(start["date"])
            end_date = datetime.datetime.fromisoformat(end["date"])
            return start_date.date() <= today and today < end_date.date()
        elif "dateTime" in start:
            dateTime = datetime.datetime.fromisoformat(start["dateTime"])
            return dateTime.date() == today
        else:
            return False

    def __is_tomorrow_event(self, event):
        start = event.event["start"]
        end = event.event["end"]
        tomorrow = datetime.date.today() + datetime.timedelta(days=1)
        if "date" in start:
            start_date = datetime.datetime.fromisoformat(start["date"])
            end_date = datetime.datetime.fromisoformat(end["date"])
            return start_date.date() <= tomorrow and tomorrow < end_date.date()
        elif "dateTime" in start:
            dateTime = datetime.datetime.fromisoformat(start["dateTime"])
            return dateTime.hour < self.morning_threshold and dateTime.date() == tomorrow
        return False

    def __time_remaining(self, event):
        unknown = "Unknown"
        start = event.event["start"]
        if "date" in start:
            # Whole day event. Will be notified morning/evening notify
            return unknown
        elif "dateTime" in start:
            dateTime = datetime.datetime.fromisoformat(start["dateTime"])
            diff = dateTime.timestamp() - self.time.timestamp()
            max_notify = max(self.notify_before) if self.notify_before else 60
            if diff < 0 or diff > max_notify * 60:
                return unknown
            # Find the smallest threshold that diff falls under
            for minutes in self.notify_before:
                if diff < minutes * 60:
                    return f"{minutes} minutes"
            return unknown
        else:
            return unknown

    def __is_current_event(self, event):
        start = event.event["start"]
        if "date" in start:
            return False
        elif "dateTime" in start:
            dateTime = datetime.datetime.fromisoformat(start["dateTime"])
            diff = dateTime.timestamp() - self.time.timestamp()
            return diff < 5 * 60 and diff > -(5 * 60)
        else:
            return False

    def _notify_raw(self, title, msg):
        notify = Notify.Notification.new(title, msg)
        notify.show()

    def __is_already_notified_event(self, event, title):
        id = event.event["id"]
        if title not in self.notified_upcoming_events:
            return False
        if id not in self.notified_upcoming_events[title]:
            return False
        return True

    def __record_notified_event(self, event, title):
        id = event.event["id"]
        if title not in self.notified_upcoming_events:
            self.notified_upcoming_events[title] = set()
        self.notified_upcoming_events[title].add(id)

    def __notify_event(self, event, title):
        if self.__is_already_notified_event(event, title):
            return
        self.__record_notified_event(event, title)
        time = ""
        start = event.event["start"]
        if "dateTime" in start:
            dateTime = datetime.datetime.fromisoformat(start["dateTime"])
            time = " at " + dateTime.strftime("%H:%M")
        logger.info("Notify: %s - %s: %s", title, event.calendar, event.event["summary"])
        self._notify_raw(title, event.calendar + ": " + event.event["summary"] + time)

    def notify_foreach_event(self, should_notify_event):
        # XXX: should_notify_event is required to return a title.
        notified = False
        for event in self.events:
            ok, title = should_notify_event(event)
            if ok:
                self.__notify_event(event, title)
                notified = True
        return notified

    def notify(self):
        __do_morning_notify = self.__do_morning_notify()
        __do_evening_notify = self.__do_evening_notify()

        def should_notify_event(event):
            time_remaining = self.__time_remaining(event)
            conds = [
                (__do_evening_notify and self.__is_tomorrow_event(event), "Tomorrow"),
                (
                    __do_morning_notify
                    and self.__is_today_event(event)
                    and self.__is_morning_event(event),
                    "Today",
                ),
                (
                    time_remaining != "Unknown",
                    "Upcoming - " + time_remaining + " left",
                ),
                (self.__is_current_event(event), "Now"),
            ]
            # TODO: Ugly
            for cond, title in conds:
                if cond:
                    return True, title
            return False, ""

        self.notify_foreach_event(should_notify_event)

    def __format_event_time(self, event):
        start = event.event["start"]
        if "date" in start:
            return "All day"
        elif "dateTime" in start:
            dt = datetime.datetime.fromisoformat(start["dateTime"])
            return dt.strftime("%H:%M")
        return ""

    def show_events(self):
        now = datetime.datetime.now().astimezone()
        if now.hour >= self.evening_notify_hour:
            label = "Tomorrow"
            filtered_events = [e for e in self.events if self.__is_tomorrow_event(e)]
        else:
            label = "Today"
            filtered_events = [e for e in self.events if self.__is_today_event(e)]
        if not filtered_events:
            self._notify_raw(f"{label}'s Events", "No events")
            return

        # Sort by start time
        def get_start_time(e):
            start = e.event["start"]
            if "date" in start:
                # All-day event, put at the beginning
                return datetime.datetime.fromisoformat(start["date"]).replace(tzinfo=datetime.UTC)
            elif "dateTime" in start:
                return datetime.datetime.fromisoformat(start["dateTime"])
            return datetime.datetime.min.replace(tzinfo=datetime.UTC)

        try:
            filtered_events.sort(key=get_start_time)
        except Exception as e:
            logger.error("Failed to sort events: %s", e)

        lines = []
        for event in filtered_events:
            time_str = self.__format_event_time(event)
            summary = event.event.get("summary", "(No title)")
            lines.append(f"{time_str} - {summary}")

        # Split into chunks to avoid dunst truncation
        max_lines = 8
        total = len(filtered_events)
        for i in range(0, len(lines), max_lines):
            chunk = lines[i:i + max_lines]
            part = i // max_lines + 1
            parts = math.ceil(len(lines) / max_lines)
            if parts == 1:
                title = f"{label}'s Events ({total})"
            else:
                title = f"{label}'s Events ({total}) [{part}/{parts}]"
            self._notify_raw(title, "\n".join(chunk))

    def __should_ignore_event(self, event):
        event_summary = event.get("summary", "")
        for pattern in self.ignore_events:
            if re.search(pattern, event_summary):
                return True
        return False

    def extend_events(self, summary, events):
        for event in events:
            if not self.__should_ignore_event(event):
                self.events.append(Notifier.Entry(summary, event))


def fetch_events(service, notifier, now):
    # Use local midnight as timeMin so early morning events are not missed
    local_today = datetime.datetime.now().astimezone().replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    time_min = local_today.isoformat()
    for calendar in calendars:
        summary = calendar["summary"]
        events_result = (
            service.events()
            .list(
                calendarId=calendar["id"],
                timeMin=time_min,
                maxResults=20,
                singleEvents=True,
                orderBy="startTime",
            )
            .execute()
        )
        events = events_result.get("items")
        notifier.extend_events(summary, events)


async def notify_upcoming_events(service, conf):
    global notifier
    while True:
        now = datetime.datetime.now(datetime.UTC)
        logger.info("Check event at %s", now)
        notifier.reinit()
        retrieve_failed = False
        try:
            fetch_events(service, notifier, now)
        except Exception as e:
            logger.error("Failed to fetch events: %s", e)
            notifier._notify_raw(app_name, auth_error)
            retrieve_failed = True
        notifier.notify()
        logger.info("notification done")

        # Recalculate now
        now_ts = datetime.datetime.now(datetime.UTC).timestamp()
        period_min = (5 if not retrieve_failed else 300)
        period_sec = 60 * period_min

        def calc_until(now, period):
            return (period * (math.floor(now / period))) + period

        until_ts = calc_until(now_ts, period_sec)
        waiting_ts = until_ts - now_ts
        logger.debug("Will check again after %s seconds", waiting_ts)
        await asyncio.sleep(waiting_ts)


def handle_exit(args):
    # TODO
    pass


def handle_today(args):
    logger.info("Show events")
    global notifier
    if notifier is None:
        return
    notifier.show_events()


def handle_command(command):
    command_tables = {"exit": handle_exit, "today": handle_today}
    toks = command.split(maxsplit=1)
    if len(toks) < 1:
        return
    cmd, args = toks[0], toks[1:]
    if cmd in command_tables:
        command_tables[cmd](args)


async def poll_command(service, conf):
    sock_path = conf.get("socket_path", "/tmp/gcalnoti.socket")

    try:
        if os.path.exists(sock_path):
            os.unlink(sock_path)
    except OSError:
        if os.path.exists(sock_path):
            raise

    with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as sock:
        sock.setblocking(False)
        sock.bind(sock_path)
        logger.info("receiving commands from %s", sock_path)
        loop = asyncio.get_event_loop()
        while True:
            readable = asyncio.Event()
            loop.add_reader(sock.fileno(), readable.set)
            try:
                await readable.wait()
            finally:
                loop.remove_reader(sock.fileno())
            try:
                command, _ = sock.recvfrom(4096)
            except OSError as e:
                err = e.args[0]
                if err == errno.EAGAIN or err == errno.EWOULDBLOCK:
                    continue
                else:
                    logger.error("Socket error: %s", e)
                    sys.exit(1)
            else:
                command = command.decode("utf-8")
                command = command.strip()
                handle_command(command)


async def coroutine_gather(service, conf):
    coroutines = [
        update_calendar_list_loop(service, conf),
        notify_upcoming_events(service, conf),
        poll_command(service, conf),
    ]
    return await asyncio.gather(*coroutines, return_exceptions=True)


def init_notifier(conf):
    global notifier
    notifier = Notifier(conf)


def notification_loop(svc, conf):
    global service
    service = svc
    init_notifier(conf)
    try:
        asyncio.run(coroutine_gather(service, conf))
    except Exception as e:
        logger.error("Exception in notification loop: %s", e)


def load_conf(filename):
    if not isinstance(filename, str):
        return {}
    with open(filename) as f:
        conf = json.load(f)
        return conf


def exit_callback(signum, frame):
    signame = ""
    if signum is not None:
        signame = " ({})".format(signal.Signals(signum).name)
    notify = Notify.Notification.new(app_name, exit_message + signame)
    notify.show()
    exit(0)


def reload_conf_callback(signum, frame):
    global conf, conf_path, notifier, service
    logger.info("Reloading config...")
    try:
        new_conf = load_conf(conf_path)
        conf = new_conf
        if notifier is not None:
            notifier.update_conf(conf)
        if service is not None:
            update_calendar_list(service, conf)
        notify = Notify.Notification.new(app_name, "Config reloaded")
        notify.show()
        logger.info("Config reloaded successfully")
    except Exception as e:
        logger.error("Failed to reload config: %s", e)
        notify = Notify.Notification.new(app_name, f"Config reload failed: {e}")
        notify.show()


def main():
    """Notify upcoming events from Google calendars"""
    global conf, conf_path

    parser = argparse.ArgumentParser(
        description="Notify upcoming events from Google calendars"
    )
    parser.add_argument("--conf", action="store", help="JSON configuration")
    args = parser.parse_args()

    conf_path = args.conf
    conf = load_conf(conf_path)

    creds = None
    # The file TOKEN_PATH stores the user's access and refresh
    # tokens, and is created automatically when the authorization flow
    # completes for the first time.
    if os.path.exists(TOKEN_PATH):
        creds = Credentials.from_authorized_user_file(TOKEN_PATH, SCOPES)
    # If there are no (valid) credentials available, let the user log in.
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            try:
                creds.refresh(Request())
            except Exception as e:
                logger.error("Failed to refresh credentials: %s", e)
                notify = Notify.Notification.new(app_name, auth_error)
                notify.show()
                exit(-1)
        else:
            flow = InstalledAppFlow.from_client_secrets_file(
                "./client_secret.json", SCOPES
            )
            creds = flow.run_local_server(port=0)
        # Save the credentials for the next run
        with open(TOKEN_PATH, "w") as token:
            token.write(creds.to_json())

    try:
        service = build("calendar", "v3", credentials=creds)
        notification_loop(service, conf)
    except HttpError as err:
        logger.error("HTTP error: %s", err)


if __name__ == "__main__":
    exit_signals = [
        signal.SIGINT,
        signal.SIGUSR1,
        signal.SIGUSR2,
        signal.SIGTERM,
    ]
    for sign in exit_signals:
        signal.signal(sign, exit_callback)
    signal.signal(signal.SIGHUP, reload_conf_callback)
    main()
