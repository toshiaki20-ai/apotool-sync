#!/usr/bin/env python3
"""
Apotool → Google Calendar 同期スクリプト（GitHub Actions版）
認証情報は環境変数 GOOGLE_SERVICE_ACCOUNT_JSON から取得
"""

import json
import os
import re
import sys
from datetime import datetime, timedelta
from google.oauth2 import service_account
from googleapiclient.discovery import build

# ===== 設定 =====
SCOPES = ['https://www.googleapis.com/auth/calendar']
APO_TAG_KEY = 'apoSync'
APO_TAG_VALUE = 'true'

# カレンダーID
CALENDAR_IDS = {
    '01_個人': 'toshiaki.yoshiokadentaloffice@gmail.com',
    '02_家族': 'b57c5ad016658231d5fb9897f543eb6c2689609924fdbb5739b6db739a1dde61@group.calendar.google.com',
    '03_理事長集中時間': 'e6f1ffae6cd0feaaa1ad95e69429993ad572a40f10eab0b289da741bd0c7f5ec@group.calendar.google.com',
    '04_メンバーMTG': '7f56a02a3fb32f83a3fbca3ea85cc4baf352618fa5232740778038d727e29916@group.calendar.google.com',
    '05_外部MTG': '265fd000ca3392991d4aaa91c058f9a509b3ee92249a9e40ac0105c1ab03d8c6@group.calendar.google.com',
    '06_オペ': 'classroom102922097932539509382@group.calendar.google.com',
    '07_インプラント初診': 'classroom117195644962134754346@group.calendar.google.com',
    '08_コンサル': '397f3431ad160ae1096522fa079d724a59b27af578fde4cbd5c25e1980a2aa5f@group.calendar.google.com',
    '09_症例相談・ガイド制作': 'a120f21b197307cd0c0ac33c3401619f6687d55b6378707d6b41f178ed61161f@group.calendar.google.com',
    '10_勉強会': '16da2e8799c40fb2a29ebdec34ab819251d9e95608ffb3c9c45550e28ff9d842@group.calendar.google.com',
    '11_ADA公務': '89ab03936e3abfd6778a57583ea0ad468aeb7b321a2b00f4f75552b2e8a15611@group.calendar.google.com',
    '12_接待': 'd811cc4d57892e108202726d5748824f286b4e06a8030ba7621b9180e50baf09@group.calendar.google.com',
    '13_雑務': '70d84545ee0505dd826075621a9f17ed218da7703ed05247569f501eda79dedb@group.calendar.google.com',
}

# ===== 振り分けルール =====
ROUTING_RULES = [
    (['OPE', 'ｵﾍﾟ'], '06_オペ'),
    (['インプラント初診', '自費初診'], '07_インプラント初診'),
    (['ｺﾝｻﾙ', 'コンサル'], '08_コンサル'),
    (['症例相談', 'ガイド制作'], '09_症例相談・ガイド制作'),
    (['KG', 'QGM', '外部', 'SMC', '名南'], '05_外部MTG'),
    (['ミーティング', 'MTG', '面談', '会議', '見学', '面接'], '04_メンバーMTG'),
    (['不在', '出張', 'アチーブ', 'セミナー'], '10_勉強会'),
    (['ADA'], '11_ADA公務'),
    (['接待'], '12_接待'),
    (['本山', '家族'], '02_家族'),
    (['カット'], '01_個人'),
    (['埋めない', 'opeなし', 'オペ入れない'], '13_雑務'),
]


def classify_event(text):
    clean = re.sub(r'^\d{1,2}:\d{2}-\d{1,2}:\d{2}\s*', '', text)
    clean = clean.replace('登史彰', '').strip()
    if not clean:
        return '13_雑務'
    for keywords, calendar_name in ROUTING_RULES:
        for kw in keywords:
            if kw in text:
                return calendar_name
    return '05_外部MTG'


def extract_title(text):
    clean = re.sub(r'^\d{1,2}:\d{2}-\d{1,2}:\d{2}\s*', '', text)
    clean = clean.replace('登史彰', '').strip()
    clean = re.sub(r'^\d{5,6}\s*', '', clean)
    clean = clean.strip()
    if not clean:
        return '（空枠）'
    if len(clean) > 80:
        clean = clean[:77] + '...'
    return clean


def parse_time(date_str, time_str):
    return datetime.strptime(f'{date_str} {time_str}', '%Y-%m-%d %H:%M')


def build_service():
    """GitHub Secretsから認証情報を取得してAPIサービスを構築"""
    sa_json = os.environ.get('GOOGLE_SERVICE_ACCOUNT_JSON')
    if sa_json:
        # 環境変数からJSON文字列を直接読み込み
        info = json.loads(sa_json)
        creds = service_account.Credentials.from_service_account_info(info, scopes=SCOPES)
    else:
        # ファイルから読み込み（ローカルテスト用）
        sa_file = os.environ.get('SERVICE_ACCOUNT_FILE', 'service-account-key.json')
        creds = service_account.Credentials.from_service_account_file(sa_file, scopes=SCOPES)
    return build('calendar', 'v3', credentials=creds)


def get_existing_apo_events(service, calendar_id, time_min, time_max):
    events = []
    page_token = None
    while True:
        result = service.events().list(
            calendarId=calendar_id,
            timeMin=time_min,
            timeMax=time_max,
            privateExtendedProperty=f'{APO_TAG_KEY}={APO_TAG_VALUE}',
            singleEvents=True,
            maxResults=2500,
            pageToken=page_token,
        ).execute()
        events.extend(result.get('items', []))
        page_token = result.get('nextPageToken')
        if not page_token:
            break
    return events


