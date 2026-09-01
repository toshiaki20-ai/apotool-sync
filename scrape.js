/**
 * Apotool スクレイピング（GitHub Actions版）
 * 認証情報: APOTOOL_EMAIL, APOTOOL_PASSWORD
 */
const puppeteer = require('puppeteer');
const fs = require('fs');

const LOGIN_URL = 'https://user.stransa.co.jp/login';
const CALENDAR_URL = 'https://apo-toolboxes.stransa.co.jp/calendar/';
const EMAIL = process.env.APOTOOL_EMAIL;
const PASSWORD = process.env.APOTOOL_PASSWORD;
const OUTPUT_FILE = process.env.OUTPUT_FILE || '/tmp/apotool_data.json';
if (!EMAIL || !PASSWORD) {
  console.error('[ERROR] APOTOOL_EMAIL / APOTOOL_PASSWORD が設定されていません');
  process.exit(1);
}

const WAIT_AFTER_LOGIN = 12000;
const WAIT_AFTER_JUMP = 2000;
const WAIT_FOR_TABLE = 5000;
const WAIT_AFTER_STAFF_BTN = 6000;
// 初回と、失敗時のみ1分後・さらに1分後に再試行する。
const RETRY_DELAYS_MS = [0, 60 * 1000, 60 * 1000];
const RETRYABLE_STATUS_CODES = new Set([403, 408, 429, 500, 502, 503, 504]);

class RetryableApotoolError extends Error {
  constructor(message) {
    super(message);
    this.name = 'RetryableApotoolError';
  }
}
function sleep(ms) { return new Promise(resolve => setTimeout(resolve, ms)); }

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
  }
  if (args.length >= 2) {
    const [year, month] = args;
    return [{ year, month, daysInMonth: new Date(year, month, 0).getDate() }];
  }
  return [{ year: now.getFullYear(), month: now.getMonth() + 1, daysInMonth: new Date(now.getFullYear(), now.getMonth() + 1, 0).getDate() }];
}

async function getDisplayedDate(page) {
  for (let retry = 0; retry < 5; retry++) {
    try {
      const result = await page.evaluate(() => {
        const el = document.querySelector('#target_date');
        if (!el) return null;
        const match = el.textContent.match(/(\d{4})年(\d{1,2})月(\d{1,2})日/);
        return match ? { year: +match[1], month: +match[2], day: +match[3], text: match[0] } : null;
      });
      if (result && result.year > 2020) return result;
    } catch (_) {}
    await sleep(1000);
  }
  return null;
}

async function getUiState(page) {
  return page.evaluate(() => {
    const dateEl = document.querySelector('#target_date');
    const dateText = dateEl ? dateEl.textContent.trim() : '';
    return {
      url: location.href,
      title: document.title,
      hasLoginForm: !!document.querySelector('input[type="password"]'),
      hasRealDate: /202\d年/.test(dateText),
      hasCalendar: typeof calendar !== 'undefined' && typeof calendar.setTargetDate === 'function',
      hasStaffButton: !!document.querySelector('#staff_btn'),
      hasDailyTable: !!document.querySelector('table.daily'),
      dateText
    };
  });
}

function isTemporaryFailure(title) {
  return /(?:403|forbidden|service unavailable|gateway)/i.test(title || '');
}

async function waitForFullUI(page) {
  const timeoutSeconds = 120;
  let lastStatus = null;
  for (let i = 0; i < timeoutSeconds; i++) {
    await sleep(1000);
    try {
      const status = await getUiState(page);
      lastStatus = status;
      // 403が画面読み込み途中に出るケースも即時に再試行対象へ移す。
      if (isTemporaryFailure(status.title)) {
        throw new RetryableApotoolError(`画面読み込み中に一時的なアクセス拒否を検出: ${status.title}`);
      }
      if (status.hasRealDate && status.hasCalendar && status.hasStaffButton) {
        console.log(`  UIロード完了 (${i + 1}秒) ${status.dateText}`);
        return true;
      }
      if ((i + 1) % 15 === 0) {
        console.log(`  UI待機中 (${i + 1}秒): 日付=${status.hasRealDate}, calendar=${status.hasCalendar}, スタッフ=${status.hasStaffButton}, ログイン画面=${status.hasLoginForm}`);
      }
    } catch (error) {
      if (error instanceof RetryableApotoolError) throw error;
      if ((i + 1) % 15 === 0) console.log(`  UI確認リトライ: ${error.message}`);
    }
  }
  if (lastStatus) {
    console.error(`[DIAG] UI未準備: URL=${lastStatus.url}, title=${lastStatus.title}, 日付=${lastStatus.hasRealDate}, calendar=${lastStatus.hasCalendar}, スタッフ=${lastStatus.hasStaffButton}, テーブル=${lastStatus.hasDailyTable}, ログイン画面=${lastStatus.hasLoginForm}`);
  }
  return false;
}

