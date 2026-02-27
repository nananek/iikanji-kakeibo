import { test, expect, Page } from "@playwright/test";

const BASE_URL = "http://127.0.0.1:5000";

// ---------------------------------------------------------------------------
// ヘルパー
// ---------------------------------------------------------------------------

/** テスト用テーブルを持つページを構築し、drag_select.js を読み込む */
async function setupPage(
  page: Page,
  rowCount: number,
  checkedRows: number[] = []
) {
  const rows = Array.from({ length: rowCount }, (_, i) => {
    const checked = checkedRows.includes(i) ? "checked" : "";
    return `<tr data-idx="${i}">
      <td><input type="checkbox" class="form-check-input row-check" data-idx="${i}" ${checked}></td>
      <td>Row ${i + 1}</td>
    </tr>`;
  }).join("\n");

  await page.setContent(`<!DOCTYPE html>
<html><head><title>DragSelect Test</title>
<style>
  table { border-collapse: collapse; width: 300px; }
  td, th { border: 1px solid #ccc; padding: 8px 12px; }
</style>
</head><body>
<table id="testTable">
  <thead><tr><th style="width:40px"></th><th>Name</th></tr></thead>
  <tbody>${rows}</tbody>
</table>
</body></html>`);

  // drag_select.js を読み込んで初期化
  await page.addScriptTag({ url: `${BASE_URL}/static/js/drag_select.js` });
  await page.evaluate(() => {
    (window as any)._changeCount = 0;
    (window as any).initDragSelect(
      "#testTable",
      ".row-check",
      () => (window as any)._changeCount++
    );
  });
}

/** 全行のチェック状態を配列で返す */
async function getStates(page: Page): Promise<boolean[]> {
  return page.$$eval(".row-check", (cbs: HTMLInputElement[]) =>
    cbs.map((cb) => cb.checked)
  );
}

/** idx 行目の td (最初のセル) の BoundingBox を返す */
async function cellBox(page: Page, idx: number) {
  const td = page.locator("#testTable tbody tr").nth(idx).locator("td").first();
  return (await td.boundingBox())!;
}

/** onChange コールバックの呼び出し回数 */
async function changeCount(page: Page): Promise<number> {
  return page.evaluate(() => (window as any)._changeCount);
}

// ---------------------------------------------------------------------------
// 単体クリック
// ---------------------------------------------------------------------------
test.describe("チェックボックス単体クリック", () => {
  test("未チェックのチェックボックスをクリック → チェックが入る", async ({
    page,
  }) => {
    await setupPage(page, 5);
    const cb = page.locator(".row-check").nth(2);
    await expect(cb).not.toBeChecked();

    // チェックボックス input を直接クリック
    await cb.click();

    await expect(cb).toBeChecked();
  });

  test("チェック済みのチェックボックスをクリック → チェックが外れる", async ({
    page,
  }) => {
    await setupPage(page, 5, [2]);
    const cb = page.locator(".row-check").nth(2);
    await expect(cb).toBeChecked();

    await cb.click();

    await expect(cb).not.toBeChecked();
  });

  test("チェックボックスのセル (td) をクリック → チェックが入る", async ({
    page,
  }) => {
    await setupPage(page, 5);
    const cb = page.locator(".row-check").nth(2);
    await expect(cb).not.toBeChecked();

    // td の左上付近をクリック（checkbox 自体ではなくセル余白）
    const box = await cellBox(page, 2);
    await page.mouse.click(box.x + 2, box.y + 2);

    await expect(cb).toBeChecked();
  });

  test("単体クリックしても他の行には影響しない", async ({ page }) => {
    await setupPage(page, 5, [0, 1]);

    await page.locator(".row-check").nth(2).click();

    expect(await getStates(page)).toEqual([true, true, true, false, false]);
  });

  test("単体クリックで onChange コールバックが呼ばれる", async ({ page }) => {
    await setupPage(page, 5);
    expect(await changeCount(page)).toBe(0);

    await page.locator(".row-check").nth(0).click();

    expect(await changeCount(page)).toBeGreaterThanOrEqual(1);
  });
});

// ---------------------------------------------------------------------------
// ドラッグ複数選択
// ---------------------------------------------------------------------------
test.describe("ドラッグ複数選択", () => {
  test("下方向にドラッグ → 範囲内の行が選択される", async ({ page }) => {
    await setupPage(page, 5);

    const start = await cellBox(page, 0);
    await page.mouse.move(start.x + 5, start.y + 5);
    await page.mouse.down();

    for (let i = 1; i <= 3; i++) {
      const box = await cellBox(page, i);
      await page.mouse.move(box.x + 5, box.y + 5);
    }
    await page.mouse.up();

    expect(await getStates(page)).toEqual([true, true, true, true, false]);
  });

  test("上方向にドラッグ → 範囲内の行が選択される", async ({ page }) => {
    await setupPage(page, 5);

    const start = await cellBox(page, 3);
    await page.mouse.move(start.x + 5, start.y + 5);
    await page.mouse.down();

    for (let i = 2; i >= 0; i--) {
      const box = await cellBox(page, i);
      await page.mouse.move(box.x + 5, box.y + 5);
    }
    await page.mouse.up();

    expect(await getStates(page)).toEqual([true, true, true, true, false]);
  });

  test("チェック済みの行をドラッグで一括解除", async ({ page }) => {
    await setupPage(page, 5, [0, 1, 2, 3, 4]);

    const start = await cellBox(page, 1);
    await page.mouse.move(start.x + 5, start.y + 5);
    await page.mouse.down();

    for (let i = 2; i <= 3; i++) {
      const box = await cellBox(page, i);
      await page.mouse.move(box.x + 5, box.y + 5);
    }
    await page.mouse.up();

    expect(await getStates(page)).toEqual([true, false, false, false, true]);
  });

  test("ドラッグ範囲外の行はドラッグ前の状態を維持する", async ({ page }) => {
    await setupPage(page, 5, [0, 4]);

    const start = await cellBox(page, 1);
    await page.mouse.move(start.x + 5, start.y + 5);
    await page.mouse.down();

    for (let i = 2; i <= 3; i++) {
      const box = await cellBox(page, i);
      await page.mouse.move(box.x + 5, box.y + 5);
    }
    await page.mouse.up();

    // 行0,4 はドラッグ前のまま checked、行1-3 は新たに checked
    expect(await getStates(page)).toEqual([true, true, true, true, true]);
  });
});

