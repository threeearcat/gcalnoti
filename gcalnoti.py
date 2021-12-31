#!/usr/bin/env python
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

async def notify_upcoming_events(service):
    while True:
        total_events = []
        print(datetime.datetime.utcnow())
        now = datetime.datetime.utcnow().isoformat() + 'Z' # 'Z' indicates UTC time

        for calendar in calendars:
            summary = calendar['summary']
            events_result = service.events().list(calendarId=calendar['id'], timeMin=now,
                                                  maxResults=10, singleEvents=True,
                                                  orderBy='startTime').execute()
            events = events_result.get('items')
            total_events.extend(events)

        for event in total_events:
            pass

        await asyncio.sleep(10)
        
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
