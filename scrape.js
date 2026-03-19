/**
 * Apotool スクレイピング（GitHub Actions版）
 * 
 * 認証情報は環境変数（GitHub Secrets）から取得:
 *   APOTOOL_EMAIL, APOTOOL_PASSWORD
 * 
 * 使い方:
 *   node scrape.js 2026 3 2026 6    # 2026年3月〜6月
 */

const puppeteer = require('puppeteer');
const fs = require('fs');

// ===== 認証情報（GitHub Secretsから取得） =====
const LOGIN_URL = 'https://user.stransa.co.jp/login';
const CALENDAR_URL = 'https://apo-toolboxes.stransa.co.jp/calendar/';
const EMAIL = process.env.APOTOOL_EMAIL;
const PASSWORD = process.env.APOTOOL_PASSWORD;
const OUTPUT_FILE = process.env.OUTPUT_FILE || '/tmp/apotool_data.json';

if (!EMAIL || !PASSWORD) {
  console.error('[ERROR] APOTOOL_EMAIL / APOTOOL_PASSWORD が設定されていません');
  process.exit(1);
}

// ===== 待機時間（ミリ秒） =====
const WAIT_AFTER_LOGIN = 12000;
const WAIT_AFTER_JUMP = 2000;
const WAIT_FOR_TABLE = 5000;
const WAIT_AFTER_STAFF_BTN = 6000;

function sleep(ms) { return new Promise(r => setTimeout(r, ms)); }

function getTargetMonths() {
  const args = process.argv.slice(2).map(Number);
  const now = new Date();
  if (args.length >= 4) {
    const months = [];
    let y = args[0], m = args[1];
    const endY = args[2], endM = args[3];
    while (y < endY || (y === endY && m <= endM)) {
      months.push({ year: y, month: m, daysInMonth: new Date(y, m, 0).getDate() });
      m++;
      if (m > 12) { m = 1; y++; }
    }
    return months;
  } else if (args.length >= 2) {
    const y = args[0], m = args[1];
    return [{ year: y, month: m, daysInMonth: new Date(y, m, 0).getDate() }];
  } else {
    return [{ year: now.getFullYear(), month: now.getMonth() + 1, daysInMonth: new Date(now.getFullYear(), now.getMonth() + 1, 0).getDate() }];
  }
}

async function getDisplayedDate(page) {
  for (let retry = 0; retry < 5; retry++) {
    try {
      const result = await page.evaluate(() => {
        const el = document.querySelector('#target_date');
        if (!el) return null;
        const m = el.textContent.match(/(\d{4})年(\d{1,2})月(\d{1,2})日/);
        return m ? { year: +m[1], month: +m[2], day: +m[3], text: m[0] } : null;
      });
      if (result && result.year > 2020) return result;
    } catch (e) { /* retry */ }
    await sleep(1000);
  }
  return null;
}

async function waitForFullUI(page) {
  for (let i = 0; i < 60; i++) {
    await sleep(1000);
    try {
      const status = await page.evaluate(() => {
        const dateEl = document.querySelector('#target_date');
        const dateText = dateEl ? dateEl.textContent : '';
        return {
          hasRealDate: /202\d年/.test(dateText),
          hasCalendar: typeof calendar !== 'undefined' && typeof calendar.setTargetDate === 'function',
          dateText: dateText.trim()
        };
      });
      if (status.hasRealDate && status.hasCalendar) {
        console.log(`  UIロード完了 (${i + 1}秒) ${status.dateText}`);
        return true;
      }
    } catch (e) { /* retry */ }
  }
  return false;
}

async function jumpToDate(page, year, month, day) {
  for (let retry = 0; retry < 3; retry++) {
    try {
      await page.evaluate((y, m, d) => {
        const date = new Date(y, m - 1, d);
        calendar.setTargetDate(date);
        calendar.updateTimetable();
        const dateStr = y + '/' + String(m).padStart(2, '0') + '/' + String(d).padStart(2, '0');
        $('#navigation_calendar').datepicker('update', dateStr);
      }, year, month, day);
      
      await sleep(WAIT_AFTER_JUMP);
      
      const cur = await getDisplayedDate(page);
      if (cur && cur.year === year && cur.month === month && cur.day === day) {
        return true;
      }
      console.log(`  [WARN] ジャンプ後日付不一致 (retry ${retry}): 期待=${year}/${month}/${day} 実際=${cur ? cur.text : '不明'}`);
      await sleep(3000);
    } catch (e) {
      console.log(`  [WARN] ジャンプ失敗 (retry ${retry}): ${e.message}`);
      await sleep(3000);
    }
  }
  return false;
}

