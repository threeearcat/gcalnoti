#!/usr/bin/env python
import gi
gi.require_version('Notify', '0.7')
from gi.repository import Notify
Notify.init('gcalnoti')

import os.path
import datetime
import asyncio

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

# If modifying these scopes, delete the file TOKEN_PATH.
SCOPES = ['https://www.googleapis.com/auth/calendar.readonly']
TOKEN_PATH = os.path.join(os.environ['HOME'], '.credentials', 'gcalnoti.json')

calendars = []

async def update_calendar_list(service):
    global calendars
    while True:
        page_token = None
        new_calendars = []
        while True:
            calendar_list = service.calendarList().list(
                pageToken=page_token).execute()
            new_calendars.extend(calendar_list['items'])
            page_token = calendar_list.get('nextPageToken')
            if not page_token:
                break

        calendars = new_calendars

        MIN = 60
        await asyncio.sleep(30 * MIN)

# TODO: timezone
class Notifier:
    class Entry:
        def __init__(self, calendar, event):
            self.calendar = calendar
            self.event = event

    def __init__(self):
        self.time = datetime.datetime.now()
        self.evening_notify_done = False
        self.morning_notify_done = False
        self.events = []
        self.morning_notify_hour = 10
        self.evening_notify_hour = 21

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
            self.evening_notify_done = True
            return True
        return False

    def __do_morning_notify(self):
        if self.evening_notify_done:
            return False
        if self.time.hour >= self.morning_notify_hour:
            self.evening_notify_done = True
            return True
        return False

    def __is_today_event(self, event):
        start = event.event['start']
        if 'date' in start:
            date = datetime.datetime.fromisoformat(start['date'])
            return date.date() == datetime.date.today()
        else:
            return False

    def __is_tomorrow_event(self, event):
        start = event.event['start']
        if 'date' in start:
            date = datetime.datetime.fromisoformat(start['date'])
            tomorrow = datetime.date.today() + datetime.timedelta(days=1) 
            return date.date() == tomorrow
        else:
            return False

    def __is_upcoming_event(self, event):
        start = event.event['start']
        if 'date' in start:
            # Whole day event. Will be notified morning/evening notify
            return None
        elif 'dateTime' in start:
            dateTime = datetime.datetime.fromisoformat(start['dateTime'])
            diff = dateTime.timestamp() - self.time.timestamp()
            if diff < 0 or diff > 1 * 60 * 60:
                return None
            elif diff < 30 * 60:
                return "30 minutes"
            else:
                return "1 hour"
        else:
            return None

    def __notify_event(self, event, title):
        print(title, event.calendar, event.event['summary'])
        notify = Notify.Notification.new(title, event.calendar + ': ' + event.event['summary'])
        notify.show()

    def notify(self):
        __do_morning_notify = self.__do_morning_notify()
        __do_evening_notify = self.__do_evening_notify()
        for event in self.events:
            if __do_evening_notify and self.__is_tomorrow_event(event):
                self.__notify_event(event, 'Tomorrow')
            elif __do_morning_notify and self.__is_today_event(event):
                self.__notify_event(event, 'Today')
            elif (time_remaining := self.__is_upcoming_event(event)) is not None:
                self.__notify_event(event, 'Upcomming - ' + time_remaining + ' left')

    def extend_events(self, summary, events):
        for event in events:
            self.events.append(Notifier.Entry(summary, event))
        
async def notify_upcoming_events(service):
    notifier = Notifier()
    while True:
        total_events = []
        now = datetime.datetime.utcnow()
        print('Check event at', now)
        notifier.reinit()

        now_utc = now.isoformat() + 'Z' # 'Z' indicates UTC time
        for calendar in calendars:
            summary = calendar['summary']
            events_result = service.events().list(calendarId=calendar['id'], timeMin=now_utc,
                                                  maxResults=10, singleEvents=True,
                                                  orderBy='startTime').execute()
            events = events_result.get('items')
            notifier.extend_events(summary, events)
        notifier.notify()
        print("notification done")

        # Recalculate now
        now_ts = datetime.datetime.utcnow().timestamp()
        PERIOD = 30 * 60
        def calc_until(now, period=PERIOD):
            import math
            return (period * (math.floor(now / period))) + period
        until_ts = calc_until(now_ts)
        print('Will check again after', until_ts - now_ts)
        await asyncio.sleep(until_ts - now_ts)
        
async def coroutine_gather(service):
    coroutines = [update_calendar_list(service), notify_upcoming_events(service)]
    return await asyncio.gather(*coroutines, return_exceptions=True)

def notification_loop(service):
    try:
        asyncio.run(coroutine_gather(service))
    except Error:
        print(e)

def main():
    """Notify upcoming events from Google calendars
    """
    creds = None
    # The file TOKEN_PATH stores the user's access and refresh
    # tokens, and is created automatically when the authorization flow
    # completes for the first time.
    if os.path.exists(TOKEN_PATH):
        creds = Credentials.from_authorized_user_file(TOKEN_PATH, SCOPES)
    # If there are no (valid) credentials available, let the user log in.
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(
                './client_secret.json', SCOPES)
            creds = flow.run_local_server(port=0)
        # Save the credentials for the next run
        with open(TOKEN_PATH, 'w') as token:
            token.write(creds.to_json())


    try:
        service = build('calendar', 'v3', credentials=creds)
        notification_loop(service)
    except HttpError as err:
        print(err)

if __name__ == '__main__':
    main()