async function openCalendarPage(page) {
  let response;
  try {
    response = await page.goto(CALENDAR_URL, { waitUntil: 'networkidle2', timeout: 30000 });
  } catch (error) {
    throw new RetryableApotoolError(`カレンダー画面への接続失敗: ${error.message}`);
  }
  const status = response ? response.status() : 0;
  const title = await page.title().catch(() => '');
  if (RETRYABLE_STATUS_CODES.has(status) || isTemporaryFailure(title)) {
    throw new RetryableApotoolError(`カレンダー画面が一時的に利用不可: HTTP ${status || '不明'} / ${title || 'タイトル不明'}`);
  }
  if (status >= 400) throw new Error(`カレンダー画面の取得に失敗: HTTP ${status} / ${title || 'タイトル不明'}`);
}

async function jumpToDate(page, year, month, day) {
  for (let retry = 0; retry < 3; retry++) {
    try {
      await page.evaluate((y, m, d) => {
        const date = new Date(y, m - 1, d);
        calendar.setTargetDate(date);
        calendar.updateTimetable();
        $('#navigation_calendar').datepicker('update', y + '/' + String(m).padStart(2, '0') + '/' + String(d).padStart(2, '0'));
      }, year, month, day);
      await sleep(WAIT_AFTER_JUMP);
      const current = await getDisplayedDate(page);
      if (current && current.year === year && current.month === month && current.day === day) return true;
      console.log(`  [WARN] ジャンプ後日付不一致 (retry ${retry}): 期待=${year}/${month}/${day} 実際=${current ? current.text : '不明'}`);
    } catch (error) {
      console.log(`  [WARN] ジャンプ失敗 (retry ${retry}): ${error.message}`);
    }
    await sleep(3000);
  }
  return false;
}

async function waitForTable(page) {
  const startedAt = Date.now();
  while (Date.now() - startedAt < WAIT_FOR_TABLE) {
    try {
      const hasTable = await page.evaluate(() => {
        const table = document.querySelector('table.daily');
        return !!table && table.querySelectorAll('tr').length > 3;
      });
      if (hasTable) return true;
    } catch (_) {}
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
      const events = [], seen = new Set(), rows = table.querySelectorAll('tr');
      const timePattern = /(\d{1,2}:\d{2})-(\d{1,2}:\d{2})/;
      for (const row of rows) {
        const cells = row.querySelectorAll('td, th');
        for (const [column, index] of [[1, col1], [2, col2]]) {
          if (index < 0 || !cells[index]) continue;
          const text = cells[index].textContent.trim();
          if (!text || !timePattern.test(text) || seen.has(`${column}:${text}`)) continue;
          seen.add(`${column}:${text}`);
          const match = text.match(timePattern), cell = cells[index];
          let bgColor = cell.style.backgroundColor || window.getComputedStyle(cell).backgroundColor || '';
          const inner = cell.querySelector('div[style], span[style]');
          if (inner && inner.style.backgroundColor) bgColor = inner.style.backgroundColor;
          events.push({ column, startTime: match[1], endTime: match[2], text: text.replace(/\s+/g, ' '), bgColor });
        }
      }
      return { found: true, events };
    });
  } catch (error) {
    return { found: false, events: [], message: 'error: ' + error.message };
  }
}