async function waitForTable(page) {
  const startMs = Date.now();
  while (Date.now() - startMs < WAIT_FOR_TABLE) {
    try {
      const hasTable = await page.evaluate(() => {
        const table = document.querySelector('table.daily');
        if (!table) return false;
        const rows = table.querySelectorAll('tr');
        return rows.length > 3;
      });
      if (hasTable) return true;
    } catch (e) { /* retry */ }
    await sleep(500);
  }
  return false;
}

async function extractData(page) {
  try {
    return await page.evaluate(() => {
      const table = document.querySelector('table.daily');
      if (!table) return { found: false, events: [], message: 'テーブルなし' };

      const headers = table.querySelectorAll('tr:first-child th, tr:first-child td');
      let col1 = -1, col2 = -1;
      for (let i = 0; i < headers.length; i++) {
        const text = headers[i].textContent.trim();
        if (text.includes('登史彰') && text.includes('(1)')) col1 = i;
        if (text.includes('登史彰') && text.includes('(2)')) col2 = i;
      }
      if (col1 < 0) return { found: false, events: [], message: '登史彰列なし' };

      const rows = table.querySelectorAll('tr');
      const events = [];
      const seen = new Set();
      const timePattern = /(\d{1,2}:\d{2})-(\d{1,2}:\d{2})/;

      for (const row of rows) {
        const cells = row.querySelectorAll('td, th');

        if (cells[col1]) {
          const t = cells[col1].textContent.trim();
          if (t && timePattern.test(t) && !seen.has('1:' + t)) {
            seen.add('1:' + t);
            const m = t.match(timePattern);
            const cell = cells[col1];
            let bgColor = cell.style.backgroundColor || '';
            if (!bgColor) bgColor = window.getComputedStyle(cell).backgroundColor;
            const innerDiv = cell.querySelector('div[style], span[style]');
            if (innerDiv && innerDiv.style.backgroundColor) bgColor = innerDiv.style.backgroundColor;
            events.push({
              column: 1, startTime: m[1], endTime: m[2],
              text: t.replace(/\s+/g, ' '), bgColor: bgColor
            });
          }
        }

        if (col2 >= 0 && cells[col2]) {
          const t = cells[col2].textContent.trim();
          if (t && timePattern.test(t) && !seen.has('2:' + t)) {
            seen.add('2:' + t);
            const m = t.match(timePattern);
            const cell = cells[col2];
            let bgColor = cell.style.backgroundColor || '';
            if (!bgColor) bgColor = window.getComputedStyle(cell).backgroundColor;
            const innerDiv = cell.querySelector('div[style], span[style]');
            if (innerDiv && innerDiv.style.backgroundColor) bgColor = innerDiv.style.backgroundColor;
            events.push({
              column: 2, startTime: m[1], endTime: m[2],
              text: t.replace(/\s+/g, ' '), bgColor: bgColor
            });
          }
        }
      }

      return { found: true, events };
    });
  } catch (e) {
    return { found: false, events: [], message: 'error: ' + e.message };
  }
}

