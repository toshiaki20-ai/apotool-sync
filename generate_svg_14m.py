#!/usr/bin/env python3
"""
Googleカレンダーデータから巨大SVGカレンダーを生成（14ヶ月版）
2ヶ月横並び × 7段
"""

import json
import sys
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, date, timezone
from calendar import monthrange
from collections import defaultdict
import jpholiday

# ===== レイアウト設定 =====
HOUR_START = 6
HOUR_END = 22
SLOT_MINUTES = 15
SLOTS_PER_HOUR = 60 // SLOT_MINUTES
TOTAL_SLOTS = (HOUR_END - HOUR_START) * SLOTS_PER_HOUR  # 64

CELL_W = 120
CELL_H = 14
HEADER_H = 50
ALLDAY_H = CELL_H * 2
TIME_COL_W = 44
MONTH_TITLE_H = 36
TITLE_H = 32
GAP_X = 30   # 2ヶ月間の横の隙間
GAP_Y = 20   # 段間の縦の隙間

DAYS_JP = ['月', '火', '水', '木', '金', '土', '日']
JP_FONT = "'Noto Sans CJK JP', 'Noto Sans JP', sans-serif"

CALENDAR_COLORS = {
    '01_個人': '#3F51B5',
    '02_家族': '#7CB342',
    '03_理事長集中時間': '#0B8043',
    '04_メンバーMTG': '#EF6C00',
    '05_外部MTG': '#F6BF26',
    '06_オペ': '#D50000',
    '07_インプラント初診': '#039BE5',
    '08_コンサル': '#4285F4',
    '09_症例相談・ガイド制作': '#CC006C',
    '10_勉強会': '#C0CA33',
    '11_ADA公務': '#AD1457',
    '12_接待': '#795548',
    '13_雑務': '#A79B8E',
}

CLINIC_HOURS_WEEKDAY = [(8, 30, 12, 15), (13, 45, 18, 30)]
CLINIC_HOURS_SAT = [(8, 30, 12, 15), (13, 45, 16, 30)]


def parse_dt(s):
    if not s:
        return None
    if len(s) == 10:
        return datetime.strptime(s, '%Y-%m-%d')
    is_utc = s.endswith('Z')
    s_clean = s.replace('+09:00', '').replace('+0900', '').replace('Z', '')
    try:
        dt = datetime.strptime(s_clean[:19], '%Y-%m-%dT%H:%M:%S')
        if is_utc:
            dt = dt + timedelta(hours=9)
        return dt
    except:
        return None


