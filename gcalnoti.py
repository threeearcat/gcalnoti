#!/usr/bin/env python
import gi

gi.require_version("Notify", "0.7")
from gi.repository import Notify

Notify.init("gcalnoti")

import os.path
import datetime
import asyncio
import signal

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


def filter_calendar(calendar, regexps):
    import re

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
        print(e)
        return

    if "ignore" in conf:
        new_calendars = filter_calendars(new_calendars, conf["ignore"])

    calendars = new_calendars
    # for calendar in new_calendars:
    #     print(calendar['summary'])


async def update_calendar_list_loop(service, conf):
    while True:
        update_calendar_list(service, conf)
        MIN = 60
        await asyncio.sleep(30 * MIN)


# TODO: timezone
class Notifier:
    class Entry:
        def __init__(self, calendar, event):
            self.calendar = calendar
            self.event = event

    def __init__(self, conf):
        self.time = datetime.datetime.now()
        self.evening_notify_done = False
        self.morning_notify_done = False
        self.events = []
        self.morning_notify_hour = (
            10 if not "morning_notify" in conf else conf["morning_notify"]
        )
        self.evening_notify_hour = (
            21 if not "evening_notify" in conf else conf["evening_notify"]
        )
        self.reminder_threshold = (
            15 if not "reminder_threshold" in conf else conf["reminder_threshold"]
        )

    def reinit(self):
        self.events = []
        self.prev = self.time
        self.time = datetime.datetime.now()
        if self.time.day != self.prev.day:
            self.evening_notify_done = False
            self.morning_notify_done = False

    def __do_evening_notify(self):
        if self.evening_notify_done:
            return False
        if self.time.hour >= self.evening_notify_hour:
            print("do evening notify")
            self.evening_notify_done = True
            return True
        return False

    def __do_morning_notify(self):
        if self.morning_notify_done:
            return False
        if self.time.hour >= self.morning_notify_hour:
            print("do morning notify")
            self.morning_notify_done = True
            return True
        return False

    def __is_early_event(self, event):
        start = event.event["start"]
        if "date" in start:
            # All day event
            return True
        elif "dateTime" in start:
            dateTime = datetime.datetime.fromisoformat(start["dateTime"])
            return dateTime.hour < self.reminder_threshold
        else:
            return False

    def __is_today_event(self, event):
        start = event.event["start"]
        end = event.event["end"]
        today = datetime.date.today()
        if "date" in start:
            start_date = datetime.datetime.fromisoformat(start["date"])
            end_date = datetime.datetime.fromisoformat(end["date"])
            return start_date.date() <= today and today <= end_date.date()
        elif "dateTime" in start:
            dateTime = datetime.datetime.fromisoformat(start["dateTime"])
            return dateTime.date() == today
        else:
            return False

    def __is_tomorrow_event(self, event):
        # TODO: Refactoring
        start = event.event["start"]
        end = event.event["end"]
        tomorrow = datetime.date.today() + datetime.timedelta(days=1)
        if "date" in start:
            start_date = datetime.datetime.fromisoformat(start["date"])
            end_date = datetime.datetime.fromisoformat(end["date"])
            return start_date.date() <= tomorrow and tomorrow <= end_date.date()
        elif "dateTime" in start:
            dateTime = datetime.datetime.fromisoformat(start["dateTime"])
            if dateTime.hour < self.reminder_threshold and dateTime.date() == tomorrow:
                return True
        else:
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
            if diff < 5 * 60 or diff > 1 * 60 * 60:
                return unknown
            elif diff < 30 * 60:
                return "30 minutes"
            else:
                return "1 hour"
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

    def __notify_raw(self, title, msg):
        notify = Notify.Notification.new(title, msg)
        notify.show()

    def __notify_event(self, event, title):
        time = ""
        start = event.event["start"]
        if "dateTime" in start:
            dateTime = datetime.datetime.fromisoformat(start["dateTime"])
            time = " at " + dateTime.strftime("%H:%M")
        print(title, event.calendar, event.event["summary"])
        self.__notify_raw(title, event.calendar + ": " + event.event["summary"] + time)

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
                    and self.__is_early_event(event),
                    "Today",
                ),
                (
                    time_remaining != "Unknown",
                    "Upcomming - " + time_remaining + " left",
                ),
                (self.__is_current_event(event), "Now"),
            ]
            # TODO: Ugly
            for cond, title in conds:
                if cond:
                    return True, title
            return False, ""

        self.notify_foreach_event(should_notify_event)

    def remind_events(self):
        def should_remind_event(event):
            now = datetime.datetime.now()
            if now.hour >= self.evening_notify_hour:
                return self.__is_tomorrow_event(event), "Tomorrow"
            else:
                return self.__is_today_event(event), "Today"

        notified = self.notify_foreach_event(should_remind_event)
        if not notified:
            self.__notify_raw("Reminder", "Your day is clear")

    def extend_events(self, summary, events):
        for event in events:
            self.events.append(Notifier.Entry(summary, event))