// ===== メイン処理 =====
(async () => {
  const startTime = Date.now();
  const targetMonths = getTargetMonths();
  console.log(`[START] ${new Date().toLocaleString('ja-JP', { timeZone: 'Asia/Tokyo' })}`);
  console.log(`[対象] ${targetMonths.map(m => `${m.year}/${m.month}`).join(', ')}`);
  console.log(`[出力] ${OUTPUT_FILE}`);

  const launchOptions = {
    headless: 'new',
    args: [
      '--no-sandbox',
      '--disable-setuid-sandbox',
      '--disable-dev-shm-usage',
      '--disable-gpu',
      '--single-process',
    ],
    env: { ...process.env, TZ: 'Asia/Tokyo' }
  };
  if (process.env.PUPPETEER_EXECUTABLE_PATH) {
    launchOptions.executablePath = process.env.PUPPETEER_EXECUTABLE_PATH;
  }
  const browser = await puppeteer.launch(launchOptions);
  const page = await browser.newPage();
  await page.setViewport({ width: 1920, height: 1080 });
  await page.emulateTimezone('Asia/Tokyo');

  // ===== ログイン =====
  console.log('\n[1] ログイン...');
  await page.goto(LOGIN_URL, { waitUntil: 'networkidle2', timeout: 30000 });
  await sleep(3000);

  const emailInput = await page.$('input[type="text"], input[type="email"], input[name="email"]');
  if (emailInput) {
    await emailInput.click({ clickCount: 3 });
    await emailInput.type(EMAIL, { delay: 30 });
  }
  const passInput = await page.$('input[type="password"]');
  if (passInput) {
    await passInput.click({ clickCount: 3 });
    await passInput.type(PASSWORD, { delay: 30 });
  }
  const loginBtn = await page.$('button[type="submit"], input[type="submit"], button');
  if (loginBtn) {
    await loginBtn.click();
  } else {
    await page.keyboard.press('Enter');
  }

  console.log('[1] ログイン送信、12秒待機...');
  await sleep(WAIT_AFTER_LOGIN);

  console.log('[1] カレンダーページに移動...');
  await page.goto(CALENDAR_URL, { waitUntil: 'networkidle2', timeout: 30000 });

  // ===== UI完全ロード待ち =====
  console.log('\n[2] UI完全ロード待ち...');
  const uiReady = await waitForFullUI(page);
  if (!uiReady) {
    console.log('[ERROR] UIロードタイムアウト');
    await browser.close();
    process.exit(1);
  }

  console.log('[2] 追加10秒待機...');
  await sleep(10000);

  const initialDate = await getDisplayedDate(page);
  console.log(`[2] 現在表示: ${initialDate ? initialDate.text : '不明'}`);

  // ===== スタッフ表示に切り替え =====
  console.log('\n[3] スタッフ表示に切り替え...');
  await page.click('#staff_btn');
  await sleep(WAIT_AFTER_STAFF_BTN);
  console.log('[3] スタッフ表示切替完了');

  // ===== 各月のデータ抽出 =====
  const allResults = {};
  let totalSuccess = 0, totalSkip = 0;

  for (const monthInfo of targetMonths) {
    const { year, month, daysInMonth } = monthInfo;
    console.log(`\n[4] === ${year}年${month}月 (${daysInMonth}日間) ===`);

    for (let day = 1; day <= daysInMonth; day++) {
      const dateStr = `${year}-${String(month).padStart(2, '0')}-${String(day).padStart(2, '0')}`;

      const jumped = await jumpToDate(page, year, month, day);
      if (!jumped) {
        console.log(`  [${day}] ${dateStr} [SKIP] ジャンプ失敗`);
        allResults[dateStr] = [];
        totalSkip++;
        continue;
      }

      const hasTable = await waitForTable(page);
      if (!hasTable) {
        console.log(`  [${day}] ${dateStr} → 休診日`);
        allResults[dateStr] = [];
        totalSkip++;
        continue;
      }

      const data = await extractData(page);
      if (data.found) {
        allResults[dateStr] = data.events;
        totalSuccess++;
        console.log(`  [${day}] ${dateStr} → ${data.events.length}件`);
      } else {
        allResults[dateStr] = [];
        totalSkip++;
        console.log(`  [${day}] ${dateStr} → ${data.message}`);
      }

      if (day % 10 === 0 || day === daysInMonth) {
        const partial = {
          targetMonths: targetMonths.map(m => `${m.year}-${String(m.month).padStart(2, '0')}`),
          extractedAt: new Date().toISOString(),
          successCount: totalSuccess,
          skipCount: totalSkip,
          data: allResults
        };
        fs.writeFileSync(OUTPUT_FILE, JSON.stringify(partial, null, 2), 'utf-8');
      }
    }
  }

  // ===== 最終保存 =====
  const elapsed = ((Date.now() - startTime) / 1000).toFixed(1);
  console.log(`\n[完了] 成功:${totalSuccess} スキップ:${totalSkip} 時間:${elapsed}秒`);

  const finalResult = {
    targetMonths: targetMonths.map(m => `${m.year}-${String(m.month).padStart(2, '0')}`),
    extractedAt: new Date().toISOString(),
    elapsedSeconds: parseFloat(elapsed),
    successCount: totalSuccess,
    skipCount: totalSkip,
    data: allResults
  };
  fs.writeFileSync(OUTPUT_FILE, JSON.stringify(finalResult, null, 2), 'utf-8');
  console.log(`[保存] ${OUTPUT_FILE}`);

  await browser.close();
})();