// ---------------------------------------------------------------------------
// バックトラック（ドラッグ範囲を縮小）
// ---------------------------------------------------------------------------
test.describe("ドラッグ中のバックトラック", () => {
  test("範囲を広げてから戻すと、外れた行がドラッグ前の状態に復帰する", async ({
    page,
  }) => {
    await setupPage(page, 5, [3]); // 行3だけ初期チェック

    const start = await cellBox(page, 0);
    await page.mouse.move(start.x + 5, start.y + 5);
    await page.mouse.down();

    // 行3まで広げる
    for (let i = 1; i <= 3; i++) {
      const box = await cellBox(page, i);
      await page.mouse.move(box.x + 5, box.y + 5);
    }
    // 行0-3 がすべて checked
    expect(await getStates(page)).toEqual([true, true, true, true, false]);

    // 行1まで戻す → 行2,3 はドラッグ前の状態へ
    const back = await cellBox(page, 1);
    await page.mouse.move(back.x + 5, back.y + 5);

    // 行0,1=checked(ドラッグ中), 行2=false(元), 行3=true(元), 行4=false(元)
    expect(await getStates(page)).toEqual([true, true, false, true, false]);

    await page.mouse.up();

    // mouseup 後も状態が維持される
    expect(await getStates(page)).toEqual([true, true, false, true, false]);
  });

  test("ドラッグ開始行まで戻すと開始行だけが変更される", async ({ page }) => {
    await setupPage(page, 5);

    const start = await cellBox(page, 2);
    await page.mouse.move(start.x + 5, start.y + 5);
    await page.mouse.down();

    // 行4まで広げる
    for (let i = 3; i <= 4; i++) {
      const box = await cellBox(page, i);
      await page.mouse.move(box.x + 5, box.y + 5);
    }
    expect(await getStates(page)).toEqual([false, false, true, true, true]);

    // 開始行(2)まで戻す
    const back = await cellBox(page, 2);
    await page.mouse.move(back.x + 5, back.y + 5);

    expect(await getStates(page)).toEqual([false, false, true, false, false]);

    await page.mouse.up();
  });
});

// ---------------------------------------------------------------------------
// エッジケース
// ---------------------------------------------------------------------------
test.describe("エッジケース", () => {
  test("1行だけのテーブルでクリック選択が機能する", async ({ page }) => {
    await setupPage(page, 1);

    await page.locator(".row-check").first().click();

    expect(await getStates(page)).toEqual([true]);
  });

  test("テーブル外で mouseup してもドラッグが終了する", async ({ page }) => {
    await setupPage(page, 5);

    const start = await cellBox(page, 0);
    await page.mouse.move(start.x + 5, start.y + 5);
    await page.mouse.down();

    const box1 = await cellBox(page, 1);
    await page.mouse.move(box1.x + 5, box1.y + 5);

    // テーブル外で mouseup
    await page.mouse.move(0, 0);
    await page.mouse.up();

    // ドラッグ終了後、新しいクリックが独立して動く
    const cb3 = page.locator(".row-check").nth(3);
    await cb3.click();

    const states = await getStates(page);
    expect(states[3]).toBe(true);
    // 行0,1 はドラッグ中の状態を維持（行0のみ確実にチェック済み）
    expect(states[0]).toBe(true);
  });

  test("連続クリック：チェック → 解除 → チェック", async ({ page }) => {
    await setupPage(page, 3);
    const cb = page.locator(".row-check").nth(1);

    await cb.click();
    await expect(cb).toBeChecked();

    await cb.click();
    await expect(cb).not.toBeChecked();

    await cb.click();
    await expect(cb).toBeChecked();
  });

  test("ドラッグ後にクリックで個別選択できる", async ({ page }) => {
    await setupPage(page, 5);

    // まず行0-2をドラッグ選択
    const start = await cellBox(page, 0);
    await page.mouse.move(start.x + 5, start.y + 5);
    await page.mouse.down();
    for (let i = 1; i <= 2; i++) {
      const box = await cellBox(page, i);
      await page.mouse.move(box.x + 5, box.y + 5);
    }
    await page.mouse.up();
    expect(await getStates(page)).toEqual([true, true, true, false, false]);

    // 次に行4をクリック
    await page.locator(".row-check").nth(4).click();
    expect(await getStates(page)).toEqual([true, true, true, false, true]);
  });
});
