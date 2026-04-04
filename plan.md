⏺ 基於目前的流程，我看到幾個方向：

  ---
  現有流程的摩擦點

  1. 投遞後的追蹤很薄弱
  目前只有 follow_up_at 日期和 notes 欄位。德國公司回覆週期常常 3–6
  週，中間你完全不知道對方狀態。可以做「標準化面試記錄」：每輪面試後填幾個欄位（
  面試官、問了什麼題、自我感覺）存進 DB，最後有一份完整的求職日誌。

  2. Cover Letter 是一次性產物
  現在生成後就只能手動編輯。但每家公司的 tone 不同，有的偏 formal、有的偏
  startup casual。可以加「重新生成（指定語氣）」的按鈕，讓你選 formal / startup
  / concise 再生成一版。

  3. 沒有公司研究
  投遞前對公司幾乎一無所知。可以在 dashboard 加一個「公司摘要」按鈕，自動抓
  company LinkedIn / About 頁面，用 LLM 摘要成：成立時間、規模、產品、tech
  stack、最近新聞。對面試準備也有用。

  ---
  德國求職特有的痛點

  4. 薪資談判幾乎沒有資料
  德國 job ad 很多不列薪資，你靠 salary_range 欄位只能收集到少數資料。可以整合
  Levels.fyi / Glassdoor / Kununu 的薪資資料（或用 LLM 從 JD
  推估市場區間），讓你在談判前有個 anchor。

  5. Chancenkarte 簽證相容性判斷太粗糙
  目前只有 eu_only / open / sponsored / unclear 四個值，但實際上 Chancenkarte
  在「eu_only」的情況下不一定不行——取決於公司的理解。可以讓 LLM
  給出更細的分析：「JD 說 right to work required，但沒有明確排除
  Chancenkarte，建議投遞時主動說明」。

  6. 德文 JD 翻譯
  Bundesagentur 的 JD 很多是德文，你現在的 grading
  是用英文規則去評德文文字，準確度會打折。可以在 pre-flight
  加一步：偵測語言，如果是德文就先翻譯再評分。

  ---
  效率類

  7. A 級職缺 email 通知
  目前要手動開 dashboard 才知道有新 A 級職缺。可以在 scheduler 跑完 Phase 2
  後，如果有新 A 級職缺就發一封摘要 email（或 Telegram bot）。

  8. 重複投遞防護
  目前只防 URL 重複，但同一家公司可能用不同 URL 重新發同一個職位。可以在
  dashboard 顯示「你曾投遞過這家公司的類似職位」的提醒。