def slot_index(hour, minute):
    total_min = (hour - HOUR_START) * 60 + minute
    return max(0, min(total_min // SLOT_MINUTES, TOTAL_SLOTS - 1))


def is_closed_day(year, month, day):
    dt = datetime(year, month, day)
    weekday = dt.weekday()
    holiday = jpholiday.is_holiday(date(year, month, day))
    return weekday == 3 or weekday == 6 or holiday


def get_holiday_name(year, month, day):
    d = date(year, month, day)
    return jpholiday.is_holiday_name(d)


def get_clinic_slots(year, month, day):
    if is_closed_day(year, month, day):
        return None
    weekday = datetime(year, month, day).weekday()
    if weekday == 5:
        hours = CLINIC_HOURS_SAT
    else:
        hours = CLINIC_HOURS_WEEKDAY
    slots = []
    for sh, sm, eh, em in hours:
        s = slot_index(sh, sm)
        e = slot_index(eh, em)
        slots.append((s, e))
    return slots


def compute_event_slots(day_events):
    """重複するイベントの横位置を計算"""
    positioned = []
    for ev in day_events:
        start = parse_dt(ev['start'])
        end = parse_dt(ev['end'])

        if ev.get('allDay') or not start or not end:
            positioned.append((ev, None, None, 0, 1))
            continue

        s_hour, s_min = start.hour, start.minute
        e_hour, e_min = end.hour, end.minute

        if e_hour < HOUR_START or s_hour >= HOUR_END:
            positioned.append((ev, None, None, 0, 1))
            continue

        s_hour_c = max(s_hour, HOUR_START)
        s_min_c = 0 if start.hour < HOUR_START else s_min
        e_hour_c = min(e_hour, HOUR_END)
        e_min_c = 0 if e_hour_c == HOUR_END else e_min

        s_slot = slot_index(s_hour_c, s_min_c)
        e_slot = slot_index(e_hour_c, e_min_c)
        if e_slot <= s_slot:
            e_slot = s_slot + 1

        positioned.append((ev, s_slot, e_slot, 0, 1))

    timed = [(i, s, e) for i, (ev, s, e, _, _) in enumerate(positioned) if s is not None]
    timed.sort(key=lambda x: (x[1], -(x[2] - x[1])))

    clusters = []
    for idx, s, e in timed:
        placed = False
        for cluster in clusters:
            overlaps = False
            for ci, cs, ce in cluster:
                if s < ce and e > cs:
                    overlaps = True
                    break
            if overlaps:
                cluster.append((idx, s, e))
                placed = True
                break
        if not placed:
            clusters.append([(idx, s, e)])

    col_assignments = {}
    for cluster in clusters:
        columns = []
        for idx, s, e in cluster:
            placed = False
            for col_i, col_events in enumerate(columns):
                conflict = False
                for _, cs, ce in col_events:
                    if s < ce and e > cs:
                        conflict = True
                        break
                if not conflict:
                    col_events.append((idx, s, e))
                    col_assignments[idx] = col_i
                    placed = True
                    break
            if not placed:
                columns.append([(idx, s, e)])
                col_assignments[idx] = len(columns) - 1
        total_cols = len(columns)
        for idx, _, _ in cluster:
            col_assignments[idx] = (col_assignments[idx], total_cols)

    result = []
    for i, (ev, s_slot, e_slot, _, _) in enumerate(positioned):
        if i in col_assignments:
            col_i, total_cols = col_assignments[i]
            result.append((ev, s_slot, e_slot, col_i, total_cols))
        else:
            result.append((ev, s_slot, e_slot, 0, 1))

    return result


def draw_month(svg, data_events, year, month, offset_x, offset_y, clip_counter):
    _, days_in_month = monthrange(year, month)

    events_by_date = defaultdict(list)
    for ev in data_events:
        start = parse_dt(ev['start'])
        if not start:
            continue
        if start.year == year and start.month == month:
            events_by_date[start.day].append(ev)

    month_w = TIME_COL_W + CELL_W * days_in_month
    grid_h = TOTAL_SLOTS * CELL_H

    # 月タイトル（左揃え）
    t = ET.SubElement(svg, 'text', {
        'x': str(offset_x + TIME_COL_W),
        'y': str(offset_y + 24),
        'text-anchor': 'start',
        'class': 'month-title',
    })
    t.text = f'{year}年{month}月'

    oy = offset_y + MONTH_TITLE_H

    # 日付ヘッダー
    for day in range(1, days_in_month + 1):
        x = offset_x + TIME_COL_W + (day - 1) * CELL_W
        weekday = datetime(year, month, day).weekday()
        holiday_name = get_holiday_name(year, month, day)
        closed = is_closed_day(year, month, day)

        if closed:
            header_bg = '#FFEBEE'
            text_fill = '#C62828'
        elif weekday == 5:
            header_bg = '#E3F2FD'
            text_fill = '#1565C0'
        else:
            header_bg = '#F5F5F5'
            text_fill = '#333333'

        ET.SubElement(svg, 'rect', {
            'x': str(x), 'y': str(oy),
            'width': str(CELL_W), 'height': str(HEADER_H),
            'fill': header_bg, 'stroke': '#BDBDBD', 'stroke-width': '0.5',
        })

        label = ET.SubElement(svg, 'text', {
            'x': str(x + CELL_W // 2), 'y': str(oy + 24),
            'text-anchor': 'middle', 'class': 'date-num',
            'fill': text_fill,
        })
        label.text = str(day)

        dow_text = DAYS_JP[weekday]
        if holiday_name:
            dow_text = f'{dow_text} {holiday_name}'
        dow = ET.SubElement(svg, 'text', {
            'x': str(x + CELL_W // 2), 'y': str(oy + 42),
            'text-anchor': 'middle', 'class': 'date-dow',
            'fill': text_fill,
        })
        dow.text = dow_text

    # === 終日イベントエリア ===
    allday_top = oy + HEADER_H

    for day in range(1, days_in_month + 1):
        x = offset_x + TIME_COL_W + (day - 1) * CELL_W
        closed = is_closed_day(year, month, day)
        bg = '#FFF0F0' if closed else '#F8F8F8'
        ET.SubElement(svg, 'rect', {
            'x': str(x), 'y': str(allday_top),
            'width': str(CELL_W), 'height': str(ALLDAY_H),
            'fill': bg, 'stroke': '#E0E0E0', 'stroke-width': '0.5',
        })

    t = ET.SubElement(svg, 'text', {
        'x': str(offset_x + TIME_COL_W - 4),
        'y': str(allday_top + ALLDAY_H // 2 + 3),
        'text-anchor': 'end', 'class': 'time-label',
    })
    t.text = '終日'

    ET.SubElement(svg, 'line', {
        'x1': str(offset_x + TIME_COL_W),
        'y1': str(allday_top + ALLDAY_H),
        'x2': str(offset_x + TIME_COL_W + CELL_W * days_in_month),
        'y2': str(allday_top + ALLDAY_H),
        'stroke': '#BDBDBD', 'stroke-width': '1',
    })

    grid_top = allday_top + ALLDAY_H

    # 背景色
    for day in range(1, days_in_month + 1):
        x = offset_x + TIME_COL_W + (day - 1) * CELL_W
        closed = is_closed_day(year, month, day)

        if closed:
            ET.SubElement(svg, 'rect', {
                'x': str(x), 'y': str(grid_top),
                'width': str(CELL_W), 'height': str(grid_h),
                'fill': '#FFF0F0',
            })
        else:
            ET.SubElement(svg, 'rect', {
                'x': str(x), 'y': str(grid_top),
                'width': str(CELL_W), 'height': str(grid_h),
                'fill': '#F0F0F0',
            })
            clinic_slots = get_clinic_slots(year, month, day)
            if clinic_slots:
                for s_slot, e_slot in clinic_slots:
                    sy = grid_top + s_slot * CELL_H
                    sh = (e_slot - s_slot) * CELL_H
                    ET.SubElement(svg, 'rect', {
                        'x': str(x), 'y': str(sy),
                        'width': str(CELL_W), 'height': str(sh),
                        'fill': '#FFFFFF',
                    })

    # 時刻ラベル + 横線
    for slot in range(TOTAL_SLOTS + 1):
        y = grid_top + slot * CELL_H
        hour = HOUR_START + slot // SLOTS_PER_HOUR
        minute = (slot % SLOTS_PER_HOUR) * SLOT_MINUTES

        if slot < TOTAL_SLOTS and minute == 0:
            t = ET.SubElement(svg, 'text', {
                'x': str(offset_x + TIME_COL_W - 4),
                'y': str(y + 10),
                'text-anchor': 'end', 'class': 'time-label',
            })
            t.text = f'{hour}:00'

        line_class = 'hour-line' if minute == 0 else 'grid-line'
        ET.SubElement(svg, 'line', {
            'x1': str(offset_x + TIME_COL_W),
            'y1': str(y),
            'x2': str(offset_x + TIME_COL_W + CELL_W * days_in_month),
            'y2': str(y),
            'class': line_class,
        })

    # 縦線
    for day in range(days_in_month + 1):
        x = offset_x + TIME_COL_W + day * CELL_W
        if day < days_in_month:
            right_weekday = datetime(year, month, day + 1).weekday()
        else:
            right_weekday = -1
        is_week_boundary = (right_weekday == 0)
        lw = '1.5' if is_week_boundary else '0.5'
        stroke_color = '#999999' if is_week_boundary else '#E0E0E0'
        vline_top = oy if is_week_boundary else allday_top
        ET.SubElement(svg, 'line', {
            'x1': str(x), 'y1': str(vline_top),
            'x2': str(x), 'y2': str(grid_top + grid_h),
            'stroke': stroke_color,
            'stroke-width': lw,
        })

    # イベント描画
    for day in range(1, days_in_month + 1):
        day_events = events_by_date.get(day, [])
        col_x = offset_x + TIME_COL_W + (day - 1) * CELL_W

        positioned = compute_event_slots(day_events)

        allday_idx = 0

        for ev, s_slot, e_slot, col_i, total_cols in positioned:
            cal_name = ev.get('calendar', '')
            color = CALENDAR_COLORS.get(cal_name, '#999')
            summary = ev.get('summary', '')

            if ev.get('allDay'):
                ey = allday_top + allday_idx * CELL_H
                if ey + CELL_H <= allday_top + ALLDAY_H:
                    ET.SubElement(svg, 'rect', {
                        'x': str(col_x + 1), 'y': str(ey + 1),
                        'width': str(CELL_W - 2), 'height': str(CELL_H - 2),
                        'fill': color, 'rx': '2', 'opacity': '0.85',
                    })
                    t = ET.SubElement(svg, 'text', {
                        'x': str(col_x + 3), 'y': str(ey + CELL_H - 3),
                        'font-size': '7', 'fill': '#000000',
                        'font-family': JP_FONT,
                    })
                    t.text = summary[:12]
                allday_idx += 1
                continue

            if s_slot is None:
                continue

            slot_w = CELL_W / total_cols
            ev_x = col_x + col_i * slot_w
            ey = grid_top + s_slot * CELL_H
            rect_h = max((e_slot - s_slot) * CELL_H, CELL_H)
            rect_w = slot_w - 1

            clip_id = f'clip_{clip_counter[0]}'
            clip_counter[0] += 1
            clip_path = ET.SubElement(svg, 'clipPath', {'id': clip_id})
            ET.SubElement(clip_path, 'rect', {
                'x': f'{ev_x:.1f}', 'y': str(ey),
                'width': f'{rect_w:.1f}', 'height': str(rect_h),
            })

            ET.SubElement(svg, 'rect', {
                'x': f'{ev_x:.1f}', 'y': str(ey),
                'width': f'{rect_w:.1f}', 'height': str(rect_h),
                'fill': color, 'rx': '2', 'opacity': '0.85',
            })

            txt_color = '#000000'

            start = parse_dt(ev['start'])
            end = parse_dt(ev['end'])
            s_hour, s_min = start.hour, start.minute
            e_hour, e_min = end.hour, end.minute
            time_str = f'{s_hour:02d}:{s_min:02d}~{e_hour:02d}:{e_min:02d}'

            display_lines_raw = [summary, time_str]

            usable_w = rect_w - 4

            for font_size in [8, 7, 6, 5, 4]:
                char_w = font_size * 0.6
                line_h = font_size + 2
                chars_per_line = max(1, int(usable_w / char_w) - 1)
                lines = []
                for raw_line in display_lines_raw:
                    remaining = raw_line
                    while remaining:
                        lines.append(remaining[:chars_per_line])
                        remaining = remaining[chars_per_line:]

                total_text_h = len(lines) * line_h
                if total_text_h <= rect_h - 2:
                    break

            g = ET.SubElement(svg, 'g', {'clip-path': f'url(#{clip_id})'})
            for li, line_text in enumerate(lines):
                t = ET.SubElement(g, 'text', {
                    'x': f'{ev_x + 2:.1f}',
                    'y': str(ey + 2 + (li + 1) * line_h),
                    'font-size': str(font_size),
                    'fill': txt_color,
                    'font-family': JP_FONT,
                    'font-weight': 'bold' if li == 0 else 'normal',
                })
                t.text = line_text

    # 月の数字をウォーターマークとして背景に描画（イベントの上に重ねる）
    wm_top_slot = (9 - HOUR_START) * SLOTS_PER_HOUR
    wm_bottom_slot = (21 - HOUR_START) * SLOTS_PER_HOUR
    wm_top_y = oy + HEADER_H + ALLDAY_H + wm_top_slot * CELL_H
    wm_bottom_y = oy + HEADER_H + ALLDAY_H + wm_bottom_slot * CELL_H
    wm_h = wm_bottom_y - wm_top_y
    wm_font_size = int(wm_h * 0.95 * 1.4 * 1.1)
    wm_label = f'{year}/{month}' if month == 1 else str(month)
    ET.SubElement(svg, 'text', {
        'x': str(offset_x + TIME_COL_W + 10),
        'y': str(wm_bottom_y - int(wm_h * 0.05)),
        'text-anchor': 'start',
        'font-size': str(wm_font_size),
        'font-weight': '900',
        'fill': '#000000',
        'opacity': '0.15',
        'font-family': JP_FONT,
    }).text = wm_label

    # 今日の列をハイライト（JST）
    jst = timezone(timedelta(hours=9))
    today = datetime.now(jst).date()
    if today.year == year and today.month == month:
        today_x = offset_x + TIME_COL_W + (today.day - 1) * CELL_W
        total_h = HEADER_H + ALLDAY_H + grid_h
        ET.SubElement(svg, 'rect', {
            'x': str(today_x), 'y': str(oy),
            'width': str(CELL_W), 'height': str(total_h),
            'fill': 'none', 'stroke': '#1565C0', 'stroke-width': '3',
        })
        ET.SubElement(svg, 'rect', {
            'x': str(today_x), 'y': str(oy),
            'width': str(CELL_W), 'height': str(HEADER_H),
            'fill': '#1565C0', 'opacity': '0.9', 'rx': '0',
        })
        label = ET.SubElement(svg, 'text', {
            'x': str(today_x + CELL_W // 2), 'y': str(oy + 24),
            'text-anchor': 'middle', 'class': 'date-num',
            'fill': '#FFFFFF',
        })
        label.text = str(today.day)
        weekday = today.weekday()
        dow_text = DAYS_JP[weekday]
        holiday_name = get_holiday_name(today.year, today.month, today.day)
        if holiday_name:
            dow_text = f'{dow_text} {holiday_name}'
        dow = ET.SubElement(svg, 'text', {
            'x': str(today_x + CELL_W // 2), 'y': str(oy + 42),
            'text-anchor': 'middle', 'class': 'date-dow',
            'fill': '#FFFFFF',
        })
        dow.text = dow_text

    return month_w, MONTH_TITLE_H + HEADER_H + ALLDAY_H + grid_h


def generate_svg_14m(data_path, output_path):
    with open(data_path, 'r', encoding='utf-8') as f:
        data = json.load(f)

    events = data['events']
    period_start = datetime.strptime(data['periodStart'], '%Y-%m-%d')
    start_year, start_month = period_start.year, period_start.month

    jst = timezone(timedelta(hours=9))
    now = datetime.now(jst)
    update_str = now.strftime('%Y.%m.%d  %H:%M')

    # 14ヶ月分の(year, month)リスト
    months = []
    y, m = start_year, start_month
    for _ in range(14):
        months.append((y, m))
        m += 1
        if m > 12:
            m = 1
            y += 1

    # 各月の幅を計算（最大幅を2ヶ月ペアで求める）
    month_widths = []
    for y, m in months:
        _, dim = monthrange(y, m)
        month_widths.append(TIME_COL_W + CELL_W * dim)

    month_h = MONTH_TITLE_H + HEADER_H + ALLDAY_H + TOTAL_SLOTS * CELL_H

    # レイアウト: 左列(0〜5) + 右列(6〜13)、8段
    # 左列: months[0]〜[5] (6ヶ月)、行0〜5に配置、行6〜7は空
    # 右列: months[6]〜[13] (8ヶ月)、行0〜7に配置
    NUM_ROWS = 8

    # 左列の最大幅
    left_max_w = max(month_widths[i] for i in range(6))
    # 右列の最大幅
    right_max_w = max(month_widths[i] for i in range(6, 14))

    svg_w = left_max_w + GAP_X + right_max_w + 40  # 左右マージン
    svg_h = TITLE_H + NUM_ROWS * (month_h + GAP_Y) + 20

    svg = ET.Element('svg', {
        'xmlns': 'http://www.w3.org/2000/svg',
        'width': str(svg_w),
        'height': str(svg_h),
        'viewBox': f'0 0 {svg_w} {svg_h}',
    })

    style = ET.SubElement(svg, 'style')
    style.text = f"""
        text {{ font-family: {JP_FONT}; }}
        .main-title {{ font-size: 28px; font-weight: 900; fill: #333; }}
        .month-title {{ font-size: 20px; font-weight: bold; fill: #333; }}
        .date-num {{ font-size: 22px; font-weight: bold; }}
        .date-dow {{ font-size: 14px; }}
        .time-label {{ font-size: 8px; font-family: monospace; fill: #999; }}
        .ev-text {{ font-size: 8px; font-weight: bold; }}
        .ev-time {{ font-size: 7px; font-family: monospace; }}
        .ev-detail {{ font-size: 6px; opacity: 0.8; }}
        .grid-line {{ stroke: #E8E8E8; stroke-width: 0.5; }}
        .hour-line {{ stroke: #BDBDBD; stroke-width: 0.8; }}
    """

    ET.SubElement(svg, 'rect', {
        'width': str(svg_w), 'height': str(svg_h), 'fill': '#FFFFFF',
    })

    # タイトル（大きく左揃え）
    t = ET.SubElement(svg, 'text', {
        'x': str(20),
        'y': str(26),
        'text-anchor': 'start',
        'class': 'main-title',
    })
    t.text = f'としあきカレンダー\u3000\u3000最終更新：{update_str}'

    clip_counter = [0]

    for row in range(NUM_ROWS):
        row_y = TITLE_H + row * (month_h + GAP_Y)

        # 左列: months[0]〜[5]（行0〜5のみ）
        left_idx = row
        if left_idx < 6:
            y_left, m_left = months[left_idx]
            ox_left = 20
            draw_month(svg, events, y_left, m_left, ox_left, row_y, clip_counter)

        # 右列: months[6]〜[13]（行0〜7）
        right_idx = row + 6
        if right_idx < 14:
            y_right, m_right = months[right_idx]
            ox_right = 20 + left_max_w + GAP_X
            draw_month(svg, events, y_right, m_right, ox_right, row_y, clip_counter)

    tree = ET.ElementTree(svg)
    ET.indent(tree, space='  ')
    tree.write(output_path, encoding='utf-8', xml_declaration=True)

    print(f'SVG生成完了: {output_path}')
    print(f'サイズ: {svg_w} x {svg_h}px')
    print(f'月数: {len(months)}ヶ月')
    print(f'イベント数: {len(events)}')


if __name__ == '__main__':
    if len(sys.argv) >= 3:
        generate_svg_14m(sys.argv[1], sys.argv[2])
    else:
        generate_svg_14m(
            '/home/ubuntu/calendar-svg/calendar_data_14m.json',
            '/home/ubuntu/calendar-svg/calendar_14m.svg'
        )