async function runOnce(targetMonths, attempt) {
  const startedAt = Date.now();
  console.log(`\n[試行 ${attempt}/${RETRY_DELAYS_MS.length}]`);
  console.log(`[START] ${new Date().toLocaleString('ja-JP', { timeZone: 'Asia/Tokyo' })}`);
  console.log(`[対象] ${targetMonths.map(month => `${month.year}/${month.month}`).join(', ')}`);
  console.log(`[出力] ${OUTPUT_FILE}`);
  const browser = await puppeteer.launch({
    headless: 'new',
    args: ['--no-sandbox', '--disable-setuid-sandbox', '--disable-dev-shm-usage', '--disable-gpu', '--single-process'],
    env: { ...process.env, TZ: 'Asia/Tokyo' },
    ...(process.env.PUPPETEER_EXECUTABLE_PATH ? { executablePath: process.env.PUPPETEER_EXECUTABLE_PATH } : {})
  });
  try {
    const page = await browser.newPage();
    await page.setViewport({ width: 1920, height: 1080 });
    await page.emulateTimezone('Asia/Tokyo');
    console.log('\n[1] ログイン...');
    await page.goto(LOGIN_URL, { waitUntil: 'networkidle2', timeout: 30000 });
    await sleep(3000);
    const emailInput = await page.$('input[type="text"], input[type="email"], input[name="email"]');
    if (emailInput) { await emailInput.click({ clickCount: 3 }); await emailInput.type(EMAIL, { delay: 30 }); }
    const passInput = await page.$('input[type="password"]');
    if (passInput) { await passInput.click({ clickCount: 3 }); await passInput.type(PASSWORD, { delay: 30 }); }
    const loginButton = await page.$('button[type="submit"], input[type="submit"], button');
    if (loginButton) await loginButton.click(); else await page.keyboard.press('Enter');
    console.log('[1] ログイン送信、12秒待機...');
    await sleep(WAIT_AFTER_LOGIN);
    console.log('[1] カレンダーページに移動...');
    await openCalendarPage(page);
    console.log('\n[2] UI完全ロード待ち...');
    if (!await waitForFullUI(page)) throw new Error('UIロードタイムアウト');
    console.log('[2] 追加10秒待機...');
    await sleep(10000);
    const initialDate = await getDisplayedDate(page);
    console.log(`[2] 現在表示: ${initialDate ? initialDate.text : '不明'}`);
    console.log('\n[3] スタッフ表示に切り替え...');
    await page.click('#staff_btn');
    await sleep(WAIT_AFTER_STAFF_BTN);
    console.log('[3] スタッフ表示切替完了');
    const allResults = {};
    let totalSuccess = 0, totalSkip = 0;
    for (const { year, month, daysInMonth } of targetMonths) {
      console.log(`\n[4] === ${year}年${month}月 (${daysInMonth}日間) ===`);
      for (let day = 1; day <= daysInMonth; day++) {
        const dateStr = `${year}-${String(month).padStart(2, '0')}-${String(day).padStart(2, '0')}`;
        if (!await jumpToDate(page, year, month, day)) {
          console.log(`  [${day}] ${dateStr} [SKIP] ジャンプ失敗`);
          allResults[dateStr] = []; totalSkip++; continue;
        }
        if (!await waitForTable(page)) {
          console.log(`  [${day}] ${dateStr} → 休診日`);
          allResults[dateStr] = []; totalSkip++; continue;
        }
        const data = await extractData(page);
        if (data.found) {
          allResults[dateStr] = data.events; totalSuccess++;
          console.log(`  [${day}] ${dateStr} → ${data.events.length}件`);
        } else {
          allResults[dateStr] = []; totalSkip++;
          console.log(`  [${day}] ${dateStr} → ${data.message}`);
        }
        if (day % 10 === 0 || day === daysInMonth) {
          fs.writeFileSync(OUTPUT_FILE, JSON.stringify({
            targetMonths: targetMonths.map(item => `${item.year}-${String(item.month).padStart(2, '0')}`),
            extractedAt: new Date().toISOString(), successCount: totalSuccess, skipCount: totalSkip, data: allResults
          }, null, 2), 'utf-8');
        }
      }
    }
    const elapsed = ((Date.now() - startedAt) / 1000).toFixed(1);
    const result = {
      targetMonths: targetMonths.map(item => `${item.year}-${String(item.month).padStart(2, '0')}`),
      extractedAt: new Date().toISOString(), elapsedSeconds: parseFloat(elapsed),
      successCount: totalSuccess, skipCount: totalSkip, data: allResults
    };
    fs.writeFileSync(OUTPUT_FILE, JSON.stringify(result, null, 2), 'utf-8');
    console.log(`\n[完了] 成功:${totalSuccess} スキップ:${totalSkip} 時間:${elapsed}秒`);
    console.log(`[保存] ${OUTPUT_FILE}`);
    return result;
  } finally {
    await browser.close().catch(() => {});
  }
}

(async () => {
  const targetMonths = getTargetMonths();
  let lastError;
  for (let index = 0; index < RETRY_DELAYS_MS.length; index++) {
    const delay = RETRY_DELAYS_MS[index];
    if (delay > 0) {
      console.log(`\n[再試行待機] ${Math.round(delay / 60000)}分後に再試行します。`);
      await sleep(delay);
    }
    try {
      await runOnce(targetMonths, index + 1);
      process.exit(0);
    } catch (error) {
      lastError = error;
      const canRetry = error instanceof RetryableApotoolError && index < RETRY_DELAYS_MS.length - 1;
      console.error(`[失敗] ${error.message}`);
      if (!canRetry) break;
      console.log('[再試行対象] 一時的なアクセス拒否または通信障害として扱います。');
    }
  }
  console.error(`[最終失敗] ${lastError ? lastError.message : '不明なエラー'}`);
  process.exit(1);
})();
