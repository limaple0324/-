# 掃描

這是獨立的單畫面模板掃描器，不會修改同步器。

## 目前內容

- 正式目標：`mysterious_examiner`，顯示名稱為 `神秘考官`
- 測試目標：`road_t`，目前已停用，保留給路人T測試
- 神秘考官模板：`templates/mysterious_examiner/`
- 備用身體模板：`templates/mysterious_examiner_body_optional/`
- 固定 NPC 排除樣本：`templates/exclusions/`
- 測試圖：`samples/`
- 結果圖：`results/`

目前的掃描器支援兩種模式：掃指定圖片，以及即時擷取遊戲視窗持續掃描。

## 掃描器控制台

目前 `掃描.exe` 已內建 GUI 控制台。直接雙擊 `掃描.exe`，或執行：

```bat
掃描.exe --gui
```

控制台可以：

- 只顯示 Flash 視窗，並自由勾選要掃描的視窗數量
- 開始 / 停止掃描神秘考官
- GUI 已改成金色簡化控制台；同步器用的群組整理、整理鍵等控制不會顯示
- 設定掃描間隔、提示音、紅框秒數與粗細
- 未找到時自動保存診斷圖與診斷 JSON，方便下次補模板
- 透過「區塊」選單自由顯示 / 隱藏掃描視窗、掃描設定、地圖資料與狀態紀錄
- 開始掃描後自動收起主控制台，改用桌面小型狀態窗顯示掃描狀態
- 顯示目前所有地圖資料與可使用狀態

檢查所有地圖資料：

```bat
掃描.exe --validate-maps
```

未找到時的診斷資料會保存到：

```text
results/diagnostics/
```

診斷圖會用黃框標出最高分數的位置，旁邊的 JSON 會記錄最高分數、最高模板、視窗編號與可能原因。每個 Flash 視窗預設約 10 秒最多保存一次診斷，且只保留最近 30 筆，避免資料夾暴增。

## 即時掃描遊戲視窗

先列出目前可掃描的視窗：

```bat
掃描.exe --list-windows
```

如果有很多個 `Adobe Flash Player 11`，建議使用視窗編號啟動：

```bat
掃描.exe --watch --window-handle 52691438 --target mysterious_examiner --interval 0.5 --beep --bring-to-front
```

上面的 `52691438` 要換成你自己 `--list-windows` 看到的視窗編號。

也可以不用指定視窗編號，啟動後 3 秒內點選遊戲視窗：

```bat
掃描.exe --watch --target mysterious_examiner --interval 0.5 --beep
```

只測試一次擷取是否正確：

```bat
掃描.exe --watch --once --window-handle 52691438 --target mysterious_examiner --show-not-found
```

## 多視窗掃描與紅框提示

先列出所有 Flash 視窗：

```bat
掃描.exe --list-windows
```

挑選你實際要同步掃描的 Flash 視窗編號，使用逗號串起來；數量不限，但視窗畫面不能互相遮蓋：

```bat
掃描.exe --watch --window-handles 460312,656664,3933490,9046718 --target mysterious_examiner --interval 0.5 --beep
```

找到神秘考官時，程式會：

- 在終端顯示是哪一個視窗，例如 `hwnd=460312`
- 顯示畫面座標與螢幕座標
- 在找到的 Flash 視窗外圍顯示紅框，預設保留 3 秒
- 如果有加 `--beep`，會發出提示音

紅框可調整：

```bat
掃描.exe --watch --window-handles 460312,656664,3933490,9046718 --target mysterious_examiner --red-frame-seconds 5 --red-frame-thickness 8 --beep
```

不想顯示紅框：

```bat
掃描.exe --watch --window-handles 460312,656664,3933490,9046718 --target mysterious_examiner --no-red-frame
```

也可以用標題自動抓所有 Flash 視窗：

```bat
掃描.exe --watch --multi-watch --window-title "Adobe Flash Player 11" --target mysterious_examiner --beep
```

但如果同時開了很多 Flash，這會全部掃描，速度會變慢，也可能掃到不是你要管理的視窗；正式使用建議用 `--window-handles`。

注意：目前即時擷取讀的是螢幕實際像素。遊戲視窗如果被其他視窗遮住，被遮住的地方也會被掃進去；多視窗模式請讓所有遊戲視窗互不遮蓋。需要保留擷取畫面診斷時，加上 `--save-live-capture`。

## 執行自我測試

EXE：

```bat
掃描.exe --self-test
```

Batch：

```bat
powershell -ExecutionPolicy Bypass -File .\掃描.ps1 --self-test
```

## 掃描指定圖片

EXE：

```bat
掃描.exe --target mysterious_examiner --image samples\mysterious_examiner\mysterious_examiner_found_02.png
```

Batch：

```bat
powershell -ExecutionPolicy Bypass -File .\掃描.ps1 --image samples\positive_center_01.png
```

輸出會包含：

- 找到 / 未找到
- 目標中心座標 X/Y
- 信心分數
- 使用的模板與方法
- debug 標框圖路徑

## 神秘考官目前規則

- 明確露出本體或包含名字時，會回報 `神秘考官` 與中心座標。
- 正式掃描目前只使用「神秘考官名字 + 角色」模板，避免同模型角色誤報。
- 身體模板已移到備用資料夾，先不參與正式掃描。
- 同地點沒有出現神秘考官的畫面已作為負樣本測試。
- `村克的守衛` 已放入排除樣本，避免在沉睡森林誤判。
- 完全被玩家、地物或 UI 蓋住時，畫面上沒有足夠像素，掃描器不會硬判定。

## 地圖資料

目前已建立：

```text
maps/void_sand_sea/                 虛空沙海
maps/jade_peak_forest/              玉峰林
maps/strange_stone_fantasy_land/    奇石幻地
maps/lily_of_valley_basin/          鈴蘭盆地
maps/holy_covenant_land/            聖約之地
maps/snowy_plum_forest/             雪域梅林
maps/sleeping_forest/               沉睡森林
maps/windbreak_gorge/               止風峽谷
maps/demon_subduing_school/         伏魔塾
maps/guardian_land/                 守護之地
```

每個地圖資料夾存放：

```text
minimap_full.png
regions/
manifest.json
```