def fetch_events(service, notifier, now):
    today = now.replace(hour=0, minute=0, second=0, microsecond=0)
    today_utc = today.isoformat() + "Z"  # 'Z' indicates UTC time
    for calendar in calendars:
        summary = calendar["summary"]
        events_result = (
            service.events()
            .list(
                calendarId=calendar["id"],
                timeMin=today_utc,
                maxResults=10,
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
        now = datetime.datetime.utcnow()
        print("Check event at", now)
        notifier.reinit()
        retrieve_failed = False
        try:
            fetch_events(service, notifier, now)
        except Exception:
            notifier.__notify_raw(app_name, auth_error)
            retrieve_failed = True
        notifier.notify()
        print("notification done")

        # Recalculate now
        now_ts = datetime.datetime.utcnow().timestamp()
        period = 60 * (30 if not retrieve_failed else 300)

        def calc_until(now, period):
            import math

            return (period * (math.floor(now / period))) + period

        until_ts = calc_until(now_ts, period)
        print("Will check again after", until_ts - now_ts)
        await asyncio.sleep(until_ts - now_ts)


def handle_exit(args):
    # TODO
    pass


def handle_remind(args):
    # TODO
    print("Remind today events")
    global notifier
    if notifier == None:
        return
    notifier.remind_events()


def handle_command(command):
    command_tables = {"exit": handle_exit, "remind": handle_remind}
    toks = command.split(maxsplit=1)
    if len(toks) < 1:
        return
    cmd, args = toks[0], toks[1:]
    if cmd in command_tables:
        command_tables[cmd](args)


async def poll_command(service, conf):
    import socket

    sock_path_config = "socket_path"
    sock_path_default = "/tmp/gcalnoti.socket"

    sock_path = (
        conf[sock_path_config] if sock_path_config in conf else sock_path_default
    )

    try:
        if os.path.exists(sock_path):
            os.unlink(sock_path)
    except OSError:
        if os.path.exists(sock_path):
            raise

    with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as sock:
        sock.bind(sock_path)
        print("receiving commands from {}".format(sock_path))
        while True:
            command, _ = sock.recvfrom(4096)
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


def notification_loop(service, conf):
    init_notifier(conf)
    try:
        asyncio.run(coroutine_gather(service, conf))
    except Exception as e:
        print(e)


def load_conf(filename):
    import json

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


def main():
    """Notify upcoming events from Google calendars"""
    import argparse

    parser = argparse.ArgumentParser(
        description="Notify upcoming events from Google calendars"
    )
    parser.add_argument("--conf", action="store", help="JSON configuration")
    args = parser.parse_args()

    conf = load_conf(args.conf)

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
                print(e)
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
        print(err)


if __name__ == "__main__":
    signals = [
        signal.SIGINT,
        signal.SIGUSR1,
        signal.SIGUSR2,
        signal.SIGTERM,
    ]
    for sign in signals:
        signal.signal(sign, exit_callback)
    main()