def normalize_dt(dt_dict):
    dt_str = dt_dict.get('dateTime', '')
    return dt_str[:19] if dt_str else dt_str


def event_matches(existing, new_body):
    if existing.get('summary', '') != new_body.get('summary', ''):
        return False
    if normalize_dt(existing.get('start', {})) != normalize_dt(new_body.get('start', {})):
        return False
    if normalize_dt(existing.get('end', {})) != normalize_dt(new_body.get('end', {})):
        return False
    return True


def sync_calendar(json_path, date_from=None, date_to=None):
    with open(json_path, 'r', encoding='utf-8') as f:
        raw = json.load(f)
    days_data = raw['data']

    service = build_service()

    all_dates = sorted(days_data.keys())
    if date_from:
        all_dates = [d for d in all_dates if d >= date_from]
    if date_to:
        all_dates = [d for d in all_dates if d <= date_to]

    if not all_dates:
        print('対象日付がありません')
        return

    period_start = all_dates[0]
    period_end = all_dates[-1]
    time_min = f'{period_start}T00:00:00+09:00'
    time_max_dt = datetime.strptime(period_end, '%Y-%m-%d') + timedelta(days=1)
    time_max = f'{time_max_dt.strftime("%Y-%m-%d")}T00:00:00+09:00'

    print(f'同期期間: {period_start} 〜 {period_end}')

    new_events_by_cal = {}
    for date_str in all_dates:
        appointments = days_data[date_str]
        if not appointments:
            continue
        for appt in appointments:
            text = appt['text']
            start_time = appt['startTime']
            end_time = appt['endTime']

            cal_name = classify_event(text)
            title = extract_title(text)

            start_dt = parse_time(date_str, start_time)
            end_dt = parse_time(date_str, end_time)

            event_body = {
                'summary': title,
                'start': {
                    'dateTime': start_dt.strftime('%Y-%m-%dT%H:%M:%S') + '+09:00',
                    'timeZone': 'Asia/Tokyo',
                },
                'end': {
                    'dateTime': end_dt.strftime('%Y-%m-%dT%H:%M:%S') + '+09:00',
                    'timeZone': 'Asia/Tokyo',
                },
                'extendedProperties': {
                    'private': {
                        APO_TAG_KEY: APO_TAG_VALUE,
                        'apoSource': text[:200],
                    }
                },
            }

            if cal_name not in new_events_by_cal:
                new_events_by_cal[cal_name] = []
            new_events_by_cal[cal_name].append(event_body)

    stats = {'created': 0, 'unchanged': 0, 'deleted': 0, 'errors': 0}

    all_cal_names = set(list(CALENDAR_IDS.keys()))
    for cal_name in sorted(all_cal_names):
        cal_id = CALENDAR_IDS[cal_name]
        new_events = new_events_by_cal.get(cal_name, [])

        try:
            existing = get_existing_apo_events(service, cal_id, time_min, time_max)
        except Exception as e:
            print(f'  [{cal_name}] 既存イベント取得エラー: {e}')
            stats['errors'] += 1
            continue

        matched_existing_ids = set()
        unmatched_new = []

        for new_ev in new_events:
            found = False
            for ex_ev in existing:
                if ex_ev['id'] in matched_existing_ids:
                    continue
                if event_matches(ex_ev, new_ev):
                    matched_existing_ids.add(ex_ev['id'])
                    found = True
                    stats['unchanged'] += 1
                    break
            if not found:
                unmatched_new.append(new_ev)

        for ex_ev in existing:
            if ex_ev['id'] not in matched_existing_ids:
                try:
                    service.events().delete(
                        calendarId=cal_id, eventId=ex_ev['id']
                    ).execute()
                    stats['deleted'] += 1
                except Exception as e:
                    print(f'  [{cal_name}] 削除エラー: {e}')
                    stats['errors'] += 1

        for new_ev in unmatched_new:
            try:
                service.events().insert(
                    calendarId=cal_id, body=new_ev
                ).execute()
                stats['created'] += 1
            except Exception as e:
                print(f'  [{cal_name}] 作成エラー: {e}')
                stats['errors'] += 1

        n_del = len([e for e in existing if e['id'] not in matched_existing_ids])
        n_new = len(unmatched_new)
        n_keep = len(matched_existing_ids)
        if n_del > 0 or n_new > 0:
            print(f'  [{cal_name}] 作成:{n_new} 維持:{n_keep} 削除:{n_del}')
        elif n_keep > 0:
            print(f'  [{cal_name}] 変更なし（{n_keep}件維持）')

    print(f'\n=== 同期完了 ===')
    print(f'作成: {stats["created"]}件')
    print(f'維持: {stats["unchanged"]}件')
    print(f'削除: {stats["deleted"]}件')
    print(f'エラー: {stats["errors"]}件')


if __name__ == '__main__':
    json_path = sys.argv[1] if len(sys.argv) > 1 else '/tmp/apotool_data.json'
    date_from = sys.argv[2] if len(sys.argv) > 2 else None
    date_to = sys.argv[3] if len(sys.argv) > 3 else None
    sync_calendar(json_path, date_from, date_to)
